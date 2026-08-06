"""Schedule and device records for the desktop companion.

Supabase is the record of truth for the parent's single schedule and single enrolled
device. The phone still enforces offline from its own SharedPreferences copy — this is
purely so the desktop app has somewhere durable to read state back from, since the
schedule can't be read back off the device.

Schedules are immutable while active: there is deliberately no update or delete route a
parent can call directly. A parent who needs an active schedule changed emails support,
who edits the row directly in Supabase. Removing the app is gated behind
profiles.remove_enabled, which only support can flip.

The one exception is expiry: once the schedule's effective expiry (`_effective_expiry`)
is in the past, the schedule is spent — the phone has already unblocked itself — so it's
lazily deleted the next time it's read (`_get_schedule`, below), which is what lets a
parent set up a fresh one. This isn't a client-facing update/delete; it only ever fires
from the backend's own read path. For a same-day lock window that expiry is the last
day's lock end time; for an overnight window it's still midnight at the end of the last
day, since `active_until` alone can't express "end of lock window" once that window
crosses into the next calendar day.

Uses the same module-level `supabase` client as the rest of the backend — no separate
admin client. That means SUPABASE_KEY must be the service-role key, which is already the
case today: `update_profile` writes `profiles` through this same client from the Stripe
webhook, and that works in production.
"""

from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from routers.apps import supabase, get_user_from_token, get_profile

router = APIRouter()


# ── Helpers ─────────────────────────────────────────────────────────────────

def _require_user(authorization: Optional[str]):
    """Resolve the bearer token to a Supabase user, or raise 401."""
    if not supabase:
        raise HTTPException(status_code=503, detail="Supabase client not configured")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = authorization.split(" ", 1)[1].strip()
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user


def _effective_expiry(schedule: dict) -> datetime:
    """The instant the schedule actually stops mattering.

    `active_until` is stored as local midnight at the *end* of the last day (see
    SkywardDesktop's `endOfLocalDay`) — that's the right boundary for an overnight lock
    window (e.g. 10pm-6am), which still needs to fire past midnight on its last day. But
    for a same-day window (e.g. 2pm-6pm) there's no reason to keep the row alive until
    midnight — it should go once that last day's lock window closes.
    """
    active_until = datetime.fromisoformat(schedule["active_until"])

    spans_midnight = (
        schedule["lock_end_hour"] * 60 + schedule["lock_end_minute"]
        <= schedule["lock_start_hour"] * 60 + schedule["lock_start_minute"]
    )
    if spans_midnight:
        return active_until

    try:
        tz = ZoneInfo(schedule["timezone_id"])
    except Exception:
        return active_until

    last_day = (active_until.astimezone(tz) - timedelta(days=1)).date()
    return datetime.combine(
        last_day,
        time(schedule["lock_end_hour"], schedule["lock_end_minute"]),
        tzinfo=tz,
    )


def _is_expired(schedule: dict) -> bool:
    return _effective_expiry(schedule) <= datetime.now(timezone.utc)


def _get_schedule(user_id: str) -> Optional[dict]:
    """The account's schedule, or None — expired schedules are deleted here so they don't
    linger in the DB and so the caller sees "no schedule" and can create a new one."""
    res = supabase.table("schedules").select("*").eq("user_id", user_id).maybe_single().execute()
    schedule = res.data if res else None
    if schedule and _is_expired(schedule):
        supabase.table("schedules").delete().eq("id", schedule["id"]).eq("user_id", user_id).execute()
        return None
    return schedule


def _get_device(user_id: str) -> Optional[dict]:
    res = supabase.table("devices").select("*").eq("user_id", user_id).maybe_single().execute()
    return res.data if res else None


# ── Models ──────────────────────────────────────────────────────────────────

class ScheduleCreate(BaseModel):
    lock_start_hour: int = Field(ge=0, le=23)
    lock_start_minute: int = Field(ge=0, le=59)
    lock_end_hour: int = Field(ge=0, le=23)
    lock_end_minute: int = Field(ge=0, le=59)
    # Bit 0 = Sunday … bit 6 = Saturday. At least one day must be selected.
    days_mask: int = Field(ge=1, le=127)
    active_from: str
    active_until: str
    timezone_id: str


class DeviceCreate(BaseModel):
    serial: str
    model: Optional[str] = None


# ── Routes ──────────────────────────────────────────────────────────────────

@router.get("/me")
async def get_me(authorization: Optional[str] = Header(None)):
    """Everything the desktop shell needs in one call: profile flags, device, schedule."""
    user = _require_user(authorization)
    profile = get_profile(user.id) or {}

    return {
        "email": getattr(user, "email", None),
        "subscriptionStatus": profile.get("subscription_status"),
        "removeEnabled": bool(profile.get("remove_enabled", False)),
        "schedule": _get_schedule(user.id),
        "device": _get_device(user.id),
    }


@router.post("/schedule", status_code=201)
async def create_schedule(body: ScheduleCreate, authorization: Optional[str] = Header(None)):
    """Create the account's one and only schedule. 409 if one already exists."""
    user = _require_user(authorization)

    if _get_schedule(user.id):
        raise HTTPException(
            status_code=409,
            detail="A schedule already exists for this account. Contact support to change it.",
        )

    if body.active_until <= body.active_from:
        raise HTTPException(status_code=400, detail="active_until must be after active_from")

    payload = body.model_dump()
    payload["user_id"] = user.id

    try:
        res = supabase.table("schedules").insert(payload).execute()
    except Exception as e:
        # The UNIQUE index on user_id is the real guard — the check above loses a
        # double-submit race, this doesn't.
        if "duplicate key" in str(e).lower() or "schedules_one_per_user" in str(e):
            raise HTTPException(
                status_code=409,
                detail="A schedule already exists for this account. Contact support to change it.",
            )
        raise HTTPException(status_code=500, detail=f"Could not create schedule: {e}")

    if not res.data:
        raise HTTPException(status_code=500, detail="Could not create schedule")
    return res.data[0]


@router.post("/schedule/{schedule_id}/pushed")
async def mark_schedule_pushed(schedule_id: str, authorization: Optional[str] = Header(None)):
    """Record that the schedule reached a device. Scoped to the caller's own row."""
    user = _require_user(authorization)

    # A concrete timestamp rather than "now()" — PostgREST passes the value through as a
    # literal, and 'now()' is not a valid timestamptz literal.
    res = (
        supabase.table("schedules")
        .update({
            "status": "pushed",
            "pushed_at": datetime.now(timezone.utc).isoformat(),
        })
        .eq("id", schedule_id)
        .eq("user_id", user.id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return res.data[0]


@router.post("/devices", status_code=201)
async def register_device(body: DeviceCreate, authorization: Optional[str] = Header(None)):
    """Register the enrolled device. Re-registering the same serial is a no-op refresh."""
    user = _require_user(authorization)

    existing = _get_device(user.id)
    if existing:
        if existing.get("serial") == body.serial:
            return existing
        raise HTTPException(
            status_code=409,
            detail="A device is already enrolled on this account. Contact support to change it.",
        )

    try:
        res = supabase.table("devices").insert({
            "user_id": user.id,
            "serial": body.serial,
            "model": body.model,
        }).execute()
    except Exception as e:
        if "duplicate key" in str(e).lower() or "devices_one_per_user" in str(e):
            raise HTTPException(
                status_code=409,
                detail="A device is already enrolled on this account. Contact support to change it.",
            )
        raise HTTPException(status_code=500, detail=f"Could not register device: {e}")

    if not res.data:
        raise HTTPException(status_code=500, detail="Could not register device")
    return res.data[0]


@router.delete("/devices/{serial}")
async def unregister_device(serial: str, authorization: Optional[str] = Header(None)):
    """Drop the device record after removal. Gated on profiles.remove_enabled."""
    user = _require_user(authorization)

    profile = get_profile(user.id) or {}
    if not profile.get("remove_enabled", False):
        raise HTTPException(
            status_code=403,
            detail="Removal is not enabled for this account. Contact support to request it.",
        )

    supabase.table("devices").delete().eq("user_id", user.id).eq("serial", serial).execute()
    return {"success": True}

"""Schedule and device records for the desktop companion.

Supabase is the record of truth for the parent's single schedule and single enrolled
device — the phone still enforces offline from its own SharedPreferences copy, but the
desktop app needs somewhere durable to read state back from, since USB debugging is sealed
after enrolment and the schedule can no longer be read off the device.

Schedules are immutable: there is deliberately no update or delete route. Parents who need
a change email support, who edit the row directly in Supabase. Likewise, removing the app
is gated behind profiles.remove_enabled, which only support can flip.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from routers.apps import supabase, get_user_from_token, get_profile

router = APIRouter()


# ── Auth helper ─────────────────────────────────────────────────────────────

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


def _get_schedule(user_id: str) -> Optional[dict]:
    res = supabase.table("schedules").select("*").eq("user_id", user_id).maybe_single().execute()
    return res.data if res else None


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
        # The UNIQUE index on user_id is the real guard against a double-submit racing
        # past the check above.
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

    # Send a concrete timestamp rather than "now()" — PostgREST passes the value through
    # as a literal, and 'now()' is not a valid timestamptz literal.
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

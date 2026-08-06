"""The current SkywardBlocker build, for the desktop companion to download and install.

The APK bytes live in the private `app-releases` Storage bucket. Nothing about that bucket
is reachable with the anon key, so the desktop app can't fetch from it directly — this
route resolves the current release and hands back a short-lived signed URL alongside the
metadata needed to verify the download.

Signed rather than public because the desktop app already authenticates here, so making the
bucket public would trade a real access control for no gain. The URL is deliberately
short-lived: it only has to survive one download that starts moments later.

Uses the same service-role `supabase` client as the rest of the backend, which bypasses
storage RLS — that is what lets it sign URLs for a bucket with no public policy.
"""

import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from routers.apps import supabase

# The shared bearer-token → user resolver. Imported rather than reimplemented so the auth
# behaviour of this route can't drift from /me and the schedule routes.
from routers.schedules import _require_user

router = APIRouter()

BUCKET = "app-releases"

# Long enough to absorb a slow start and a retry, short enough that a leaked URL is stale
# before it is useful. The download itself is ~9 MB.
SIGNED_URL_TTL_SECONDS = 600


def _extract_signed_url(result) -> Optional[str]:
    """Pull the URL out of a create_signed_url response.

    storage3 has spelled this key both `signedURL` and `signedUrl` across versions, and
    returns a plain dict in some and an object in others. Since a wrong guess here would
    only surface in production as "no download URL", accept every shape rather than pinning
    a storage3 version.
    """
    if result is None:
        return None

    if not isinstance(result, dict):
        result = getattr(result, "__dict__", {}) or {}

    for key in ("signedURL", "signedUrl", "signed_url"):
        value = result.get(key)
        if value:
            # Some versions return a path relative to the storage API rather than an
            # absolute URL. Absolutise it so the client never has to care which it got.
            if value.startswith("http"):
                return value
            base = (os.getenv("SUPABASE_URL") or "").rstrip("/")
            return f"{base}/storage/v1/{value.lstrip('/')}"

    return None


@router.get("/releases/latest")
async def latest_release(authorization: Optional[str] = Header(None)):
    """Metadata plus a signed download URL for the build marked `is_current`.

    404 when nothing is published yet, which is a real state on a fresh environment — the
    desktop app surfaces it as "no release available" rather than as a crash.
    """
    _require_user(authorization)

    res = (
        supabase.table("app_releases")
        .select("*")
        .eq("is_current", True)
        .maybe_single()
        .execute()
    )
    release = res.data if res else None
    if not release:
        raise HTTPException(
            status_code=404,
            detail="No SkywardBlocker release has been published yet.",
        )

    try:
        signed = supabase.storage.from_(BUCKET).create_signed_url(
            release["storage_path"], SIGNED_URL_TTL_SECONDS
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not sign the download URL: {e}")

    download_url = _extract_signed_url(signed)
    if not download_url:
        # The row points at an object that isn't in the bucket, or the bucket is missing.
        # Worth its own message: it means a release was recorded but never uploaded.
        raise HTTPException(
            status_code=502,
            detail=(
                f"Release {release['version_name']} is recorded but its APK "
                f"({release['storage_path']}) could not be found in storage."
            ),
        )

    return {
        "version_code": release["version_code"],
        "version_name": release["version_name"],
        "sha256": release["sha256"],
        "size_bytes": release["size_bytes"],
        "release_notes": release.get("release_notes"),
        "released_at": release["released_at"],
        "download_url": download_url,
    }

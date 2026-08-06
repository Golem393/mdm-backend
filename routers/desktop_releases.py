"""The current Skyward Installer build, in the shape Tauri's updater expects.

Serves two audiences from one row:

  GET /api/desktop/latest.json  — Tauri's updater manifest, polled by installed copies
  GET /api/desktop/downloads    — plain list for the website's download buttons

Deliberately unauthenticated, unlike every other router here. An installed copy checks for
updates on launch, before the parent has logged in, so there is no bearer token to send and
no API key that wouldn't already be extractable from the binary doing the asking. Nothing
returned is secret: these are the same installers the download page hands to anyone.

Integrity does not come from the transport. Each artifact carries a signature made with the
updater private key, and the public half is compiled into the app — a client that cannot
verify the signature refuses the update, so neither this route nor the CDN is trusted to
serve the right bytes.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException

from routers.apps import supabase

router = APIRouter()

# Friendly labels for the website's download buttons, keyed by Tauri's target names.
# AppImage rather than .deb is the promoted Linux build on purpose: it is the only Linux
# format Tauri can self-update, since a .deb belongs to the package manager.
_DOWNLOAD_LABELS = {
    "windows-x86_64": ("Windows", "Windows 10 or later"),
    "darwin-aarch64": ("macOS", "Apple Silicon and Intel"),
    "linux-x86_64": ("Linux", "AppImage, most distributions"),
}


def _current_release() -> dict:
    """The row flagged is_current, or a 404 that says so in words."""
    res = (
        supabase.table("desktop_releases")
        .select("*")
        .eq("is_current", True)
        .maybe_single()
        .execute()
    )
    release = res.data if res else None
    if not release:
        raise HTTPException(
            status_code=404,
            detail="No Skyward Installer release has been published yet.",
        )
    return release


@router.get("/desktop/latest.json")
async def latest_desktop_release():
    """Tauri updater manifest for the current build.

    The client compares `version` against its own and only updates when this one is
    strictly greater, so this route stays a plain description of what is current — it does
    not try to decide whether any particular caller is out of date.
    """
    release = _current_release()

    platforms = release.get("platforms") or {}
    if not platforms:
        # The row exists but was published without artifacts. Worth distinguishing from
        # "nothing released yet": it means a CI run half-finished.
        raise HTTPException(
            status_code=502,
            detail=f"Release {release['version']} is recorded but lists no platforms.",
        )

    return {
        "version": release["version"],
        "notes": release.get("notes") or "",
        "pub_date": release["released_at"],
        "platforms": platforms,
    }


@router.get("/desktop/downloads")
async def desktop_downloads():
    """The current build's installers, for the website's download section.

    Separate from the manifest above because the two have genuinely different consumers:
    this one is allowed to change shape as the download page evolves, while the manifest
    is a format Tauri pins and we cannot alter.
    """
    release = _current_release()
    # `downloads`, not `platforms`: on macOS the updater artifact is a .app.tar.gz, which
    # is useless to someone who has never installed the app. See the migration.
    available = release.get("downloads") or {}

    downloads = []
    for target, (label, detail) in _DOWNLOAD_LABELS.items():
        entry = available.get(target)
        if not entry or not entry.get("url"):
            continue
        downloads.append(
            {
                "target": target,
                "label": label,
                "detail": detail,
                "url": entry["url"],
            }
        )

    return {
        "version": release["version"],
        "notes": release.get("notes") or "",
        "released_at": release["released_at"],
        "downloads": downloads,
    }

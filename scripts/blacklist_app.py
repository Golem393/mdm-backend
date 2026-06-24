"""
blacklist_app.py

Fetch an app from the Supabase `app_categories` table by packageName,
then add it to the ManageEngine MDM blacklist.

Usage:
    python scripts/blacklist_app.py <packageName>

Example:
    python scripts/blacklist_app.py com.example.game
"""

import asyncio
import os
import sys

import httpx

try:
    from supabase import create_client
except ImportError:
    create_client = None

from dotenv import load_dotenv
load_dotenv()


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------

def get_supabase_client():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not create_client or not url or not key:
        raise RuntimeError("Supabase is not configured. Check SUPABASE_URL and SUPABASE_KEY.")
    return create_client(url, key)


def fetch_app(package_name: str) -> dict | None:
    """Return the first matching row from app_categories, or None."""
    client = get_supabase_client()
    response = (
        client.table("app_categories")
        .select("packageName, appname, category")
        .eq("packageName", package_name)
        .maybe_single()
        .execute()
    )
    return response.data  # None if not found


# ---------------------------------------------------------------------------
# Zoho / ManageEngine
# ---------------------------------------------------------------------------

async def get_zoho_access_token() -> str:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://accounts.zoho.com/oauth/v2/token",
            params={
                "refresh_token": os.getenv("ZOHO_REFRESH_TOKEN"),
                "client_id":     os.getenv("ZOHO_CLIENT_ID"),
                "client_secret": os.getenv("ZOHO_CLIENT_SECRET"),
                "grant_type":    "refresh_token",
            },
        )
    data = response.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Failed to get Zoho access token: {data}")
    return token


async def blacklist_on_manageengine(package_name: str, app_name: str | None = None) -> None:
    access_token = await get_zoho_access_token()
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://mdm.manageengine.com/api/v1/mdm/blacklist/apps",
            json={
                "apps": [
                    {
                        "identifier": package_name,
                        "platform":   1,
                        "appname":    app_name or package_name,
                    }
                ]
            },
            headers={
                "Authorization": f"Zoho-oauthtoken {access_token}",
                "Content-Type":  "application/json",
            },
        )
        response.raise_for_status()
        print(f"[ManageEngine] Blacklisted '{package_name}' — HTTP {response.status_code}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(package_name: str) -> None:
    # 1. Fetch from Supabase
    print(f"Looking up '{package_name}' in Supabase...")
    app = fetch_app(package_name)

    if not app:
        print(f"No entry found in app_categories for packageName='{package_name}'.")
        sys.exit(1)

    print(
        f"Found — packageName: {app.get('packageName')} | "
        f"appName: {app.get('appname')} | "
        f"category: {app.get('category')}"
    )

    # 2. Blacklist on ManageEngine
    print("Adding to ManageEngine blacklist...")
    await blacklist_on_manageengine(package_name, app_name=app.get("appname"))
    print("Done.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/blacklist_app.py <packageName>")
        sys.exit(1)

    asyncio.run(main(sys.argv[1]))

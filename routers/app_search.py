"""
Public endpoint: lets website visitors check whether a given app
is blocked or allowed on Skyward.

Mounted WITHOUT API-key auth -- the website calls this unauthenticated.
"""

import os
import asyncio
import time
import functools

from fastapi import APIRouter, HTTPException
from google_play_scraper import app as get_app_info
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urlparse, parse_qs

try:
    from supabase import create_client, Client
except ImportError:
    create_client = None

router = APIRouter()

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")
supabase = None
if create_client and supabase_url and supabase_key:
    supabase = create_client(supabase_url, supabase_key)


def _check_supabase(package_name: str):
    if not supabase:
        return None
    try:
        response = supabase.table("app_categories").select("*").eq("packageName", package_name).execute()
        return response.data
    except Exception as e:
        print(f"Supabase read error: {e}")
        return None


def _insert_supabase(package_name: str, app_name, category: str):
    if not supabase:
        return
    try:
        data = {"packageName": package_name, "category": category}
        if app_name:
            data["appName"] = app_name
        supabase.table("app_categories").insert(data).execute()
    except Exception as e:
        print(f"Supabase insert error: {e}")


def get_first_app_id(query: str):
    """Scrape Google Play search results and return the first app's package name."""
    search_url = f"https://play.google.com/store/search?q={quote_plus(query)}&c=apps&gl=us"
    headers = {"User-Agent": "Mozilla/5.0"}

    response = requests.get(search_url, headers=headers, timeout=10)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "/store/apps/details" in href and "id=" in href:
            parsed = urlparse(href)
            params = parse_qs(parsed.query)
            app_id = params.get("id", [None])[0]
            if app_id:
                return app_id
    return None


_play_store_lock = asyncio.Lock()
_last_request_time = 0.0


async def lookup_app_category(package_name: str) -> dict:
    if not package_name:
        raise HTTPException(status_code=400, detail="Missing packageName")

    loop = asyncio.get_running_loop()

    # 1. Check DB cache
    if supabase:
        db_data = await loop.run_in_executor(None, _check_supabase, package_name)
        if db_data and len(db_data) > 0:
            entry = db_data[0]
            return {
                "packageName": package_name,
                "category": entry.get("category"),
                "appName": entry.get("appName"),
            }

    global _last_request_time

    try:
        async with _play_store_lock:
            now = time.time()
            elapsed = now - _last_request_time
            if elapsed < 0.5:
                await asyncio.sleep(0.5 - elapsed)
            try:
                app_data = None
                countries = ["us", "de", "kr", "ae"]
                for country in countries:
                    try:
                        func = functools.partial(get_app_info, package_name, lang="en", country=country)
                        app_data = await loop.run_in_executor(None, func)
                        if app_data:
                            break
                    except Exception as e:
                        print(f"Failed to fetch {package_name} in country '{country}': {e}")
                        continue
            finally:
                _last_request_time = time.time()

        if not app_data:
            category = "Unknown"
            app_name = None
        else:
            category = app_data.get("genreId", "Unknown")
            app_name = app_data.get("title", "")

        # 2. Cache in DB
        if supabase:
            loop.run_in_executor(None, _insert_supabase, package_name, app_name, category)

        return {"packageName": package_name, "category": category, "appName": app_name}

    except Exception as e:
        print(f"Error looking up category for {package_name}: {e}")
        return {"packageName": package_name, "category": "Unknown"}


_BLOCKED_CATEGORIES = {"SOCIAL", "ENTERTAINMENT", "VIDEO_PLAYERS_ENTERTAINMENT"}


@router.get("/blocked-app-search")
async def app_search(app_name: str):
    """Search the Play Store by name and return whether the app is blocked or allowed."""
    try:
        loop = asyncio.get_running_loop()
        app_id = await loop.run_in_executor(None, get_first_app_id, app_name)

        if not app_id:
            raise HTTPException(status_code=404, detail="No app found")

        category_result = await lookup_app_category(app_id)
        category = category_result.get("category")
        is_browser = "browser" in str(category_result.get("appName", "")).lower() or app_id in {"com.android.chrome", "org.mozilla.firefox", "com.sec.android.app.sbrowser", "com.opera.browser", "com.brave.browser", "com.microsoft.emmx", "com.duckduckgo.mobile.android"}
        app_status = "Blocked" if (category in _BLOCKED_CATEGORIES or "GAME" in category or is_browser) else "Allowed"

        return {
            "app_id": app_id,
            "category": category,
            "status": app_status,
            "appName": category_result.get("appName"),
        }

    except HTTPException:
        raise

    except Exception as e:
        print(f"Error in /blocked-app-search: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch app")


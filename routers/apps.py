import os
import csv
import asyncio
import time
from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from google_play_scraper import app as get_app_info, search as play_store_search
import functools
import openai
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urlparse, parse_qs

try:
    from supabase import create_client, Client
except ImportError:
    create_client = None

router = APIRouter()
public_router = APIRouter()  # Routes on this router require no API key

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")
supabase_service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase = None
if create_client and supabase_url and supabase_key:
    supabase = create_client(supabase_url, supabase_key)

# Admin client uses the service role key — bypasses RLS and can create users.
supabase_admin = None
if create_client and supabase_url and supabase_service_role_key:
    supabase_admin = create_client(supabase_url, supabase_service_role_key)

def check_supabase(package_name):
    if not supabase: return None
    try:
        response = supabase.table('app_categories').select('*').eq('packageName', package_name).execute()
        return response.data
    except Exception as e:
        print(f"Supabase read error: {e}")
        return None


def get_user_from_token(access_token: str):
    """Return the authenticated user for a Supabase access token, or None."""
    try:
        res = supabase.auth.get_user(access_token)
    except Exception:
        return None
    return res.user if res else None


def get_profile(user_id: str) -> Optional[dict]:
    client = supabase_admin
    if not client:
        return None
    res = client.table("profiles").select("*").eq("id", user_id).maybe_single().execute()
    return res.data if res else None


def update_profile(user_id: str, values: dict) -> None:
    client = supabase_admin
    if not client:
        print(f"WARNING: No Supabase client available to update profile for user {user_id}.")
        return
    res = client.table("profiles").update(values).eq("id", user_id).execute()
    if not res.data:
        print(f"WARNING: Failed to update profile for user {user_id}. RLS might be blocking it (check SUPABASE_SERVICE_ROLE_KEY) or user doesn't exist.")

def update_profile_by_customer(customer_id: str, values: dict) -> None:
    client = supabase_admin
    if not client:
        print(f"WARNING: No Supabase client available to update profile for customer {customer_id}.")
        return
    res = client.table("profiles").update(values).eq("stripe_customer_id", customer_id).execute()
    if not res.data:
        print(f"WARNING: Failed to update profile for customer {customer_id}. RLS might be blocking it (check SUPABASE_SERVICE_ROLE_KEY) or customer doesn't exist.")

def get_first_app_id(query: str):
    search_url = f"https://play.google.com/store/search?q={quote_plus(query)}&c=apps&gl=us"

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

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


async def classify_video_player(app_name: str, description: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set. Defaulting to VIDEO_PLAYERS_ENTERTAINMENT.")
        return "VIDEO_PLAYERS_ENTERTAINMENT"
    
    try:
        client = openai.AsyncOpenAI(api_key=api_key)
        short_desc = description[:1000] # Truncate to save tokens
        prompt = f"App Name: {app_name}\nDescription: {short_desc}\n\nIs this application primarily distracting and for Entertainment (like streaming services, movies, TV shows) or a Video Player Tool for utility (like local media players, editors, downloaders)? Reply with ONLY 'VideoPlayer Entertainment' or 'VideoPlayer Tools'."
        print(f"[DEBUG] LLM Input - App Name: '{app_name}' | Description: '{short_desc}'")
        
        response = await client.chat.completions.create(
            model="gpt-5-nano",
            messages=[{"role": "user", "content": prompt}]
            # Removed max_completion_tokens because reasoning models need token budget to "think"
        )
        
        result = response.choices[0].message.content.strip()
        print(f"[DEBUG] LLM classification for '{app_name}': {result}")
        if "tools" in result.lower():
            return "VIDEO_PLAYERS_TOOLS"
        else:
            return "VIDEO_PLAYERS_ENTERTAINMENT"
    except Exception as e:
        print(f"LLM Classification error for {app_name}: {e}")
        return "VIDEO_PLAYERS_ENTERTAINMENT"

def insert_supabase(package_name, app_name, category):
    if not supabase: return
    try:
        data = {
            "packageName": package_name,
            "category": category
        }
        if app_name:
            data["appName"] = app_name
            
        supabase.table('app_categories').insert(data).execute()
    except Exception as e:
        print(f"Supabase insert error: {e}")

async def lookup_app_category(package_name: str):
    if not package_name:
        raise HTTPException(status_code=400, detail="Missing packageName")

    loop = asyncio.get_running_loop()

    # 1. Check DB first
    if supabase:
        db_data = await loop.run_in_executor(None, check_supabase, package_name)
        if db_data and len(db_data) > 0:
            entry = db_data[0]
            category = entry.get("category")
            appName = entry.get("appName")
            return {"packageName": package_name, "category": category, "appName" : appName}

    global last_request_time

    try:
        async with play_store_lock:
            now = time.time()
            elapsed = now - last_request_time
            if elapsed < 0.5:
                await asyncio.sleep(0.5 - elapsed)

            try:
                app_data = None
                # Edge cases exist like Tesco Grocery & Clubcard are only available in the UK, but not US
                countries = ["us", "de", "kr", "ae"]

                for country in countries:
                    try:
                        func = functools.partial(
                            get_app_info,
                            package_name,
                            lang="en",
                            country=country
                        )
                        app_data = await loop.run_in_executor(None, func)

                        if app_data:
                            break

                    except Exception as e:
                        print(f"Failed to fetch {package_name} in country '{country}': {e}")
                        continue

            finally:
                last_request_time = time.time()

        if not app_data:
            category = "Unknown"
            app_name = None
        else:
            category = app_data.get("genreId", "Unknown")
            app_name = app_data.get("title", "")
            description = app_data.get("description", "")

            if category == "VIDEO_PLAYERS":
                category = await classify_video_player(app_name, description)

        # 2. Insert into DB
        if supabase:
            await loop.run_in_executor(
                None,
                insert_supabase,
                package_name,
                app_name,
                category
            )

        return {
            "packageName": package_name,
            "category": category,
            "appName" : app_name
        }

    except Exception as e:
        print(f"Error looking up category for {package_name}: {e}")
        return {
            "packageName": package_name,
            "category": "Unknown"
        }


play_store_lock = asyncio.Lock()
last_request_time = 0.0
popular_apps_cache = None

def parse_app_categories():
    csv_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'app_categories2.csv')
    if not os.path.exists(csv_path):
        return []
    
    apps = []
    with open(csv_path, mode='r', encoding='utf-8') as f:
        reader = csv.reader(f)
        # Skip header
        try:
            next(reader)
        except StopIteration:
            pass
        
        for row in reader:
            if len(row) >= 2:
                package_name = row[0].strip()
                category = row[1].strip()
                
                # Handled quotes in category
                if category.startswith('"') and category.endswith('"'):
                    category = category[1:-1]
                    
                apps.append({"packageName": package_name, "category": category})
    return apps

@public_router.get('/blocked-app-search')
# Currently only the US is being used when searching if an app exists. Apps like Tesco Grocery & Clubcard are only available in the UK but not the USA.
async def app_search(app_name: str):
    try:
        app_id = get_first_app_id(app_name)
        
        if not app_id:
            raise HTTPException(status_code=404, detail="No app found")

        category_result = await lookup_app_category(app_id)
        category = category_result.get("category")
        app_status = ""

        if category in {"SOCIAL", "ENTERTAINMENT", "VIDEO_PLAYERS_ENTERTAINMENT"} or "GAME" in category:
            app_status = "Blocked"
        else:
            app_status = "Allowed"

        return {
            "app_id": app_id,
            "category": category,
            "status": app_status,
            "appName" : category_result.get("appName")
        }

    except HTTPException:
        raise

    except Exception as e:
        print(f"Error in /blocked-app-search: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch app")

@router.get('/popular-apps')
async def get_popular_apps():
    global popular_apps_cache
    try:
        if popular_apps_cache is None:
            popular_apps_cache = parse_app_categories()
        return {"apps": popular_apps_cache}
    except Exception as e:
        print(f"Error in /popular-apps: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch popular apps")

class AppCategoryRequest(BaseModel):
    packageName: str

@router.post('/app-category')
async def get_app_category(request: AppCategoryRequest):
    return await lookup_app_category(request.packageName)

class SetupAuthRequest(BaseModel):
    email: str
    password: str

def verify_user_credentials(email, password):
    if not create_client or not supabase_url or not supabase_key:
        raise Exception("Supabase client not configured")
        
    temp_client = create_client(supabase_url, supabase_key)
    auth_response = temp_client.auth.sign_in_with_password({
        "email": email, 
        "password": password
    })
    
    user = auth_response.user
    if not user:
        raise Exception("Authentication failed")
        
    res = temp_client.table("subscription").select("*").eq("user_id", user.id).maybe_single().execute()
    subscription = res.data
    
    if not subscription:
        raise Exception("User subscription not found")
        
    status = subscription.get("status")
    if status != "active":
        raise Exception("Active subscription required. Please manage your plan.")
        
    return True

@router.post('/setup-auth')
async def setup_auth(request: SetupAuthRequest):
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None, 
            verify_user_credentials, 
            request.email, 
            request.password
        )
        return {"success": True}
    except Exception as e:
        print(f"Error in /setup-auth: {e}")
        msg = str(e)
        if "Invalid login credentials" in msg:
            msg = "Invalid email or password"
        elif "Email not confirmed" in msg:
            msg = "Please verify your email address before logging in"
        return {"success": False, "errorMessage": msg}

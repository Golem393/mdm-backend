import os
import csv
import asyncio
import time
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from google_play_scraper import app as get_app_info
import functools

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

def check_supabase(package_name):
    if not supabase: return None
    try:
        response = supabase.table('app_categories').select('*').eq('packageName', package_name).execute()
        return response.data
    except Exception as e:
        print(f"Supabase read error: {e}")
        return None

def insert_supabase(package_name, app_name, category):
    if not supabase: return
    try:
        data = {
            "packageName": package_name,
            "category": category
        }
        if app_name:
            data["appname"] = app_name
            
        supabase.table('app_categories').insert(data).execute()
    except Exception as e:
        print(f"Supabase insert error: {e}")


play_store_lock = asyncio.Lock()
last_request_time = 0.0
popular_apps_cache = None

def parse_app_categories():
    csv_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'app_categories.csv')
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
    package_name = request.packageName
    if not package_name:
        raise HTTPException(status_code=400, detail="Missing packageName")

    loop = asyncio.get_running_loop()

    # 1. Check DB first
    if supabase:
        db_data = await loop.run_in_executor(None, check_supabase, package_name)
        if db_data and len(db_data) > 0:
            entry = db_data[0]
            category = entry.get('category')
            return {"packageName": package_name, "category": category}

    global last_request_time
    try:
        async with play_store_lock:
            now = time.time()
            elapsed = now - last_request_time
            if elapsed < 0.5:
                await asyncio.sleep(0.5 - elapsed)

            try:
                # MUST run the synchronous scraper in a thread executor
                app_data = None
                countries = ['us', 'de', 'kr', 'ae']
                
                for country in countries:
                    try:
                        func = functools.partial(get_app_info, package_name, lang='en', country=country)
                        app_data = await loop.run_in_executor(None, func)
                        if app_data:
                            break
                    except Exception as e:
                        print(f"Failed to fetch {package_name} in country '{country}': {e}")
                        continue
                        
            finally:
                last_request_time = time.time()

        if not app_data:
            category = 'Unknown'
            app_name = None
        else:
            category = app_data.get('genreId', 'Unknown')
            app_name = app_data.get('title', '')
        
        # 2. Insert into DB
        if supabase:
            loop.run_in_executor(None, insert_supabase, package_name, app_name, category)

        return {"packageName": package_name, "category": category}

    except Exception as e:
        print(f"Error in /app-category for {package_name}: {e}")
        return {"packageName": package_name, "category": 'Unknown'}

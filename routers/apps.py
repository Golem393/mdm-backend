import os
import csv
import asyncio
import time
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from google_play_scraper import app as get_app_info

router = APIRouter()

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

    global last_request_time
    try:
        async with play_store_lock:
            now = time.time()
            elapsed = now - last_request_time
            if elapsed < 0.5:
                await asyncio.sleep(0.5 - elapsed)

            try:
                # MUST run the synchronous scraper in a thread executor
                loop = asyncio.get_running_loop()
                app_data = await loop.run_in_executor(None, get_app_info, package_name)
            finally:
                last_request_time = time.time()

        category = app_data.get('genreId', 'Unknown')
        return {"packageName": package_name, "category": category}

    except Exception as e:
        print(f"Error in /app-category for {package_name}: {e}")
        return {"packageName": package_name, "category": 'Unknown'}

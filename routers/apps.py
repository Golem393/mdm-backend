import os
import csv
import asyncio
import time
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from google_play_scraper import app as get_app_info

router = APIRouter()

# Rate limiting for Google Play requests
play_store_lock = asyncio.Lock()
last_request_time = 0.0

# Memory cache for popular apps to avoid reading the file on every request
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
        # Acquire lock to ensure only one request to Google Play Store at a time
        async with play_store_lock:
            now = time.time()
            elapsed = now - last_request_time
            # If less than 0.25s has passed since the last request, sleep for the difference
            if elapsed < 0.25:
                await asyncio.sleep(0.25 - elapsed)

            try:
                # Fetch data from Google Play
                app_data = get_app_info(package_name)
            finally:
                # Update the last request time regardless of success or failure
                last_request_time = time.time()

        category = app_data.get('genreId', 'Unknown')
        return {"packageName": package_name, "category": category}

    except Exception as e:
        print(f"Error in /app-category for {package_name}: {e}")
        # If app is not found or error occurs, return Unknown so app doesn't crash
        return {"packageName": package_name, "category": 'Unknown'}

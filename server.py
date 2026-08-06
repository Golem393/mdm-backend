import os
from fastapi import FastAPI, Depends, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize FastAPI app
app = FastAPI(title="MDM Backend API")

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Update this to match your deployed frontend, e.g. ["https://your-live-website.com"]
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Key Authentication Setup
API_KEY_NAME = "x-api-key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

async def get_api_key(api_key: str = Security(api_key_header)):
    expected_api_key = os.getenv("API_KEY")
    if not api_key or api_key != expected_api_key:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing API Key")
    return api_key

# Import routers
from routers import devices
from routers import apps
from routers import schedules
from routers import releases
from routers import stripe_routes

# Register routes to the root (/) requiring the API Key
app.include_router(devices.router, prefix="/api", dependencies=[Depends(get_api_key)])
app.include_router(apps.router, prefix="/api", dependencies=[Depends(get_api_key)])
app.include_router(schedules.router, prefix="/api", dependencies=[Depends(get_api_key)])
app.include_router(releases.router, prefix="/api", dependencies=[Depends(get_api_key)])
app.include_router(stripe_routes.router, prefix="/api/stripe")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 3000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=True)

import os
import httpx
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException

router = APIRouter()

async def get_zoho_access_token() -> str:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            'https://accounts.zoho.com/oauth/v2/token',
            params={
                'refresh_token': os.getenv("ZOHO_REFRESH_TOKEN"),
                'client_id': os.getenv("ZOHO_CLIENT_ID"),
                'client_secret': os.getenv("ZOHO_CLIENT_SECRET"),
                'grant_type': 'refresh_token'
            }
        )
    
    data = response.json()
    new_access_token = data.get("access_token")
    if not new_access_token:
        raise Exception("Failed to get fresh access token from Zoho. Response: " + str(data))
    return new_access_token

class MoveMemberRequest(BaseModel):
    memberId: str

@router.put('/move-member-to-official')
async def move_member_to_official(request: MoveMemberRequest):
    member_id = request.memberId
    if not member_id:
        raise HTTPException(status_code=400, detail="Missing memberId")
        
    try:
        new_access_token = await get_zoho_access_token()
        
        async with httpx.AsyncClient() as client:
            response = await client.put(
                f"https://mdm.manageengine.com/api/v1/mdm/groups/{os.getenv('KIOSK_GROUP_ID')}/targetgroups",
                json={
                    "member_ids": [member_id],
                    "target_group_ids": [os.getenv('OFFICIAL_GROUP_ID')]
                },
                headers={
                    'Authorization': f'Zoho-oauthtoken {new_access_token}',
                    'Content-Type': 'application/json'
                }
            )
            response.raise_for_status()
            return response.json()
            
    except httpx.HTTPStatusError as exc:
        print(f"API Error in move-member-to-official: {exc.response.text}")
        raise HTTPException(status_code=exc.response.status_code, detail="API Call Failed")
    except Exception as e:
        print(f"Error in move-member-to-official: {e}")
        raise HTTPException(status_code=500, detail="API Call Failed")

@router.get('/kiosk-devices')
async def get_kiosk_devices():
    try:
        new_access_token = await get_zoho_access_token()
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                'https://mdm.manageengine.com/api/v1/mdm/devices',
                params={
                    'group_id': os.getenv('KIOSK_GROUP_ID'),
                    'exclude_removed': 'true'
                },
                headers={
                    'Authorization': f'Zoho-oauthtoken {new_access_token}',
                    'Accept': 'application/json'
                }
            )
            response.raise_for_status()
            return response.json()
            
    except httpx.HTTPStatusError as exc:
        print(f"API Error in kiosk-devices: {exc.response.text}")
        raise HTTPException(status_code=exc.response.status_code, detail="API Call Failed")
    except Exception as e:
        print(f"Error in kiosk-devices: {e}")
        raise HTTPException(status_code=500, detail="API Call Failed")

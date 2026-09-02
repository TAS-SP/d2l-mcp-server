import os
import time
import httpx
from fastmcp import FastMCP

mcp = FastMCP("D2L-v146-Server")

# In-memory token caching to support single-use D2L token rotation
TOKEN_CACHE = {
    "access_token": None,
    "expires_at": 0,
    "current_refresh_token": None
}

async def get_valid_access_token() -> str:
    """Retrieve active Bearer token using memory cache or single-use refresh token exchange."""
    now = time.time()
    
    if TOKEN_CACHE["access_token"] and TOKEN_CACHE["expires_at"] > now + 60:
        return TOKEN_CACHE["access_token"]

    client_id = os.environ.get("D2L_CLIENT_ID")
    client_secret = os.environ.get("D2L_CLIENT_SECRET")
    refresh_token = (TOKEN_CACHE["current_refresh_token"] or os.environ.get("D2L_REFRESH_TOKEN", "")).strip()

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError("Missing D2L_CLIENT_ID, D2L_CLIENT_SECRET, or D2L_REFRESH_TOKEN.")

    token_url = "https://auth.brightspace.com/core/connect/token"
    payload = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(token_url, data=payload)
        response.raise_for_status()
        data = response.json()

        TOKEN_CACHE["access_token"] = data["access_token"]
        TOKEN_CACHE["expires_at"] = now + data.get("expires_in", 7200)
        
        if "refresh_token" in data:
            TOKEN_CACHE["current_refresh_token"] = data["refresh_token"]

        return TOKEN_CACHE["access_token"]


@mcp.tool()
async def get_d2l_users_146(username: str = "", domain: str = "") -> dict:
    """Fetch user details (including Active status) via D2L Brightspace LP API v1.46."""
    domain = domain or os.environ.get("D2L_DOMAIN", "sp.brightspace.com")

    try:
        bearer_token = await get_valid_access_token()
        url = f"https://{domain}/d2l/api/lp/1.46/users/"
        headers = {"Authorization": f"Bearer {bearer_token}"}
        params = {"userName": username} if username else {}
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"D2L User API returned HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": f"An error occurred: {str(e)}"}


@mcp.tool()
async def validate_d2l_module_146(org_unit_identifier: str, domain: str = "") -> dict:
    """Validate if a D2L Org Unit / Module is valid (IsActive is true AND IsDeleted is false).
    
    Accepts an OrgUnit ID (e.g. '123456') or exact Module Code (e.g. 'SP-ET101').
    """
    domain = domain or os.environ.get("D2L_DOMAIN", "sp.brightspace.com")

    try:
        bearer_token = await get_valid_access_token()
        headers = {"Authorization": f"Bearer {bearer_token}"}
        
        # Determine if input is a numeric OrgUnit ID or a string Module Code
        if org_unit_identifier.isdigit():
            url = f"https://{domain}/d2l/api/lp/1.46/orgstructure/{org_unit_identifier}"
            params = {}
        else:
            url = f"https://{domain}/d2l/api/lp/1.46/orgstructure/"
            params = {"exactOrgUnitCode": org_unit_identifier}

        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()

            # Handle array result from search endpoint vs direct object from ID endpoint
            org_unit = data["Items"][0] if "Items" in data and data["Items"] else data

            if not org_unit or "Identifier" not in org_unit:
                return {
                    "valid": False,
                    "reason": f"Org Unit '{org_unit_identifier}' was not found in Brightspace."
                }

            is_active = org_unit.get("IsActive", False)
            is_deleted = org_unit.get("IsDeleted", False)
            
            # Validation check: IsActive must be True AND IsDeleted must be False
            is_valid_module = (is_active is True) and (is_deleted is False)

            return {
                "valid": is_valid_module,
                "org_unit_id": org_unit.get("Identifier"),
                "name": org_unit.get("Name"),
                "code": org_unit.get("Code"),
                "is_active": is_active,
                "is_deleted": is_deleted,
                "status_message": (
                    "Module is active and valid." if is_valid_module
                    else f"Module invalid (IsActive={is_active}, IsDeleted={is_deleted})."
                )
            }
            
    except httpx.HTTPStatusError as e:
        return {"error": f"D2L OrgStructure API returned HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": f"An error occurred: {str(e)}"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    mcp.run(transport="http", host="0.0.0.0", port=port)

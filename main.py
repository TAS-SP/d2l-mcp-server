import os
import time
import httpx
from fastmcp import FastMCP

# Initialize FastMCP Server
mcp = FastMCP("D2L-v146-Server")

# In-memory token cache to support single-use D2L token rotation across requests
TOKEN_CACHE = {
    "access_token": None,
    "expires_at": 0,
    "current_refresh_token": None
}

async def get_valid_access_token() -> str:
    """
    Retrieve active Bearer token using memory cache or executing single-use refresh token exchange.
    Handles D2L's automatic token rotation logic seamlessly.
    """
    now = time.time()
    
    # 1. Reuse active access token if valid (with 60-second safety buffer)
    if TOKEN_CACHE["access_token"] and TOKEN_CACHE["expires_at"] > now + 60:
        return TOKEN_CACHE["access_token"]

    client_id = os.environ.get("D2L_CLIENT_ID")
    client_secret = os.environ.get("D2L_CLIENT_SECRET")
    refresh_token = (TOKEN_CACHE["current_refresh_token"] or os.environ.get("D2L_REFRESH_TOKEN", "")).strip()

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError("Missing D2L_CLIENT_ID, D2L_CLIENT_SECRET, or D2L_REFRESH_TOKEN environment variables.")

    # Safeguard check to ensure an Access Token JWT was not accidentally set
    if refresh_token.startswith("eyJ"):
        print("LOG ERROR: D2L_REFRESH_TOKEN starts with 'eyJ'. It is an Access Token (JWT), not a Refresh Token.")
        raise ValueError("D2L_REFRESH_TOKEN is set to a JWT Access Token instead of an opaque Refresh Token.")
    else:
        print(f"LOG SUCCESS: Refreshing token (Prefix: {refresh_token[:5]}...)")

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

        # Update in-memory cache with new access token and expiration time
        TOKEN_CACHE["access_token"] = data["access_token"]
        TOKEN_CACHE["expires_at"] = now + data.get("expires_in", 7200)
        
        # Persist single-use rotated refresh token in memory
        if "refresh_token" in data:
            TOKEN_CACHE["current_refresh_token"] = data["refresh_token"]

        return TOKEN_CACHE["access_token"]


@mcp.tool()
async def get_d2l_users_146(username: str = "", domain: str = "") -> dict:
    """
    Fetch user details (including IsActive status) directly via D2L Brightspace LP API version 1.46.
    """
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
        return {"error": f"An error occurred while fetching user: {str(e)}"}


@mcp.tool()
async def validate_d2l_module_146(org_unit_identifier: str, domain: str = "") -> dict:
    """
    Validate if a D2L Org Unit / Module is valid (IsActive is true AND IsDeleted is false).
    Searches against the Code column (exact match first, then partial match fallback), or by numeric OrgUnit ID.
    """
    domain = domain or os.environ.get("D2L_DOMAIN", "sp.brightspace.com")

    try:
        bearer_token = await get_valid_access_token()
        headers = {"Authorization": f"Bearer {bearer_token}"}
        
        org_unit = None
        
        # 1. If input is strictly numeric, check directly by OrgUnit ID first
        if org_unit_identifier.isdigit():
            url = f"https://{domain}/d2l/api/lp/1.46/orgstructure/{org_unit_identifier}"
            async with httpx.AsyncClient() as client:
                res = await client.get(url, headers=headers)
                if res.status_code == 200:
                    org_unit = res.json()

        # 2. Search against the Code column
        if not org_unit:
            search_url = f"https://{domain}/d2l/api/lp/1.46/orgstructure/"
            
            # Step 2A: Exact match on Code column
            async with httpx.AsyncClient() as client:
                res = await client.get(search_url, headers=headers, params={"exactOrgUnitCode": org_unit_identifier})
                if res.status_code == 200:
                    data = res.json()
                    items = data.get("Items", [])
                    if items:
                        org_unit = items[0]

            # Step 2B: Substring match fallback on Code column if exact match returned 0 results
            if not org_unit:
                async with httpx.AsyncClient() as client:
                    res = await client.get(search_url, headers=headers, params={"orgUnitCode": org_unit_identifier})
                    if res.status_code == 200:
                        data = res.json()
                        items = data.get("Items", [])
                        if items:
                            org_unit = items[0]

        # Handle case where no match was found for the Code or ID
        if not org_unit or "Identifier" not in org_unit:
            return {
                "valid": False,
                "reason": f"No Org Unit matching Code or ID '{org_unit_identifier}' was found in Brightspace."
            }

        is_active = org_unit.get("IsActive", False)
        is_deleted = org_unit.get("IsDeleted", False)
        
        # Strict validation condition: IsActive MUST be True AND IsDeleted MUST be False
        is_valid = (is_active is True) and (is_deleted is False)

        return {
            "valid": is_valid,
            "org_unit_id": org_unit.get("Identifier"),
            "name": org_unit.get("Name"),
            "code": org_unit.get("Code"),
            "is_active": is_active,
            "is_deleted": is_deleted,
            "status_message": (
                f"Module '{org_unit.get('Code')}' is valid (IsActive=True, IsDeleted=False)."
                if is_valid
                else f"Module '{org_unit.get('Code')}' is INVALID (IsActive={is_active}, IsDeleted={is_deleted})."
            )
        }
        
    except httpx.HTTPStatusError as e:
        return {"error": f"D2L OrgStructure API returned HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": f"An error occurred while validating module: {str(e)}"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    mcp.run(transport="http", host="0.0.0.0", port=port)

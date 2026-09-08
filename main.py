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
    
    if TOKEN_CACHE["access_token"] and TOKEN_CACHE["expires_at"] > now + 60:
        return TOKEN_CACHE["access_token"]

    client_id = os.environ.get("D2L_CLIENT_ID")
    client_secret = os.environ.get("D2L_CLIENT_SECRET")
    refresh_token = (TOKEN_CACHE["current_refresh_token"] or os.environ.get("D2L_REFRESH_TOKEN", "")).strip()

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError("Missing D2L_CLIENT_ID, D2L_CLIENT_SECRET, or D2L_REFRESH_TOKEN environment variables.")

    if refresh_token.startswith("eyJ"):
        print("LOG ERROR: D2L_REFRESH_TOKEN starts with 'eyJ'. It is an Access Token (JWT), not a Refresh Token.")
        raise ValueError("D2L_REFRESH_TOKEN is set to a JWT Access Token instead of an opaque Refresh Token.")

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
    Checks exact match first. If no exact match exists, searches for related modules and returns candidates to prompt the user.
    """
    domain = domain or os.environ.get("D2L_DOMAIN", "sp.brightspace.com")
    clean_code = org_unit_identifier.strip()

    try:
        bearer_token = await get_valid_access_token()
        headers = {"Authorization": f"Bearer {bearer_token}"}
        
        target_id = None
        search_url = f"https://{domain}/d2l/api/lp/1.46/orgstructure/"
        
        # 1. Direct ID lookup if input is strictly numeric
        if clean_code.isdigit():
            target_id = clean_code
        else:
            # 2. Check for Exact Match on Code column
            async with httpx.AsyncClient() as client:
                res = await client.get(search_url, headers=headers, params={"exactOrgUnitCode": clean_code})
                if res.status_code == 200:
                    items = res.json().get("Items", [])
                    for item in items:
                        if item.get("Code", "").strip().lower() == clean_code.lower():
                            target_id = item.get("Identifier")
                            break

        # 3. Exact Match Found: Fetch full course details and return validation status
        if target_id:
            detail_url = f"https://{domain}/d2l/api/lp/1.46/courses/{target_id}"
            async with httpx.AsyncClient() as client:
                res = await client.get(detail_url, headers=headers)
                if res.status_code != 200:
                    detail_url = f"https://{domain}/d2l/api/lp/1.46/orgstructure/{target_id}"
                    res = await client.get(detail_url, headers=headers)

                res.raise_for_status()
                org_unit = res.json()

            is_active = org_unit.get("IsActive", False)
            is_deleted = org_unit.get("IsDeleted", False)
            is_valid = (is_active is True) and (is_deleted is False)

            return {
                "valid": is_valid,
                "exact_match_found": True,
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

        # 4. No Exact Match: Run Partial/Fuzzy search to pull up to 5 candidates
        candidates = []
        async with httpx.AsyncClient() as client:
            res = await client.get(search_url, headers=headers, params={"search": clean_code})
            if res.status_code == 200:
                items = res.json().get("Items", [])
                for item in items[:5]:
                    candidates.append({
                        "org_unit_id": item.get("Identifier"),
                        "code": item.get("Code"),
                        "name": item.get("Name")
                    })

        if candidates:
            return {
                "valid": False,
                "exact_match_found": False,
                "status_message": f"Exact module code '{clean_code}' was not found. Please select from the candidate list below.",
                "candidate_modules": candidates,
                "instruction": "Present these candidate module options (Code and Name) to the user and ask them to confirm which one they meant."
            }

        return {
            "valid": False,
            "exact_match_found": False,
            "reason": f"No Org Unit matching Code or ID '{clean_code}' was found in Brightspace."
        }
        
    except httpx.HTTPStatusError as e:
        return {"error": f"D2L API returned HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": f"An error occurred while validating module: {str(e)}"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    mcp.run(transport="http", host="0.0.0.0", port=port)

import os
import time
import httpx
from fastmcp import FastMCP

# Initialize FastMCP Server
mcp = FastMCP("D2L-v146-Server")

# In-memory token cache to support token lifetime and rotation across requests
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

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(token_url, data=payload)
            if response.status_code != 200:
                print(f"D2L Auth Error Response ({response.status_code}): {response.text}")
                
            response.raise_for_status()
            data = response.json()

            TOKEN_CACHE["access_token"] = data["access_token"]
            TOKEN_CACHE["expires_at"] = now + data.get("expires_in", 1800)
            
            if "refresh_token" in data:
                TOKEN_CACHE["current_refresh_token"] = data["refresh_token"]

            return TOKEN_CACHE["access_token"]

        except (httpx.TimeoutException, httpx.HTTPStatusError, Exception) as e:
            TOKEN_CACHE["access_token"] = None
            TOKEN_CACHE["expires_at"] = 0
            raise RuntimeError(f"Failed to retrieve access token from D2L: {str(e)}")


@mcp.tool()
async def get_d2l_users_146(username: str = "", domain: str = "") -> dict:
    """
    Validate if a user exists and is active in D2L Brightspace LP API version 1.46.
    Returns strictly the validation status result without sensitive user metadata.
    """
    domain = domain or os.environ.get("D2L_DOMAIN", "sptest.brightspace.com")
    clean_user = username.strip()

    if not clean_user:
        return {"valid": False, "status_message": "Username parameter cannot be empty."}

    try:
        bearer_token = await get_valid_access_token()
        headers = {"Authorization": f"Bearer {bearer_token}"}
        url = f"https://{domain}/d2l/api/lp/1.46/users/"
        
        items = []
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers, params={"userName": clean_user})
            if response.status_code == 200:
                raw_data = response.json()
                items = raw_data.get("Items", raw_data) if isinstance(raw_data, dict) else raw_data
                if not isinstance(items, list):
                    items = []

            if not items:
                response = await client.get(url, headers=headers, params={"query": clean_user})
                if response.status_code == 200:
                    raw_data = response.json()
                    items = raw_data.get("Items", raw_data) if isinstance(raw_data, dict) else raw_data
                    if not isinstance(items, list):
                        items = []

        exact_user = None
        target = clean_user.lower()
        for u in items:
            u_name = str(u.get("UserName", "")).strip().lower()
            u_email = str(u.get("EmailAddress", "")).strip().lower()
            if u_name == target or u_email == target:
                exact_user = u
                break

        if exact_user:
            activation = exact_user.get("Activation", {})
            is_active = activation.get("IsActive", False) if isinstance(activation, dict) else False
            
            if is_active:
                return {
                    "valid": True,
                    "status_message": f"User '{clean_user}' is VALID."
                }
            else:
                return {
                    "valid": False,
                    "status_message": f"User '{clean_user}' is INACTIVE."
                }

        return {
            "valid": False,
            "status_message": f"User '{clean_user}' is INVALID."
        }

    except Exception as e:
        return {"valid": False, "status_message": f"User validation failed: {str(e)}"}


@mcp.tool()
async def validate_d2l_module_146(module_code: str = "", domain: str = "") -> dict:
    """
    Validate if a D2L Org Unit / Module is valid strictly against the Code column.
    Returns strictly the validation status result without further module details.
    """
    domain = domain or os.environ.get("D2L_DOMAIN", "sptest.brightspace.com")
    clean_code = module_code.strip()

    if not clean_code:
        return {"valid": False, "status_message": "Module code parameter cannot be empty."}

    try:
        bearer_token = await get_valid_access_token()
        headers = {"Authorization": f"Bearer {bearer_token}"}
        search_url = f"https://{domain}/d2l/api/lp/1.46/orgstructure/"
        
        target_id = None
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(search_url, headers=headers, params={"exactOrgUnitCode": clean_code})
            if res.status_code == 200:
                items = res.json().get("Items", [])
                for item in items:
                    if item.get("Code", "").strip().lower() == clean_code.lower():
                        target_id = item.get("Identifier")
                        break

        if not target_id:
            return {
                "valid": False,
                "status_message": f"Module code '{clean_code}' is INVALID."
            }

        async with httpx.AsyncClient(timeout=10.0) as client:
            detail_url = f"https://{domain}/d2l/api/lp/1.46/courses/{target_id}"
            res = await client.get(detail_url, headers=headers)
            if res.status_code != 200:
                detail_url = f"https://{domain}/d2l/api/lp/1.46/orgstructure/{target_id}"
                res = await client.get(detail_url, headers=headers)

            res.raise_for_status()
            org_unit = res.json()

        is_active = org_unit.get("IsActive", False)
        is_deleted = org_unit.get("IsDeleted", False)
        is_valid = (is_active is True) and (is_deleted is False)

        if is_valid:
            return {
                "valid": True,
                "status_message": f"Module code '{clean_code}' is VALID."
            }

        return {
            "valid": False,
            "status_message": f"Module code '{clean_code}' is INVALID."
        }

    except Exception as e:
        return {"valid": False, "status_message": f"Module code validation failed: {str(e)}"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    mcp.run(transport="http", host="0.0.0.0", port=port)

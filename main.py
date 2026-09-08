import os
import time
import httpx
import difflib
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
    Fetch user details via D2L Brightspace LP API version 1.46.
    Checks exact match first. If missing, validates similarity scores so unrelated 
    users returned by D2L search are discarded rather than suggested.
    """
    domain = domain or os.environ.get("D2L_DOMAIN", "sp.brightspace.com")
    clean_user = username.strip()

    if not clean_user:
        return {"error": "Username parameter cannot be empty."}

    try:
        bearer_token = await get_valid_access_token()
        headers = {"Authorization": f"Bearer {bearer_token}"}
        url = f"https://{domain}/d2l/api/lp/1.46/users/"
        
        # 1. Query D2L users endpoint
        items = []
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, params={"userName": clean_user})
            if response.status_code == 200:
                raw_data = response.json()
                items = raw_data.get("Items", raw_data) if isinstance(raw_data, dict) else raw_data
                if not isinstance(items, list):
                    items = []

            # Step 1B: Fallback search if userName parameter returned 0 results
            if not items:
                response = await client.get(url, headers=headers, params={"query": clean_user})
                if response.status_code == 200:
                    raw_data = response.json()
                    items = raw_data.get("Items", raw_data) if isinstance(raw_data, dict) else raw_data
                    if not isinstance(items, list):
                        items = []

        # 2. Look for an Exact Match on UserName or EmailAddress
        exact_user = None
        target = clean_user.lower()
        for u in items:
            u_name = str(u.get("UserName", "")).strip().lower()
            u_email = str(u.get("EmailAddress", "")).strip().lower()
            if u_name == target or u_email == target:
                exact_user = u
                break

        # 3. Exact Match Found: Return detailed user status
        if exact_user:
            activation = exact_user.get("Activation", {})
            is_active = activation.get("IsActive", False) if isinstance(activation, dict) else False
            
            return {
                "found": True,
                "exact_match_found": True,
                "username": exact_user.get("UserName"),
                "first_name": exact_user.get("FirstName"),
                "last_name": exact_user.get("LastName"),
                "email": exact_user.get("EmailAddress"),
                "org_defined_id": exact_user.get("OrgDefinedId"),
                "is_active": is_active,
                "status_message": (
                    f"User '{exact_user.get('UserName')}' is ACTIVE."
                    if is_active
                    else f"User '{exact_user.get('UserName')}' is INACTIVE."
                )
            }

        # 4. No Exact Match: Filter candidates by similarity threshold
        best_candidate = None
        highest_score = 0.0

        for u in items:
            u_name = str(u.get("UserName", "")).strip().lower()
            u_email = str(u.get("EmailAddress", "")).strip().lower()

            score_name = difflib.SequenceMatcher(None, target, u_name).ratio()
            score_email = difflib.SequenceMatcher(None, target, u_email).ratio()
            max_score = max(score_name, score_email)

            if target in u_name or target in u_email or u_name.startswith(target):
                max_score = max(max_score, 0.7)

            if max_score > highest_score:
                highest_score = max_score
                best_candidate = u

        if best_candidate and highest_score >= 0.45:
            display_name = f"{best_candidate.get('FirstName', '')} {best_candidate.get('LastName', '')}".strip()
            matched_username = best_candidate.get("UserName")
            matched_email = best_candidate.get("EmailAddress")

            return {
                "found": False,
                "exact_match_found": False,
                "similar_match": {
                    "username": matched_username,
                    "name": display_name,
                    "email": matched_email
                },
                "status_message": f"Exact match for '{clean_user}' not found.",
                "instruction": f"Ask the user: 'Exact match for {clean_user} not found. Did you mean {display_name} ({matched_username})?'"
            }

        return {
            "found": False,
            "exact_match_found": False,
            "reason": f"No user matching '{clean_user}' was found in Brightspace."
        }

    except httpx.HTTPStatusError as e:
        return {"error": f"D2L User API returned HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": f"An error occurred while fetching user: {str(e)}"}


@mcp.tool()
async def validate_d2l_module_146(module_code: str = "", domain: str = "") -> dict:
    """
    Validate if a D2L Org Unit / Module is valid strictly against the Code column.
    Returns valid = True ONLY if an exact Code match is found, IsActive is True,
    and IsDeleted is False. Otherwise returns valid = False.
    """
    domain = domain or os.environ.get("D2L_DOMAIN", "sp.brightspace.com")
    clean_code = module_code.strip()

    if not clean_code:
        return {"valid": False, "status_message": "Module code parameter cannot be empty."}

    try:
        bearer_token = await get_valid_access_token()
        headers = {"Authorization": f"Bearer {bearer_token}"}
        search_url = f"https://{domain}/d2l/api/lp/1.46/orgstructure/"
        
        target_id = None
        
        # 1. Search strictly by exactOrgUnitCode
        async with httpx.AsyncClient() as client:
            res = await client.get(search_url, headers=headers, params={"exactOrgUnitCode": clean_code})
            if res.status_code == 200:
                items = res.json().get("Items", [])
                for item in items:
                    if item.get("Code", "").strip().lower() == clean_code.lower():
                        target_id = item.get("Identifier")
                        break

        # 2. If no exact match on Code exists, return INVALID
        if not target_id:
            return {
                "valid": False,
                "code": clean_code,
                "status_message": f"Module code '{clean_code}' is INVALID (Code not found)."
            }

        # 3. Fetch module details to evaluate IsActive and IsDeleted
        async with httpx.AsyncClient() as client:
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
                "code": org_unit.get("Code", clean_code),
                "name": org_unit.get("Name"),
                "status_message": f"Module code '{org_unit.get('Code', clean_code)}' is VALID."
            }
        
        # Build specific explanation for invalid status
        reasons = []
        if not is_active:
            reasons.append("IsActive=False")
        if is_deleted:
            reasons.append("IsDeleted=True")

        return {
            "valid": False,
            "code": org_unit.get("Code", clean_code),
            "name": org_unit.get("Name"),
            "status_message": f"Module code '{org_unit.get('Code', clean_code)}' is INVALID ({', '.join(reasons)})."
        }

    except httpx.HTTPStatusError as e:
        return {"valid": False, "error": f"D2L API returned HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"valid": False, "error": f"An error occurred while validating module code: {str(e)}"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    mcp.run(transport="http", host="0.0.0.0", port=port)

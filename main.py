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
    Fetch user details (including IsActive status) via D2L Brightspace LP API version 1.46.
    Checks exact match first (by UserName or Email). If no exact match exists, returns only a single 
    top similar candidate to confirm identity without exposing internal User IDs or multiple matches.
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

        # 4. No Exact Match: Return only the single top similar candidate without User ID
        if items:
            top_match = items[0]
            display_name = f"{top_match.get('FirstName', '')} {top_match.get('LastName', '')}".strip()
            matched_username = top_match.get("UserName")
            matched_email = top_match.get("EmailAddress")

            return {
                "found": False,
                "exact_match_found": False,
                "similar_match": {
                    "username": matched_username,
                    "name": display_name,
                    "email": matched_email
                },
                "status_message": f"Exact match for '{clean_user}' not found.",
                "instruction": f"Ask the user: 'Exact match not found. Did you mean {display_name} ({matched_username})?'"
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
async def validate_d2l_module_146(org_unit_identifier: str, domain: str = "") -> dict:
    """
    Validate if a D2L Org Unit / Module is valid (IsActive is true AND IsDeleted is false).
    Checks exact match first. If no exact match exists, searches for candidate modules matching the code or base prefix
    and asks the user to confirm.
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

        # 4. No Exact Match: Perform fallback candidate search
        candidates_raw = []
        async with httpx.AsyncClient() as client:
            # Step 4A: Partial match on Code column
            res = await client.get(search_url, headers=headers, params={"orgUnitCode": clean_code})
            if res.status_code == 200:
                candidates_raw = res.json().get("Items", [])

            # Step 4B: If 0 results and code contains separator (e.g. MA1108-S2010), search by base code prefix (e.g. MA1108)
            if not candidates_raw and ("-" in clean_code or "_" in clean_code):
                base_prefix = clean_code.replace("_", "-").split("-")[0].strip()
                res = await client.get(search_url, headers=headers, params={"orgUnitCode": base_prefix})
                if res.status_code == 200:
                    candidates_raw = res.json().get("Items", [])

            # Step 4C: Broad search fallback if still empty
            if not candidates_raw:
                res = await client.get(search_url, headers=headers, params={"search": clean_code})
                if res.status_code == 200:
                    candidates_raw = res.json().get("Items", [])

        # Filter candidate list to unique module codes and names without internal IDs
        candidate_modules = []
        seen_codes = set()
        for item in candidates_raw:
            c_code = item.get("Code", "").strip()
            c_name = item.get("Name", "").strip()
            if c_code and c_code.lower() not in seen_codes:
                seen_codes.add(c_code.lower())
                candidate_modules.append({
                    "code": c_code,
                    "name": c_name
                })
            if len(candidate_modules) >= 5:
                break

        if candidate_modules:
            return {
                "valid": False,
                "exact_match_found": False,
                "status_message": f"Exact module code '{clean_code}' was not found in Brightspace.",
                "candidate_modules": candidate_modules,
                "instruction": f"Exact match for '{clean_code}' was not found. Ask the user if they meant one of these module codes: {', '.join([c['code'] for c in candidate_modules])}."
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

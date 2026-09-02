import os
import time
import httpx
from fastmcp import FastMCP

mcp = FastMCP("D2L-v146-Server")

# In-memory token storage to handle D2L token rotation & access token reuse
TOKEN_CACHE = {
    "access_token": None,
    "expires_at": 0,
    "current_refresh_token": None
}

async def get_valid_access_token() -> str:
    """Retrieve active Bearer token, reusing memory cache or executing single-use refresh token exchange."""
    now = time.time()
    
    # Reuse active access token if still valid (with a 60-second safety buffer)
    if TOKEN_CACHE["access_token"] and TOKEN_CACHE["expires_at"] > now + 60:
        return TOKEN_CACHE["access_token"]

    client_id = os.environ.get("D2L_CLIENT_ID")
    client_secret = os.environ.get("D2L_CLIENT_SECRET")
    
    # Use rotated token from memory if available, otherwise fall back to environment variable
    refresh_token = TOKEN_CACHE["current_refresh_token"] or os.environ.get("D2L_REFRESH_TOKEN")

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError("Missing D2L_CLIENT_ID, D2L_CLIENT_SECRET, or D2L_REFRESH_TOKEN.")

    token_url = "https://auth.brightspace.com/core/connect/token"
    payload = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token.strip()
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(token_url, data=payload)
        response.raise_for_status()
        data = response.json()

        # Update in-memory cache
        TOKEN_CACHE["access_token"] = data["access_token"]
        TOKEN_CACHE["expires_at"] = now + data.get("expires_in", 7200)
        
        # Save the new single-use refresh token returned by D2L
        if "refresh_token" in data:
            TOKEN_CACHE["current_refresh_token"] = data["refresh_token"]

        return TOKEN_CACHE["access_token"]

@mcp.tool()
async def get_d2l_users_146(username: str = "", domain: str = "") -> dict:
    """Fetch user details directly via D2L Brightspace LP API version 1.46."""
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
        return {"error": f"D2L API returned HTTP {e.response.status_code}: {e.response.text}"}
    except Exception as e:
        return {"error": f"An error occurred: {str(e)}"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    mcp.run(transport="http", host="0.0.0.0", port=port)

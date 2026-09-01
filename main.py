import os
import httpx
from fastmcp import FastMCP

mcp = FastMCP("D2L-v146-Server")

async def get_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Exchange D2L Refresh Token for a Bearer Access Token."""
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
        return response.json()["access_token"]

@mcp.tool()
async def get_d2l_users_146(username: str = "", domain: str = "") -> dict:
    """Fetch user details directly via D2L Brightspace LP API version 1.46."""
    client_id = os.environ.get("D2L_CLIENT_ID")
    client_secret = os.environ.get("D2L_CLIENT_SECRET")
    refresh_token = os.environ.get("D2L_REFRESH_TOKEN")
    domain = domain or os.environ.get("D2L_DOMAIN")

    if not all([client_id, client_secret, refresh_token, domain]):
        return {"error": "Missing D2L credentials in Render environment variables."}

    try:
        bearer_token = await get_access_token(client_id, client_secret, refresh_token)
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

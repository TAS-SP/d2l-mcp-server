import os
import httpx
from fastmcp import FastMCP

mcp = FastMCP("D2L-v146-Server")

async def get_access_token(client_id: str, client_secret: str, scope: str = "*:*") -> str:
    """Exchange OAuth client credentials for a D2L Bearer Token."""
    token_url = "https://auth.brightspace.com/core/connect/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": scope
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
    domain = domain or os.environ.get("D2L_DOMAIN")

    if not client_id or not client_secret or not domain:
        return {"error": "Missing D2L_CLIENT_ID, D2L_CLIENT_SECRET, or D2L_DOMAIN in Render environment."}

    bearer_token = await get_access_token(client_id, client_secret)
    url = f"https://{domain}/d2l/api/lp/1.46/users/"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    params = {"userName": username} if username else {}
    
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, params=params)
        return response.json()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    mcp.run(transport="sse", host="0.0.0.0", port=port)

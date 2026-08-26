import os
import httpx
from fastmcp import FastMCP

mcp = FastMCP("D2L-v146-Server")

@mcp.tool()
async def get_d2l_users_146(domain: str, bearer_token: str, username: str = "") -> dict:
    """Fetch user details directly via D2L Brightspace LP API version 1.46."""
    url = f"https://{domain}/d2l/api/lp/1.46/users/"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    params = {"userName": username} if username else {}

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, params=params)
        return response.json()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    mcp.run(transport="sse", host="0.0.0.0", port=port)

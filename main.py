import asyncio
import difflib
import os
import time

import httpx
import psycopg
from fastmcp import FastMCP
from psycopg.rows import dict_row


# Initialize FastMCP Server
mcp = FastMCP("D2L-v146-Server")


# ============================================================
# D2L authentication configuration
# ============================================================

D2L_TOKEN_URL = "https://auth.brightspace.com/core/connect/token"
TOKEN_EXPIRY_BUFFER = 60


# Access tokens may remain in memory because they can be recreated.
# The rotating refresh token is stored permanently in PostgreSQL.
TOKEN_CACHE = {
    "access_token": None,
    "expires_at": 0.0,
}


# Prevent simultaneous refreshes in the same Python process.
TOKEN_LOCK = asyncio.Lock()


# ============================================================
# PostgreSQL helpers
# ============================================================

async def open_database():
    database_url = os.environ.get("DATABASE_URL", "").strip()

    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is missing from the Render environment."
        )

    return await psycopg.AsyncConnection.connect(
        database_url,
        row_factory=dict_row,
    )


async def initialize_token_store() -> None:
    """
    Create the PostgreSQL token table.

    If the table does not contain a Brightspace token yet,
    D2L_REFRESH_TOKEN is inserted as the initial token.

    Existing PostgreSQL tokens are never overwritten by the
    Render environment variable.
    """

    initial_refresh_token = os.environ.get(
        "D2L_REFRESH_TOKEN",
        "",
    ).strip()

    if initial_refresh_token.startswith("eyJ"):
        raise RuntimeError(
            "D2L_REFRESH_TOKEN appears to be a JWT access token, "
            "not a Brightspace refresh token."
        )

    connection = await open_database()

    try:
        async with connection.transaction():
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS d2l_oauth_tokens (
                        provider TEXT PRIMARY KEY,
                        access_token TEXT,
                        refresh_token TEXT NOT NULL,
                        expires_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                        updated_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )

                await cursor.execute(
                    """
                    SELECT provider
                    FROM d2l_oauth_tokens
                    WHERE provider = %s
                    """,
                    ("brightspace",),
                )

                existing_record = await cursor.fetchone()

                if existing_record is None:
                    if not initial_refresh_token:
                        raise RuntimeError(
                            "No token exists in PostgreSQL and "
                            "D2L_REFRESH_TOKEN is missing from Render."
                        )

                    await cursor.execute(
                        """
                        INSERT INTO d2l_oauth_tokens (
                            provider,
                            access_token,
                            refresh_token,
                            expires_at,
                            updated_at
                        )
                        VALUES (
                            %s,
                            NULL,
                            %s,
                            0,
                            CURRENT_TIMESTAMP
                        )
                        """,
                        (
                            "brightspace",
                            initial_refresh_token,
                        ),
                    )

    finally:
        await connection.close()


async def get_stored_tokens() -> dict:
    """
    Read the latest Brightspace tokens from PostgreSQL.
    """

    connection = await open_database()

    try:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                SELECT
                    access_token,
                    refresh_token,
                    expires_at
                FROM d2l_oauth_tokens
                WHERE provider = %s
                """,
                ("brightspace",),
            )

            record = await cursor.fetchone()

    finally:
        await connection.close()

    if record is None:
        raise RuntimeError(
            "Brightspace token record was not found in PostgreSQL."
        )

    return dict(record)


# ============================================================
# D2L token refresh
# ============================================================

async def refresh_d2l_access_token() -> str:
    """
    Exchange the latest persistent refresh token for new tokens.

    PostgreSQL locks the token row while the refresh is taking
    place. This prevents two concurrent requests from consuming
    the same one-time Brightspace refresh token.
    """

    client_id = os.environ.get("D2L_CLIENT_ID", "").strip()
    client_secret = os.environ.get(
        "D2L_CLIENT_SECRET",
        "",
    ).strip()

    if not client_id or not client_secret:
        raise RuntimeError(
            "D2L_CLIENT_ID or D2L_CLIENT_SECRET is missing "
            "from the Render environment."
        )

    connection = await open_database()

    new_access_token = None
    new_expires_at = 0.0

    try:
        async with connection.transaction():
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT
                        access_token,
                        refresh_token,
                        expires_at
                    FROM d2l_oauth_tokens
                    WHERE provider = %s
                    FOR UPDATE
                    """,
                    ("brightspace",),
                )

                record = await cursor.fetchone()

                if record is None:
                    raise RuntimeError(
                        "Brightspace token record does not exist."
                    )

                stored_access_token = record["access_token"]

                stored_refresh_token = str(
                    record["refresh_token"] or ""
                ).strip()

                stored_expires_at = float(
                    record["expires_at"] or 0
                )

                current_time = time.time()

                # A different request might have refreshed the token
                # while this request was waiting for the row lock.
                if (
                    stored_access_token
                    and stored_expires_at
                    > current_time + TOKEN_EXPIRY_BUFFER
                ):
                    TOKEN_CACHE["access_token"] = (
                        stored_access_token
                    )
                    TOKEN_CACHE["expires_at"] = (
                        stored_expires_at
                    )

                    return stored_access_token

                if not stored_refresh_token:
                    raise RuntimeError(
                        "The PostgreSQL refresh token is empty."
                    )

                if stored_refresh_token.startswith("eyJ"):
                    raise RuntimeError(
                        "The token stored in PostgreSQL appears to "
                        "be a JWT access token, not a refresh token."
                    )

                # This preserves your current working D2L request
                # format with client credentials in the form body.
                payload = {
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": stored_refresh_token,
                }

                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(30.0)
                ) as client:
                    response = await client.post(
                        D2L_TOKEN_URL,
                        data=payload,
                        headers={
                            "Accept": "application/json",
                            "Content-Type":
                                "application/x-www-form-urlencoded",
                        },
                    )

                if response.status_code != 200:
                    try:
                        error_data = response.json()

                        safe_error = (
                            error_data.get("error_description")
                            or error_data.get("error")
                            or "No error details returned"
                        )
                    except Exception:
                        safe_error = "Non-JSON error response"

                    raise RuntimeError(
                        "Brightspace token refresh failed. "
                        f"HTTP {response.status_code}: {safe_error}"
                    )

                try:
                    token_data = response.json()
                except Exception as exc:
                    raise RuntimeError(
                        "Brightspace returned an invalid JSON "
                        "token response."
                    ) from exc

                new_access_token = token_data.get("access_token")
                new_refresh_token = token_data.get(
                    "refresh_token"
                )

                if not new_access_token:
                    raise RuntimeError(
                        "Brightspace did not return an access_token."
                    )

                if not new_refresh_token:
                    raise RuntimeError(
                        "Brightspace did not return a new "
                        "refresh_token."
                    )

                try:
                    expires_in = int(
                        token_data.get("expires_in", 1800)
                    )
                except (TypeError, ValueError):
                    expires_in = 1800

                expires_in = max(
                    60,
                    min(expires_in, 86400),
                )

                new_expires_at = time.time() + expires_in

                # Persist the newly rotated refresh token before the
                # database transaction is committed.
                await cursor.execute(
                    """
                    UPDATE d2l_oauth_tokens
                    SET
                        access_token = %s,
                        refresh_token = %s,
                        expires_at = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE provider = %s
                    """,
                    (
                        new_access_token,
                        new_refresh_token,
                        new_expires_at,
                        "brightspace",
                    ),
                )

        # Update memory only after PostgreSQL commits successfully.
        TOKEN_CACHE["access_token"] = new_access_token
        TOKEN_CACHE["expires_at"] = new_expires_at

        return new_access_token

    except Exception:
        TOKEN_CACHE["access_token"] = None
        TOKEN_CACHE["expires_at"] = 0.0
        raise

    finally:
        await connection.close()


async def get_valid_access_token() -> str:
    """
    Return an active Brightspace access token.

    The function checks:
    1. In-memory access token
    2. PostgreSQL access token
    3. PostgreSQL refresh token
    """

    current_time = time.time()

    cached_access_token = TOKEN_CACHE.get("access_token")
    cached_expires_at = float(
        TOKEN_CACHE.get("expires_at", 0) or 0
    )

    if (
        cached_access_token
        and cached_expires_at
        > current_time + TOKEN_EXPIRY_BUFFER
    ):
        return cached_access_token

    async with TOKEN_LOCK:
        # Check again after obtaining the lock.
        current_time = time.time()

        cached_access_token = TOKEN_CACHE.get(
            "access_token"
        )
        cached_expires_at = float(
            TOKEN_CACHE.get("expires_at", 0) or 0
        )

        if (
            cached_access_token
            and cached_expires_at
            > current_time + TOKEN_EXPIRY_BUFFER
        ):
            return cached_access_token

        # Creates the table and performs the initial migration.
        await initialize_token_store()

        stored_tokens = await get_stored_tokens()

        stored_access_token = stored_tokens.get(
            "access_token"
        )

        stored_expires_at = float(
            stored_tokens.get("expires_at", 0) or 0
        )

        if (
            stored_access_token
            and stored_expires_at
            > time.time() + TOKEN_EXPIRY_BUFFER
        ):
            TOKEN_CACHE["access_token"] = (
                stored_access_token
            )
            TOKEN_CACHE["expires_at"] = stored_expires_at

            return stored_access_token

        return await refresh_d2l_access_token()

@mcp.tool()
async def get_d2l_users_146(
    username: str = "",
    user_id: str = "",
    user_name: str = "",
    user: str = "",
    domain: str = ""
) -> dict:
    """
    Validate if a user exists and is active in D2L strictly against UserName/Email.
    Accepts aliases (username, user_id, user_name, user) to support various caller schemas.
    """
    domain = domain or os.environ.get("D2L_DOMAIN", "sptest.brightspace.com")
    raw_user = username or user_name or user_id or user
    clean_user = raw_user.strip()

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

        # 1. Exact Match Check
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

        # 2. Similarity Search for Username Suggestions
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
            matched_username = best_candidate.get("UserName")
            return {
                "valid": False,
                "status_message": f"Exact match for '{clean_user}' not found.",
                "similar_match": matched_username,
                "instruction": f"Ask the user: 'Exact match for {clean_user} not found. Did you mean {matched_username}?'"
            }

        return {
            "valid": False,
            "status_message": f"User '{clean_user}' is INVALID."
        }

    except Exception as e:
        return {"valid": False, "status_message": f"User validation failed: {str(e)}"}


@mcp.tool()
async def validate_d2l_module_146(
    module_code: str = "",
    org_unit_identifier: str = "",
    org_unit_code: str = "",
    code: str = "",
    domain: str = ""
) -> dict:
    """
    Validate a D2L Org Unit / Module STRICTLY against the Code column.
    Accepts aliases (module_code, org_unit_identifier, org_unit_code, code) to support Composio/Agent parameters.
    """
    domain = domain or os.environ.get("D2L_DOMAIN", "sptest.brightspace.com")
    raw_code = module_code or org_unit_identifier or org_unit_code or code
    clean_code = raw_code.strip()

    if not clean_code:
        return {"valid": False, "status_message": "Module code parameter cannot be empty."}

    try:
        bearer_token = await get_valid_access_token()
        headers = {"Authorization": f"Bearer {bearer_token}"}
        search_url = f"https://{domain}/d2l/api/lp/1.46/orgstructure/"
        
        target_id = None
        items = []
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            # 1. Exact match strictly by the Code column
            res = await client.get(search_url, headers=headers, params={"exactOrgUnitCode": clean_code})
            if res.status_code == 200:
                items = res.json().get("Items", [])
                for item in items:
                    if str(item.get("Code", "")).strip().lower() == clean_code.lower():
                        target_id = item.get("Identifier")
                        break

            # 2. Candidate collection for similarity search if exact Code match failed
            if not target_id:
                res_broad = await client.get(search_url, headers=headers, params={"orgUnitCode": clean_code})
                if res_broad.status_code == 200:
                    items = res_broad.json().get("Items", [])

        # Validate exact match if Code column yielded an Identifier
        if target_id:
            async with httpx.AsyncClient(timeout=10.0) as client:
                detail_url = f"https://{domain}/d2l/api/lp/1.46/courses/{target_id}"
                res = await client.get(detail_url, headers=headers)
                if res.status_code != 200:
                    detail_url = f"https://{domain}/d2l/api/lp/1.46/orgstructure/{target_id}"
                    res = await client.get(detail_url, headers=headers)

                if res.status_code == 200:
                    org_unit = res.json()
                    is_active = org_unit.get("IsActive", False)
                    is_deleted = org_unit.get("IsDeleted", False)
                    is_valid = (is_active is True) and (is_deleted is False)

                    if is_valid:
                        return {
                            "valid": True,
                            "status_message": f"Module code '{clean_code}' is VALID."
                        }
                    else:
                        return {
                            "valid": False,
                            "status_message": f"Module code '{clean_code}' is INACTIVE or DELETED."
                        }

        # 3. Similarity Search strictly evaluating candidate Code values
        best_candidate_code = None
        highest_score = 0.0
        target_code = clean_code.lower()

        for item in items:
            item_code = str(item.get("Code", "")).strip().lower()
            if not item_code:
                continue

            score = difflib.SequenceMatcher(None, target_code, item_code).ratio()
            if target_code in item_code or item_code.startswith(target_code):
                score = max(score, 0.7)

            if score > highest_score:
                highest_score = score
                best_candidate_code = item.get("Code")

        if best_candidate_code and highest_score >= 0.45:
            return {
                "valid": False,
                "status_message": f"Exact match for module code '{clean_code}' not found.",
                "similar_match": best_candidate_code,
                "instruction": f"Ask the user: 'Module code {clean_code} was not found. Did you mean {best_candidate_code}?'"
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

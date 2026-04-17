"""Live auth tests -- real Logto JWKS, real MongoDB."""

import os

import pytest
from starlette.requests import Request


pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_real_logto_jwks_reachable(live_env):
    """Fetch JWKS from real Logto endpoint -> returns signing keys."""
    import httpx

    endpoint = live_env["LOGTO_ENDPOINT"]
    jwks_url = f"{endpoint}/oidc/jwks"

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(jwks_url)
    resp.raise_for_status()
    data = resp.json()
    assert "keys" in data
    assert len(data["keys"]) > 0


@pytest.mark.asyncio
async def test_real_token_validates(live_core, live_user_token):
    """Mint real token via refresh grant -> verify_token succeeds."""
    scope = {
        "type": "http", "method": "GET", "path": "/",
        "headers": [(b"authorization", f"Bearer {live_user_token}".encode())],
    }
    req = Request(scope)
    payload = await live_core.auth.verify_token(req)
    assert payload is not None
    assert "sub" in payload
    assert payload["sub"]  # non-empty


@pytest.mark.asyncio
async def test_real_expired_token_rejected(live_core):
    """A clearly bogus token should be rejected by real JWKS."""
    from fastapi import HTTPException

    # Use a random string that isn't a valid JWT at all
    scope = {
        "type": "http", "method": "GET", "path": "/",
        "headers": [(b"authorization", b"Bearer this-is-not-a-jwt-token")],
    }
    req = Request(scope)
    with pytest.raises(HTTPException) as exc_info:
        await live_core.auth.verify_token(req)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_real_user_created_in_mongodb(live_core, live_db, live_user_token):
    """verify_token + get_or_create_user -> user document exists in real MongoDB."""
    scope = {
        "type": "http", "method": "GET", "path": "/",
        "headers": [(b"authorization", f"Bearer {live_user_token}".encode())],
    }
    req = Request(scope)
    payload = await live_core.auth.verify_token(req)
    user = await live_core.auth.get_or_create_user(live_db, payload)

    assert user["logto_user_id"] == payload["sub"]
    assert user["free_credits"] == 10

    # Verify in DB directly
    db_user = await live_db["users"].find_one({"logto_user_id": payload["sub"]})
    assert db_user is not None


@pytest.mark.asyncio
async def test_real_user_has_free_credits(live_core, live_db, live_user_token):
    """New user in real DB -> free_credits = configured value."""
    scope = {
        "type": "http", "method": "GET", "path": "/",
        "headers": [(b"authorization", f"Bearer {live_user_token}".encode())],
    }
    req = Request(scope)
    payload = await live_core.auth.verify_token(req)
    user = await live_core.auth.get_or_create_user(live_db, payload)
    assert user["free_credits"] == 10
    assert user["credits_used"] >= 0


@pytest.mark.asyncio
async def test_real_user_idempotent(live_core, live_db, live_user_token):
    """Call get_or_create_user twice -> one document, credits unchanged."""
    scope = {
        "type": "http", "method": "GET", "path": "/",
        "headers": [(b"authorization", f"Bearer {live_user_token}".encode())],
    }
    req = Request(scope)
    payload = await live_core.auth.verify_token(req)

    user1 = await live_core.auth.get_or_create_user(live_db, payload)
    user2 = await live_core.auth.get_or_create_user(live_db, payload)

    assert user1["logto_user_id"] == user2["logto_user_id"]
    count = await live_db["users"].count_documents({"logto_user_id": payload["sub"]})
    assert count == 1

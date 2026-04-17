"""Live integration tests -- full stack against real services."""

import os
import time

import pytest
from fastapi import FastAPI, Request

from mcp_core import MCPCore


pytestmark = pytest.mark.live


@pytest.fixture
def live_app(live_core, live_db):
    """FastAPI app wired to real services."""
    app = FastAPI()
    live_core.install_routes(app)

    @app.post("/api/mcp/free_tool")
    async def free_tool(request: Request):
        user = await live_core.auth_and_bill(request, "free_tool")
        await live_core.log_tool_call(request, "free_tool", user=user, duration_ms=5)
        return {"ok": True, "user_id": user.get("logto_user_id", "")}

    @app.post("/api/mcp/paid_tool")
    async def paid_tool(request: Request):
        user = await live_core.auth_and_bill(request, "paid_tool")
        await live_core.log_tool_call(request, "paid_tool", user=user, duration_ms=10)
        return {"ok": True, "user_id": user["logto_user_id"]}

    return app


@pytest.fixture
async def async_client(live_app):
    """Async HTTP client that keeps the event loop alive."""
    from httpx import ASGITransport, AsyncClient
    transport = ASGITransport(app=live_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_free_tool_no_auth_real_db(async_client):
    """POST /free_tool -> 200, anonymous user handling works with real MongoDB."""
    r = await async_client.post("/api/mcp/free_tool", json={})
    assert r.status_code == 200
    assert r.json()["user_id"] == "anonymous"


@pytest.mark.asyncio
async def test_paid_tool_real_token_real_billing(
    async_client, live_user_token, live_db
):
    """Mint real Logto token, POST /paid_tool -> 200, credits deducted."""
    # Reset credits so the test works on repeated runs
    # The M2M token sub is the app ID -- find and reset that user
    headers = {"Authorization": f"Bearer {live_user_token}"}

    # Decode the sub from token to find the user
    import jwt
    payload = jwt.decode(live_user_token, options={"verify_signature": False})
    user_sub = payload.get("sub", "")
    if user_sub:
        await live_db["users"].update_one(
            {"logto_user_id": user_sub},
            {"$set": {"credits_used": 0, "free_credits": 10}},
            upsert=True,
        )

    r = await async_client.post("/api/mcp/paid_tool", json={}, headers=headers)
    assert r.status_code == 200
    user_id = r.json()["user_id"]
    assert user_id  # non-empty

    # Verify credits were deducted in real DB
    user = await live_db["users"].find_one({"logto_user_id": user_id})
    assert user["credits_used"] >= 2


@pytest.mark.asyncio
async def test_tool_call_logged_in_real_db(
    async_client, live_user_token, live_db
):
    """After tool call -> tool_logs collection in real MongoDB has the entry."""
    # Reset credits
    import jwt
    payload = jwt.decode(live_user_token, options={"verify_signature": False})
    user_sub = payload.get("sub", "")
    if user_sub:
        await live_db["users"].update_one(
            {"logto_user_id": user_sub},
            {"$set": {"credits_used": 0, "free_credits": 10}},
            upsert=True,
        )

    headers = {"Authorization": f"Bearer {live_user_token}"}
    r = await async_client.post("/api/mcp/paid_tool", json={}, headers=headers)
    assert r.status_code == 200

    # Verify log in real DB
    log = await live_db["tool_logs"].find_one(
        {"product": "mcp-core-live-test", "tool": "paid_tool"},
        sort=[("ts", -1)],
    )
    assert log is not None
    assert log["status"] == "ok"


@pytest.mark.asyncio
async def test_health_real_services(async_client):
    """GET /health -> all checks pass."""
    r = await async_client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["product"] == "mcp-core-live-test"


@pytest.mark.asyncio
async def test_oauth_metadata_matches_real_logto(async_client):
    """GET /.well-known/oauth-protected-resource -> issuer matches real Logto."""
    r = await async_client.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    data = r.json()
    logto_endpoint = os.environ["LOGTO_ENDPOINT"]
    assert f"{logto_endpoint}/oidc" in data["authorization_servers"]

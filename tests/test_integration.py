"""Integration tests -- full auth_and_bill flow via HTTP TestClient."""

import pytest


# ── Free tool ─────────────────────────────────────────────

def test_free_tool_no_auth(client):
    r = client.post("/api/mcp/free_tool", json={})
    assert r.status_code == 200
    data = r.json()
    assert data["user_id"] == "anonymous"


def test_free_tool_with_auth(client, auth_headers):
    r = client.post("/api/mcp/free_tool", json={}, headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["user_id"] == "user_test_123"


# ── Paid tool ─────────────────────────────────────────────

def test_paid_tool_no_auth_401(client):
    r = client.post("/api/mcp/paid_tool", json={})
    assert r.status_code == 401


def test_paid_tool_valid_auth_200(client, auth_headers):
    r = client.post("/api/mcp/paid_tool", json={}, headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["user_id"] == "user_test_123"


def test_paid_tool_dev_bypass(client, dev_headers):
    r = client.post("/api/mcp/paid_tool", json={}, headers=dev_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["user_id"] == "local-dev-user"


# ── Billing exhaustion ───────────────────────────────────

@pytest.mark.asyncio
async def test_paid_tool_exhausted_402(client, auth_headers, core):
    """Exhaust credits then expect 402."""
    # Set user credits to 0
    await core.db["users"].update_one(
        {"logto_user_id": "user_test_123"},
        {"$set": {"credits_used": 10, "free_credits": 10}},
        upsert=True,
    )
    # First call creates user with fresh credits, so exhaust after creation
    client.post("/api/mcp/paid_tool", json={}, headers=auth_headers)
    await core.db["users"].update_one(
        {"logto_user_id": "user_test_123"},
        {"$set": {"credits_used": 10}},
    )
    r = client.post("/api/mcp/paid_tool", json={}, headers=auth_headers)
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["error"] == "Payment required"
    assert "setup_url" in detail


# ── Tool logging ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_call_logged(client, auth_headers, core):
    client.post("/api/mcp/paid_tool", json={}, headers=auth_headers)
    docs = await core.db["tool_logs"].find({}).to_list(100)
    assert len(docs) >= 1
    log = docs[-1]
    assert log["tool"] == "paid_tool"
    assert log["product"] == "test-product"


# ── Health endpoint ───────────────────────────────────────

def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["product"] == "test-product"


# ── Credits endpoint ──────────────────────────────────────

def test_credits_endpoint_no_auth_401(client):
    r = client.get("/api/billing/credits")
    assert r.status_code == 401


def test_credits_endpoint_with_auth(client, auth_headers):
    r = client.get("/api/billing/credits", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert "free_credits" in data
    assert "remaining" in data
    assert "has_subscription" in data


# ── OAuth metadata ────────────────────────────────────────

def test_oauth_metadata(client):
    r = client.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    data = r.json()
    assert data["resource"] == "https://api.test.app"
    assert "header" in data["bearer_methods_supported"]


# ── Dev bypass full flow ──────────────────────────────────

@pytest.mark.asyncio
async def test_dev_bypass_full_flow(client, dev_headers, core):
    """Dev bypass token -> auth + billing + logging all work."""
    r = client.post("/api/mcp/paid_tool", json={}, headers=dev_headers)
    assert r.status_code == 200

    # Check user was created
    user = await core.db["users"].find_one({"logto_user_id": "local-dev-user"})
    assert user is not None
    assert user["credits_used"] >= 3

    # Check log was written
    log = await core.db["tool_logs"].find_one({"user_id": "local-dev-user"})
    assert log is not None

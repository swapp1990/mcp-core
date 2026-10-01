"""Shared-account deletion: Logto's User.Deleted webhook purges this product's records."""

import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mcp_core import MCPCore, RevenueCatBilling, RevenueCatClient
from mcp_core.accounts import deleted_user_id, delete_logto_user

KEY = "hook-signing-key"


def _signed(payload: dict, key: str = KEY):
    body = json.dumps(payload).encode()
    return body, {"logto-signature-sha-256": hmac.new(key.encode(), body, hashlib.sha256).hexdigest(), "content-type": "application/json"}


def _app(mock_db, on_deleted=None):
    core = MCPCore(product_name="videogen", dev_auth_bypass=True)
    core.db = mock_db
    app = FastAPI()
    core.install_account_deletion_webhook(app, signing_key=KEY, on_deleted=on_deleted)
    return TestClient(app)


@pytest.mark.asyncio
async def test_user_deleted_removes_this_products_record(mock_db):
    await mock_db["users"].insert_one({"auth_user_id": "logto:u1", "free_credits": 50})
    await mock_db["users"].insert_one({"auth_user_id": "logto:u2", "free_credits": 50})
    seen = []

    async def extra(sub, db):
        seen.append(sub)

    body, headers = _signed({"event": "User.Deleted", "data": {"id": "u1"}})
    r = _app(mock_db, extra).post("/api/logto/webhook", content=body, headers=headers)

    assert r.status_code == 200
    assert r.json()["user_records_removed"] == 1
    assert await mock_db["users"].count_documents({"auth_user_id": "logto:u1"}) == 0
    assert await mock_db["users"].count_documents({"auth_user_id": "logto:u2"}) == 1
    assert seen == ["u1"]


@pytest.mark.asyncio
async def test_bad_signature_is_rejected_and_nothing_is_removed(mock_db):
    await mock_db["users"].insert_one({"auth_user_id": "logto:u1"})
    body, headers = _signed({"event": "User.Deleted", "data": {"id": "u1"}}, key="wrong-key")

    r = _app(mock_db).post("/api/logto/webhook", content=body, headers=headers)

    assert r.status_code == 401
    assert await mock_db["users"].count_documents({}) == 1


@pytest.mark.asyncio
async def test_other_events_are_ignored(mock_db):
    await mock_db["users"].insert_one({"auth_user_id": "logto:u1"})
    body, headers = _signed({"event": "User.Created", "data": {"id": "u1"}})

    r = _app(mock_db).post("/api/logto/webhook", content=body, headers=headers)

    assert r.json()["status"] == "ignored"
    assert await mock_db["users"].count_documents({}) == 1


def test_deleted_user_id_reads_data_or_params():
    assert deleted_user_id({"data": {"id": "a"}}) == "a"
    assert deleted_user_id({"data": None, "params": {"userId": "b"}}) == "b"
    assert deleted_user_id({"event": "User.Deleted", "data": None, "path": "/users/c", "method": "DELETE"}) == "c"
    assert deleted_user_id({}) == ""


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _FakeDCR:
    endpoint = "https://auth.example.com"

    def __init__(self, status_code):
        self.status_code = status_code
        self.calls = []

    async def _get_token(self):
        return "mgmt-token"

    async def _mgmt_request(self, method, url, *, token):
        self.calls.append((method, url, token))
        return _FakeResponse(self.status_code)


@pytest.mark.asyncio
async def test_delete_logto_user_calls_the_management_api():
    dcr = _FakeDCR(204)
    assert await delete_logto_user(dcr, "u1") is True
    assert dcr.calls == [("DELETE", "https://auth.example.com/api/users/u1", "mgmt-token")]
    assert await delete_logto_user(_FakeDCR(404), "gone") is False


@pytest.mark.asyncio
async def test_delete_logto_user_needs_management_credentials():
    with pytest.raises(RuntimeError):
        await delete_logto_user(None, "u1")


@pytest.mark.asyncio
async def test_signing_key_comes_from_the_documented_env_var(mock_db, monkeypatch):
    monkeypatch.setenv("LOGTO_WEBHOOK_SIGNING_KEY", KEY)
    core = MCPCore(product_name="videogen", dev_auth_bypass=True)
    core.db = mock_db
    app = FastAPI()
    core.install_account_deletion_webhook(app)
    await mock_db["users"].insert_one({"auth_user_id": "logto:u1"})

    body, headers = _signed({"event": "User.Deleted", "params": {"userId": "u1"}, "data": None})
    r = TestClient(app).post("/api/logto/webhook", content=body, headers=headers)

    assert r.status_code == 200
    assert await mock_db["users"].count_documents({}) == 0


def test_without_a_signing_key_the_route_rejects_every_call(mock_db, monkeypatch):
    monkeypatch.delenv("LOGTO_WEBHOOK_SIGNING_KEY", raising=False)
    monkeypatch.delenv("MCP_CORE_LOGTO_WEBHOOK_SIGNING_KEY", raising=False)
    core = MCPCore(product_name="videogen", dev_auth_bypass=True)
    core.db = mock_db
    app = FastAPI()
    core.install_account_deletion_webhook(app)

    body, headers = _signed({"event": "User.Deleted", "data": {"id": "u1"}})
    assert TestClient(app).post("/api/logto/webhook", content=body, headers=headers).status_code == 401


# ── POST /api/account/delete ──────────────────────────────


def _route_client(core, **kwargs):
    app = FastAPI()
    core.install_account_routes(app, **kwargs)
    return TestClient(app)


class _OrderDCR(_FakeDCR):
    """Records how many users records exist at the moment Logto is asked to delete."""

    def __init__(self, db, events, status_code=204):
        super().__init__(status_code)
        self.db, self.events = db, events

    async def _mgmt_request(self, method, url, *, token):
        self.events.append(("logto", await self.db["users"].count_documents({})))
        return await super()._mgmt_request(method, url, token=token)


@pytest.mark.asyncio
async def test_account_delete_runs_before_delete_then_purges_then_deletes_logto(core, mock_db, make_token):
    events = []
    core.dcr = _OrderDCR(mock_db, events)

    async def before_delete(user, db):
        events.append(("before", user["auth_user_id"], await db["users"].count_documents({})))
        return {"stories_deleted": 2}

    r = _route_client(core, before_delete=before_delete).post(
        "/api/account/delete", headers={"Authorization": f"Bearer {make_token()}"}
    )

    assert r.status_code == 200, r.text
    assert r.json() == {"stories_deleted": 2, "deleted": True, "identity_deleted": True}
    assert events == [("before", "logto:user_test_123", 1), ("logto", 0)]
    assert core.dcr.calls[0][:2] == ("DELETE", "https://auth.example.com/api/users/user_test_123")
    assert await mock_db["deleted_accounts"].count_documents({"_id": "logto:user_test_123"}) == 1


@pytest.mark.asyncio
async def test_account_delete_refuses_personal_access_tokens(core, mock_db):
    core.dcr = _FakeDCR(204)
    product_verify = core.auth.verify_token

    async def verify_with_pat(request):
        # A product wrapper that accepts its own PATs, as WriteForYou installs.
        if request.headers.get("authorization", "").endswith("wpat_abc"):
            return {"sub": "user_test_123", "mcp_token_id": "t1"}
        return await product_verify(request)

    core.auth.verify_token = verify_with_pat
    headers = {"Authorization": "Bearer wpat_abc"}

    assert _route_client(core).post("/api/account/delete", headers=headers).status_code == 401
    refused = _route_client(core, pat_prefixes=("wpat_",)).post("/api/account/delete", headers=headers)
    assert refused.status_code == 403
    assert "Personal access tokens" in refused.json()["detail"]
    assert core.dcr.calls == []
    assert await mock_db["users"].count_documents({}) == 0


def test_account_delete_refuses_tokens_for_another_product_and_machine_tokens(core, make_token):
    core.dcr = _FakeDCR(204)
    client = _route_client(core)
    other_audience = make_token(aud="https://api.designforyou.app")
    machine = make_token(sub="m2m_app", client_id="m2m_app")

    assert client.post("/api/account/delete", headers={"Authorization": f"Bearer {other_audience}"}).status_code == 401
    assert client.post("/api/account/delete", headers={"Authorization": f"Bearer {machine}"}).status_code == 403
    assert client.post("/api/account/delete").status_code == 401
    assert core.dcr.calls == []


@pytest.mark.asyncio
async def test_account_delete_deletes_nothing_without_the_management_api(core, mock_db, make_token):
    await mock_db["users"].insert_one({"auth_user_id": "logto:user_test_123", "logto_user_id": "user_test_123"})
    called = []

    async def before_delete(user, db):
        called.append(user)

    r = _route_client(core, before_delete=before_delete).post(
        "/api/account/delete", headers={"Authorization": f"Bearer {make_token()}"}
    )

    assert r.status_code == 503
    assert called == []
    assert await mock_db["users"].count_documents({}) == 1


def test_account_delete_reports_a_logto_failure(core, make_token):
    core.dcr = _FakeDCR(500)
    r = _route_client(core).post("/api/account/delete", headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 502


# ── RevenueCat customer on User.Deleted ───────────────────


def _revenuecat(status_code, calls):
    def handler(request):
        calls.append((request.method, request.url.path, request.headers.get("authorization")))
        return httpx.Response(status_code, json={})

    transport = httpx.MockTransport(handler)
    client = RevenueCatClient("rc_secret", http_client_factory=lambda: httpx.AsyncClient(transport=transport))
    return RevenueCatBilling(client=client, sandbox_allowlist="")


def _app_with_revenuecat(mock_db, revenuecat):
    core = MCPCore(product_name="actforyou", dev_auth_bypass=True, revenuecat=revenuecat)
    core.db = mock_db
    app = FastAPI()
    core.install_account_deletion_webhook(app, signing_key=KEY)
    return TestClient(app)


@pytest.mark.asyncio
@pytest.mark.parametrize(("status_code", "outcome"), [(200, "deleted"), (404, "not_found"), (500, "failed")])
async def test_user_deleted_also_deletes_the_revenuecat_customer(mock_db, status_code, outcome):
    await mock_db["users"].insert_one({"auth_user_id": "logto:u1"})
    calls = []
    body, headers = _signed({"event": "User.Deleted", "data": {"id": "u1"}})

    r = _app_with_revenuecat(mock_db, _revenuecat(status_code, calls)).post(
        "/api/logto/webhook", content=body, headers=headers
    )

    assert r.status_code == 200
    assert r.json()["store_customer"] == outcome
    assert r.json()["user_records_removed"] == 1
    assert calls == [("DELETE", "/v1/subscribers/u1", "Bearer rc_secret")]
    assert await mock_db["users"].count_documents({}) == 0


def test_without_a_revenuecat_secret_no_customer_call_is_made(mock_db):
    calls = []
    revenuecat = _revenuecat(200, calls)
    revenuecat.client.secret_key = ""
    body, headers = _signed({"event": "User.Deleted", "data": {"id": "u1"}})

    r = _app_with_revenuecat(mock_db, revenuecat).post("/api/logto/webhook", content=body, headers=headers)

    assert r.json()["store_customer"] == "skipped"
    assert calls == []


# ── Tombstone ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_token_issued_before_deletion_cannot_recreate_the_user(auth, mock_db):
    await mock_db["users"].insert_one({"auth_user_id": "logto:u1", "logto_user_id": "u1", "free_credits": 0})
    body, headers = _signed({"event": "User.Deleted", "data": {"id": "u1"}})
    assert _app(mock_db).post("/api/logto/webhook", content=body, headers=headers).status_code == 200

    with pytest.raises(HTTPException) as exc:
        await auth.get_or_create_user(mock_db, {"sub": "u1", "iat": int(time.time()) - 60})
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException):
        await auth.get_or_create_user(mock_db, {"sub": "u1"})
    assert await mock_db["users"].count_documents({}) == 0

    again = await auth.get_or_create_user(mock_db, {"sub": "u1", "iat": int(time.time()) + 5})
    assert again["auth_user_id"] == "logto:u1"


def test_a_deleted_accounts_old_token_gets_401_on_a_paid_tool(client, mock_db, auth_headers):
    asyncio.run(mock_db["deleted_accounts"].insert_one({
        "_id": "logto:user_test_123",
        "sub": "user_test_123",
        "deleted_at": datetime.now(timezone.utc) + timedelta(seconds=60),
    }))

    r = client.post("/api/mcp/paid_tool", headers=auth_headers)

    assert r.status_code == 401
    assert asyncio.run(mock_db["users"].count_documents({})) == 0


@pytest.mark.asyncio
async def test_other_accounts_are_unaffected_by_a_tombstone(auth, mock_db):
    await mock_db["deleted_accounts"].insert_one({"_id": "logto:gone", "deleted_at": datetime.now(timezone.utc)})
    user = await auth.get_or_create_user(mock_db, {"sub": "still_here", "iat": 0})
    assert user["free_credits"] == 10

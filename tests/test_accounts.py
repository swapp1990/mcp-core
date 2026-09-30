"""Shared-account deletion: Logto's User.Deleted webhook purges this product's records."""

import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcp_core import MCPCore
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

"""Tests for Supabase auth provider support."""

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from mcp_core import MCPCore
from mcp_core.auth import SupabaseAuth, user_identity


def _fake_request(token: str = "") -> StarletteRequest:
    headers = {}
    if token:
        headers["authorization"] = f"Bearer {token}"
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    return StarletteRequest(scope)


def _transport(status: int = 200, body: dict | None = None, calls: list | None = None):
    def _handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(
                {
                    "url": str(request.url),
                    "headers": dict(request.headers),
                    "method": request.method,
                }
            )
        return httpx.Response(status, json=body or {})

    return httpx.MockTransport(_handler)


def _auth(transport: httpx.MockTransport, calls: list | None = None) -> SupabaseAuth:
    return SupabaseAuth(
        supabase_url="https://project.supabase.co",
        anon_key="sb_publishable_test",
        api_resource="https://api.test.app",
        free_credits=10,
        dev_bypass=True,
        read_only_tools={"free_tool"},
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )


@pytest.mark.asyncio
async def test_supabase_valid_token_returns_normalized_payload():
    calls = []
    auth = _auth(
        _transport(
            body={
                "id": "supabase-user-1",
                "email": "writer@example.com",
                "user_metadata": {
                    "full_name": "Writer Person",
                    "avatar_url": "https://example.com/avatar.png",
                },
                "app_metadata": {
                    "provider": "google",
                    "providers": ["google"],
                },
            },
            calls=calls,
        )
    )

    payload = await auth.verify_token(_fake_request("supabase-token"))

    assert payload["sub"] == "supabase-user-1"
    assert payload["email"] == "writer@example.com"
    assert payload["name"] == "Writer Person"
    assert payload["picture"] == "https://example.com/avatar.png"
    assert payload["provider"] == "google"
    assert payload["auth_provider"] == "supabase"
    assert calls[0]["url"] == "https://project.supabase.co/auth/v1/user"
    assert calls[0]["headers"]["authorization"] == "Bearer supabase-token"
    assert calls[0]["headers"]["apikey"] == "sb_publishable_test"


@pytest.mark.asyncio
async def test_supabase_invalid_token_raises_401():
    auth = _auth(_transport(status=401, body={"msg": "invalid"}))

    with pytest.raises(HTTPException) as exc:
        await auth.verify_token(_fake_request("bad-token"))

    assert exc.value.status_code == 401
    assert exc.value.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_supabase_provider_unavailable_raises_503():
    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    auth = _auth(httpx.MockTransport(_handler))

    with pytest.raises(HTTPException) as exc:
        await auth.verify_token(_fake_request("token"))

    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_supabase_user_without_subject_raises_401():
    auth = _auth(_transport(body={"email": "missing-sub@example.com"}))

    with pytest.raises(HTTPException) as exc:
        await auth.verify_token(_fake_request("token"))

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_supabase_get_or_create_user_writes_neutral_identity(mock_db):
    auth = _auth(_transport())
    payload = {
        "sub": "supabase-user-1",
        "email": "writer@example.com",
        "name": "Writer Person",
        "picture": "https://example.com/avatar.png",
        "auth_provider": "supabase",
    }

    user = await auth.get_or_create_user(mock_db, payload)

    assert user["auth_provider"] == "supabase"
    assert user["auth_subject"] == "supabase-user-1"
    assert user["auth_user_id"] == "supabase:supabase-user-1"
    assert user["email"] == "writer@example.com"
    assert "logto_user_id" not in user
    assert user_identity(user) == "supabase:supabase-user-1"

    again = await auth.get_or_create_user(mock_db, payload)
    count = await mock_db["users"].count_documents(
        {"auth_user_id": "supabase:supabase-user-1"}
    )
    assert again["auth_user_id"] == user["auth_user_id"]
    assert count == 1


def test_mcpcore_selects_supabase_provider_from_constructor():
    core = MCPCore(
        product_name="test",
        auth_provider="supabase",
        supabase_url="https://project.supabase.co",
        supabase_anon_key="sb_publishable_test",
    )

    assert core.auth_provider == "supabase"
    assert isinstance(core.auth, SupabaseAuth)
    assert core.auth.auth_base_url == "https://project.supabase.co/auth/v1"


def test_mcpcore_infers_supabase_provider_from_env(monkeypatch):
    monkeypatch.setenv("MCP_CORE_SUPABASE_URL", "https://env.supabase.co")
    monkeypatch.setenv("MCP_CORE_SUPABASE_ANON_KEY", "sb_env")

    core = MCPCore(product_name="test")

    assert core.auth_provider == "supabase"
    assert isinstance(core.auth, SupabaseAuth)


def test_supabase_mode_does_not_install_logto_oauth_proxy():
    core = MCPCore(
        product_name="test",
        auth_provider="supabase",
        supabase_url="https://project.supabase.co",
        supabase_anon_key="sb_publishable_test",
        supabase_api_resource="https://api.test.app",
    )
    app = FastAPI()
    core.install_routes(app)
    client = TestClient(app, follow_redirects=False)

    r = client.get(
        "/.well-known/oauth-protected-resource",
    )
    assert r.status_code == 200
    assert r.json()["resource"] == "https://api.test.app"
    assert r.json()["authorization_servers"] == [
        "https://project.supabase.co/auth/v1"
    ]

    # Logto-only proxy should not exist in Supabase mode.
    assert client.get("/oauth/authorize").status_code == 404


def test_supabase_metadata_stays_on_upstream_auth_server_with_mcp_client_id():
    core = MCPCore(
        product_name="test",
        auth_provider="supabase",
        supabase_url="https://project.supabase.co",
        supabase_anon_key="sb_publishable_test",
        supabase_api_resource="https://api.test.app",
        mcp_supabase_client_id="supabase-mcp-client",
    )
    app = FastAPI()
    core.install_routes(app)
    client = TestClient(app, follow_redirects=False)

    r = client.get("/.well-known/oauth-protected-resource")

    assert r.status_code == 200
    assert r.json()["authorization_servers"] == [
        "https://project.supabase.co/auth/v1"
    ]


def test_supabase_oauth_metadata_url_uses_authorization_server_discovery_path():
    auth = SupabaseAuth(
        supabase_url="https://project.supabase.co",
        anon_key="sb_publishable_test",
    )

    assert (
        auth.oauth_metadata_url
        == "https://project.supabase.co/.well-known/oauth-authorization-server/auth/v1"
    )

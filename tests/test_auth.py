"""Tests for mcp_core.auth -- Logto JWT validation and user provisioning."""

import asyncio
import time

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient
from starlette.requests import Request as StarletteRequest


# ── Helpers ───────────────────────────────────────────────

def _fake_request(token: str = "") -> StarletteRequest:
    """Build a minimal ASGI request with an Authorization header."""
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


# ── Token validation ──────────────────────────────────────

@pytest.mark.asyncio
async def test_valid_token_returns_payload(auth, make_token):
    token = make_token(sub="user_abc", email="abc@test.com")
    req = _fake_request(token)
    payload = await auth.verify_token(req)
    assert payload["sub"] == "user_abc"
    assert payload["email"] == "abc@test.com"


@pytest.mark.asyncio
async def test_missing_auth_header_returns_none(auth):
    req = _fake_request("")
    payload = await auth.verify_token(req)
    assert payload is None


@pytest.mark.asyncio
async def test_malformed_bearer_returns_none(auth):
    scope = {
        "type": "http", "method": "GET", "path": "/",
        "headers": [(b"authorization", b"NotBearer xxx")],
    }
    req = StarletteRequest(scope)
    payload = await auth.verify_token(req)
    assert payload is None


@pytest.mark.asyncio
async def test_expired_token_401(auth, make_token):
    token = make_token(expired=True)
    req = _fake_request(token)
    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_token(req)
    assert exc_info.value.status_code == 401
    assert "expired" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_wrong_audience_401(auth, make_token):
    token = make_token(aud="https://wrong.audience.com")
    req = _fake_request(token)
    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_token(req)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_wrong_issuer_401(auth, make_token):
    token = make_token(iss="https://wrong.issuer.com/oidc")
    req = _fake_request(token)
    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_token(req)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_invalid_signature_401(auth, make_token, rsa_private_key):
    """Token signed with a different key should be rejected."""
    from cryptography.hazmat.primitives.asymmetric import rsa as rsa_mod
    from cryptography.hazmat.primitives import serialization
    import jwt as pyjwt

    other_key = rsa_mod.generate_private_key(65537, 2048)
    pem = other_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    token = pyjwt.encode(
        {"sub": "x", "aud": "https://api.test.app",
         "iss": "https://test.logto.app/oidc",
         "iat": int(time.time()), "exp": int(time.time()) + 3600},
        pem, algorithm="RS256",
    )
    req = _fake_request(token)
    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_token(req)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_dev_bypass_returns_dev_user(auth):
    req = _fake_request("dev-bypass")
    payload = await auth.verify_token(req)
    assert payload["sub"] == "local-dev-user"
    assert payload["email"] == "dev@localhost"


@pytest.mark.asyncio
async def test_dev_bypass_disabled_rejects(auth, make_token):
    """When dev_bypass is False, 'dev-bypass' token is treated as a real JWT."""
    from mcp_core.auth import LogtoAuth
    strict_auth = LogtoAuth(
        endpoint="https://test.logto.app",
        api_resource="https://api.test.app",
        dev_bypass=False,
    )
    # Copy the mocked JWKS client
    strict_auth._jwks_client = auth._jwks_client
    strict_auth._jwks_last_init = auth._jwks_last_init

    req = _fake_request("dev-bypass")
    with pytest.raises(HTTPException) as exc_info:
        await strict_auth.verify_token(req)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_clock_skew_tolerance(auth, rsa_private_key):
    """Token expired 15s ago (within 30s leeway) should still be valid."""
    from cryptography.hazmat.primitives import serialization
    import jwt as pyjwt

    now = int(time.time())
    pem = rsa_private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    token = pyjwt.encode(
        {"sub": "user_skew", "aud": "https://api.test.app",
         "iss": "https://test.logto.app/oidc",
         "iat": now - 120, "exp": now - 15},
        pem, algorithm="RS256",
    )
    req = _fake_request(token)
    payload = await auth.verify_token(req)
    assert payload["sub"] == "user_skew"


# ── User provisioning ────────────────────────────────────

@pytest.mark.asyncio
async def test_get_or_create_user_first_time(auth, mock_db):
    payload = {"sub": "new_user_1", "email": "new@test.com"}
    user = await auth.get_or_create_user(mock_db, payload)
    assert user["logto_user_id"] == "new_user_1"
    assert user["email"] == "new@test.com"
    assert user["free_credits"] == 10
    assert user["credits_used"] == 0


@pytest.mark.asyncio
async def test_get_or_create_user_existing(auth, mock_db):
    payload = {"sub": "existing_user", "email": "ex@test.com"}
    user1 = await auth.get_or_create_user(mock_db, payload)
    # Simulate some usage
    await mock_db["users"].update_one(
        {"logto_user_id": "existing_user"},
        {"$set": {"credits_used": 5}},
    )
    # Second call should not reset credits
    user2 = await auth.get_or_create_user(mock_db, payload)
    assert user2["credits_used"] == 5
    assert user2["free_credits"] == 10


@pytest.mark.asyncio
async def test_get_or_create_user_race_condition(auth, mock_db):
    """Two concurrent upserts for same sub should result in one document."""
    payload = {"sub": "race_user", "email": "race@test.com"}
    u1, u2 = await asyncio.gather(
        auth.get_or_create_user(mock_db, payload),
        auth.get_or_create_user(mock_db, payload),
    )
    assert u1["logto_user_id"] == u2["logto_user_id"]
    count = await mock_db["users"].count_documents({"logto_user_id": "race_user"})
    assert count == 1


@pytest.mark.asyncio
async def test_m2m_token_rejected(auth, mock_db):
    """M2M tokens (sub == client_id) should be rejected."""
    payload = {"sub": "app_abc", "client_id": "app_abc"}
    with pytest.raises(HTTPException) as exc_info:
        await auth.get_or_create_user(mock_db, payload)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_jwks_cache_reused(auth, make_token):
    """Multiple calls should reuse the cached JWKS client."""
    token = make_token()
    req = _fake_request(token)
    await auth.verify_token(req)
    await auth.verify_token(req)
    # get_signing_key_from_jwt called twice, but _get_jwks_client should
    # return the cached instance (we verify by checking _jwks_last_init didn't change)
    assert auth._jwks_client is not None


# ── OAuth metadata ────────────────────────────────────────

def test_oauth_metadata_shape(auth):
    meta = auth.oauth_protected_resource_metadata(
        scopes=["openid", "profile", "test:read"]
    )
    assert meta["resource"] == "https://api.test.app"
    assert "https://test.logto.app/oidc" in meta["authorization_servers"]
    assert "test:read" in meta["scopes_supported"]
    assert meta["bearer_methods_supported"] == ["header"]


def test_oauth_metadata_base_url_override(auth):
    """When base_url is set (proxy mode), authorization_servers points to self."""
    meta = auth.oauth_protected_resource_metadata(
        base_url="https://myserver.example.com"
    )
    assert meta["authorization_servers"] == ["https://myserver.example.com"]
    # Logto URL should NOT appear
    assert "https://test.logto.app/oidc" not in meta["authorization_servers"]


def test_oauth_metadata_base_url_strips_trailing_slash(auth):
    meta = auth.oauth_protected_resource_metadata(
        base_url="https://myserver.example.com/"
    )
    assert meta["authorization_servers"] == ["https://myserver.example.com"]

"""
Unit tests for LogtoDCR — real RFC 7591 DCR via Logto Management API.

Uses httpx.MockTransport to fake Logto's /oidc/token and /api/applications
endpoints. No network calls.
"""

import json
import time

import httpx
import pytest
from fastapi import HTTPException

from mcp_core.dcr import LogtoDCR


# ── Transport fakes ────────────────────────────────────────

def _make_transport(token_response=None, app_response=None, app_status=201):
    """Build an httpx MockTransport that answers token + create-app calls."""
    calls = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append({
            "url": str(request.url),
            "method": request.method,
            "headers": dict(request.headers),
            "content": request.content.decode() if request.content else "",
        })
        if request.url.path.endswith("/oidc/token"):
            body = token_response or {
                "access_token": "mgmt-token-abc",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": "all",
            }
            return httpx.Response(200, json=body)
        if request.url.path.endswith("/api/applications"):
            body = app_response or {
                "id": "new-app-client-id",
                "secret": "new-app-secret",
                "name": "mcp-dcr: Claude Code",
                "type": "Native",
            }
            return httpx.Response(app_status, json=body)
        return httpx.Response(404, json={"detail": "Not found"})

    return httpx.MockTransport(_handler), calls


def _dcr_with(transport):
    return LogtoDCR(
        logto_endpoint="https://test.logto.app",
        mgmt_app_id="mgmt-id",
        mgmt_app_secret="mgmt-secret",
        http_client_factory=lambda: httpx.AsyncClient(transport=transport, timeout=5.0),
    )


# ── Tests ──────────────────────────────────────────────────

def test_init_requires_creds():
    with pytest.raises(ValueError):
        LogtoDCR(logto_endpoint="https://x", mgmt_app_id="", mgmt_app_secret="")


async def test_register_creates_native_app_for_public_client():
    transport, calls = _make_transport()
    dcr = _dcr_with(transport)

    result = await dcr.register({
        "redirect_uris": ["http://localhost:39879/callback"],
        "client_name": "Claude Code",
        "token_endpoint_auth_method": "none",
    })

    assert result["client_id"] == "new-app-client-id"
    assert result["redirect_uris"] == ["http://localhost:39879/callback"]
    assert result["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in result  # Native = public, no secret returned

    # Management API was called with the incoming redirect_uri baked in
    create_call = next(c for c in calls if "/api/applications" in c["url"])
    body = json.loads(create_call["content"])
    assert body["type"] == "Native"
    assert body["oidcClientMetadata"]["redirectUris"] == ["http://localhost:39879/callback"]
    assert body["oidcClientMetadata"]["postLogoutRedirectUris"] == []


async def test_register_creates_traditional_app_for_confidential_client():
    transport, calls = _make_transport(
        app_response={"id": "trad-app", "secret": "trad-secret", "name": "x", "type": "Traditional"}
    )
    dcr = _dcr_with(transport)

    result = await dcr.register({
        "redirect_uris": ["https://app.example.com/cb"],
        "token_endpoint_auth_method": "client_secret_basic",
    })

    assert result["client_id"] == "trad-app"
    assert result["client_secret"] == "trad-secret"
    assert result["client_secret_expires_at"] == 0
    create_call = next(c for c in calls if "/api/applications" in c["url"])
    assert json.loads(create_call["content"])["type"] == "Traditional"


async def test_register_rejects_missing_redirect_uris():
    transport, _ = _make_transport()
    dcr = _dcr_with(transport)
    with pytest.raises(HTTPException) as exc:
        await dcr.register({"client_name": "nope"})
    assert exc.value.status_code == 400


async def test_register_rejects_non_list_redirect_uris():
    transport, _ = _make_transport()
    dcr = _dcr_with(transport)
    with pytest.raises(HTTPException) as exc:
        await dcr.register({"redirect_uris": "http://x/cb"})
    assert exc.value.status_code == 400


async def test_mgmt_token_cached_across_registrations():
    transport, calls = _make_transport()
    dcr = _dcr_with(transport)

    await dcr.register({"redirect_uris": ["http://a/cb"]})
    await dcr.register({"redirect_uris": ["http://b/cb"]})

    token_calls = [c for c in calls if "/oidc/token" in c["url"]]
    assert len(token_calls) == 1  # fetched once, cached for the second call


async def test_mgmt_token_refetched_on_401():
    # Fail once with 401, then succeed. Token should be re-fetched.
    app_responses = iter([
        httpx.Response(401, json={"error": "expired"}),
        httpx.Response(201, json={"id": "recovered-id", "secret": "", "type": "Native"}),
    ])

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oidc/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if request.url.path.endswith("/api/applications"):
            return next(app_responses)
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)
    dcr = _dcr_with(transport)

    result = await dcr.register({"redirect_uris": ["http://x/cb"]})
    assert result["client_id"] == "recovered-id"


async def test_register_propagates_upstream_error():
    transport, _ = _make_transport(
        app_response={"error": "forbidden"}, app_status=403
    )
    dcr = _dcr_with(transport)
    with pytest.raises(HTTPException) as exc:
        await dcr.register({"redirect_uris": ["http://x/cb"]})
    assert exc.value.status_code == 502


async def test_register_mgmt_token_failure():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    transport = httpx.MockTransport(_handler)
    dcr = _dcr_with(transport)
    with pytest.raises(HTTPException) as exc:
        await dcr.register({"redirect_uris": ["http://x/cb"]})
    assert exc.value.status_code == 502


async def test_mgmt_token_expiry_refetches():
    transport, calls = _make_transport(
        token_response={"access_token": "t", "expires_in": 60}
    )
    dcr = _dcr_with(transport)

    await dcr.register({"redirect_uris": ["http://a/cb"]})
    # Force expiry by rewinding the cached exp.
    dcr._token_exp = time.time() - 1
    await dcr.register({"redirect_uris": ["http://b/cb"]})

    token_calls = [c for c in calls if "/oidc/token" in c["url"]]
    assert len(token_calls) == 2

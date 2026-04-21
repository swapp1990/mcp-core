"""Tests for the branded sign-in redirect feature on /oauth/authorize.

When `branded_sign_in_url` is configured, /oauth/authorize bounces
unauthenticated users to the product's own sign-in page instead of
forwarding straight to Logto's hosted UI. The product's page is expected
to sign the user in and redirect back with `signed_in=1`, at which point
/oauth/authorize falls through to Logto as normal (Logto now sees the
apex-domain session cookie and issues a code silently).
"""

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcp_core import MCPCore


def _make_app(branded_sign_in_url: str = "") -> TestClient:
    """Fresh MCPCore + FastAPI with only the routes needed for /oauth/authorize."""
    core = MCPCore(
        product_name="test-product",
        logto_endpoint="https://test.logto.app",
        logto_api_resource="https://api.test.app",
        mcp_logto_app_id="test-mcp-app",
        branded_sign_in_url=branded_sign_in_url,
    )
    app = FastAPI()
    core.install_routes(app)
    # TestClient follows redirects by default; we want to inspect them raw.
    return TestClient(app, follow_redirects=False)


# ── When branded_sign_in_url is not set: existing behavior ────────────

def test_authorize_forwards_to_logto_when_no_branded_url():
    client = _make_app()
    r = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "abc",
            "redirect_uri": "http://localhost:1234/cb",
            "state": "xyz",
            "code_challenge": "chal",
            "code_challenge_method": "S256",
            "scope": "openid profile",
        },
    )
    assert r.status_code == 307
    loc = r.headers["location"]
    assert loc.startswith("https://test.logto.app/oidc/auth?")
    qs = parse_qs(urlparse(loc).query)
    assert qs["client_id"] == ["abc"]
    assert qs["resource"] == ["https://api.test.app"]
    assert "signed_in" not in qs


# ── When branded_sign_in_url IS set ───────────────────────────────────

def test_authorize_redirects_to_branded_sign_in_on_first_visit():
    client = _make_app(branded_sign_in_url="https://product.test/sign-in")
    r = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "abc",
            "redirect_uri": "http://localhost:1234/cb",
            "state": "xyz",
            "code_challenge": "chal",
            "code_challenge_method": "S256",
            "scope": "openid profile",
        },
    )
    assert r.status_code == 302
    loc = r.headers["location"]
    parsed = urlparse(loc)
    # Points at the product's sign-in page.
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://product.test/sign-in"

    # `return_to` carries the original /oauth/authorize URL with signed_in=1
    # appended so the retry doesn't loop.
    qs = parse_qs(parsed.query)
    assert "return_to" in qs
    return_to = qs["return_to"][0]
    rt_parsed = urlparse(return_to)
    assert rt_parsed.path == "/oauth/authorize"
    rt_qs = parse_qs(rt_parsed.query)
    assert rt_qs["signed_in"] == ["1"]
    # Original OAuth params preserved.
    assert rt_qs["client_id"] == ["abc"]
    assert rt_qs["state"] == ["xyz"]
    assert rt_qs["code_challenge"] == ["chal"]
    assert rt_qs["scope"] == ["openid profile"]


def test_authorize_skips_branded_redirect_when_signed_in_marker_present():
    """After the product's sign-in page bounces back with signed_in=1, we
    forward to Logto (which now sees the apex session cookie)."""
    client = _make_app(branded_sign_in_url="https://product.test/sign-in")
    r = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "abc",
            "redirect_uri": "http://localhost:1234/cb",
            "state": "xyz",
            "code_challenge": "chal",
            "code_challenge_method": "S256",
            "scope": "openid profile",
            "signed_in": "1",
        },
    )
    assert r.status_code == 307
    loc = r.headers["location"]
    assert loc.startswith("https://test.logto.app/oidc/auth?")
    # signed_in marker must be stripped before forwarding so Logto doesn't
    # see a parameter it doesn't recognize.
    qs = parse_qs(urlparse(loc).query)
    assert "signed_in" not in qs
    assert qs["client_id"] == ["abc"]
    assert qs["resource"] == ["https://api.test.app"]


def test_authorize_branded_url_with_existing_query_string():
    """If the product's sign-in URL already has a `?`, we use `&` to
    append return_to rather than clobbering their params."""
    client = _make_app(
        branded_sign_in_url="https://product.test/sign-in?utm=mcp"
    )
    r = client.get(
        "/oauth/authorize",
        params={"client_id": "abc", "redirect_uri": "http://x/cb"},
    )
    assert r.status_code == 302
    loc = r.headers["location"]
    parsed = urlparse(loc)
    qs = parse_qs(parsed.query)
    assert qs["utm"] == ["mcp"]
    assert "return_to" in qs


# ── Env-var configuration ──────────────────────────────────────────────

def test_branded_sign_in_url_from_env(monkeypatch):
    """MCP_CORE_BRANDED_SIGN_IN_URL env var is picked up when the
    constructor arg is not provided."""
    monkeypatch.setenv(
        "MCP_CORE_BRANDED_SIGN_IN_URL", "https://product.test/sign-in"
    )
    core = MCPCore(
        product_name="test",
        logto_endpoint="https://test.logto.app",
        logto_api_resource="https://api.test.app",
    )
    assert core._branded_sign_in_url == "https://product.test/sign-in"


def test_constructor_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("MCP_CORE_BRANDED_SIGN_IN_URL", "https://env.test/sign-in")
    core = MCPCore(
        product_name="test",
        logto_endpoint="https://test.logto.app",
        logto_api_resource="https://api.test.app",
        branded_sign_in_url="https://arg.test/sign-in",
    )
    assert core._branded_sign_in_url == "https://arg.test/sign-in"

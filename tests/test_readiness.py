"""Tests for MCP OAuth readiness diagnostics."""

from __future__ import annotations

import urllib.parse

import pytest

from mcp_core.readiness import (
    ReadinessError,
    check_mcp_readiness,
    metadata_candidates,
)


MCP_URL = "https://writer.test/mcp/v2/"
ORIGIN = "https://writer.test"
SUPABASE_AS = "https://project.supabase.co/auth/v1"
SUPABASE_METADATA = "https://project.supabase.co/.well-known/oauth-authorization-server/auth/v1"
SUPABASE_AUTHORIZE = "https://project.supabase.co/auth/v1/oauth/authorize"
SUPABASE_TOKEN = "https://project.supabase.co/auth/v1/oauth/token"
SUPABASE_REGISTER = "https://project.supabase.co/auth/v1/oauth/clients/register"
PROBE_REDIRECT = "http://localhost:53682/callback"


class FakeHTTP:
    def __init__(self):
        self.responses = {}
        self.redirects = {}
        self.form_responses = {}
        self.calls = []

    def respond(self, method: str, url: str, status: int, payload: dict) -> None:
        self.responses[(method, url)] = (status, payload)

    def get_json(self, url, *, body=None, method="GET", headers=None):
        self.calls.append({
            "method": method,
            "url": url,
            "body": body,
            "headers": headers or {},
        })
        try:
            return self.responses[(method, url)]
        except KeyError:
            return 404, {"error": f"unexpected {method} {url}"}

    def redirect_location(self, url):
        self.calls.append({"method": "REDIRECT", "url": url})
        parsed = urllib.parse.urlparse(url)
        key = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        return self.redirects.get(key, (404, "", {"error": f"unexpected redirect {url}"}))

    def post_form_json(self, url, form):
        self.calls.append({"method": "FORM", "url": url, "form": form})
        return self.form_responses.get(url, {"error": f"unexpected form {url}"})


def add_common_supabase_responses(http: FakeHTTP) -> None:
    http.respond(
        "GET",
        f"{ORIGIN}/.well-known/oauth-protected-resource",
        200,
        {"resource": ORIGIN, "authorization_servers": [SUPABASE_AS]},
    )
    http.respond(
        "GET",
        SUPABASE_METADATA,
        200,
        {
            "issuer": SUPABASE_AS,
            "authorization_endpoint": SUPABASE_AUTHORIZE,
            "token_endpoint": SUPABASE_TOKEN,
            "registration_endpoint": SUPABASE_REGISTER,
        },
    )
    http.respond(
        "POST",
        SUPABASE_REGISTER,
        201,
        {"client_id": "probe-client", "redirect_uris": [PROBE_REDIRECT]},
    )
    http.respond(
        "POST",
        MCP_URL,
        401,
        {
            "_headers": {
                "www-authenticate": (
                    'Bearer resource_metadata="'
                    f'{ORIGIN}/.well-known/oauth-protected-resource"'
                )
            }
        },
    )


def test_metadata_candidates_include_rfc_path_for_auth_server_with_path():
    assert metadata_candidates(SUPABASE_AS)[0] == SUPABASE_METADATA


def test_supabase_readiness_checks_dcr_and_consent_redirect():
    http = FakeHTTP()
    add_common_supabase_responses(http)
    http.redirects[SUPABASE_AUTHORIZE] = (
        302,
        f"{ORIGIN}/consent?authorization_id=auth_123",
        {},
    )

    events = check_mcp_readiness(MCP_URL, http=http)

    assert [event.step for event in events] == ["1", "2", "3", "4", "5", "6", "7"]
    registration_call = next(
        call for call in http.calls
        if call["method"] == "POST" and call["url"] == SUPABASE_REGISTER
    )
    assert registration_call["body"]["redirect_uris"] == [PROBE_REDIRECT]
    authorize_call = next(call for call in http.calls if call["method"] == "REDIRECT")
    assert "client_id=probe-client" in authorize_call["url"]
    assert "code_challenge_method=S256" in authorize_call["url"]


def test_supabase_readiness_explains_disabled_oauth_server():
    http = FakeHTTP()
    http.respond(
        "GET",
        f"{ORIGIN}/.well-known/oauth-protected-resource",
        200,
        {"resource": ORIGIN, "authorization_servers": [SUPABASE_AS]},
    )
    http.respond(
        "GET",
        SUPABASE_METADATA,
        403,
        {"error_code": "feature_disabled", "message": "OAuth Server is disabled"},
    )

    with pytest.raises(ReadinessError) as exc_info:
        check_mcp_readiness(MCP_URL, http=http)

    assert exc_info.value.step == "2"
    assert "Supabase OAuth Server is disabled" in exc_info.value.message


def test_supabase_readiness_requires_dynamic_client_registration():
    http = FakeHTTP()
    http.respond(
        "GET",
        f"{ORIGIN}/.well-known/oauth-protected-resource",
        200,
        {"resource": ORIGIN, "authorization_servers": [SUPABASE_AS]},
    )
    http.respond(
        "GET",
        SUPABASE_METADATA,
        200,
        {
            "issuer": SUPABASE_AS,
            "authorization_endpoint": SUPABASE_AUTHORIZE,
            "token_endpoint": SUPABASE_TOKEN,
        },
    )

    with pytest.raises(ReadinessError) as exc_info:
        check_mcp_readiness(MCP_URL, http=http)

    assert exc_info.value.step == "2"
    assert "Dynamic Client Registration" in exc_info.value.message


def test_supabase_readiness_catches_wrong_consent_origin():
    http = FakeHTTP()
    add_common_supabase_responses(http)
    http.redirects[SUPABASE_AUTHORIZE] = (
        302,
        "https://melo.test/consent?authorization_id=auth_123",
        {},
    )

    with pytest.raises(ReadinessError) as exc_info:
        check_mcp_readiness(MCP_URL, http=http)

    assert exc_info.value.step == "7"
    assert "expected app origin" in exc_info.value.message


def test_logto_readiness_keeps_same_origin_proxy_and_resource_rewrite_check():
    http = FakeHTTP()
    http.respond(
        "GET",
        f"{ORIGIN}/.well-known/oauth-protected-resource",
        200,
        {"resource": ORIGIN, "authorization_servers": [ORIGIN]},
    )
    http.respond(
        "GET",
        f"{ORIGIN}/.well-known/oauth-authorization-server",
        200,
        {
            "issuer": ORIGIN,
            "authorization_endpoint": f"{ORIGIN}/oauth/authorize",
            "token_endpoint": f"{ORIGIN}/oauth/token",
            "registration_endpoint": f"{ORIGIN}/oauth/register",
        },
    )
    http.respond(
        "POST",
        f"{ORIGIN}/oauth/register",
        201,
        {"client_id": "logto-probe", "redirect_uris": [PROBE_REDIRECT]},
    )
    http.respond(
        "POST",
        MCP_URL,
        401,
        {
            "_headers": {
                "www-authenticate": (
                    'Bearer resource_metadata="'
                    f'{ORIGIN}/.well-known/oauth-protected-resource"'
                )
            }
        },
    )
    http.form_responses[f"{ORIGIN}/oauth/token"] = {"error": "invalid_grant"}

    events = check_mcp_readiness(MCP_URL, http=http)

    assert [event.step for event in events] == ["1", "2", "3", "4", "5", "6"]
    token_call = next(call for call in http.calls if call["method"] == "FORM")
    assert token_call["form"]["resource"] == f"{ORIGIN}/"


def test_logto_readiness_fails_when_token_proxy_rejects_trailing_slash_resource():
    http = FakeHTTP()
    http.respond(
        "GET",
        f"{ORIGIN}/.well-known/oauth-protected-resource",
        200,
        {"resource": ORIGIN, "authorization_servers": [ORIGIN]},
    )
    http.respond(
        "GET",
        f"{ORIGIN}/.well-known/oauth-authorization-server",
        200,
        {
            "issuer": ORIGIN,
            "authorization_endpoint": f"{ORIGIN}/oauth/authorize",
            "token_endpoint": f"{ORIGIN}/oauth/token",
            "registration_endpoint": f"{ORIGIN}/oauth/register",
        },
    )
    http.respond(
        "POST",
        f"{ORIGIN}/oauth/register",
        201,
        {"client_id": "logto-probe", "redirect_uris": [PROBE_REDIRECT]},
    )
    http.respond(
        "POST",
        MCP_URL,
        401,
        {
            "_headers": {
                "www-authenticate": (
                    'Bearer resource_metadata="'
                    f'{ORIGIN}/.well-known/oauth-protected-resource"'
                )
            }
        },
    )
    http.form_responses[f"{ORIGIN}/oauth/token"] = {
        "error": "invalid_target",
        "error_description": "unknown resource",
    }

    with pytest.raises(ReadinessError) as exc_info:
        check_mcp_readiness(MCP_URL, http=http)

    assert exc_info.value.step == "6"
    assert "rejects trailing-slash resource" in exc_info.value.message

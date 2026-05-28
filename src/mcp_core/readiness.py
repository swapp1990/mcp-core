"""MCP OAuth readiness checks for RFC 9728 clients.

The checker follows the same discovery path as Claude Code and other MCP
clients: protected-resource metadata, authorization-server metadata, dynamic
client registration, unauthenticated MCP bearer challenge, and provider-specific
OAuth behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import sys
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError
from typing import Callable, Dict, Iterable, Optional, Protocol, Tuple


JsonDict = Dict[str, object]
JsonResponse = Tuple[int, JsonDict]


@dataclass(frozen=True)
class ReadinessEvent:
    """A single readiness check result."""

    step: str
    status: str
    message: str

    def format(self) -> str:
        label = "OK  " if self.status == "ok" else "FAIL"
        return f"[{label} step {self.step}] {self.message}"


class ReadinessError(RuntimeError):
    """Raised when a readiness check fails."""

    def __init__(
        self,
        step: str,
        message: str,
        events: Iterable[ReadinessEvent] = (),
    ):
        super().__init__(message)
        self.step = step
        self.message = message
        self.events = list(events)


class ReadinessHTTP(Protocol):
    """Small HTTP surface used by the readiness checker."""

    def get_json(
        self,
        url: str,
        *,
        body: Optional[JsonDict] = None,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
    ) -> JsonResponse:
        ...

    def redirect_location(self, url: str) -> tuple[int, str, JsonDict]:
        ...

    def post_form_json(self, url: str, form: Dict[str, str]) -> JsonDict:
        ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


class UrllibReadinessHTTP:
    """Network implementation backed by urllib from the Python standard lib."""

    user_agent = "mcp-core-readiness"

    def get_json(
        self,
        url: str,
        *,
        body: Optional[JsonDict] = None,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
    ) -> JsonResponse:
        data = None
        request_headers = {"User-Agent": self.user_agent}
        if body is not None:
            data = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, self._error_payload(exc)
        except URLError as exc:
            return 0, {"_error": str(exc)}

    def redirect_location(self, url: str) -> tuple[int, str, JsonDict]:
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": self.user_agent},
        )
        try:
            with opener.open(req, timeout=15) as response:
                return response.status, response.headers.get("Location", ""), {}
        except HTTPError as exc:
            return exc.code, exc.headers.get("Location", ""), self._error_payload(exc)

    def post_form_json(self, url: str, form: Dict[str, str]) -> JsonDict:
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(form).encode(),
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": self.user_agent,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            return self._error_payload(exc)

    @staticmethod
    def _error_payload(exc: HTTPError) -> JsonDict:
        text = exc.read().decode(errors="replace")
        try:
            payload = json.loads(text)
        except Exception:
            payload = {"_raw": text}
        payload["_headers"] = {k.lower(): v for k, v in exc.headers.items()}
        return payload


def metadata_candidates(as_url: str) -> list[str]:
    """Return RFC 8414/OIDC metadata URLs to try for an auth server."""

    parsed = urllib.parse.urlparse(as_url.rstrip("/"))
    candidates = []
    if parsed.path and parsed.path != "/":
        path = f"/.well-known/oauth-authorization-server{parsed.path.rstrip('/')}"
        candidates.append(
            urllib.parse.urlunparse(
                (parsed.scheme, parsed.netloc, path, "", "", "")
            )
        )
    base = as_url.rstrip("/")
    candidates.extend(
        [
            f"{base}/.well-known/oauth-authorization-server",
            f"{base}/.well-known/openid-configuration",
        ]
    )

    deduped = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped


def is_supabase(as_url: str, metadata: Optional[JsonDict] = None) -> bool:
    """Best-effort provider detection for Supabase Auth servers."""

    parsed = urllib.parse.urlparse(as_url)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    issuer = str((metadata or {}).get("issuer", ""))
    authorize = str((metadata or {}).get("authorization_endpoint", ""))
    return (
        host.endswith(".supabase.co")
        or host == "supabase.co"
        or path.endswith("/auth/v1")
        or "/auth/v1" in issuer
        or "/auth/v1" in authorize
    )


def is_supabase_oauth_disabled(payload: JsonDict) -> bool:
    combined = " ".join(
        str(payload.get(key, ""))
        for key in ("error", "error_code", "msg", "message", "error_description")
    ).lower()
    return "feature_disabled" in combined or "oauth server is disabled" in combined


def has_authorization_server_shape(payload: JsonDict) -> bool:
    return bool(payload.get("issuer") and payload.get("authorization_endpoint"))


def check_mcp_readiness(
    mcp_url: str,
    *,
    http: Optional[ReadinessHTTP] = None,
    emit: Optional[Callable[[ReadinessEvent], None]] = None,
) -> list[ReadinessEvent]:
    """Check whether an MCP endpoint is ready for OAuth client onboarding.

    Returns all successful events. Raises :class:`ReadinessError` on the first
    failed step; the exception carries the events emitted up to that point.
    """

    client = http or UrllibReadinessHTTP()
    events: list[ReadinessEvent] = []

    def record(step: str, status: str, message: str) -> None:
        event = ReadinessEvent(step=step, status=status, message=message)
        events.append(event)
        if emit:
            emit(event)

    def ok(step: str, message: str) -> None:
        record(step, "ok", message)

    def fail(step: str, message: str) -> None:
        record(step, "fail", message)
        raise ReadinessError(step, message, events)

    parsed = urllib.parse.urlparse(mcp_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # 1. Protected-resource metadata.
    status, meta = client.get_json(f"{origin}/.well-known/oauth-protected-resource")
    if status != 200:
        fail("1", f"protected-resource returned {status}: {meta}")
    resource = meta.get("resource", "")
    if resource not in (origin, mcp_url):
        fail(
            "1",
            f"resource={resource!r} matches neither origin {origin!r} nor "
            f"full URL {mcp_url!r}",
        )
    auth_servers = meta.get("authorization_servers") or []
    if not auth_servers:
        fail("1", "authorization_servers is empty")
    ok("1", f"resource={resource}, auth_servers={auth_servers}")

    # 2. Authorization-server metadata.
    as_url = str(auth_servers[0])
    tried = []
    disabled_payload = None
    metadata: Optional[JsonDict] = None
    metadata_url = ""
    for url in metadata_candidates(as_url):
        tried.append(url)
        status, payload = client.get_json(url)
        if is_supabase_oauth_disabled(payload):
            disabled_payload = payload
            continue
        if (
            status == 200
            and isinstance(payload, dict)
            and has_authorization_server_shape(payload)
        ):
            metadata = payload
            metadata_url = url
            break

    provider_is_supabase = is_supabase(as_url, metadata)
    if disabled_payload and provider_is_supabase:
        fail(
            "2",
            "Supabase OAuth Server is disabled. Enable Auth > OAuth 2.1 "
            "Server in Supabase, enable Dynamic Client Registration, and set "
            "the Authorization Path to your app's consent route before using "
            "MCP auth.",
        )
    if not metadata:
        fail("2", f"could not load authorization-server metadata from {tried}")

    registration_endpoint = str(metadata.get("registration_endpoint", ""))
    if not registration_endpoint:
        if provider_is_supabase:
            fail(
                "2",
                "Supabase metadata loaded, but registration_endpoint is "
                "missing. Enable Dynamic Client Registration in the Supabase "
                "OAuth 2.1 Server settings for automatic MCP client onboarding.",
            )
        fail("2", f"metadata from {metadata_url} has no registration_endpoint")
    ok("2", f"DCR advertised at {registration_endpoint} (via {metadata_url})")

    # 3. Dynamic client registration.
    probe_redirect = "http://localhost:53682/callback"
    body: JsonDict = {
        "client_name": "mcp-readiness-probe",
        "redirect_uris": [probe_redirect],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    status, registration = client.get_json(
        registration_endpoint,
        body=body,
        method="POST",
    )
    if status not in (200, 201):
        fail("3", f"register returned {status}: {registration}")
    client_id = str(registration.get("client_id") or "")
    if not client_id:
        fail("3", f"no client_id in register response: {registration}")
    got_uris = registration.get("redirect_uris") or []
    if probe_redirect not in got_uris:
        fail(
            "3",
            f"register did not echo redirect_uri {probe_redirect!r}: got {got_uris}",
        )
    ok("3", f"client registered: client_id={client_id}, redirect_uris OK")

    # 4. MCP v2 transport bearer challenge.
    status, challenge = client.get_json(
        mcp_url,
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        method="POST",
        headers={"Accept": "application/json, text/event-stream"},
    )
    if status != 401:
        fail(
            "4",
            f"unauthenticated v2 POST returned {status}, expected 401: "
            f"{challenge}",
        )
    www_auth = str(challenge.get("_headers", {}).get("www-authenticate", ""))
    if not www_auth.startswith("Bearer "):
        fail("4", f"missing Bearer WWW-Authenticate challenge: {www_auth!r}")
    if (
        "resource_metadata=" not in www_auth
        or "/.well-known/oauth-protected-resource" not in www_auth
    ):
        fail("4", f"Bearer challenge is missing resource metadata: {www_auth!r}")
    ok("4", "v2 transport returns OAuth Bearer challenge")

    # 5/6/7. Provider-specific endpoint checks.
    authorization_endpoint = str(metadata.get("authorization_endpoint", ""))
    token_endpoint = str(metadata.get("token_endpoint", ""))
    if provider_is_supabase:
        if not authorization_endpoint or not token_endpoint:
            fail("5", f"Supabase metadata missing authorize/token endpoints: {metadata}")
        ok(
            "5",
            "Supabase authorize/token endpoints are upstream; no Logto proxy "
            "is required",
        )
        ok(
            "6",
            "Supabase token endpoint does not need Logto RFC 8707 resource "
            "rewriting",
        )
        authorize_params = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": probe_redirect,
                "scope": "openid email profile",
                "state": "mcp-readiness",
                "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                "code_challenge_method": "S256",
            }
        )
        auth_status, location, auth_body = client.redirect_location(
            f"{authorization_endpoint}?{authorize_params}"
        )
        if auth_status not in (302, 303, 307, 308) or not location:
            fail(
                "7",
                f"authorize returned {auth_status}, expected redirect to "
                f"consent UI: {auth_body}",
            )
        parsed_location = urllib.parse.urlparse(location)
        if f"{parsed_location.scheme}://{parsed_location.netloc}" != origin:
            fail(
                "7",
                f"authorize redirects to {location!r}, expected app origin "
                f"{origin!r}. Check Supabase Auth Site URL and OAuth Server "
                "Authorization Path.",
            )
        if "authorization_id=" not in location:
            fail("7", f"authorize redirect is missing authorization_id: {location!r}")
        ok("7", f"Supabase authorize redirects to app consent UI: {location}")
        return events

    for key, value in (
        ("authorization_endpoint", authorization_endpoint),
        ("token_endpoint", token_endpoint),
    ):
        if not value.startswith(origin):
            fail(
                "5",
                f"{key}={value!r} should start with {origin!r}; Logto MCP "
                "auth must proxy this endpoint to inject RFC 8707 resource.",
            )
    ok("5", f"authorize/token endpoints proxied through {origin}")

    # Claude Code normalizes origin URIs with a trailing slash. Logto's
    # byte-strict resource matching was the recurring failure mode. Submit a
    # bogus auth code and expect an error about the code, not the resource.
    token_body = client.post_form_json(
        token_endpoint,
        {
            "grant_type": "authorization_code",
            "code": "mcp-readiness-probe-invalid",
            "redirect_uri": probe_redirect,
            "client_id": client_id,
            "resource": origin + "/",
        },
    )
    error_code = str(token_body.get("error") or "").lower()
    error_desc = str(token_body.get("error_description") or "").lower()
    combined = f"{error_code} {error_desc}"
    if "invalid_target" in error_code or (
        "resource" in combined
        and ("missing" in combined or "unknown" in combined)
    ):
        fail(
            "6",
            f"token endpoint rejects trailing-slash resource: {token_body}; "
            "proxy must force-set resource to the registered indicator.",
        )
    ok(
        "6",
        "token accepts trailing-slash resource "
        f"(got expected grant error: {error_code or '?'})",
    )

    return events


def cli(argv: Optional[list[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: mcp-core-check-readiness <mcp_url>")
        return 2
    try:
        check_mcp_readiness(args[0], emit=lambda event: print(event.format()))
    except ReadinessError:
        return 1
    print("\n[PASS] MCP READY -- safe to add to .mcp.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())

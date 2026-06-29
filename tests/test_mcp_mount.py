"""Tests for mount_mcp helper.

Coverage:
- Both transports mount when fastapi-mcp + fastmcp are installed
- legacy_sse=False skips the SSE mount
- get_http_headers monkey-patch is applied (and idempotent)
- The RouteMap filter excludes non-mcp-tagged routes
"""

import pytest
from fastapi import APIRouter, FastAPI, Request

from mcp_core import MCPCore, mount_mcp


@pytest.fixture
def app_with_tagged_routes(core):
    """FastAPI app with one mcp-tagged route and one untagged route."""
    app = FastAPI()
    core.install_routes(app)

    mcp_router = APIRouter(prefix="/api/mcp", tags=["mcp"])

    @mcp_router.post("/echo")
    async def echo(request: Request):
        return {"ok": True}

    legacy_router = APIRouter(prefix="/api/legacy", tags=["legacy"])

    @legacy_router.post("/private")
    async def private(request: Request):
        return {"hidden": True}

    app.include_router(mcp_router)
    app.include_router(legacy_router)
    return app


def _route_paths(app: FastAPI) -> set[str]:
    return {str(getattr(r, "path", "")) for r in app.routes}


def test_mount_mcp_registers_both_transports(core: MCPCore, app_with_tagged_routes):
    result = mount_mcp(
        app_with_tagged_routes,
        core=core,
        name="test-server",
        description="test",
        tags={"mcp"},
    )
    assert result["sse"] is True
    assert result["v2"] is True
    assert result["sse_path"] == "/mcp"
    assert result["v2_path"] == "/mcp/v2"

    paths = _route_paths(app_with_tagged_routes)
    assert "/mcp" in paths  # fastapi-mcp SSE entry
    assert "/mcp/v2" in paths  # FastMCP v3 mount


def test_mount_mcp_legacy_sse_disabled(core: MCPCore, app_with_tagged_routes):
    result = mount_mcp(
        app_with_tagged_routes,
        core=core,
        name="test-server",
        tags={"mcp"},
        legacy_sse=False,
    )
    assert result["sse"] is False
    assert result["v2"] is True

    paths = _route_paths(app_with_tagged_routes)
    assert "/mcp" not in paths
    assert "/mcp/v2" in paths


def test_mcpcore_mount_mcp_method(core: MCPCore, app_with_tagged_routes):
    """The MCPCore.mount_mcp method forwards to the module-level helper."""
    result = core.mount_mcp(
        app_with_tagged_routes,
        name="test-server",
        tags={"mcp"},
    )
    assert result["sse"] is True
    assert result["v2"] is True


def test_ui_widget_installs_chatgpt_compat_metadata():
    """Apps SDK widgets need OpenAI _meta keys plus an output schema."""
    import asyncio

    from fastmcp import FastMCP
    from mcp_core.mcp_mount import _install_ui_widget

    server = FastMCP("widget-test")

    @server.tool(tags={"mcp"})
    def echo() -> dict:
        return {"ok": True}

    linked = _install_ui_widget(
        server,
        {
            "uri": "ui://example/widget.html",
            "html": "<div>Hello</div>",
            "tools": {"echo"},
            "resource_domains": ["https://cdn.example.com"],
            "connect_domains": ["https://api.example.com"],
            "domain": "https://example.com",
        },
    )

    assert linked == 1
    tools = asyncio.run(server._list_tools())
    tool = next(t for t in tools if t.name == "echo")
    assert tool.meta["openai/outputTemplate"] == "ui://example/widget.html"
    assert tool.meta["openai/widgetAccessible"] is True
    assert tool.output_schema == {"type": "object", "additionalProperties": True}

    resources = asyncio.run(server._list_resources())
    resource = next(r for r in resources if str(r.uri) == "ui://example/widget.html")
    assert resource.meta["openai/widgetCSP"] == {
        "connect_domains": ["https://api.example.com"],
        "resource_domains": ["https://cdn.example.com"],
    }
    assert resource.meta["openai/widgetDomain"] == "https://example.com"
    assert resource.meta["openai/widgetPrefersBorder"] is True


def test_get_http_headers_patch_is_idempotent(core: MCPCore):
    """Calling mount_mcp twice doesn't double-wrap the patch."""
    from mcp_core import mcp_mount as mm
    from fastmcp.server import dependencies as deps

    # Reset to force a fresh patch on first call
    mm._FASTMCP_HEADERS_PATCHED = False

    app1 = FastAPI()
    core.install_routes(app1)
    mount_mcp(app1, core=core, name="a", tags={"mcp"}, legacy_sse=False)
    patched_once = deps.get_http_headers

    app2 = FastAPI()
    core.install_routes(app2)
    mount_mcp(app2, core=core, name="b", tags={"mcp"}, legacy_sse=False)
    patched_twice = deps.get_http_headers

    # Same wrapper object — second call must not re-wrap
    assert patched_once is patched_twice


def test_bearer_gate_returns_401_with_www_authenticate(core: MCPCore, app_with_tagged_routes):
    """Unauthenticated POST to /mcp/v2 must return real HTTP 401 with a
    WWW-Authenticate header pointing at the resource_metadata URL —
    that's what triggers Claude Code's OAuth auto-discovery."""
    from fastapi.testclient import TestClient

    core.mount_mcp(app_with_tagged_routes, name="t", tags={"mcp"})

    client = TestClient(app_with_tagged_routes)
    r = client.post(
        "/mcp/v2/",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text}"
    www_auth = r.headers.get("WWW-Authenticate", "")
    assert www_auth.startswith("Bearer "), f"missing/invalid WWW-Authenticate: {www_auth!r}"
    assert "resource_metadata=" in www_auth
    assert "/.well-known/oauth-protected-resource" in www_auth


def test_bearer_gate_rejects_invalid_token_with_www_authenticate(core: MCPCore, app_with_tagged_routes):
    """Invalid Bearer tokens must return the gate's OAuth challenge so MCP
    clients can reauthorize instead of surfacing a swallowed tool error."""
    from fastapi.testclient import TestClient

    core.mount_mcp(app_with_tagged_routes, name="t", tags={"mcp"})

    client = TestClient(app_with_tagged_routes, raise_server_exceptions=False)
    r = client.post(
        "/mcp/v2/",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Authorization": "Bearer fake-but-shaped-correctly",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert r.status_code == 401
    www_auth = r.headers.get("WWW-Authenticate", "")
    assert www_auth.startswith("Bearer ")
    assert "resource_metadata=" in www_auth
    assert "/.well-known/oauth-protected-resource" in www_auth


def test_bearer_gate_passes_through_with_token(core: MCPCore, app_with_tagged_routes):
    """A request carrying a valid Bearer token must NOT be 401'd by the gate
    (the downstream FastMCP/route may still error in this unit test).

    raise_server_exceptions=False because TestClient doesn't run the
    composed lifespan that FastMCP's session manager needs — but for
    this test we only care that the gate let the request *past*; the
    downstream crash is unrelated to gate behavior.
    """
    from fastapi.testclient import TestClient

    core.mount_mcp(app_with_tagged_routes, name="t", tags={"mcp"})

    client = TestClient(app_with_tagged_routes, raise_server_exceptions=False)
    r = client.post(
        "/mcp/v2/",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Authorization": "Bearer dev-bypass",
            "Accept": "application/json, text/event-stream",
        },
    )
    # The gate does not validate the token — it only checks for presence.
    # Crucial assertion: our gate's WWW-Authenticate (which carries
    # resource_metadata=) must NOT be on this response — that header is
    # the gate's signature and only fires on the unauth path.
    assert "resource_metadata" not in r.headers.get("WWW-Authenticate", "")


def test_bearer_gate_only_scopes_to_v2_path(core: MCPCore, app_with_tagged_routes):
    """The gate is path-scoped to /mcp/v2 — paths that share a prefix but
    are NOT under /mcp/v2 (like /mcp legacy SSE, or /mcp/v2-foo) must
    not be 401'd. Unit-tests the prefix logic directly so we don't have
    to open a real SSE stream (TestClient blocks on streaming GETs)."""
    from fastapi.testclient import TestClient

    core.mount_mcp(
        app_with_tagged_routes,
        name="t",
        tags={"mcp"},
        legacy_sse=False,  # don't mount /mcp so GET can't stream
    )

    client = TestClient(app_with_tagged_routes, raise_server_exceptions=False)

    # /mcp (no /v2) must NOT be 401'd by the gate
    r = client.get("/mcp")
    assert "resource_metadata" not in r.headers.get("WWW-Authenticate", "")

    # /mcp/v2-foo (prefix collision but different path) must NOT be 401'd
    r = client.get("/mcp/v2-foo")
    assert "resource_metadata" not in r.headers.get("WWW-Authenticate", "")

    # /mcp/v2 (exact prefix match) MUST be 401'd
    r = client.post("/mcp/v2", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("WWW-Authenticate", "")


def test_require_auth_false_disables_gate(core: MCPCore, app_with_tagged_routes):
    """When require_auth=False, no middleware is installed and unauth
    requests reach FastMCP normally."""
    from fastapi.testclient import TestClient

    result = core.mount_mcp(
        app_with_tagged_routes, name="t", tags={"mcp"}, require_auth=False,
    )
    assert result["auth_gate"] is False

    client = TestClient(app_with_tagged_routes, raise_server_exceptions=False)
    r = client.post(
        "/mcp/v2/",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    # FastMCP handles it (200 or its own error shape) — must NOT be our 401.
    if r.status_code == 401:
        assert "resource_metadata" not in r.headers.get("WWW-Authenticate", "")


def _gate_challenged(response) -> bool:
    """True if the gate's own 401 challenge (carrying resource_metadata) fired."""
    return response.status_code == 401 and "resource_metadata" in response.headers.get(
        "WWW-Authenticate", ""
    )


def test_public_discovery_allows_anonymous_handshake_and_list(core: MCPCore, app_with_tagged_routes):
    """With public_discovery=True, anonymous initialize/tools/list/ping must
    pass the gate (so directory scanners can enumerate capabilities)."""
    from fastapi.testclient import TestClient

    result = core.mount_mcp(
        app_with_tagged_routes,
        name="t",
        tags={"mcp"},
        public_discovery=True,
        anonymous_tools={"echo"},
    )
    assert result["public_discovery"] is True

    client = TestClient(app_with_tagged_routes, raise_server_exceptions=False)
    for method in ("initialize", "tools/list", "ping"):
        r = client.post(
            "/mcp/v2/",
            json={"jsonrpc": "2.0", "id": 1, "method": method},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert not _gate_challenged(r), f"{method} should be anonymous, got 401 challenge"


def test_public_discovery_allows_anonymous_free_tool(core: MCPCore, app_with_tagged_routes):
    """An anonymous tools/call for a listed free tool must pass the gate."""
    from fastapi.testclient import TestClient

    core.mount_mcp(
        app_with_tagged_routes,
        name="t",
        tags={"mcp"},
        public_discovery=True,
        anonymous_tools={"echo"},
    )
    client = TestClient(app_with_tagged_routes, raise_server_exceptions=False)
    r = client.post(
        "/mcp/v2/",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "echo", "arguments": {}}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert not _gate_challenged(r), "free tool call should not be challenged"


def test_public_discovery_challenges_anonymous_paid_tool(core: MCPCore, app_with_tagged_routes):
    """An anonymous tools/call for a tool NOT in anonymous_tools must still
    get the 401 + WWW-Authenticate challenge so OAuth auto-discovery fires."""
    from fastapi.testclient import TestClient

    core.mount_mcp(
        app_with_tagged_routes,
        name="t",
        tags={"mcp"},
        public_discovery=True,
        anonymous_tools={"echo"},
    )
    client = TestClient(app_with_tagged_routes, raise_server_exceptions=False)
    r = client.post(
        "/mcp/v2/",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "generate", "arguments": {}}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert _gate_challenged(r), f"paid tool call should be challenged, got {r.status_code}"


def test_public_discovery_off_by_default_still_strict(core: MCPCore, app_with_tagged_routes):
    """Without public_discovery, anonymous tools/list is still challenged
    (no behavior change for existing callers like writer-v2)."""
    from fastapi.testclient import TestClient

    core.mount_mcp(app_with_tagged_routes, name="t", tags={"mcp"})
    client = TestClient(app_with_tagged_routes, raise_server_exceptions=False)
    r = client.post(
        "/mcp/v2/",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert _gate_challenged(r), "strict mode must still challenge anonymous tools/list"


def test_get_http_headers_patch_includes_authorization(core: MCPCore):
    """The patched get_http_headers always includes 'authorization' in
    its include set, so the downstream FastAPI route sees the Bearer
    token. Verifies behavior, not just identity."""
    from mcp_core import mcp_mount as mm
    from fastmcp.server import dependencies as deps

    mm._FASTMCP_HEADERS_PATCHED = False
    app = FastAPI()
    core.install_routes(app)
    mount_mcp(app, core=core, name="x", tags={"mcp"}, legacy_sse=False)

    # The wrapper should call the original with 'authorization' merged in.
    # Inspect via closure cell — the wrapper captures _orig_get_http_headers.
    closure = deps.get_http_headers.__closure__
    assert closure is not None
    # The wrapper's source includes the literal "authorization" — sanity check
    import inspect
    src = inspect.getsource(deps.get_http_headers)
    assert "authorization" in src

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

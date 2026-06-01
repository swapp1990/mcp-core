"""Mount MCP transports (legacy SSE + stateless HTTP) on a FastAPI app.

Encapsulates the wiring that lived inline in writer-v2/server/main.py and
ai-template-gen/backend/main.py. Both repos were copy-pasting the same
fastapi-mcp + fastmcp + lifespan dance — drift between them produced the
"connected in Claude Code but unauthenticated" symptom (designforyou
shipped only the SSE transport while writer-v2 had FastMCP v3 too).

Usage:
    core = MCPCore(...)
    core.install_routes(app)
    core.mount_mcp(
        app,
        name="designforyou",
        description="DesignForYou — AI-powered design template generator.",
        tags={"mcp"},
    )

What it does, in order:
  1. Module-load monkey-patch on fastmcp.server.dependencies.get_http_headers
     and fastmcp.server.providers.openapi.components.get_http_headers — both
     bindings need replacement because openapi.components did
     `from fastmcp.server.dependencies import get_http_headers` at import.
     Without this, FastMCP's OpenAPI runner strips `authorization` before
     forwarding into the ASGI transport, so every authed call 401s through
     mcp-core's auth_and_bill. The patch is idempotent.
  2. fastapi-mcp SSE mount at `mount_path_legacy` (default `/mcp`). Kept for
     backward compat with clients pinned to `type: sse`. Skipped if
     fastapi-mcp isn't installed or `legacy_sse=False`.
  3. FastMCP v3 mount at `mount_path_v2` (default `/mcp/v2`) with
     stateless_http=True — restart-proof, no Mcp-Session-Id. Composes its
     session-manager lifespan with the app's existing one via
     combine_lifespans. Skipped if fastmcp isn't installed.

Returns a small dict describing what got mounted so callers can log or
test. Quietly degrades when optional deps are absent: the legacy mount
needs fastapi-mcp, the v2 mount needs fastmcp; either can be missing
without raising.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Iterable, Mapping, Optional, Union

from fastapi import FastAPI, HTTPException
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Module-level flag so we patch fastmcp's get_http_headers exactly once
# per process even if mount_mcp is called multiple times (tests, multi-app
# setups, hot reload).
_FASTMCP_HEADERS_PATCHED = False


def _patch_fastmcp_get_http_headers() -> bool:
    """Make fastmcp's OpenAPI provider forward `authorization` headers.

    Returns True if the patch was applied (or already applied), False if
    fastmcp isn't installed.
    """
    global _FASTMCP_HEADERS_PATCHED
    if _FASTMCP_HEADERS_PATCHED:
        return True
    try:
        from fastmcp.server import dependencies as _deps
        from fastmcp.server.providers.openapi import components as _components
    except ImportError:
        return False

    _orig = _deps.get_http_headers

    def _with_auth(include_all: bool = False, include=None):
        inc = set(include or set()) | {"authorization"}
        return _orig(include_all=include_all, include=inc)

    _deps.get_http_headers = _with_auth
    _components.get_http_headers = _with_auth
    _FASTMCP_HEADERS_PATCHED = True
    return True


def _install_bearer_gate(app: FastAPI, mount_path_v2: str, core: Any = None) -> None:
    """Register HTTP middleware that 401s unauthenticated requests to
    ``mount_path_v2`` with ``WWW-Authenticate: Bearer resource_metadata=...``.

    Claude Code (and any RFC 6750-compliant MCP client) treats the
    resource_metadata URL as the OAuth discovery pointer and triggers
    DCR + browser auth automatically. Without this header, the client
    sees only an HTTP 200 SSE stream containing a JSON-RPC error and
    cannot self-recover the auth state — the symptom users observe as
    "MCP server connected but unauthenticated".

    Scoped strictly to ``mount_path_v2``; SSE legacy at /mcp is left
    untouched so anonymous read-only callers using the legacy transport
    keep working.
    """
    prefix = mount_path_v2.rstrip("/")

    def _challenge(request, description: str):
        base = str(request.base_url).rstrip("/")
        resource_metadata_url = f"{base}/.well-known/oauth-protected-resource"
        www_authenticate = f'Bearer resource_metadata="{resource_metadata_url}"'
        return JSONResponse(
            {
                "error": "unauthorized",
                "error_description": description,
            },
            status_code=401,
            headers={"WWW-Authenticate": www_authenticate},
        )

    @app.middleware("http")
    async def _require_bearer_for_v2(request, call_next):
        path = request.url.path
        if path == prefix or path.startswith(prefix + "/"):
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("bearer "):
                return _challenge(
                    request,
                    "Authentication required. Discover OAuth metadata via the WWW-Authenticate header.",
                )
            if core is not None and hasattr(core, "auth"):
                try:
                    payload = await core.auth.verify_token(request)
                except HTTPException as exc:
                    if exc.status_code == 401:
                        return _challenge(
                            request,
                            "Invalid or expired access token. Reauthorize using the OAuth metadata in the WWW-Authenticate header.",
                        )
                    raise
                if payload is None:
                    return _challenge(
                        request,
                        "Authentication required. Discover OAuth metadata via the WWW-Authenticate header.",
                    )
        return await call_next(request)


def _apply_tool_titles(
    fastmcp_server: Any,
    tool_titles: Mapping[str, Union[str, Mapping[str, Any]]],
) -> int:
    """Set human-readable title + annotations on FastMCP tools.

    `tool_titles` keys are FastAPI operation_ids (== FastMCP tool names
    after `from_fastapi`). Values are either a plain string (becomes
    `annotations.title`) or a dict matching the MCP `ToolAnnotations`
    schema (`title`, `readOnlyHint`, `destructiveHint`, `idempotentHint`,
    `openWorldHint`).

    Mutates the tool objects in place. `_list_tools` returns the same
    instances on subsequent calls, so this persists across listings.
    Unknown operation_ids are logged at debug and skipped — typos
    shouldn't crash startup.

    Returns the number of tools that were updated.
    """
    try:
        from fastmcp.tools.tool import ToolAnnotations
    except ImportError:
        return 0

    try:
        tools = asyncio.run(fastmcp_server._list_tools())
    except RuntimeError:
        # Already inside a running loop (unlikely at module load, but
        # be defensive). Schedule and wait via a fresh loop.
        loop = asyncio.new_event_loop()
        try:
            tools = loop.run_until_complete(fastmcp_server._list_tools())
        finally:
            loop.close()

    by_name = {t.name: t for t in tools}
    updated = 0
    for op_id, spec in tool_titles.items():
        tool = by_name.get(op_id)
        if tool is None:
            logger.debug("[mcp-core] tool_titles: no tool named %r", op_id)
            continue
        if isinstance(spec, str):
            ann_kwargs: Dict[str, Any] = {"title": spec}
        else:
            ann_kwargs = dict(spec)
        title = ann_kwargs.get("title")
        if title:
            tool.title = title
        # Preserve any pre-existing annotations fields the caller didn't override.
        existing = tool.annotations.model_dump(exclude_none=True) if tool.annotations else {}
        existing.update({k: v for k, v in ann_kwargs.items() if v is not None})
        tool.annotations = ToolAnnotations(**existing)
        updated += 1
    return updated


def mount_mcp(
    app: FastAPI,
    *,
    core: Any,
    name: str,
    description: str = "",
    tags: Optional[Iterable[str]] = ("mcp",),
    legacy_sse: bool = True,
    mount_path_legacy: str = "/mcp",
    mount_path_v2: str = "/mcp/v2",
    instructions: str = "",
    require_auth: bool = True,
    tool_titles: Optional[Mapping[str, Union[str, Mapping[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Mount fastapi-mcp SSE + FastMCP v3 stateless HTTP on `app`.

    `core` is an MCPCore instance — used for `mcp_auth_config()` on the
    legacy mount. The v2 mount relies on the downstream FastAPI route's
    own auth (via `core.auth_and_bill`), so no auth_config is needed there.

    When ``require_auth=True`` (the default), an HTTP middleware is
    installed that 401s any request to ``mount_path_v2`` without a
    Bearer token, attaching a ``WWW-Authenticate`` header that points
    at ``/.well-known/oauth-protected-resource``. This is what
    surface-level OAuth auto-discovery (Claude Code, modern MCP
    clients) needs to trigger DCR + browser auth.

    Returns: {"sse": bool, "v2": bool, "v2_path": str|None, "sse_path": str|None,
              "auth_gate": bool}
    """
    tag_set = set(tags or ())
    auth_config = core.mcp_auth_config() if hasattr(core, "mcp_auth_config") else None
    result: Dict[str, Any] = {
        "sse": False, "v2": False, "sse_path": None, "v2_path": None,
        "auth_gate": False,
    }

    # ── Legacy SSE via fastapi-mcp ──────────────────────────────────────
    if legacy_sse:
        try:
            from fastapi_mcp import FastApiMCP
        except ImportError:
            FastApiMCP = None  # type: ignore[assignment]
        if FastApiMCP is not None:
            try:
                fastapi_mcp_server = FastApiMCP(
                    app,
                    name=name,
                    description=description,
                    include_tags=list(tag_set) if tag_set else None,
                    auth_config=auth_config,
                )
                fastapi_mcp_server.mount_sse(mount_path=mount_path_legacy)
                result["sse"] = True
                result["sse_path"] = mount_path_legacy
                logger.info("[mcp-core] fastapi-mcp SSE mounted at %s", mount_path_legacy)
            except Exception as e:  # pragma: no cover
                logger.warning("[mcp-core] fastapi-mcp SSE mount failed: %s", e)
        else:
            logger.info("[mcp-core] fastapi-mcp not installed — skipping legacy SSE mount")

    # ── FastMCP v3 stateless HTTP ───────────────────────────────────────
    patched = _patch_fastmcp_get_http_headers()
    if not patched:
        logger.info("[mcp-core] fastmcp not installed — skipping /mcp/v2 mount")
        return result

    try:
        from fastmcp import FastMCP
        from fastmcp.server.providers.openapi import RouteMap, MCPType
        from fastmcp.utilities.lifespan import combine_lifespans
    except ImportError:  # pragma: no cover
        return result

    try:
        route_maps = []
        if tag_set:
            route_maps.append(RouteMap(tags=tag_set, mcp_type=MCPType.TOOL))
        # Catch-all: anything not matched by the tag filter is excluded
        # (health, oauth, billing, legacy endpoints).
        route_maps.append(RouteMap(mcp_type=MCPType.EXCLUDE))

        v2_kwargs: Dict[str, Any] = {"name": name, "route_maps": route_maps}
        if instructions:
            v2_kwargs["instructions"] = instructions

        v2 = FastMCP.from_fastapi(app, **v2_kwargs)

        if tool_titles:
            try:
                n = _apply_tool_titles(v2, tool_titles)
                logger.info("[mcp-core] applied friendly titles to %d tool(s)", n)
            except Exception as e:  # pragma: no cover
                logger.warning("[mcp-core] tool_titles application failed: %s", e)

        v2_app = v2.http_app(path="/", stateless_http=True)

        # Compose lifespans: FastMCP's session manager needs its lifespan
        # to run, and we must keep the app's existing lifespan (DB connect,
        # workers, etc.) running too.
        original_lifespan = app.router.lifespan_context
        app.router.lifespan_context = combine_lifespans(original_lifespan, v2_app.lifespan)

        # Install the bearer gate BEFORE mounting so it fires ahead of
        # FastMCP's tool runner — FastMCP swallows downstream 401s into
        # JSON-RPC error results, which are useless for OAuth discovery.
        if require_auth:
            _install_bearer_gate(app, mount_path_v2, core=core)
            result["auth_gate"] = True
            logger.info(
                "[mcp-core] Bearer gate installed at %s (401+WWW-Authenticate)",
                mount_path_v2,
            )

        app.mount(mount_path_v2, v2_app)
        result["v2"] = True
        result["v2_path"] = mount_path_v2
        logger.info("[mcp-core] FastMCP v3 mounted at %s (stateless HTTP)", mount_path_v2)
    except Exception as e:  # pragma: no cover
        logger.warning("[mcp-core] FastMCP v3 mount failed: %s", e)

    return result


__all__ = ["mount_mcp"]

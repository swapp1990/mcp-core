# Migrating from `fastapi-mcp` to `fastmcp` (v3)

Field notes from the 2026-04-21 migration of `writer-v2` onto PrefectHQ/fastmcp
v3.2+. Captures the specific patterns that worked and the gotchas that bit us.

## Why migrate

`fastapi-mcp` (tadata-org) has been unmaintained since July 2024. Its streamable
HTTP transport has a known bug where authorization headers get stripped before
reaching the tool dispatch, so the only working transport is SSE — and SSE keeps
session state in process memory. **Every container restart kills every MCP
client session.** Claude Code, Cursor, etc. all need a fresh `/mcp` reauth after
each deploy.

PrefectHQ/fastmcp v3 is actively maintained, ships `FastMCP.from_fastapi()` so
no handler rewrites are needed, and supports `stateless_http=True` — each tool
call is self-contained (Bearer token, no `Mcp-Session-Id`), so redeploys are
invisible to clients.

## The migration, end to end

### 1. Install fastmcp alongside fastapi-mcp

```
# requirements.txt
fastapi-mcp>=0.4       # kept during the switchover; remove in Phase 3
fastmcp>=3.0           # new
```

Leave the old library installed for the switchover period. Mount both transports
in parallel, then retire the old one once production clients have moved.

### 2. Patch `get_http_headers` at module load

`FastMCP.from_fastapi()` runs tool calls by going through `httpx.ASGITransport`
back into the same FastAPI app. When it builds the internal request, it calls
`fastmcp.server.dependencies.get_http_headers()` which **strips
`authorization` by default** — so the downstream FastAPI route sees the call as
anonymous and `mcp-core`'s `auth_and_bill` returns 401.

Patch the module-level reference so auth is always forwarded. Do this at module
load time *before* `FastMCP.from_fastapi` runs:

```python
from fastmcp.server import dependencies as _fastmcp_deps
from fastmcp.server.providers.openapi import components as _fastmcp_components

_orig_get_http_headers = _fastmcp_deps.get_http_headers

def _get_http_headers_with_auth(include_all: bool = False, include=None):
    inc = set(include or set()) | {"authorization"}
    return _orig_get_http_headers(include_all=include_all, include=inc)

# Both bindings need replacement — `openapi.components` did
# `from fastmcp.server.dependencies import get_http_headers` at import time.
_fastmcp_deps.get_http_headers = _get_http_headers_with_auth
_fastmcp_components.get_http_headers = _get_http_headers_with_auth
```

The downstream route then sees the Bearer token and `mcp-core.auth_and_bill`
runs normally — validation, user lookup, billing.

### 3. Build the MCP server from your existing FastAPI app

```python
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import RouteMap, MCPType
from fastmcp.utilities.lifespan import combine_lifespans

def create_app() -> FastAPI:
    app = FastAPI(title="My MCP Server", lifespan=my_existing_lifespan)
    # ... add routers, CORS, core.install_routes(app), etc.

    # Build MCP tools from the fully-configured OpenAPI spec. Route-map
    # filter matches `fastapi-mcp`'s `include_tags=["mcp"]` pattern —
    # first-match-wins, so tag-mcp routes become TOOLs and everything
    # else (health, oauth, billing, legacy endpoints) is excluded.
    mcp = FastMCP.from_fastapi(
        app,
        name="my-mcp-server",
        route_maps=[
            RouteMap(tags={"mcp"}, mcp_type=MCPType.TOOL),
            RouteMap(mcp_type=MCPType.EXCLUDE),
        ],
    )
    mcp_app = mcp.http_app(path="/", stateless_http=True)

    # Combine lifespans so FastMCP's session manager initializes with
    # your existing connect_db/setup code.
    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = combine_lifespans(original_lifespan, mcp_app.lifespan)
    app.mount("/mcp/v2", mcp_app)
    return app
```

`stateless_http=True` is the magic — without it FastMCP still keeps
`Mcp-Session-Id` in memory and you reintroduce the restart problem.

### 4. Tag your MCP routes

For `from_fastapi` to distinguish MCP-exposed routes from internal ones, tag
them in the router:

```python
mcp_router = APIRouter(prefix="/api/mcp", tags=["mcp"])
legacy_router = APIRouter(prefix="/api/mcp", tags=["legacy"])
```

The `RouteMap(tags={"mcp"}, mcp_type=MCPType.TOOL)` filter picks up `mcp_router`
and excludes `legacy_router`, same as `fastapi-mcp`'s `include_tags=["mcp"]`.

### 5. Test on the new endpoint

```python
# tests/test_mcp_tools.py
def test_fastmcp_v2_forwards_authorization(client, auth_headers):
    r = client.post(
        "/mcp/v2/",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_credits", "arguments": {}},
        },
        headers={**auth_headers, "Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 200
    # If the patch isn't active, this assertion fails — the downstream
    # FastAPI route sees the call as anonymous and returns the free-credits
    # default instead of the authenticated user's balance.
    assert "credits_remaining" in r.text
```

### 6. Switch clients over, verify restart survival

- Update MCP client configs from `{"type": "sse", "url": ".../mcp"}` to
  `{"type": "http", "url": ".../mcp/v2/"}`. **The trailing slash matters**
  — without it Starlette issues a 307 redirect that some clients don't follow.
- After reconnect, force a backend container restart (`docker restart
  backend-container`) and confirm tool calls still work without a reauth. If
  they do, the migration succeeded.
- Keep the old `/mcp` SSE mount alive for a week or two — agents/scripts with
  cached configs may still hit it.

### 7. Retire fastapi-mcp

Once prod clients have moved: drop `fastapi-mcp` from requirements, delete the
SSE + streamable-HTTP mounts, delete the `KNOWN_ISSUES.md` entry about auth
forwarding.

## Auth integration with mcp-core

Nothing about `mcp-core` changes. Tools still call `core.auth_and_bill(request,
tool_name)` at the top of the handler — FastMCP's ASGI proxy forwards the Bearer
token through, the handler gets an authenticated user + billing check for free.
`mcp-core` continues to own `/health`, `/api/billing/credits`, the
`/.well-known/oauth-*` endpoints, and the `/oauth/*` proxy. FastMCP only needs
the bearer-token verification step, which the downstream FastAPI route already
does via `core.auth_and_bill`.

If you ever want FastMCP to do its *own* token verification (skipping the
round-trip through the route), use its `JWTVerifier` configured with your Logto
`jwks_uri`, `issuer`, and `audience`. Not required for most deployments.

## Gotchas and their fixes

| Symptom | Cause | Fix |
|---|---|---|
| All tool calls 401 on new transport | `get_http_headers` strips `authorization` by default | Apply the module-level patch in §2 |
| `Database objects do not implement truth value testing` on read-only tools | `mcp-core` auth.py had `if payload and db:` — pymongo `Database.__bool__` raises | Fixed in mcp-core as of 2026-04-21. If pinning an older commit, use `tool_costs={"my_tool": 0}` instead of `read_only_tools={"my_tool"}` as a workaround — the paid path has the correct null check |
| Session manager errors on mount | FastMCP's session manager needs its lifespan to run | Use `combine_lifespans(original, mcp_app.lifespan)` and assign back to `app.router.lifespan_context` |
| 307 redirect on `POST /mcp/v2` | Missing trailing slash | Client URL must be `.../mcp/v2/` |
| Internal routes (health, oauth) exposed as tools | No route-map filter | Add `RouteMap(tags={"mcp"}, mcp_type=MCPType.TOOL)` followed by a catch-all `RouteMap(mcp_type=MCPType.EXCLUDE)` |

## Proof that restart survival works

```
docker restart writer-v2-backend-1          # kills the old HTTP worker
# ...wait for healthy...
# MCP client makes a tool call:
POST /mcp/v2/   -> 200 OK, correct result, no reauth
```

Logged on 2026-04-21 during the writer-v2 migration: a backend container
restart happened mid-session, the next tool call went straight to the new
worker, and the Bearer token minted before the restart was still valid. That
was the whole point of the migration.

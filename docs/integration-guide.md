# Integrating mcp-core into a New MCP Server

Step-by-step guide for building a new MCP-first server using mcp-core. Covers project setup, tool registration, auth/billing wiring, testing, and deployment.

## 1. Project Setup

### Directory Layout

```
my-mcp-server/
├── backend/
│   ├── main.py              # FastAPI app + MCPCore setup
│   ├── mcp_tools.py         # Your MCP tool definitions
│   ├── requirements.txt
│   ├── .env                 # Local credentials (gitignored)
│   └── .env.example         # Documented template
├── frontend/                # Optional: React + Vite + @logto/react
├── tests/
│   ├── conftest.py          # Test fixtures
│   ├── test_tools.py        # Tool-specific tests
│   └── test_live.py         # Live infrastructure tests
├── CLAUDE.md
└── .gitignore
```

### Install Dependencies

```bash
cd backend
pip install mcp-core fastapi-mcp uvicorn
```

Or in `requirements.txt`:

```
mcp-core>=0.1.0
fastapi-mcp>=0.3.0
uvicorn>=0.29.0
```

### Environment Variables

Copy from the template and fill in your values:

```bash
# backend/.env.example

# ── Product ──
MCP_CORE_PRODUCT_NAME=my-product

# ── Logto Auth ──
# Create a Logto tenant at https://logto.io
# Create an API Resource for your product (e.g. https://api.my-product.app)
# Create two Logto apps:
#   1. "SPA" type for your frontend (LOGTO_APP_ID)
#   2. "Machine-to-Machine" type for testing and server-to-server (M2M_LOGTO_APP_ID)
MCP_CORE_LOGTO_ENDPOINT=https://your-tenant.logto.app
MCP_CORE_LOGTO_API_RESOURCE=https://api.my-product.app
MCP_CORE_FREE_CREDITS=30

# For local dev -- accepts "Bearer dev-bypass" as a valid token:
MCP_CORE_DEV_AUTH_BYPASS=1

# ── MCP OAuth (for Claude Code integration) ──
# Create a "Traditional Web" Logto app for the MCP OAuth proxy.
# Claude Code uses this to get tokens via the MCP protocol.
MCP_CORE_MCP_LOGTO_APP_ID=
MCP_CORE_MCP_LOGTO_APP_SECRET=

# ── MongoDB ──
MCP_CORE_MONGODB_URI=mongodb+srv://user:pass@cluster.mongodb.net/
MCP_CORE_DB_NAME=my-product

# ── Stripe Billing ──
# Create a metered Price in Stripe Dashboard → Products → Add product → Recurring → Usage-based
MCP_CORE_STRIPE_SECRET_KEY=sk_test_...
MCP_CORE_STRIPE_PRICE_ID=price_...
MCP_CORE_STRIPE_WEBHOOK_SECRET=whsec_...
MCP_CORE_STRIPE_METER_EVENT=my_product_tool_calls
MCP_CORE_BILLING_SUCCESS_URL=https://my-product.app/billing/success

# ── Server ──
PORT=8001
```

## 2. Writing main.py

This is the full boilerplate. Copy it, change the product name, and you're running.

```python
"""main.py — FastAPI app with mcp-core wired up."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mcp_core import MCPCore
from mcp_tools import router as mcp_router

# ── Initialize MCPCore ────────────────────────────────────
# All config comes from MCP_CORE_* env vars (see .env.example).
# You can also pass values directly as constructor args.
core = MCPCore(
    product_name="my-product",
    # Everything else is read from env vars automatically.
    # Override specific values here if needed:
    # free_credits=50,
    # tool_costs are registered in mcp_tools.py
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: connect DB, register health checks."""
    await core.connect_db()

    # Health checks — add one per external dependency
    if core.db is not None:
        core.health.add_check("db", lambda: core.db.command("ping"))

    yield
    # Shutdown: nothing to clean up (motor handles connection pooling)


app = FastAPI(title="My MCP Server", lifespan=lifespan)

# CORS — adjust origins for your frontend domain
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:*",
        "https://my-product.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount your tool routes
app.include_router(mcp_router)

# Mount standard routes: /health, /api/billing/credits, webhook, OAuth metadata
core.install_routes(app)

# Mount MCP protocol (for Claude Code)
try:
    from fastapi_mcp import FastApiMCP

    mcp = FastApiMCP(
        app,
        name=core.product_name,
        description="My MCP-first server — describe what your tools do here.",
        include_tags=["mcp"],
        auth_config=core.mcp_auth_config(),
    )
    mcp.mount(mount_path="/mcp")
except ImportError:
    pass  # fastapi-mcp not installed — REST-only mode
```

Run it:

```bash
cd backend
uvicorn main:app --reload --port 8001
```

## 3. Registering Tools

Tools live in `mcp_tools.py`. Each tool is a FastAPI POST endpoint with a Pydantic input model.

### Tool Structure

```python
"""mcp_tools.py — MCP tool definitions for my-product."""

import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from main import core

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


# ──────────────────────────────────────────────────────────
# Step 1: Register tool costs
# ──────────────────────────────────────────────────────────
# Cost 0 = free (no auth required if also in read_only_tools)
# Cost > 0 = paid (auth required, credits deducted)

core.billing.tool_costs.update({
    "list_widgets":      0,   # Free discovery tool
    "get_widget":        0,   # Free read tool
    "generate_widget":   5,   # Paid generation tool
    "transform_widget":  3,   # Paid processing tool
})

# Read-only tools: no auth required at all (anonymous access ok)
core.auth.read_only_tools.update({"list_widgets", "get_widget"})
core.billing.read_only_tools.update({"list_widgets", "get_widget"})


# ──────────────────────────────────────────────────────────
# Step 2: Define input schemas
# ──────────────────────────────────────────────────────────
# Field descriptions become tool documentation in Claude Code.
# Be specific — Claude reads these to decide which tool to call.

class ListWidgetsInput(BaseModel):
    category: str = Field(None, description="Filter by category. Omit for all.")
    limit: int = Field(20, ge=1, le=100, description="Max results to return.")


class GetWidgetInput(BaseModel):
    widget_id: str = Field(..., description="The widget ID to retrieve.")


class GenerateWidgetInput(BaseModel):
    prompt: str = Field(..., min_length=3, description="What to generate.")
    style: str = Field("default", description="Visual style: default, minimal, bold.")


class TransformWidgetInput(BaseModel):
    widget_id: str = Field(..., description="Widget to transform.")
    operation: str = Field(..., description="Operation: resize, recolor, simplify.")


# ──────────────────────────────────────────────────────────
# Step 3: Implement tool handlers
# ──────────────────────────────────────────────────────────
# Pattern for every tool:
#   1. core.auth_and_bill()  — validates auth + deducts credits
#   2. Your product logic     — the actual work
#   3. core.log_tool_call()  — audit trail (optional but recommended)

@router.post(
    "/list_widgets",
    operation_id="list_widgets",
    summary="List available widgets. Free, no auth required.",
)
async def list_widgets(inp: ListWidgetsInput, request: Request):
    await core.auth_and_bill(request, "list_widgets")
    # Your product logic
    widgets = await get_widgets_from_db(inp.category, inp.limit)
    return {"widgets": widgets}


@router.post(
    "/get_widget",
    operation_id="get_widget",
    summary="Get a single widget by ID. Free, no auth required.",
)
async def get_widget(inp: GetWidgetInput, request: Request):
    await core.auth_and_bill(request, "get_widget")
    widget = await get_widget_by_id(inp.widget_id)
    if not widget:
        from fastapi import HTTPException
        raise HTTPException(404, f"Widget {inp.widget_id} not found")
    return widget


@router.post(
    "/generate_widget",
    operation_id="generate_widget",
    summary="Generate a new widget from a prompt. Costs 5 credits.",
)
async def generate_widget(inp: GenerateWidgetInput, request: Request):
    t0 = time.time()
    user = await core.auth_and_bill(request, "generate_widget")

    # Your product logic (LLM call, image generation, etc.)
    result = await run_generation(inp.prompt, inp.style)

    duration = int((time.time() - t0) * 1000)
    await core.log_tool_call(
        request, "generate_widget", user=user,
        duration_ms=duration,
        meta={"style": inp.style, "prompt_len": len(inp.prompt)},
    )
    return result


@router.post(
    "/transform_widget",
    operation_id="transform_widget",
    summary="Transform an existing widget. Costs 3 credits.",
)
async def transform_widget(inp: TransformWidgetInput, request: Request):
    t0 = time.time()
    user = await core.auth_and_bill(request, "transform_widget")

    result = await run_transform(inp.widget_id, inp.operation)

    duration = int((time.time() - t0) * 1000)
    await core.log_tool_call(
        request, "transform_widget", user=user,
        duration_ms=duration,
        meta={"operation": inp.operation},
    )
    return result


# ──────────────────────────────────────────────────────────
# Your product-specific functions (stubs — replace with real logic)
# ──────────────────────────────────────────────────────────

async def get_widgets_from_db(category, limit):
    col = core.db["widgets"] if core.db is not None else None
    if col is None:
        return []
    query = {"category": category} if category else {}
    cursor = col.find(query).limit(limit)
    return await cursor.to_list(limit)


async def get_widget_by_id(widget_id):
    if core.db is None:
        return None
    return await core.db["widgets"].find_one({"widget_id": widget_id})


async def run_generation(prompt, style):
    # Call your LLM, image gen API, etc.
    return {"widget_id": "w_123", "name": f"Generated: {prompt[:50]}"}


async def run_transform(widget_id, operation):
    return {"widget_id": widget_id, "operation": operation, "status": "done"}
```

### Tool Registration Checklist

For every new tool:

1. Add to `core.billing.tool_costs` with the credit cost
2. If free + no auth needed: also add to `core.auth.read_only_tools` AND `core.billing.read_only_tools`
3. Create a Pydantic input model with descriptive `Field(...)` — Claude reads these
4. Add the route with `operation_id` (becomes the MCP tool name) and `summary`
5. First line of handler: `user = await core.auth_and_bill(request, "tool_name")`
6. Last line: `await core.log_tool_call(...)` with duration and meta

### Naming Conventions

- `operation_id` must match the key in `tool_costs` — this is how auth_and_bill knows the cost
- Use snake_case for operation IDs: `generate_widget`, not `generateWidget`
- Prefix read-only tools with verbs like `list_`, `get_`, `search_`, `browse_`
- Prefix paid tools with action verbs: `generate_`, `create_`, `transform_`, `analyze_`

## 4. Testing Your Server

### Test Setup

```bash
pip install mcp-core[dev] pytest pytest-asyncio httpx
```

Create `tests/conftest.py`:

```python
"""Test fixtures for my-product."""

import os
import time

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from mcp_core import MCPCore


@pytest.fixture
def core():
    """MCPCore with dev bypass and mock-friendly config."""
    c = MCPCore(
        product_name="my-product-test",
        logto_endpoint="",        # Empty = auth disabled (dev mode)
        logto_api_resource="",
        free_credits=10,
        tool_costs={
            "list_widgets": 0,
            "get_widget": 0,
            "generate_widget": 5,
            "transform_widget": 3,
        },
        read_only_tools={"list_widgets", "get_widget"},
        dev_auth_bypass=True,
    )
    return c


@pytest.fixture
async def core_with_db(core):
    """MCPCore with mongomock for unit tests."""
    from mongomock_motor import AsyncMongoMockClient
    client = AsyncMongoMockClient()
    core.db = client["test_db"]
    return core


@pytest.fixture
def app(core_with_db):
    """FastAPI app with your tool routes."""
    app = FastAPI()
    # Import your actual routes
    # For tests, you can also define minimal routes inline:
    from mcp_tools import router
    app.include_router(router)
    core_with_db.install_routes(app)
    return app


@pytest.fixture
async def client(app):
    """Async HTTP client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def dev_headers():
    """Dev bypass auth headers."""
    return {"Authorization": "Bearer dev-bypass"}
```

### Writing Tool Tests

`tests/test_tools.py`:

```python
"""Tests for my-product MCP tools."""

import pytest


# ── Auth gate tests ───────────────────────────────────────

@pytest.mark.asyncio
async def test_free_tool_no_auth(client):
    """Free tools work without any token."""
    r = await client.post("/api/mcp/list_widgets", json={})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_paid_tool_requires_auth(client):
    """Paid tools return 401 without a token."""
    r = await client.post("/api/mcp/generate_widget", json={"prompt": "test"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_paid_tool_with_auth(client, dev_headers):
    """Paid tools work with dev-bypass token."""
    r = await client.post(
        "/api/mcp/generate_widget",
        json={"prompt": "a blue widget"},
        headers=dev_headers,
    )
    assert r.status_code == 200
    data = r.json()
    assert "widget_id" in data


# ── Input validation tests ────────────────────────────────

@pytest.mark.asyncio
async def test_generate_widget_requires_prompt(client, dev_headers):
    """Prompt is required and must be >= 3 chars."""
    r = await client.post(
        "/api/mcp/generate_widget",
        json={},  # Missing prompt
        headers=dev_headers,
    )
    assert r.status_code == 422  # Pydantic validation error


@pytest.mark.asyncio
async def test_generate_widget_prompt_too_short(client, dev_headers):
    r = await client.post(
        "/api/mcp/generate_widget",
        json={"prompt": "ab"},  # min_length=3
        headers=dev_headers,
    )
    assert r.status_code == 422


# ── Billing tests ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_credits_deducted_on_paid_tool(client, dev_headers, core_with_db):
    """Paid tool deducts credits from user."""
    r = await client.post(
        "/api/mcp/generate_widget",
        json={"prompt": "test widget"},
        headers=dev_headers,
    )
    assert r.status_code == 200

    # Check credits via API
    r2 = await client.get("/api/billing/credits", headers=dev_headers)
    credits = r2.json()
    assert credits["credits_used"] >= 5  # generate_widget costs 5


@pytest.mark.asyncio
async def test_credits_exhausted_returns_402(client, dev_headers, core_with_db):
    """When credits run out, returns 402 with checkout URL."""
    # Exhaust credits
    await core_with_db.db["users"].update_one(
        {"logto_user_id": "local-dev-user"},
        {"$set": {"credits_used": 10, "free_credits": 10}},
        upsert=True,
    )

    r = await client.post(
        "/api/mcp/generate_widget",
        json={"prompt": "test"},
        headers=dev_headers,
    )
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["error"] == "Payment required"
    assert detail["cost"] == 5


# ── Product logic tests ───────────────────────────────────

@pytest.mark.asyncio
async def test_generate_widget_returns_id(client, dev_headers):
    """Generate returns a widget with an ID."""
    r = await client.post(
        "/api/mcp/generate_widget",
        json={"prompt": "a minimal blue widget", "style": "minimal"},
        headers=dev_headers,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["widget_id"].startswith("w_")


@pytest.mark.asyncio
async def test_get_widget_not_found(client):
    """Nonexistent widget returns 404."""
    r = await client.post(
        "/api/mcp/get_widget",
        json={"widget_id": "nonexistent"},
    )
    assert r.status_code == 404


# ── Health and metadata ───────────────────────────────────

@pytest.mark.asyncio
async def test_health_endpoint(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["product"] == "my-product-test"


@pytest.mark.asyncio
async def test_oauth_metadata(client):
    r = await client.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
```

### Running Tests

```bash
# Unit tests (fast, uses mongomock, no external services)
cd backend
pytest tests/ -v

# Single test
pytest tests/test_tools.py::test_paid_tool_with_auth -v
```

### Live Infrastructure Tests

Once you have real Logto + MongoDB + Stripe configured, add live tests that verify everything works end-to-end.

Create `tests/test_live.py`:

```python
"""Live tests — real Logto, MongoDB, Stripe (test mode).

Run: RUN_LIVE_TESTS=1 pytest tests/test_live.py -v
Requires: .env with real credentials
"""

import os

import pytest
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient

load_dotenv("backend/.env")

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_LIVE_TESTS"),
    reason="RUN_LIVE_TESTS not set",
)


@pytest.fixture
async def live_core():
    from mcp_core import MCPCore
    c = MCPCore(
        product_name="my-product-live-test",
        reject_m2m=False,  # Accept M2M tokens for testing
        # All other config from MCP_CORE_* env vars
    )
    await c.connect_db()
    yield c


@pytest.fixture
async def live_token():
    """Mint M2M token via client_credentials grant."""
    import httpx

    endpoint = os.environ["MCP_CORE_LOGTO_ENDPOINT"]
    # Use your M2M app credentials (not the MCP OAuth proxy app)
    app_id = os.environ["M2M_LOGTO_APP_ID"]
    app_secret = os.environ["M2M_LOGTO_APP_SECRET"]
    resource = os.environ["MCP_CORE_LOGTO_API_RESOURCE"]

    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(
            f"{endpoint}/oidc/token",
            data={
                "grant_type": "client_credentials",
                "client_id": app_id,
                "client_secret": app_secret,
                "resource": resource,
                "scope": "openid",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert resp.status_code == 200, f"Token mint failed: {resp.text[:200]}"
    return resp.json()["access_token"]


@pytest.fixture
async def live_app(live_core):
    from fastapi import FastAPI, Request
    app = FastAPI()
    live_core.install_routes(app)

    @app.post("/api/mcp/generate_widget")
    async def generate_widget(request: Request):
        user = await live_core.auth_and_bill(request, "generate_widget")
        await live_core.log_tool_call(request, "generate_widget", user=user, duration_ms=10)
        return {"widget_id": "w_live", "user_id": user["logto_user_id"]}

    return app


@pytest.fixture
async def live_client(live_app):
    transport = ASGITransport(app=live_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_live_health(live_client):
    r = await live_client.get("/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_live_paid_tool(live_client, live_token, live_core):
    """Full flow: real token -> real auth -> real billing -> real logging."""
    # Reset credits for this M2M user
    import jwt
    payload = jwt.decode(live_token, options={"verify_signature": False})
    await live_core.db["users"].update_one(
        {"logto_user_id": payload["sub"]},
        {"$set": {"credits_used": 0, "free_credits": 10}},
        upsert=True,
    )

    headers = {"Authorization": f"Bearer {live_token}"}
    r = await live_client.post("/api/mcp/generate_widget", json={}, headers=headers)
    assert r.status_code == 200
    assert r.json()["user_id"]

    # Verify credits deducted in real DB
    user = await live_core.db["users"].find_one({"logto_user_id": payload["sub"]})
    assert user["credits_used"] >= 5

    # Verify tool call logged
    log = await live_core.db["tool_logs"].find_one(
        {"product": "my-product-live-test", "tool": "generate_widget"},
        sort=[("ts", -1)],
    )
    assert log is not None
```

Run:

```bash
# Requires .env with real credentials + M2M app configured in Logto
RUN_LIVE_TESTS=1 pytest tests/test_live.py -v
```

## 5. What mcp-core Gives You for Free

Once `core.install_routes(app)` is called, these endpoints exist automatically:

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/health` | GET | No | Health check (standard JSON shape) |
| `/api/billing/credits` | GET | Yes | User's credit balance |
| `/api/stripe/webhook` | POST | No | Stripe webhook handler |
| `/.well-known/oauth-protected-resource` | GET | No | RFC 9728 OAuth metadata |

And if `fastapi-mcp` is installed + `mcp.mount()` is called:

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/mcp` | POST | Via OAuth | MCP Streamable HTTP (Claude Code connects here) |
| `/oauth/authorize` | GET | No | OAuth proxy → Logto |
| `/oauth/token` | POST | No | Token exchange → Logto |
| `/oauth/register` | POST | No | Dynamic client registration (fake) |

## 6. Logto Setup Checklist

In your Logto tenant (https://logto.io):

1. **Create an API Resource** (e.g. `https://api.my-product.app`)
   - This becomes `LOGTO_API_RESOURCE`

2. **Create a "Single Page App"** for your frontend
   - Redirect URI: `http://localhost:5173/callback` (dev), `https://my-product.app/callback` (prod)
   - This gives you `LOGTO_APP_ID` for frontend

3. **Create a "Machine-to-Machine" app** for testing and server-to-server
   - Assign it access to your API Resource
   - This gives you `M2M_LOGTO_APP_ID` / `M2M_LOGTO_APP_SECRET`
   - Used by: live tests (client_credentials grant), backend-to-backend calls

4. **Create a "Traditional Web" app** for MCP OAuth (Claude Code)
   - Redirect URI: `http://localhost:8001/oauth/callback` (matches your server)
   - This gives you `MCP_LOGTO_APP_ID` / `MCP_LOGTO_APP_SECRET`
   - Used by: fastapi-mcp's OAuth proxy

## 7. Stripe Setup Checklist

In Stripe Dashboard:

1. **Create a Product** (e.g. "My Product API Usage")
2. **Add a Price** → Recurring → Usage-based → Metered
   - This gives you `STRIPE_PRICE_ID`
3. **Create a Billing Meter** (e.g. `my_product_tool_calls`)
   - This gives you `STRIPE_METER_EVENT`
4. **Add a Webhook endpoint** → `https://my-product.app/api/stripe/webhook`
   - Events: `checkout.session.completed`, `customer.subscription.created`
   - This gives you `STRIPE_WEBHOOK_SECRET`

Use `sk_test_*` keys for development. Switch to `sk_live_*` for production.

## 8. Frontend Auth Hook

If your server has a frontend, use this standard hook pattern:

```javascript
// hooks/useApi.js
import { useLogto } from '@logto/react';
import { useCallback } from 'react';

const API = import.meta.env.VITE_API_URL || '';
const API_RESOURCE = import.meta.env.VITE_LOGTO_API_RESOURCE;
const DEV_BYPASS = import.meta.env.VITE_DEV_AUTH_BYPASS === '1';

export function useApi() {
  const { getAccessToken } = useLogto();

  const apiFetch = useCallback(async (path, opts = {}) => {
    const headers = { ...opts.headers };
    const token = DEV_BYPASS
      ? 'dev-bypass'
      : await getAccessToken(API_RESOURCE);
    if (token) headers['Authorization'] = `Bearer ${token}`;
    return fetch(`${API}${path}`, { ...opts, headers });
  }, [getAccessToken]);

  const mcpCall = useCallback(async (tool, params = {}) => {
    const res = await apiFetch(`/api/mcp/${tool}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    });
    const data = await res.json();
    if (!res.ok) {
      const err = new Error(data?.detail?.message || data?.detail || `${tool} failed`);
      err.status = res.status;
      err.data = data?.detail;
      throw err;
    }
    return data;
  }, [apiFetch]);

  return { apiFetch, mcpCall };
}
```

Handle errors in your UI:

```javascript
try {
  const result = await mcpCall('generate_widget', { prompt: 'blue widget' });
} catch (err) {
  if (err.status === 401) showLoginModal();
  if (err.status === 402) window.location.href = err.data.setup_url;
  if (err.status === 422) showValidationError(err.message);
}
```

## 9. Quick Reference

### auth_and_bill behavior per tool type

| Tool type | Token present? | What happens |
|-----------|---------------|--------------|
| read_only | No | Returns anonymous user, no billing |
| read_only | Yes | Validates token, returns real user, no billing |
| paid | No | 401 Unauthorized |
| paid | Yes, valid | Validates token, creates/gets user, deducts credits |
| paid | Yes, expired | 401 Token expired |
| paid | Yes, valid, no credits | 402 Payment required (with Stripe checkout URL) |
| paid | Yes, M2M token | 403 (if reject_m2m=True, default) |

### MCPCore env var mapping

| Env var | Constructor arg | Default |
|---------|----------------|---------|
| `MCP_CORE_PRODUCT_NAME` | `product_name` | `"mcp-server"` |
| `MCP_CORE_LOGTO_ENDPOINT` | `logto_endpoint` | `""` |
| `MCP_CORE_LOGTO_API_RESOURCE` | `logto_api_resource` | `""` |
| `MCP_CORE_FREE_CREDITS` | `free_credits` | `30` |
| `MCP_CORE_DEV_AUTH_BYPASS` | `dev_auth_bypass` | `False` |
| `MCP_CORE_MONGODB_URI` | `mongodb_uri` | `""` |
| `MCP_CORE_DB_NAME` | `db_name` | product_name |
| `MCP_CORE_STRIPE_SECRET_KEY` | `stripe_secret_key` | `""` |
| `MCP_CORE_STRIPE_PRICE_ID` | `stripe_price_id` | `""` |
| `MCP_CORE_STRIPE_WEBHOOK_SECRET` | `stripe_webhook_secret` | `""` |
| `MCP_CORE_STRIPE_METER_EVENT` | `stripe_meter_event` | `"mcp_tool_calls"` |
| `MCP_CORE_BILLING_SUCCESS_URL` | `billing_success_url` | `""` |
| `MCP_CORE_MCP_LOGTO_APP_ID` | `mcp_logto_app_id` | `""` |
| `MCP_CORE_MCP_LOGTO_APP_SECRET` | `mcp_logto_app_secret` | `""` |

Constructor args always take precedence over env vars.

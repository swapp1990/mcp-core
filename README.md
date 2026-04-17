# mcp-core

Auth, billing, and logging infrastructure for MCP-first servers. Sits between your product code and [fastapi-mcp](https://github.com/tadata-org/fastapi-mcp).

```
Your MCP Server  (product-specific tool handlers)
     mcp-core    (auth, billing, logging, health)
    fastapi-mcp  (MCP protocol: JSON-RPC, SSE, tool discovery)
      FastAPI
```

## Install

```bash
pip install mcp-core
```

## Quick Start

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from mcp_core import MCPCore

core = MCPCore(
    product_name="my-product",
    logto_endpoint="https://your-tenant.logto.app",
    logto_api_resource="https://api.my-product.app",
    mongodb_uri="mongodb+srv://...",
    stripe_secret_key="sk_test_...",
    stripe_price_id="price_...",
    free_credits=30,
    tool_costs={"browse": 0, "generate": 5},
    read_only_tools={"browse"},
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await core.connect_db()
    yield

app = FastAPI(lifespan=lifespan)
core.install_routes(app)  # /health, /api/billing/credits, webhook, OAuth metadata

@app.post("/api/mcp/generate")
async def generate(request: Request):
    user = await core.auth_and_bill(request, "generate")
    result = do_generation()
    await core.log_tool_call(request, "generate", user=user, duration_ms=1200)
    return result
```

All config can also come from `MCP_CORE_*` environment variables.

## Modules

### Auth (`mcp_core.auth.LogtoAuth`)

Logto JWT validation via JWKS. Creates MongoDB user records on first auth.

- RS256/ES256/ES384/ES512 support
- 30s clock skew tolerance
- Race-condition-safe user upsert
- Dev bypass (`Bearer dev-bypass`) for local development
- M2M token rejection for paid tools

### Billing (`mcp_core.billing.StripeBilling`)

Stripe metered billing with free credit fallback.

- Free credits deducted first
- Stripe metered subscription as fallback
- 402 with Checkout URL when no credits and no subscription
- Webhook handler for `checkout.session.completed` and `customer.subscription.created`

### Tool Logging (`mcp_core.tool_logging.ToolLogger`)

Audit trail for every MCP tool call. Writes to MongoDB `tool_logs` collection.

### Health (`mcp_core.health.HealthCheck`)

Composable health check builder. Supports sync and async checks with timeouts.

## Testing

```bash
# Mock tests (no external services)
pip install -e ".[dev]"
pytest tests/ -v

# Live tests (requires .env.live with real credentials)
RUN_LIVE_TESTS=1 pytest tests/live/ -v
```

## License

MIT

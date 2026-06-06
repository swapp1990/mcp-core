# mcp-core

[![Release](https://img.shields.io/github/v/release/swapp1990/mcp-core)](https://github.com/swapp1990/mcp-core/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Auth, billing, and logging infrastructure for MCP-first servers. Sits between your product code and [fastapi-mcp](https://github.com/tadata-org/fastapi-mcp), with provider-aware auth for Logto and Supabase.

```
Your MCP Server  (product-specific tool handlers)
     mcp-core    (auth, billing, logging, health)
    fastapi-mcp  (MCP protocol: JSON-RPC, SSE, tool discovery)
      FastAPI
```

## Install

```bash
pip install mcp-core-auth
```

The package is published as `mcp-core-auth` on PyPI (the bare `mcp-core` name is held by an unrelated project), but the import path is unchanged:

```python
from mcp_core import MCPCore
```

## Quick Start

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from mcp_core import MCPCore

core = MCPCore(
    product_name="my-product",
    auth_provider="logto",
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

To use Supabase Auth instead, switch the provider and pass your Supabase URL
and anon key:

```python
core = MCPCore(
    product_name="my-product",
    auth_provider="supabase",
    supabase_url="https://your-project.supabase.co",
    supabase_anon_key="sb_publishable_...",
    supabase_api_resource="https://api.my-product.app",
    mongodb_uri="mongodb+srv://...",
    free_credits=30,
    tool_costs={"browse": 0, "generate": 5},
    read_only_tools={"browse"},
)
```

If `auth_provider` is omitted, mcp-core infers Supabase when both
`MCP_CORE_SUPABASE_URL` and `MCP_CORE_SUPABASE_ANON_KEY` are present;
otherwise it preserves the existing Logto default.

## Modules

### Auth (`mcp_core.auth`)

Provider-aware bearer-token auth. Creates MongoDB user records on first auth
using a neutral `auth_user_id` while preserving legacy `logto_user_id` fields
for existing Logto apps.

- Logto JWT validation via JWKS
- Supabase access-token validation through Supabase Auth
- Cloud or self-hosted provider URLs
- 30s clock skew tolerance
- Race-condition-safe user upsert
- Dev bypass (`Bearer dev-bypass`) for local development
- M2M token rejection for paid tools

### Billing (`mcp_core.billing.StripeBilling`)

Stripe billing for credit-based products and all-access subscription products.

- Credit mode deducts free credits first, then falls back to Stripe metered billing.
- Subscription mode (`BILLING_MODE=subscription`) requires an active subscription for paid tools and never consumes credits.
- Standard billing routes include `/api/billing/credits`, `/api/billing/checkout-subscription`, `/api/billing/sync`, `/api/billing/portal`, and `/api/stripe/webhook`.
- Subscription webhooks track `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.paused`, `customer.subscription.resumed`, `customer.subscription.deleted`, `invoice.payment_failed`, `invoice.payment_action_required`, and `invoice.paid`.
- Access defaults to `active`, `trialing`, and `past_due`; `unpaid`, `paused`, `incomplete`, `incomplete_expired`, and `canceled` do not grant subscription access.
- Stripe Customer Portal should handle payment methods, invoices, receipts, and cancellation self-service. Stripe Billing customer-email settings should handle invoice, receipt, failed-payment, and renewal emails; auth providers such as Supabase should remain responsible for auth and security emails.

Useful subscription env vars:

```bash
BILLING_MODE=subscription
STRIPE_SECRET_KEY=sk_live_or_test_...
STRIPE_PRICE_ID=price_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PORTAL_CONFIGURATION_ID=bpc_...
SUBSCRIPTION_PLAN_NAME="Pro"
SUBSCRIPTION_PRICE_LABEL="$20/month"
```

### Tool Logging (`mcp_core.tool_logging.ToolLogger`)

Audit trail for every MCP tool call. Writes to MongoDB `tool_logs` collection.

### Health (`mcp_core.health.HealthCheck`)

Composable health check builder. Supports sync and async checks with timeouts.

### Readiness (`mcp_core.readiness`)

Provider-aware MCP OAuth onboarding diagnostics. The bundled CLI follows the
same discovery path as RFC 9728 clients:

```bash
mcp-core-check-readiness https://your-app.example.com/mcp/v2/
```

It checks protected-resource metadata, authorization-server discovery, Dynamic
Client Registration, unauthenticated MCP bearer challenges, and provider-specific
behavior for Logto and Supabase. For Supabase, it also verifies that the
authorize endpoint redirects back to your app's consent UI with an
`authorization_id`.

## Testing

```bash
# Mock tests (no external services)
pip install -e ".[dev]"
pytest tests/ -v

# Live tests (requires .env.live with real credentials)
RUN_LIVE_TESTS=1 pytest tests/live/ -v
```

## Contributing

Bug reports and PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and [SECURITY.md](SECURITY.md) for vulnerability reporting.

The multi-provider auth design reference lives in [docs/multi-provider-auth-plan.md](docs/multi-provider-auth-plan.md).

## License

MIT

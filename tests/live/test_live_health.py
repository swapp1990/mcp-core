"""Live health check tests -- real MongoDB, real Logto."""

import os

import pytest

from mcp_core.health import HealthCheck


pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_real_mongodb_health_check(live_db):
    """Health check with real MongoDB ping -> ok=true."""
    health = HealthCheck(product_name="live-test")

    async def mongo_ping():
        await live_db.command("ping")

    health.add_check("db", mongo_ping)
    result = await health.run()
    assert result["checks"]["db"]["ok"] is True
    assert result["checks"]["db"]["ms"] >= 0


@pytest.mark.asyncio
async def test_real_logto_health_check(live_env):
    """Health check hitting real JWKS endpoint -> ok=true."""
    import httpx

    endpoint = live_env["LOGTO_ENDPOINT"]
    health = HealthCheck(product_name="live-test")

    async def logto_jwks():
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{endpoint}/oidc/jwks")
            resp.raise_for_status()

    health.add_check("logto", logto_jwks)
    result = await health.run()
    assert result["checks"]["logto"]["ok"] is True

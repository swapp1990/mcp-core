"""Tests for mcp_core.health -- health check builder."""

import asyncio

import pytest

from mcp_core.health import HealthCheck


@pytest.mark.asyncio
async def test_all_checks_pass():
    health = HealthCheck(product_name="test")
    health.add_check("db", lambda: True)
    health.add_check("api", lambda: "ok")

    result = await health.run()
    assert result["status"] == "ok"
    assert result["product"] == "test"
    assert result["checks"]["db"]["ok"] is True
    assert result["checks"]["api"]["ok"] is True
    assert isinstance(result["checks"]["db"]["ms"], int)


@pytest.mark.asyncio
async def test_one_check_fails():
    health = HealthCheck(product_name="test")
    health.add_check("good", lambda: True)
    health.add_check("bad", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    result = await health.run()
    assert result["status"] == "degraded"
    assert result["checks"]["good"]["ok"] is True
    assert result["checks"]["bad"]["ok"] is False
    assert "boom" in result["checks"]["bad"]["error"]


@pytest.mark.asyncio
async def test_check_timeout():
    health = HealthCheck(product_name="test")

    async def slow_check():
        await asyncio.sleep(10)

    health.add_check("slow", slow_check)
    result = await health.run(timeout=0.1)
    assert result["status"] == "degraded"
    assert result["checks"]["slow"]["ok"] is False
    assert result["checks"]["slow"]["error"] == "timeout"


@pytest.mark.asyncio
async def test_timing_reported():
    health = HealthCheck(product_name="test")

    async def quick():
        await asyncio.sleep(0.05)

    health.add_check("quick", quick)
    result = await health.run()
    assert result["checks"]["quick"]["ms"] >= 40  # at least ~50ms


@pytest.mark.asyncio
async def test_no_checks_registered():
    health = HealthCheck(product_name="empty")
    result = await health.run()
    assert result["status"] == "ok"
    assert result["checks"] == {}
    assert result["product"] == "empty"


@pytest.mark.asyncio
async def test_async_check_passes():
    health = HealthCheck(product_name="test")

    async def async_check():
        return {"ping": "pong"}

    health.add_check("async_db", async_check)
    result = await health.run()
    assert result["checks"]["async_db"]["ok"] is True

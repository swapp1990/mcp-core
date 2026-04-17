"""Live logging tests -- real MongoDB."""

import pytest
from starlette.requests import Request

from mcp_core.tool_logging import ToolLogger


pytestmark = pytest.mark.live


def _fake_request():
    scope = {
        "type": "http", "method": "POST", "path": "/api/mcp/test",
        "headers": [],
        "client": ("10.0.0.1", 9999),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_real_log_persisted(live_db):
    """log_tool_call writes to real MongoDB -> document queryable."""
    logger = ToolLogger(db=live_db, product_name="mcp-core-live-test")
    req = _fake_request()
    await logger.log(
        req, tool="test_tool", user_id="mcp-core-test-logger",
        duration_ms=42, status="ok", cost=1,
    )

    doc = await live_db["tool_logs"].find_one(
        {"user_id": "mcp-core-test-logger", "tool": "test_tool"}
    )
    assert doc is not None
    assert doc["duration_ms"] == 42
    assert doc["product"] == "mcp-core-live-test"


@pytest.mark.asyncio
async def test_real_log_schema(live_db):
    """Document in real DB has all required fields with correct types."""
    logger = ToolLogger(db=live_db, product_name="mcp-core-live-test")
    req = _fake_request()
    await logger.log(
        req, tool="schema_check", user_id="mcp-core-test-schema",
        duration_ms=100, status="ok", cost=2,
        meta={"model": "test"},
    )

    doc = await live_db["tool_logs"].find_one({"tool": "schema_check"})
    assert isinstance(doc["ts"], float)
    assert isinstance(doc["duration_ms"], int)
    assert isinstance(doc["meta"], dict)
    assert doc["ip"] == "10.0.0.1"

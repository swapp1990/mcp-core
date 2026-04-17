"""Tests for mcp_core.tool_logging -- audit trail for MCP tool calls."""

import pytest
from starlette.requests import Request

from mcp_core.tool_logging import ToolLogger


def _fake_request(ip: str = "127.0.0.1"):
    scope = {
        "type": "http", "method": "POST", "path": "/api/mcp/test",
        "headers": [],
        "client": (ip, 12345),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_log_writes_to_collection(mock_db):
    logger = ToolLogger(db=mock_db, product_name="test-product")
    req = _fake_request()
    await logger.log(req, tool="my_tool", user_id="u1", duration_ms=100, status="ok")

    docs = await mock_db["tool_logs"].find({}).to_list(10)
    assert len(docs) == 1


@pytest.mark.asyncio
async def test_log_schema_shape(mock_db):
    logger = ToolLogger(db=mock_db, product_name="test-product")
    req = _fake_request("10.0.0.1")
    await logger.log(
        req, tool="narrate_text", user_id="user_abc",
        duration_ms=500, status="ok", cost=2,
        meta={"voice": "carter"},
    )

    doc = await mock_db["tool_logs"].find_one({})
    assert doc["product"] == "test-product"
    assert doc["tool"] == "narrate_text"
    assert doc["user_id"] == "user_abc"
    assert doc["duration_ms"] == 500
    assert doc["status"] == "ok"
    assert doc["cost"] == 2
    assert doc["meta"]["voice"] == "carter"
    assert doc["ip"] == "10.0.0.1"
    assert isinstance(doc["ts"], float)
    assert doc["error"] == ""


@pytest.mark.asyncio
async def test_log_error_truncated(mock_db):
    logger = ToolLogger(db=mock_db, product_name="test-product")
    req = _fake_request()
    long_error = "x" * 1000
    await logger.log(req, tool="t", error=long_error)

    doc = await mock_db["tool_logs"].find_one({})
    assert len(doc["error"]) == 500


@pytest.mark.asyncio
async def test_log_db_failure_does_not_raise(mock_db):
    """If MongoDB insert fails, log() warns but doesn't propagate."""
    from unittest.mock import AsyncMock, MagicMock

    broken_db = MagicMock()
    broken_col = MagicMock()
    broken_col.insert_one = AsyncMock(side_effect=Exception("DB down"))
    broken_db.__getitem__ = MagicMock(return_value=broken_col)

    logger = ToolLogger(db=broken_db, product_name="test")
    req = _fake_request()
    # Should not raise
    await logger.log(req, tool="t", user_id="u")


@pytest.mark.asyncio
async def test_log_meta_stored(mock_db):
    logger = ToolLogger(db=mock_db, product_name="test-product")
    req = _fake_request()
    meta = {"model": "gpt-4o", "tokens": 150, "cached": True}
    await logger.log(req, tool="t", meta=meta)

    doc = await mock_db["tool_logs"].find_one({})
    assert doc["meta"] == meta


@pytest.mark.asyncio
async def test_log_no_db_is_noop():
    """When db is None, log() is a no-op."""
    logger = ToolLogger(db=None, product_name="test")
    req = _fake_request()
    await logger.log(req, tool="t")  # Should not raise

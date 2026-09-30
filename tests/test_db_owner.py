"""Tests for the one-product-per-database ownership check."""

import logging

import pytest

from mcp_core import MCPCore


def _core(product_name: str, db) -> MCPCore:
    core = MCPCore(product_name=product_name, dev_auth_bypass=True)
    core.db = db
    core._db_name = "shared"
    return core


@pytest.mark.asyncio
async def test_first_product_claims_database(mock_db, caplog):
    core = _core("videogen", mock_db)

    with caplog.at_level(logging.ERROR, logger="mcp_core"):
        await core._claim_database()

    owner = await mock_db["_mcp_core_meta"].find_one({"_id": "owner"})
    assert owner["product_name"] == "videogen"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_same_product_reconnecting_is_quiet(mock_db, caplog):
    await _core("videogen", mock_db)._claim_database()

    with caplog.at_level(logging.ERROR, logger="mcp_core"):
        await _core("videogen", mock_db)._claim_database()

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_second_product_on_same_database_logs_error(mock_db, caplog):
    await _core("designforyou", mock_db)._claim_database()

    with caplog.at_level(logging.ERROR, logger="mcp_core"):
        await _core("videogen", mock_db)._claim_database()

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("belongs to product 'designforyou'" in m for m in errors)
    owner = await mock_db["_mcp_core_meta"].find_one({"_id": "owner"})
    assert owner["product_name"] == "designforyou"

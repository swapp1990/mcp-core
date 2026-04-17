"""Live billing tests -- real MongoDB, real Stripe (test mode)."""

import os

import pytest
from fastapi import HTTPException
from starlette.requests import Request


pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_real_free_credit_deduction(live_core, live_db, live_user_token):
    """Create test user in real MongoDB, deduct credits."""
    scope = {
        "type": "http", "method": "GET", "path": "/",
        "headers": [(b"authorization", f"Bearer {live_user_token}".encode())],
    }
    req = Request(scope)
    payload = await live_core.auth.verify_token(req)

    # Reset credits so test is idempotent across runs
    await live_db["users"].update_one(
        {"logto_user_id": payload["sub"]},
        {"$set": {"credits_used": 0, "free_credits": 10}},
        upsert=True,
    )
    user = await live_core.auth.get_or_create_user(live_db, payload)

    result = await live_core.billing.check_and_deduct(live_db, user, "paid_tool")

    assert result["cost"] == 2
    assert result["source"] == "free_credits"

    db_user = await live_db["users"].find_one({"logto_user_id": payload["sub"]})
    assert db_user["credits_used"] == 2


@pytest.mark.asyncio
async def test_real_credits_exhausted_402(live_core, live_db):
    """Exhaust user's credits, call again -> 402."""
    # Create a test user with 0 remaining credits
    test_user = {
        "logto_user_id": "mcp-core-test-exhausted",
        "email": "test-exhausted@test.com",
        "free_credits": 5,
        "credits_used": 5,
        "stripe_customer_id": None,
        "stripe_subscription_id": None,
    }
    await live_db["users"].find_one_and_update(
        {"logto_user_id": test_user["logto_user_id"]},
        {"$set": test_user},
        upsert=True,
    )

    with pytest.raises(HTTPException) as exc_info:
        await live_core.billing.check_and_deduct(live_db, test_user, "paid_tool")
    assert exc_info.value.status_code == 402


@pytest.mark.asyncio
async def test_real_checkout_url_valid(live_core, live_db):
    """The checkout URL in the 402 response is a real Stripe URL."""
    if not os.getenv("STRIPE_SECRET_KEY"):
        pytest.skip("STRIPE_SECRET_KEY not configured")

    test_user = {
        "logto_user_id": "mcp-core-test-checkout",
        "free_credits": 0,
        "credits_used": 0,
        "stripe_customer_id": None,
        "stripe_subscription_id": None,
    }

    with pytest.raises(HTTPException) as exc_info:
        await live_core.billing.check_and_deduct(live_db, test_user, "paid_tool")

    detail = exc_info.value.detail
    assert "setup_url" in detail
    url = detail["setup_url"]
    assert "stripe.com" in url or "checkout" in url


@pytest.mark.asyncio
async def test_real_credits_summary(live_core, live_db, live_user_token):
    """credits_summary against real DB user -> correct remaining balance."""
    scope = {
        "type": "http", "method": "GET", "path": "/",
        "headers": [(b"authorization", f"Bearer {live_user_token}".encode())],
    }
    req = Request(scope)
    payload = await live_core.auth.verify_token(req)
    user = await live_core.auth.get_or_create_user(live_db, payload)

    summary = live_core.billing.credits_summary(user)
    assert summary["free_credits"] == user["free_credits"]
    assert summary["remaining"] == user["free_credits"] - user["credits_used"]
    assert isinstance(summary["has_subscription"], bool)

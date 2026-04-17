"""Tests for mcp_core.billing -- Stripe metered billing with free credits."""

import asyncio

import pytest
from fastapi import HTTPException

from mcp_core.billing import StripeBilling


# ── Helpers ───────────────────────────────────────────────

def _make_user(
    user_id: str = "user_1",
    free_credits: int = 10,
    credits_used: int = 0,
    stripe_customer_id: str = None,
    stripe_subscription_id: str = None,
):
    return {
        "logto_user_id": user_id,
        "free_credits": free_credits,
        "credits_used": credits_used,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
    }


# ── Credit deduction ─────────────────────────────────────

@pytest.mark.asyncio
async def test_free_tool_no_deduction(billing, mock_db):
    user = _make_user()
    result = await billing.check_and_deduct(mock_db, user, "free_tool")
    assert result["cost"] == 0
    assert result["source"] == "free"


@pytest.mark.asyncio
async def test_paid_tool_deducts_free_credits(billing, mock_db, mock_stripe):
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe

    user = _make_user(free_credits=10, credits_used=0)
    await mock_db["users"].insert_one(user.copy())

    result = await billing.check_and_deduct(mock_db, user, "paid_tool")
    assert result["cost"] == 3
    assert result["source"] == "free_credits"
    assert result["remaining_credits"] == 7

    # Verify DB was updated
    db_user = await mock_db["users"].find_one({"logto_user_id": "user_1"})
    assert db_user["credits_used"] == 3


@pytest.mark.asyncio
async def test_free_credits_exact_boundary(billing, mock_db, mock_stripe):
    """User has exactly `cost` credits remaining -> succeeds, balance = 0."""
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe

    user = _make_user(free_credits=10, credits_used=7)  # 3 remaining, cost = 3
    await mock_db["users"].insert_one(user.copy())

    result = await billing.check_and_deduct(mock_db, user, "paid_tool")
    assert result["remaining_credits"] == 0
    assert result["source"] == "free_credits"


@pytest.mark.asyncio
async def test_free_credits_exhausted_no_stripe_402(billing, mock_db, mock_stripe):
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe

    user = _make_user(free_credits=10, credits_used=10)

    with pytest.raises(HTTPException) as exc_info:
        await billing.check_and_deduct(mock_db, user, "paid_tool")
    assert exc_info.value.status_code == 402


@pytest.mark.asyncio
async def test_402_response_includes_setup_url(billing, mock_db, mock_stripe):
    fake_stripe, calls = mock_stripe
    billing._stripe = fake_stripe

    user = _make_user(free_credits=10, credits_used=10)

    with pytest.raises(HTTPException) as exc_info:
        await billing.check_and_deduct(mock_db, user, "paid_tool")

    detail = exc_info.value.detail
    assert detail["error"] == "Payment required"
    assert detail["tool"] == "paid_tool"
    assert detail["cost"] == 3
    assert "setup_url" in detail
    assert detail["setup_url"] == "https://checkout.stripe.com/fake_session_123"


@pytest.mark.asyncio
async def test_stripe_metered_when_subscription_active(billing, mock_db, mock_stripe):
    fake_stripe, calls = mock_stripe
    billing._stripe = fake_stripe

    user = _make_user(
        free_credits=10, credits_used=10,
        stripe_customer_id="cus_abc",
        stripe_subscription_id="sub_abc",
    )

    result = await billing.check_and_deduct(mock_db, user, "paid_tool")
    assert result["source"] == "stripe_metered"
    assert result["cost"] == 3

    # Verify MeterEvent was created
    meter_calls = [c for c in calls if c[0] == "billing.MeterEvent.create"]
    assert len(meter_calls) == 1
    assert meter_calls[0][1]["payload"]["stripe_customer_id"] == "cus_abc"
    assert meter_calls[0][1]["payload"]["value"] == "3"


@pytest.mark.asyncio
async def test_stripe_meter_event_has_correct_payload(billing, mock_db, mock_stripe):
    fake_stripe, calls = mock_stripe
    billing._stripe = fake_stripe

    user = _make_user(
        free_credits=0, credits_used=0,
        stripe_customer_id="cus_xyz",
        stripe_subscription_id="sub_xyz",
    )

    await billing.check_and_deduct(mock_db, user, "expensive_tool")
    meter_calls = [c for c in calls if c[0] == "billing.MeterEvent.create"]
    payload = meter_calls[0][1]
    assert payload["event_name"] == "test_tool_calls"
    assert payload["payload"]["value"] == "8"


# ── Credits summary ───────────────────────────────────────

def test_credits_summary_shape(billing):
    user = _make_user(free_credits=25, credits_used=10)
    summary = billing.credits_summary(user)
    assert summary == {
        "free_credits": 25,
        "credits_used": 10,
        "remaining": 15,
        "has_subscription": False,
    }


def test_credits_summary_with_subscription(billing):
    user = _make_user(stripe_subscription_id="sub_abc")
    summary = billing.credits_summary(user)
    assert summary["has_subscription"] is True


# ── Webhook handling ──────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_checkout_completed(billing, mock_db, mock_stripe):
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe

    await mock_db["users"].insert_one({
        "logto_user_id": "user_wh",
        "stripe_customer_id": None,
        "stripe_subscription_id": None,
    })

    import json
    from starlette.testclient import TestClient
    from fastapi import FastAPI

    event_body = json.dumps({
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "metadata": {"logto_user_id": "user_wh"},
                "customer": "cus_new",
                "subscription": "sub_new",
            }
        },
    }).encode()

    # Call handle_webhook directly
    from starlette.requests import Request
    scope = {
        "type": "http", "method": "POST", "path": "/",
        "headers": [(b"stripe-signature", b"test_sig")],
    }

    async def receive():
        return {"type": "http.request", "body": event_body}

    req = Request(scope, receive)
    result = await billing.handle_webhook(req, mock_db, webhook_secret="test")
    assert result["status"] == "ok"

    db_user = await mock_db["users"].find_one({"logto_user_id": "user_wh"})
    assert db_user["stripe_customer_id"] == "cus_new"
    assert db_user["stripe_subscription_id"] == "sub_new"


@pytest.mark.asyncio
async def test_webhook_subscription_created(billing, mock_db, mock_stripe):
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe

    await mock_db["users"].insert_one({
        "logto_user_id": "user_sub",
        "stripe_customer_id": "cus_existing",
        "stripe_subscription_id": None,
    })

    import json
    event_body = json.dumps({
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "customer": "cus_existing",
                "id": "sub_fresh",
            }
        },
    }).encode()

    from starlette.requests import Request
    scope = {
        "type": "http", "method": "POST", "path": "/",
        "headers": [(b"stripe-signature", b"test_sig")],
    }

    async def receive():
        return {"type": "http.request", "body": event_body}

    req = Request(scope, receive)
    result = await billing.handle_webhook(req, mock_db, webhook_secret="test")
    assert result["status"] == "ok"

    db_user = await mock_db["users"].find_one({"stripe_customer_id": "cus_existing"})
    assert db_user["stripe_subscription_id"] == "sub_fresh"


# ── Multiple deductions ──────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_deductions_serial(billing, mock_db, mock_stripe):
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe

    user = _make_user(free_credits=10, credits_used=0)
    await mock_db["users"].insert_one(user.copy())

    for i in range(3):
        fresh_user = await mock_db["users"].find_one({"logto_user_id": "user_1"})
        await billing.check_and_deduct(mock_db, fresh_user, "paid_tool")

    db_user = await mock_db["users"].find_one({"logto_user_id": "user_1"})
    assert db_user["credits_used"] == 9  # 3 * 3


@pytest.mark.asyncio
async def test_concurrent_deductions(billing, mock_db, mock_stripe):
    """5 parallel calls -> total deducted = 5 * cost."""
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe

    # Give enough credits: 5 * 3 = 15
    user = _make_user(free_credits=20, credits_used=0)
    await mock_db["users"].insert_one(user.copy())

    async def deduct_one():
        fresh = await mock_db["users"].find_one({"logto_user_id": "user_1"})
        return await billing.check_and_deduct(mock_db, fresh, "paid_tool")

    results = await asyncio.gather(*[deduct_one() for _ in range(5)])
    assert all(r["source"] == "free_credits" for r in results)

    db_user = await mock_db["users"].find_one({"logto_user_id": "user_1"})
    assert db_user["credits_used"] == 15  # 5 * 3

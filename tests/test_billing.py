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


def _make_supabase_user(
    user_id: str = "supabase:user_1",
    free_credits: int = 10,
    credits_used: int = 0,
    stripe_customer_id: str = None,
    stripe_subscription_id: str = None,
):
    return {
        "auth_provider": "supabase",
        "auth_subject": user_id.split(":", 1)[-1],
        "auth_user_id": user_id,
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
async def test_paid_tool_deducts_free_credits_by_auth_user_id(billing, mock_db, mock_stripe):
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe

    user = _make_supabase_user(free_credits=10, credits_used=0)
    await mock_db["users"].insert_one(user.copy())

    result = await billing.check_and_deduct(mock_db, user, "paid_tool")
    assert result["cost"] == 3
    assert result["source"] == "free_credits"
    assert result["remaining_credits"] == 7

    db_user = await mock_db["users"].find_one({"auth_user_id": "supabase:user_1"})
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
    assert meter_calls[0][1]["identifier"].startswith("mcp_paid_tool_")
    assert meter_calls[0][1]["payload"]["stripe_customer_id"] == "cus_abc"
    assert meter_calls[0][1]["payload"]["value"] == "3"
    assert result["metered_events"][0]["identifier"] == meter_calls[0][1]["identifier"]


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


@pytest.mark.asyncio
async def test_cancel_meter_events_uses_identifier(billing, mock_stripe):
    fake_stripe, calls = mock_stripe
    billing._stripe = fake_stripe

    cancelled = await billing.cancel_meter_events(
        [{
            "event_name": "test_tool_calls",
            "identifier": "mcp_paid_tool_cancel_me",
            "units": 3,
            "tool": "paid_tool",
        }],
        label="test",
    )

    assert cancelled == 1
    adjustments = [c for c in calls if c[0] == "billing.MeterEventAdjustment.create"]
    assert adjustments == [(
        "billing.MeterEventAdjustment.create",
        {
            "event_name": "test_tool_calls",
            "type": "cancel",
            "cancel": {"identifier": "mcp_paid_tool_cancel_me"},
        },
    )]


@pytest.mark.asyncio
async def test_refund_charge_refunds_credits_and_cancels_meter_event(billing, mock_db, mock_stripe):
    fake_stripe, calls = mock_stripe
    billing._stripe = fake_stripe
    user = _make_user(free_credits=10, credits_used=3)
    await mock_db["users"].insert_one(user.copy())

    await billing.refund_charge(
        mock_db,
        user,
        3,
        "test",
        metered_events=[{"identifier": "mcp_meter_refund", "event_name": "test_tool_calls"}],
    )

    db_user = await mock_db["users"].find_one({"logto_user_id": "user_1"})
    assert db_user["credits_used"] == 0
    adjustments = [c for c in calls if c[0] == "billing.MeterEventAdjustment.create"]
    assert len(adjustments) == 1


# ── Credits summary ───────────────────────────────────────

def test_credits_summary_shape(billing):
    user = _make_user(free_credits=25, credits_used=10)
    summary = billing.credits_summary(user)
    assert summary["free_credits"] == 25
    assert summary["credits_used"] == 10
    assert summary["remaining"] == 15
    assert summary["has_subscription"] is False
    assert summary["pack_options"][0]["id"] == "pack_50"
    assert summary["auto_recharge"]["enabled"] is False


def test_credits_summary_with_subscription(billing):
    user = _make_user(stripe_subscription_id="sub_abc")
    summary = billing.credits_summary(user)
    assert summary["has_subscription"] is True


def test_credits_summary_marks_unpaid_subscription_as_no_access(billing):
    user = _make_user(stripe_subscription_id="sub_abc")
    user["stripe_subscription_status"] = "unpaid"
    summary = billing.credits_summary(user)
    assert summary["has_subscription"] is False
    assert summary["subscription"]["has_subscription"] is True
    assert summary["subscription"]["allows_access"] is False


@pytest.mark.asyncio
async def test_subscription_required_blocks_without_subscription(billing, mock_db, mock_stripe):
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe
    billing.subscription_required = True
    user = _make_user(free_credits=100, credits_used=0)

    with pytest.raises(HTTPException) as exc_info:
        await billing.check_and_deduct(mock_db, user, "paid_tool")

    assert exc_info.value.status_code == 402
    assert exc_info.value.detail["billing_mode"] == "subscription"
    assert exc_info.value.detail["subscription"]["allows_access"] is False


@pytest.mark.asyncio
async def test_subscription_required_allows_active_without_deducting(billing, mock_db, mock_stripe):
    fake_stripe, calls = mock_stripe
    billing._stripe = fake_stripe
    billing.subscription_required = True
    user = _make_user(
        free_credits=100,
        credits_used=0,
        stripe_customer_id="cus_abc",
        stripe_subscription_id="sub_abc",
    )
    user["stripe_subscription_status"] = "active"
    await mock_db["users"].insert_one(user.copy())

    result = await billing.check_and_deduct(mock_db, user, "paid_tool")

    assert result["source"] == "subscription"
    assert result["cost"] == 0
    assert result["included_tool_cost"] == 3
    db_user = await mock_db["users"].find_one({"logto_user_id": "user_1"})
    assert db_user["credits_used"] == 0
    assert [c for c in calls if c[0] == "billing.MeterEvent.create"] == []


@pytest.mark.asyncio
async def test_subscription_required_allows_past_due_grace_status(billing, mock_db, mock_stripe):
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe
    billing.subscription_required = True
    user = _make_user(stripe_customer_id="cus_abc", stripe_subscription_id="sub_abc")
    user["stripe_subscription_status"] = "past_due"

    result = await billing.check_and_deduct(mock_db, user, "paid_tool")

    assert result["source"] == "subscription"
    assert result["subscription"]["status"] == "past_due"


@pytest.mark.asyncio
async def test_subscription_required_revokes_unpaid_status(billing, mock_db, mock_stripe):
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe
    billing.subscription_required = True
    user = _make_user(stripe_customer_id="cus_abc", stripe_subscription_id="sub_abc")
    user["stripe_subscription_status"] = "unpaid"

    with pytest.raises(HTTPException) as exc_info:
        await billing.check_and_deduct(mock_db, user, "paid_tool")

    assert exc_info.value.status_code == 402
    assert exc_info.value.detail["subscription"]["status"] == "unpaid"


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
async def test_credit_pack_checkout_session_uses_pack_metadata(billing, mock_db, mock_stripe):
    fake_stripe, calls = mock_stripe
    billing._stripe = fake_stripe
    user = _make_user()
    await mock_db["users"].insert_one(user.copy())

    session = await billing.create_credit_checkout_session(
        mock_db,
        user,
        "pack_50",
        origin="https://test.app",
    )

    assert session["url"] == "https://checkout.stripe.com/fake_session_123"
    create_calls = [c for c in calls if c[0] == "checkout.Session.create"]
    assert create_calls[-1][1]["mode"] == "payment"
    assert create_calls[-1][1]["metadata"]["kind"] == "credit_pack"
    assert create_calls[-1][1]["metadata"]["credits"] == "50"


@pytest.mark.asyncio
async def test_set_auto_recharge_requires_saved_card(billing, mock_db):
    user = _make_user()
    await mock_db["users"].insert_one(user.copy())

    with pytest.raises(HTTPException) as exc_info:
        await billing.set_auto_recharge(
            mock_db,
            user,
            enabled=True,
            threshold=10,
            pack_id="pack_50",
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_maybe_trigger_auto_recharge_creates_off_session_payment(billing, mock_db, mock_stripe):
    fake_stripe, calls = mock_stripe
    billing._stripe = fake_stripe
    user = _make_user(
        free_credits=10,
        credits_used=9,
        stripe_customer_id="cus_auto",
    )
    user["auto_recharge"] = {
        "enabled": True,
        "threshold": 5,
        "pack_id": "pack_50",
        "payment_method_id": "pm_auto",
    }
    await mock_db["users"].insert_one(user.copy())

    await billing.maybe_trigger_auto_recharge(mock_db, user)

    payment_calls = [c for c in calls if c[0] == "PaymentIntent.create"]
    assert len(payment_calls) == 1
    payload = payment_calls[0][1]
    assert payload["amount"] == 500
    assert payload["customer"] == "cus_auto"
    assert payload["payment_method"] == "pm_auto"
    assert payload["off_session"] is True
    assert payload["metadata"]["kind"] == "auto_recharge"


@pytest.mark.asyncio
async def test_auto_recharge_webhook_grants_credits(billing, mock_db, mock_stripe):
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe
    await mock_db["users"].insert_one(_make_user(free_credits=10, credits_used=9))

    import json
    from starlette.requests import Request

    event_body = json.dumps({
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "metadata": {
                    "kind": "auto_recharge",
                    "logto_user_id": "user_1",
                    "credits": "50",
                },
                "payment_method": "pm_auto",
            }
        },
    }).encode()
    scope = {
        "type": "http", "method": "POST", "path": "/",
        "headers": [(b"stripe-signature", b"test_sig")],
    }

    async def receive():
        return {"type": "http.request", "body": event_body}

    await billing.handle_webhook(Request(scope, receive), mock_db, webhook_secret="test")

    db_user = await mock_db["users"].find_one({"logto_user_id": "user_1"})
    assert db_user["free_credits"] == 60
    assert db_user["credits_purchased"] == 50


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
    assert db_user["stripe_subscription_status"] == "active"


@pytest.mark.asyncio
async def test_webhook_subscription_created_upserts_from_metadata(billing, mock_db, mock_stripe):
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe

    import json
    event_body = json.dumps({
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "customer": "cus_new",
                "id": "sub_new",
                "status": "active",
                "metadata": {
                    "auth_user_id": "supabase:user_meta",
                    "auth_provider": "supabase",
                    "auth_subject": "user_meta",
                    "kind": "metered_subscription",
                },
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

    result = await billing.handle_webhook(Request(scope, receive), mock_db, webhook_secret="test")
    assert result["status"] == "ok"

    db_user = await mock_db["users"].find_one({"auth_user_id": "supabase:user_meta"})
    assert db_user["stripe_customer_id"] == "cus_new"
    assert db_user["stripe_subscription_id"] == "sub_new"
    assert db_user["stripe_subscription_status"] == "active"
    assert db_user["auth_provider"] == "supabase"


@pytest.mark.asyncio
async def test_subscription_checkout_session_sets_subscription_metadata(billing, mock_db, mock_stripe):
    fake_stripe, calls = mock_stripe
    billing._stripe = fake_stripe
    user = _make_supabase_user()
    await mock_db["users"].insert_one(user.copy())

    session = await billing.create_subscription_checkout_session(
        mock_db,
        user,
        origin="https://writer.example",
    )

    assert session["url"] == "https://checkout.stripe.com/fake_session_123"
    create_calls = [c for c in calls if c[0] == "checkout.Session.create"]
    payload = create_calls[-1][1]
    assert payload["mode"] == "subscription"
    assert payload["metadata"]["auth_user_id"] == "supabase:user_1"
    assert payload["metadata"]["kind"] == "metered_subscription"
    assert payload["subscription_data"]["metadata"]["auth_provider"] == "supabase"


@pytest.mark.asyncio
async def test_sync_checkout_session_links_subscription(billing, mock_db, mock_stripe):
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe
    user = _make_user()
    await mock_db["users"].insert_one(user.copy())

    result = await billing.sync_checkout_session(mock_db, user, "cs_fake_sync")

    assert result["status"] == "ok"
    db_user = await mock_db["users"].find_one({"logto_user_id": "user_1"})
    assert db_user["stripe_customer_id"] == "cus_synced"
    assert db_user["stripe_subscription_id"] == "sub_synced"
    assert db_user["stripe_subscription_status"] == "active"


@pytest.mark.asyncio
async def test_webhook_subscription_updated_records_cancel_at_period_end(billing, mock_db, mock_stripe):
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe
    await mock_db["users"].insert_one({
        "logto_user_id": "user_sub",
        "stripe_customer_id": "cus_existing",
        "stripe_subscription_id": "sub_old",
    })

    import json
    event_body = json.dumps({
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "customer": "cus_existing",
                "id": "sub_existing",
                "status": "active",
                "cancel_at_period_end": True,
                "current_period_end": 1893456000,
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

    await billing.handle_webhook(Request(scope, receive), mock_db, webhook_secret="test")

    db_user = await mock_db["users"].find_one({"stripe_customer_id": "cus_existing"})
    assert db_user["stripe_subscription_id"] == "sub_existing"
    assert db_user["stripe_subscription_status"] == "active"
    assert db_user["stripe_subscription_cancel_at_period_end"] is True
    assert db_user["stripe_subscription_current_period_end"] == 1893456000


@pytest.mark.asyncio
async def test_webhook_subscription_deleted_revokes_access(billing, mock_db, mock_stripe):
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe
    await mock_db["users"].insert_one({
        "logto_user_id": "user_sub",
        "stripe_customer_id": "cus_existing",
        "stripe_subscription_id": "sub_existing",
        "stripe_subscription_status": "active",
    })

    import json
    event_body = json.dumps({
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "customer": "cus_existing",
                "id": "sub_existing",
                "status": "canceled",
                "current_period_end": 1893456000,
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

    await billing.handle_webhook(Request(scope, receive), mock_db, webhook_secret="test")

    db_user = await mock_db["users"].find_one({"stripe_customer_id": "cus_existing"})
    assert db_user["stripe_subscription_id"] is None
    assert db_user["stripe_subscription_status"] == "canceled"


# ── Multiple deductions ──────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_subscription_deleted_upserts_from_metadata(billing, mock_db, mock_stripe):
    fake_stripe, _ = mock_stripe
    billing._stripe = fake_stripe

    import json
    event_body = json.dumps({
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "customer": "cus_new",
                "id": "sub_new",
                "status": "canceled",
                "ended_at": 1893456001,
                "metadata": {
                    "auth_user_id": "supabase:user_deleted",
                    "auth_provider": "supabase",
                    "auth_subject": "user_deleted",
                },
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

    await billing.handle_webhook(Request(scope, receive), mock_db, webhook_secret="test")

    db_user = await mock_db["users"].find_one({"auth_user_id": "supabase:user_deleted"})
    assert db_user["stripe_customer_id"] == "cus_new"
    assert db_user["stripe_subscription_id"] is None
    assert db_user["stripe_subscription_status"] == "canceled"
    assert db_user["stripe_subscription_ended_at"] == 1893456001


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

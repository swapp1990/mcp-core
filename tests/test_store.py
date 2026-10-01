"""Store billing: RevenueCat webhook, reconcile, plans across Stripe and the App Store, require_plan."""

import copy
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mcp_core import MCPCore, Plan, PlanCatalog, RevenueCatBilling, RevenueCatClient, require_plan
from mcp_core import store as store_module
from mcp_core.store import ids_from_env

HOOK = "hook_secret"


class FakeRevenueCat:
    """RevenueCat v1 behind httpx.MockTransport: subscribers by app user id, every call recorded."""

    def __init__(self):
        self.subscribers = {}
        self.calls = []
        self.fail_next = 0
        self.delete_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer rc_secret"
        app_user_id = unquote(request.url.raw_path.decode().rsplit("/", 1)[1])
        self.calls.append((request.method, app_user_id))
        if self.fail_next:
            self.fail_next -= 1
            return httpx.Response(503, json={"message": "unavailable"})
        if request.method == "DELETE":
            return httpx.Response(self.delete_status, json={})
        empty = {"entitlements": {}, "subscriptions": {}}
        return httpx.Response(200, json={"subscriber": copy.deepcopy(self.subscribers.get(app_user_id, empty))})

    def client(self) -> RevenueCatClient:
        transport = httpx.MockTransport(self.handler)
        return RevenueCatClient("rc_secret", http_client_factory=lambda: httpx.AsyncClient(transport=transport))

    def gets(self):
        return [user for method, user in self.calls if method == "GET"]


def purchase(entitlement, product, *, expires="2099-01-01T00:00:00Z", purchased="2026-09-01T00:00:00Z",
             grace=None, sandbox=False, refunded=None, unsubscribed=None):
    ent = {"product_identifier": product, "expires_date": expires, "purchase_date": purchased}
    if grace:
        ent["grace_period_expires_date"] = grace
    sub = {"expires_date": expires, "purchase_date": purchased, "is_sandbox": sandbox, "store": "app_store",
           "refunded_at": refunded, "unsubscribe_detected_at": unsubscribed, "billing_issues_detected_at": None}
    return entitlement, product, ent, sub


def subscriber(*purchases):
    out = {"entitlements": {}, "subscriptions": {}}
    for entitlement, product, ent, sub in purchases:
        if entitlement:
            out["entitlements"][entitlement] = ent
        out["subscriptions"][product] = sub
    return out


def wfy_catalog(**kwargs):
    return PlanCatalog(
        [
            Plan("free", 0),
            Plan("plus", 1, display_name="WriteForYou Plus", stripe_price_ids={"price_plus"},
                 store_entitlements={"premium"}, store_product_ids={"lmwfy_monthly", "lmwfy_yearly"}),
            Plan("pro", 2, display_name="WriteForYou Pro", stripe_price_ids={"price_pro"},
                 store_product_ids={"lmwfy_pro_monthly"}),
        ],
        unknown_paid_plan="plus",
        **kwargs,
    )


def afy_catalog(**plan_kwargs):
    return PlanCatalog([Plan("free", 0), Plan("pro", 1, store_entitlements={"LetMeActForYou Pro"}, **plan_kwargs)])


@pytest.fixture
def rc():
    return FakeRevenueCat()


def make_core(auth, mock_db, rc, catalog=None, **rc_kwargs):
    rc_kwargs.setdefault("sandbox_allowlist", "")
    revenuecat = RevenueCatBilling(webhook_authorization=HOOK, client=rc.client(), **rc_kwargs)
    core = MCPCore(
        product_name="writer",
        logto_endpoint="https://test.logto.app",
        logto_api_resource="https://api.test.app",
        free_credits=10,
        plan_catalog=catalog or wfy_catalog(),
        revenuecat=revenuecat,
    )
    core.auth = auth
    core.db = mock_db
    return core


async def add_user(db, sub, **fields):
    doc = {"auth_user_id": f"logto:{sub}", "logto_user_id": sub, "auth_subject": sub,
           "auth_provider": "logto", "free_credits": 10, "credits_used": 0, **fields}
    await db["users"].insert_one(doc)
    return await db["users"].find_one({"auth_user_id": f"logto:{sub}"})


def webhook_client(core):
    app = FastAPI()
    core.install_store_routes(app)
    return TestClient(app)


def event(event_id="evt_1", type_="RENEWAL", app_user_id="u1", environment="PRODUCTION", **extra):
    return {"event": {"id": event_id, "type": type_, "app_user_id": app_user_id, "environment": environment, **extra}}


def post_event(client, body, auth=HOOK):
    headers = {"Authorization": auth} if auth is not None else {}
    return client.post("/api/billing/revenuecat/webhook", json=body, headers=headers)


# ── Mapping ───────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("purchase_args", "plan", "status"),
    [
        (("premium", "lmwfy_monthly"), "plus", "active"),
        (("premium", "lmwfy_pro_monthly"), "pro", "active"),
        (("premium", "lmwfy_monthly", {"expires": "2000-01-01T00:00:00Z"}), "free", "expired"),
        (("premium", "lmwfy_monthly", {"expires": "2000-01-01T00:00:00Z", "grace": "2099-01-01T00:00:00Z"}), "plus", "grace"),
        (("premium", "lmwfy_monthly", {"refunded": "2026-09-02T00:00:00Z"}), "free", "refunded"),
        (("mystery", "some_new_product"), "plus", "active"),
    ],
)
async def test_reconcile_maps_entitlements_onto_plans(auth, mock_db, rc, purchase_args, plan, status):
    *ids, opts = purchase_args if isinstance(purchase_args[-1], dict) else (*purchase_args, {})
    rc.subscribers["u1"] = subscriber(purchase(*ids, **opts))
    core = make_core(auth, mock_db, rc)
    user = await add_user(mock_db, "u1")

    projection = await core.revenuecat.reconcile(user)

    assert (projection["plan"], projection["status"]) == (plan, status)
    stored = (await mock_db["users"].find_one({"auth_user_id": "logto:u1"}))["store_subscription"]
    assert stored["plan"] == plan and stored["provider"] == "revenuecat" and stored["app_user_id"] == "u1"
    assert core.plans.resolve({**user, "store_subscription": projection}).plan == plan


@pytest.mark.asyncio
async def test_unmapped_entitlement_is_ignored_without_an_unknown_paid_plan(auth, mock_db, rc):
    rc.subscribers["u1"] = subscriber(purchase("something_else", "other_product"))
    core = make_core(auth, mock_db, rc, catalog=afy_catalog())
    projection = await core.revenuecat.reconcile(await add_user(mock_db, "u1"))
    assert (projection["plan"], projection["status"]) == ("free", "none")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rc_kwargs", "email", "plan"),
    [
        ({}, "someone@example.com", "free"),
        ({"sandbox_allowlist": "u1"}, "someone@example.com", "pro"),
        ({"sandbox_allowlist": ["Reviewer@Apple.test"]}, "reviewer@apple.test", "pro"),
        ({"accept_sandbox": True}, "someone@example.com", "pro"),
    ],
)
async def test_sandbox_purchases_count_only_for_the_allowlist(auth, mock_db, rc, rc_kwargs, email, plan):
    rc.subscribers["u1"] = subscriber(purchase("LetMeActForYou Pro", "lmafy_monthly", sandbox=True))
    core = make_core(auth, mock_db, rc, catalog=afy_catalog(), **rc_kwargs)
    projection = await core.revenuecat.reconcile(await add_user(mock_db, "u1", email=email))
    assert projection["plan"] == plan
    if plan == "pro":
        assert projection["environment"] == "sandbox"


def test_sandbox_allowlist_defaults_to_the_env_var(monkeypatch):
    monkeypatch.setenv("REVENUECAT_SANDBOX_ALLOWLIST", "u9, tester@example.com")
    billing = RevenueCatBilling()
    assert billing.sandbox_allowed({"logto_user_id": "u9"})
    assert billing.sandbox_allowed({"logto_user_id": "x", "email": "Tester@Example.com"})
    assert not billing.sandbox_allowed({"logto_user_id": "x", "email": "other@example.com"})


@pytest.mark.asyncio
async def test_stripe_plus_and_apple_pro_resolve_to_pro_and_sync_leaves_stripe_fields_alone(auth, mock_db, rc):
    stripe_fields = {
        "stripe_customer_id": "cus_1",
        "stripe_subscription_id": "sub_real",
        "stripe_subscription_status": "active",
        "stripe_subscription_price_id": "price_plus",
        "stripe_subscription_current_period_end": 1893456000,
        "stripe_subscription_cancel_at_period_end": False,
    }
    user = await add_user(mock_db, "u1", **stripe_fields)
    core = make_core(auth, mock_db, rc)
    assert core.plans.resolve(user).to_dict()["plan"] == "plus"

    rc.subscribers["u1"] = subscriber(purchase("premium", "lmwfy_pro_monthly"))
    await core.revenuecat.reconcile(user)

    after = await mock_db["users"].find_one({"auth_user_id": "logto:u1"})
    assert {key: after[key] for key in stripe_fields} == stripe_fields
    state = core.plans.resolve(after)
    assert (state.plan, state.source) == ("pro", "store")

    rc.subscribers["u1"] = subscriber(purchase("premium", "lmwfy_pro_monthly", expires="2000-01-01T00:00:00Z"))
    await core.revenuecat.reconcile(after)
    lapsed = await mock_db["users"].find_one({"auth_user_id": "logto:u1"})
    assert core.plans.resolve(lapsed).to_dict()["plan"] == "plus"
    assert lapsed["stripe_subscription_id"] == "sub_real"


def test_resolve_reads_the_app_store_shim_fields_until_0_7(auth, mock_db, rc):
    core = make_core(auth, mock_db, rc)
    shim = {
        "auth_user_id": "logto:u1",
        "stripe_subscription_id": "app_store:u1",
        "stripe_subscription_status": "active",
        "stripe_subscription_price_id": "app_store:lmwfy_pro_monthly",
        "app_store_provider": "revenuecat",
        "app_store_subscription_status": "active",
        "app_store_subscription_tier": "pro",
        "app_store_entitlement_id": "premium",
        "app_store_product_id": "lmwfy_pro_monthly",
        "app_store_expires_at": "2099-01-01T00:00:00+00:00",
    }
    state = core.plans.resolve(shim)
    assert (state.plan, state.source) == ("pro", "store")
    subscription = core.billing.subscription_state(shim)
    assert subscription["allows_access"] is True and subscription["source"] == "store"

    lapsed = {**shim, "app_store_subscription_status": "canceled", "app_store_subscription_tier": "free"}
    assert core.plans.resolve(lapsed).plan == "free"
    assert core.billing.subscription_state(lapsed)["allows_access"] is False


def test_subscription_state_counts_the_shim_without_a_catalog():
    """A product still writing the shim (WFY before step 8) keeps access on 0.6 without a catalog."""
    from mcp_core.billing import StripeBilling

    billing = StripeBilling(subscription_required=True)
    shim = {
        "stripe_subscription_id": "app_store:u1",
        "stripe_subscription_status": "active",
        "app_store_subscription_status": "active",
        "app_store_subscription_tier": "plus",
    }
    assert billing.subscription_allows_access(shim)
    assert not billing.subscription_allows_access({**shim, "app_store_subscription_status": "canceled"})


def test_direct_grants_and_dev_override(auth, mock_db, rc):
    catalog = wfy_catalog(direct_grants={"pro": "logto:vip"}, dev_force_plan="plus")
    core = make_core(auth, mock_db, rc, catalog=catalog)
    assert core.plans.resolve({"auth_user_id": "logto:vip"}).source == "direct"
    assert core.plans.resolve({"auth_user_id": "logto:other"}).plan == "free"
    catalog.dev_bypass = True
    assert core.plans.resolve({"auth_user_id": "logto:vip"}).to_dict()["source"] == "dev"


def test_ids_from_env_takes_the_first_set_name(monkeypatch):
    monkeypatch.delenv("STRIPE_PLUS_PRICE_ID", raising=False)
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_a, price_b")
    assert ids_from_env("STRIPE_PLUS_PRICE_ID", "STRIPE_PRICE_ID") == {"price_a", "price_b"}


# ── Webhook ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_authorization(auth, mock_db, rc):
    client = webhook_client(make_core(auth, mock_db, rc))
    assert post_event(client, event(), auth=None).status_code == 401
    assert post_event(client, event(), auth="Bearer nope").status_code == 401
    assert post_event(client, event(), auth="hook_secretx").status_code == 401
    assert post_event(client, event(type_="TEST")).json()["status"] == "test"
    assert post_event(client, event(type_="TEST"), auth=f"Bearer {HOOK}").status_code == 200
    assert rc.calls == []


def test_webhook_accepts_a_configured_bearer_value_exactly(auth, mock_db, rc):
    core = make_core(auth, mock_db, rc)
    core.revenuecat.webhook_authorization = "Bearer full-header-value"
    client = webhook_client(core)
    assert post_event(client, event(type_="TEST"), auth="Bearer full-header-value").status_code == 200


def test_webhook_without_configured_authorization_is_503(auth, mock_db, rc):
    core = make_core(auth, mock_db, rc)
    core.revenuecat.webhook_authorization = ""
    assert post_event(webhook_client(core), event(type_="TEST"), auth="Bearer anything").status_code == 503


@pytest.mark.asyncio
async def test_webhook_requires_an_event_id(auth, mock_db, rc):
    client = webhook_client(make_core(auth, mock_db, rc))
    body = event()
    del body["event"]["id"]
    assert post_event(client, body).status_code == 422


@pytest.mark.asyncio
async def test_replayed_event_reconciles_once(auth, mock_db, rc):
    rc.subscribers["u1"] = subscriber(purchase("premium", "lmwfy_monthly"))
    core = make_core(auth, mock_db, rc)
    await add_user(mock_db, "u1")
    client = webhook_client(core)

    first = post_event(client, event(aliases=["$RCAnonymousID:abc", "u1"], original_app_user_id="u1"))
    second = post_event(client, event())

    assert first.status_code == 200 and first.json()["status"] == "processed"
    assert second.json()["status"] == "duplicate"
    assert rc.gets() == ["u1"]
    log = await mock_db["store_billing_events"].find_one({"_id": "revenuecat:production:evt_1"})
    assert log["status"] == "processed" and log["attempts"] == 1


@pytest.mark.asyncio
async def test_transfer_reconciles_both_accounts(auth, mock_db, rc):
    core = make_core(auth, mock_db, rc)
    rc.subscribers["old"] = subscriber(purchase("premium", "lmwfy_pro_monthly"))
    await core.revenuecat.reconcile(await add_user(mock_db, "old"))
    await add_user(mock_db, "new")
    rc.subscribers["new"] = rc.subscribers.pop("old")

    r = post_event(webhook_client(core), {"event": {
        "id": "evt_transfer", "type": "TRANSFER", "environment": "PRODUCTION",
        "transferred_from": ["old"], "transferred_to": ["new"],
    }})

    assert r.status_code == 200 and r.json()["reconciled"] == 2
    old = await mock_db["users"].find_one({"auth_user_id": "logto:old"})
    new = await mock_db["users"].find_one({"auth_user_id": "logto:new"})
    assert core.plans.resolve(old).plan == "free"
    assert core.plans.resolve(new).plan == "pro"


@pytest.mark.asyncio
async def test_revenuecat_outage_fails_the_event_and_a_retry_succeeds(auth, mock_db, rc):
    rc.subscribers["u1"] = subscriber(purchase("premium", "lmwfy_monthly"))
    core = make_core(auth, mock_db, rc)
    await add_user(mock_db, "u1")
    client = webhook_client(core)
    rc.fail_next = 1

    failed = post_event(client, event())
    log = await mock_db["store_billing_events"].find_one({"_id": "revenuecat:production:evt_1"})
    assert failed.status_code == 500 and log["status"] == "failed"

    retried = post_event(client, event())
    log = await mock_db["store_billing_events"].find_one({"_id": "revenuecat:production:evt_1"})
    assert retried.status_code == 200 and retried.json()["status"] == "processed"
    assert log["status"] == "processed" and log["attempts"] == 2
    assert rc.gets() == ["u1", "u1"]


@pytest.mark.asyncio
async def test_event_already_processing_is_refused_for_a_retry(auth, mock_db, rc):
    core = make_core(auth, mock_db, rc)
    await mock_db["store_billing_events"].insert_one({
        "_id": "revenuecat:production:evt_1", "status": "processing", "attempts": 1,
        "updated_at": datetime.now(timezone.utc),
    })
    assert post_event(webhook_client(core), event()).status_code == 409
    assert rc.calls == []


@pytest.mark.asyncio
async def test_stale_processing_event_is_retried(auth, mock_db, rc):
    core = make_core(auth, mock_db, rc)
    await add_user(mock_db, "u1")
    await mock_db["store_billing_events"].insert_one({
        "_id": "revenuecat:production:evt_1", "status": "processing", "attempts": 1,
        "updated_at": datetime.now(timezone.utc) - timedelta(minutes=10),
    })
    assert post_event(webhook_client(core), event()).json()["status"] == "processed"


@pytest.mark.asyncio
async def test_sandbox_event_is_ignored_unless_allowlisted(auth, mock_db, rc):
    rc.subscribers["u1"] = subscriber(purchase("premium", "lmwfy_monthly", sandbox=True))
    await add_user(mock_db, "u1", email="tester@example.com")

    ignored = post_event(webhook_client(make_core(auth, mock_db, rc)), event(environment="SANDBOX"))
    assert ignored.json()["status"] == "ignored" and ignored.json()["reason"] == "sandbox"
    assert rc.calls == []
    log = await mock_db["store_billing_events"].find_one({"_id": "revenuecat:sandbox:evt_1"})
    assert log["status"] == "ignored"

    allowed = make_core(auth, mock_db, rc, sandbox_allowlist="tester@example.com")
    r = post_event(webhook_client(allowed), event(event_id="evt_2", environment="SANDBOX"))
    assert r.json()["status"] == "processed"
    user = await mock_db["users"].find_one({"auth_user_id": "logto:u1"})
    assert allowed.plans.resolve(user).plan == "plus"


@pytest.mark.asyncio
async def test_unknown_account_is_acknowledged_without_a_lookup(auth, mock_db, rc):
    r = post_event(webhook_client(make_core(auth, mock_db, rc)), event(app_user_id="stranger"))
    assert r.status_code == 200
    assert r.json()["handled"] is False and r.json()["reason"] == "no_account"
    assert rc.calls == []
    assert await mock_db["users"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_event_processed_by_the_legacy_log_is_a_duplicate(auth, mock_db, rc):
    await add_user(mock_db, "u1")
    await mock_db["acting_subscription_events"].insert_one({
        "_id": "PRODUCTION:evt_1", "event_id": "evt_1", "environment": "PRODUCTION", "status": "processed",
    })
    core = make_core(auth, mock_db, rc, catalog=afy_catalog(), legacy_event_collection="acting_subscription_events")
    assert post_event(webhook_client(core), event()).json()["status"] == "duplicate"
    assert rc.calls == []


# ── Credits and on_change ─────────────────────────────────


@pytest.mark.asyncio
async def test_one_renewal_grants_credits_once_across_webhooks_and_sync(auth, mock_db, rc, make_token):
    core = make_core(auth, mock_db, rc, catalog=afy_catalog(credits_per_period=5))
    rc.subscribers["user_test_123"] = subscriber(purchase("LetMeActForYou Pro", "lmafy_monthly"))
    await add_user(mock_db, "user_test_123")
    client = webhook_client(core)

    post_event(client, event("evt_a", app_user_id="user_test_123"))
    post_event(client, event("evt_b", type_="PRODUCT_CHANGE", app_user_id="user_test_123"))
    sync = client.post("/api/billing/app-store/sync", json={}, headers={"Authorization": f"Bearer {make_token()}"})
    assert sync.status_code == 200, sync.text

    user = await mock_db["users"].find_one({"auth_user_id": "logto:user_test_123"})
    assert user["free_credits"] == 15
    assert user["billing_grant_ids"] == ["revenuecat:LetMeActForYou Pro:2026-09-01T00:00:00Z"]

    rc.subscribers["user_test_123"] = subscriber(
        purchase("LetMeActForYou Pro", "lmafy_monthly", purchased="2026-10-01T00:00:00Z")
    )
    post_event(client, event("evt_c", app_user_id="user_test_123"))
    user = await mock_db["users"].find_one({"auth_user_id": "logto:user_test_123"})
    assert user["free_credits"] == 20


@pytest.mark.asyncio
async def test_calendar_month_credits_and_the_seeded_migration_key(auth, mock_db, rc, monkeypatch):
    core = make_core(auth, mock_db, rc, catalog=afy_catalog(credits_per_period=3, period="calendar_month"))
    rc.subscribers["u1"] = subscriber(purchase("LetMeActForYou Pro", "lmafy_monthly"))
    september = datetime(2026, 9, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(store_module, "_now", lambda: september)
    await add_user(mock_db, "u1", billing_grant_ids=["revenuecat:LetMeActForYou Pro:2026-09"])

    await core.revenuecat.reconcile(await mock_db["users"].find_one({"logto_user_id": "u1"}))
    assert (await mock_db["users"].find_one({"logto_user_id": "u1"}))["free_credits"] == 10

    monkeypatch.setattr(store_module, "_now", lambda: september + timedelta(days=20))
    for _ in range(2):
        await core.revenuecat.reconcile(await mock_db["users"].find_one({"logto_user_id": "u1"}))
    assert (await mock_db["users"].find_one({"logto_user_id": "u1"}))["free_credits"] == 13


@pytest.mark.asyncio
async def test_on_change_runs_after_every_reconcile_with_the_previous_state(auth, mock_db, rc):
    seen = []

    async def on_change(user, state):
        seen.append((user["auth_user_id"], state["plan"], state["changed"], (state["previous"] or {}).get("plan")))

    core = make_core(auth, mock_db, rc, catalog=afy_catalog(), on_change=on_change)
    rc.subscribers["u1"] = subscriber(purchase("LetMeActForYou Pro", "lmafy_monthly"))
    await core.revenuecat.reconcile(await add_user(mock_db, "u1"))
    await core.revenuecat.reconcile(await mock_db["users"].find_one({"logto_user_id": "u1"}))

    assert seen == [("logto:u1", "pro", True, None), ("logto:u1", "pro", False, "pro")]


# ── Sync route ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_uses_the_token_subject_not_the_body(auth, mock_db, rc, make_token):
    rc.subscribers["user_test_123"] = subscriber(purchase("premium", "lmwfy_pro_monthly"))
    client = webhook_client(make_core(auth, mock_db, rc))

    r = client.post(
        "/api/billing/app-store/sync",
        json={"app_user_id": "someone_else"},
        headers={"Authorization": f"Bearer {make_token()}"},
    )

    assert r.status_code == 200, r.text
    assert rc.gets() == ["user_test_123"]
    body = r.json()
    assert body["plan"]["plan"] == "pro" and body["plan"]["source"] == "store"
    assert body["store_subscription"]["status"] == "active"
    assert [plan["name"] for plan in body["plans"]] == ["free", "plus", "pro"]


def test_sync_reports_a_revenuecat_outage_as_503(auth, mock_db, rc, make_token):
    rc.fail_next = 1
    client = webhook_client(make_core(auth, mock_db, rc))
    r = client.post("/api/billing/app-store/sync", headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "billing_verification_unavailable"


# ── require_plan ──────────────────────────────────────────


WFY_MESSAGE = (
    "image generation requires the WriteForYou Pro plan. "
    "Upgrade your subscription to use Pro Experimental features. https://writer.example/billing"
)


@pytest.mark.asyncio
async def test_require_plan_402_matches_writeforyou_field_for_field(auth, mock_db, rc):
    core = make_core(auth, mock_db, rc, catalog=wfy_catalog(upgrade_url="https://writer.example/billing"))
    plus_user = {"auth_user_id": "logto:u1", "stripe_subscription_id": "sub_1",
                 "stripe_subscription_status": "active", "stripe_subscription_price_id": "price_plus"}

    with pytest.raises(HTTPException) as exc:
        await require_plan(core, plus_user, "pro", message=WFY_MESSAGE)

    assert exc.value.status_code == 402
    assert exc.value.detail == {
        "code": "subscription_required",
        "legacy_code": "pro_required",
        "message": WFY_MESSAGE,
        "required_tier": "pro",
        "current_tier": "plus",
        "upgrade_url": "https://writer.example/billing",
        "error": {
            "code": "subscription_required",
            "message": WFY_MESSAGE,
            "required_tier": "pro",
            "upgrade_url": "https://writer.example/billing",
        },
    }

    with pytest.raises(HTTPException) as exc:
        core.plans.require({"auth_user_id": "logto:u2"}, "plus", feature="Chapter writing")
    assert exc.value.detail["current_tier"] is None
    assert exc.value.detail["message"] == "Chapter writing requires the WriteForYou Plus plan. https://writer.example/billing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user", "source"),
    [
        ({"stripe_subscription_id": "sub_1", "stripe_subscription_status": "trialing", "stripe_subscription_price_id": "price_pro"}, "stripe"),
        ({"store_subscription": {"plan": "pro", "status": "active", "product_id": "lmwfy_pro_monthly",
                                 "expires_at": datetime(2099, 1, 1, tzinfo=timezone.utc),
                                 "verified_at": datetime.now(timezone.utc)}}, "store"),
        ({"stripe_subscription_id": "app_store:u1", "app_store_subscription_status": "active",
          "app_store_subscription_tier": "pro", "app_store_product_id": "lmwfy_pro_monthly"}, "store"),
    ],
)
async def test_require_plan_accepts_stripe_store_and_the_shim(auth, mock_db, rc, user, source):
    core = make_core(auth, mock_db, rc)
    state = await require_plan(core, {"auth_user_id": "logto:u1", **user}, "pro")
    assert (state.plan, state.source) == ("pro", source)
    assert rc.calls == []


@pytest.mark.asyncio
async def test_require_plan_reverifies_a_stale_projection(auth, mock_db, rc):
    core = make_core(auth, mock_db, rc, catalog=afy_catalog())
    rc.subscribers["u1"] = subscriber(purchase("LetMeActForYou Pro", "lmafy_monthly"))
    await core.revenuecat.reconcile(await add_user(mock_db, "u1"))
    fresh = await mock_db["users"].find_one({"logto_user_id": "u1"})

    await require_plan(core, fresh, "pro", fresh_within_s=300)
    assert rc.gets() == ["u1"]

    stale = {**fresh, "store_subscription": {**fresh["store_subscription"],
                                             "verified_at": datetime.now(timezone.utc) - timedelta(minutes=10)}}
    rc.subscribers["u1"] = subscriber(purchase("LetMeActForYou Pro", "lmafy_monthly", expires="2000-01-01T00:00:00Z"))
    with pytest.raises(HTTPException) as exc:
        await require_plan(core, stale, "pro", fresh_within_s=300)
    assert exc.value.status_code == 402
    assert rc.gets() == ["u1", "u1"]

    rc.fail_next = 1
    with pytest.raises(HTTPException) as exc:
        await require_plan(core, stale, "pro", fresh_within_s=300)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_subscription_mode_gates_paid_tools_on_the_resolved_plan(auth, mock_db, rc):
    core = make_core(auth, mock_db, rc)
    core.billing.subscription_required = True
    core.billing.tool_costs = {"paid_tool": 3}
    store_user = {"auth_user_id": "logto:u1", "store_subscription": {
        "plan": "plus", "status": "active", "entitlement_id": "premium", "expires_at": None}}

    allowed = await core.billing.check_and_deduct(mock_db, store_user, "paid_tool")
    assert allowed["source"] == "subscription"

    with pytest.raises(HTTPException) as exc:
        await core.billing.check_and_deduct(mock_db, {"auth_user_id": "logto:u2"}, "paid_tool")
    assert exc.value.status_code == 402


@pytest.mark.asyncio
async def test_store_subscribers_are_never_metered_on_stripe(auth, mock_db, rc):
    core = make_core(auth, mock_db, rc)
    core.billing.tool_costs = {"paid_tool": 3}
    user = {"auth_user_id": "logto:u1", "free_credits": 0, "credits_used": 0, "stripe_customer_id": "cus_1",
            "stripe_subscription_id": "app_store:u1", "stripe_subscription_status": "active",
            "app_store_subscription_status": "active", "app_store_subscription_tier": "plus"}
    with pytest.raises(HTTPException) as exc:
        await core.billing.check_and_deduct(mock_db, user, "paid_tool")
    assert exc.value.status_code == 402

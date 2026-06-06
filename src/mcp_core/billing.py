"""
Stripe billing for MCP-first servers.

Flow:
1. Credit mode deducts free credits first, then falls back to Stripe metered billing.
2. Subscription mode requires an active all-access subscription for paid tools.
3. If payment setup is missing, paid tools return 402 with Stripe setup context.
"""

import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Set

from fastapi import HTTPException, Request

from .auth import user_identity, user_lookup_filter

logger = logging.getLogger(__name__)

__all__ = ["StripeBilling"]

DEFAULT_SUBSCRIPTION_ACCESS_STATUSES = {"active", "trialing", "past_due"}
TERMINAL_SUBSCRIPTION_STATUSES = {"canceled", "incomplete_expired"}
SUBSCRIPTION_UPDATE_EVENTS = {
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.paused",
    "customer.subscription.resumed",
}


def _obj_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _metadata_for_user(user: Dict[str, Any], **extra: Any) -> Dict[str, str]:
    metadata = {
        "auth_user_id": user_identity(user),
        "auth_provider": user.get("auth_provider", ""),
        "auth_subject": user.get("auth_subject", ""),
    }
    if user.get("logto_user_id"):
        metadata["logto_user_id"] = user.get("logto_user_id", "")
    for key, value in extra.items():
        if value is not None:
            metadata[key] = str(value)
    return metadata


def _user_filter_from_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    auth_user_id = metadata.get("auth_user_id")
    logto_user_id = metadata.get("logto_user_id")
    clauses = []
    if auth_user_id:
        clauses.append({"auth_user_id": auth_user_id})
    if logto_user_id:
        clauses.append({"logto_user_id": logto_user_id})
    if not clauses:
        return {"auth_user_id": ""}
    return clauses[0] if len(clauses) == 1 else {"$or": clauses}


def _user_insert_from_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for key in ("auth_user_id", "auth_provider", "auth_subject", "logto_user_id"):
        value = metadata.get(key)
        if value:
            fields[key] = value
    if fields:
        fields.setdefault("free_credits", 0)
        fields.setdefault("credits_used", 0)
    return fields


class StripeBilling:
    """Stripe metered billing with free credit fallback.

    Args:
        stripe_secret_key: Stripe API key (sk_live_* or sk_test_*).
        price_id: Stripe Price ID for metered subscription.
        meter_event: Stripe meter event name (e.g. "voice_tool_calls").
        free_credits: Credits per new user (informational; actual grant is in auth).
        tool_costs: {tool_name: credit_cost} mapping.
        read_only_tools: Tools that cost 0 (skip billing entirely).
        success_url: Redirect URL after Stripe Checkout completes.
        cancel_url: Redirect URL if user cancels Checkout.
    """

    def __init__(
        self,
        stripe_secret_key: str = "",
        price_id: str = "",
        meter_event: str = "mcp_tool_calls",
        free_credits: int = 30,
        tool_costs: Optional[Dict[str, int]] = None,
        read_only_tools: Optional[Set[str]] = None,
        success_url: str = "",
        cancel_url: str = "",
        buy_url: str = "",
        credit_packs: Optional[List[Dict[str, Any]]] = None,
        auto_recharge_cooldown_sec: int = 120,
        subscription_required: bool = False,
        subscription_plan_name: str = "Pro",
        subscription_price_label: str = "",
        subscription_allowed_statuses: Optional[Set[str]] = None,
    ):
        self.stripe_secret_key = stripe_secret_key
        self.price_id = price_id
        self.meter_event = meter_event
        self.free_credits = free_credits
        self.tool_costs = tool_costs or {}
        self.read_only_tools = read_only_tools or set()
        self.success_url = success_url
        self.cancel_url = cancel_url
        self.buy_url = buy_url
        self.credit_packs = list(credit_packs or [])
        self.auto_recharge_cooldown_sec = int(auto_recharge_cooldown_sec)
        self.subscription_required = bool(subscription_required)
        self.subscription_plan_name = subscription_plan_name or "Pro"
        self.subscription_price_label = subscription_price_label or ""
        self.subscription_allowed_statuses = set(
            subscription_allowed_statuses or DEFAULT_SUBSCRIPTION_ACCESS_STATUSES
        )

        self._stripe: Any = None

    # ── Lazy Stripe init ──────────────────────────────────

    def _get_stripe(self) -> Any:
        if self._stripe is not None:
            return self._stripe
        if not self.stripe_secret_key:
            logger.warning("[billing] STRIPE_SECRET_KEY not configured")
            return None
        import stripe

        stripe.api_key = self.stripe_secret_key
        self._stripe = stripe
        logger.info("[billing] Stripe configured")
        return stripe

    # ── Cost lookup ────────────────────────────────────────

    def get_tool_cost(self, tool_name: str) -> int:
        return self.tool_costs.get(tool_name, 0)

    def set_credit_packs(self, packs: List[Dict[str, Any]]) -> None:
        """Replace the one-time pack catalog used by checkout/auto top-up.

        Apps can call this at request time when price IDs come from env vars
        that may be reloaded without reconstructing MCPCore.
        """
        self.credit_packs = list(packs or [])

    def pack_options(self) -> List[Dict[str, Any]]:
        return [dict(pack) for pack in self.credit_packs if pack.get("id")]

    def find_pack(self, pack_id: str) -> Optional[Dict[str, Any]]:
        for pack in self.pack_options():
            if pack.get("id") == pack_id:
                return pack
        return None

    def _new_meter_event_identifier(self, tool_name: str) -> str:
        safe_tool = "".join(ch if ch.isalnum() else "_" for ch in tool_name)[:24]
        return f"mcp_{safe_tool}_{uuid.uuid4().hex[:24]}"[:100]

    def metered_events_from_billing_info(self, billing_info: Optional[dict]) -> List[dict]:
        if not billing_info:
            return []
        events = billing_info.get("metered_events")
        if isinstance(events, list):
            return [event for event in events if isinstance(event, dict)]
        event = billing_info.get("metered_event")
        return [event] if isinstance(event, dict) else []

    def metered_events_from_charge(self, charge: Optional[dict]) -> List[dict]:
        if not charge:
            return []
        events = charge.get("metered_events")
        if isinstance(events, list):
            return [event for event in events if isinstance(event, dict)]
        return []

    def subscription_state(self, user: Dict[str, Any]) -> Dict[str, Any]:
        subscription_id = user.get("stripe_subscription_id") or ""
        status = user.get("stripe_subscription_status") or (
            "active" if subscription_id else ""
        )
        allows_access = bool(
            subscription_id and status in self.subscription_allowed_statuses
        )
        return {
            "plan": self.subscription_plan_name,
            "price_label": self.subscription_price_label,
            "required": self.subscription_required,
            "has_subscription": bool(subscription_id),
            "allows_access": allows_access,
            "status": status,
            "cancel_at_period_end": bool(user.get("stripe_subscription_cancel_at_period_end")),
            "current_period_end": user.get("stripe_subscription_current_period_end"),
            "customer_id": user.get("stripe_customer_id") or "",
        }

    def subscription_allows_access(self, user: Dict[str, Any]) -> bool:
        return bool(self.subscription_state(user).get("allows_access"))

    async def meter_usage(
        self,
        user: Dict[str, Any],
        units: int,
        tool_name: str,
    ) -> dict:
        if units <= 0:
            return {}
        stripe = self._get_stripe()
        if not stripe:
            raise HTTPException(503, "Billing is not configured for usage-based charges.")
        identifier = self._new_meter_event_identifier(tool_name)
        try:
            stripe.billing.MeterEvent.create(
                event_name=self.meter_event,
                identifier=identifier,
                payload={
                    "stripe_customer_id": user.get("stripe_customer_id"),
                    "value": str(units),
                },
            )
            logger.info(
                "[billing] Reported %d units to Stripe meter for %s (identifier=%s)",
                units,
                tool_name,
                identifier,
            )
            return {
                "event_name": self.meter_event,
                "identifier": identifier,
                "units": units,
                "tool": tool_name,
            }
        except Exception as e:
            logger.error("[billing] Stripe meter event failed for %s: %s", tool_name, e)
            raise HTTPException(503, "Usage-based billing could not be charged. Try again.")

    async def cancel_meter_events(self, events: List[dict], label: str = "billing") -> int:
        if not events:
            return 0
        stripe = self._get_stripe()
        if not stripe:
            logger.error("[%s] cannot cancel Stripe meter events: Stripe is not configured", label)
            return 0
        cancelled = 0
        seen = set()
        for event in events:
            identifier = event.get("identifier")
            event_name = event.get("event_name") or self.meter_event
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            try:
                stripe.billing.MeterEventAdjustment.create(
                    event_name=event_name,
                    type="cancel",
                    cancel={"identifier": identifier},
                )
                cancelled += 1
                logger.info("[%s] cancelled Stripe meter event %s", label, identifier)
            except Exception:
                logger.exception("[%s] failed to cancel Stripe meter event %s", label, identifier)
        return cancelled

    async def refund_credit_charge(
        self,
        db: Any,
        user: Dict[str, Any],
        amount: int,
        label: str = "billing",
    ) -> None:
        if amount <= 0 or user.get("_internal_bypass"):
            return
        try:
            await db["users"].update_one(
                user_lookup_filter(user),
                {"$inc": {"credits_used": -amount}},
            )
        except Exception:
            logger.exception("[%s] credit refund failed for %s", label, user_identity(user))

    async def refund_charge(
        self,
        db: Any,
        user: Dict[str, Any],
        amount: int,
        label: str = "billing",
        *,
        metered_events: Optional[List[dict]] = None,
    ) -> None:
        await self.refund_credit_charge(db, user, amount, label)
        await self.cancel_meter_events(metered_events or [], label)

    # ── Credit check + deduction ──────────────────────────

    async def check_and_deduct(
        self,
        db: Any,
        user: Dict[str, Any],
        tool_name: str,
        request: Optional[Request] = None,
    ) -> Dict[str, Any]:
        """Check credits and deduct, or meter to Stripe, or raise 402.

        Returns:
            {"cost": int, "source": str, "remaining_credits": int|None}

        Raises:
            HTTPException(402) with checkout URL if no credits and no subscription.
        """
        cost = self.get_tool_cost(tool_name)
        if cost == 0 or tool_name in self.read_only_tools:
            return {"cost": 0, "source": "free", "remaining_credits": None}

        user_id = user_identity(user)
        free_credits = user.get("free_credits", 0)
        credits_used = user.get("credits_used", 0)
        remaining = free_credits - credits_used
        stripe_customer_id = user.get("stripe_customer_id")
        stripe_subscription_id = user.get("stripe_subscription_id")

        # Subscription products can opt into a standard all-access model:
        # paid tools require an active subscription and never consume credits
        # or report metered usage.
        if self.subscription_required:
            subscription = self.subscription_state(user)
            if subscription["allows_access"]:
                logger.info(
                    "[billing] Allowed %s via subscription (user=%s, status=%s)",
                    tool_name, user_id, subscription["status"],
                )
                return {
                    "cost": 0,
                    "included_tool_cost": cost,
                    "source": "subscription",
                    "remaining_credits": max(0, remaining),
                    "subscription": subscription,
                }
            setup_url = self._get_checkout_url(
                user_id, stripe_customer_id, origin=(
                    request.headers.get("origin")
                    or str(request.base_url).rstrip("/")
                    if request
                    else None
                ), user=user
            )
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "Subscription required",
                    "message": (
                        f"{self.subscription_plan_name} is required to use {tool_name}."
                    ),
                    "setup_url": setup_url,
                    "buy_url": setup_url,
                    "tool": tool_name,
                    "billing_mode": "subscription",
                    "subscription": subscription,
                },
            )

        # Case 1: free credits remaining
        if remaining >= cost:
            if db is not None:
                await db["users"].update_one(
                    user_lookup_filter(user),
                    {"$inc": {"credits_used": cost}},
                )
            logger.info(
                "[billing] Deducted %d free credits for %s (user=%s, remaining=%d)",
                cost, tool_name, user_id, remaining - cost,
            )
            return {
                "cost": cost,
                "source": "free_credits",
                "remaining_credits": remaining - cost,
            }

        # Case 2: Stripe subscription active
        if stripe_subscription_id and stripe_customer_id and self.subscription_allows_access(user):
            event = await self.meter_usage(user, cost, tool_name)
            return {
                "cost": cost,
                "source": "stripe_metered",
                "remaining_credits": 0,
                "metered_events": [event] if event else [],
            }

        # Case 3: no credits, no subscription -> 402
        origin = None
        if request:
            origin = (
                request.headers.get("origin")
                or str(request.base_url).rstrip("/")
            )
        setup_url = self._get_checkout_url(
            user_id, stripe_customer_id, origin, user=user
        )
        raise HTTPException(
            status_code=402,
            detail={
                "error": "Payment required",
                "message": f"You have no remaining credits. "
                f"This tool costs {cost} credits.",
                "setup_url": setup_url,
                "buy_url": setup_url,
                "tool": tool_name,
                "cost": cost,
                "cost_credits": cost,
                "remaining_credits": max(0, remaining),
            },
        )

    # ── Checkout URL ──────────────────────────────────────

    def _get_checkout_url(
        self,
        user_id: str,
        stripe_customer_id: Optional[str] = None,
        origin: Optional[str] = None,
        user: Optional[Dict[str, Any]] = None,
    ) -> str:
        stripe = self._get_stripe()
        base = origin or self.success_url.rsplit("/", 1)[0] if self.success_url else ""
        if not base:
            base = "http://localhost:3000"

        if not stripe or not self.price_id:
            return f"{base}/billing/success"

        try:
            metadata = _metadata_for_user(user or {}, kind="metered_subscription")
            if not metadata.get("auth_user_id"):
                metadata["auth_user_id"] = user_id
            params: Dict[str, Any] = {
                "mode": "subscription",
                "line_items": [{"price": self.price_id}],
                "success_url": f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
                "cancel_url": self.cancel_url or f"{base}/billing",
                "metadata": metadata,
                "subscription_data": {"metadata": metadata},
            }
            if stripe_customer_id:
                params["customer"] = stripe_customer_id
            elif (user or {}).get("email"):
                params["customer_email"] = (user or {}).get("email")

            session = stripe.checkout.Session.create(**params)
            return _obj_get(session, "url")
        except Exception as e:
            logger.error("[billing] Failed to create Checkout session: %s", e)
            return f"{base}/billing/success"

    def _default_origin(self) -> str:
        if self.buy_url:
            return self.buy_url.rsplit("/", 1)[0]
        if self.success_url:
            return self.success_url.rsplit("/", 1)[0]
        return "http://localhost:3000"

    async def ensure_customer(self, db: Any, user: Dict[str, Any]) -> str:
        stripe = self._get_stripe()
        if not stripe:
            raise HTTPException(503, "Billing not configured")

        existing = user.get("stripe_customer_id")
        if existing:
            try:
                stripe.Customer.retrieve(existing)
                return existing
            except Exception as e:
                code = getattr(e, "code", "")
                msg = str(e).lower()
                if code != "resource_missing" and "no such customer" not in msg:
                    raise
                logger.info("[billing] Stored Stripe customer missing; creating a new one")

        customer = stripe.Customer.create(
            metadata=_metadata_for_user(user),
            email=user.get("email") or None,
        )
        customer_id = _obj_get(customer, "id")
        if db is not None and customer_id:
            await db["users"].update_one(
                user_lookup_filter(user),
                {"$set": {"stripe_customer_id": customer_id}},
            )
        return customer_id

    async def ensure_auto_recharge_card(self, db: Any, user: Dict[str, Any]) -> Optional[str]:
        existing = (user.get("auto_recharge") or {}).get("payment_method_id")
        if existing:
            return existing
        customer_id = user.get("stripe_customer_id")
        stripe = self._get_stripe()
        if not customer_id or not stripe:
            return None

        try:
            methods = stripe.PaymentMethod.list(customer=customer_id, type="card", limit=1)
            data = getattr(methods, "data", None)
            if data is None and isinstance(methods, dict):
                data = methods.get("data")
            if not data:
                return None
            method = data[0]
            payment_method_id = (
                getattr(method, "id", None)
                or (method.get("id") if isinstance(method, dict) else None)
            )
            if not payment_method_id:
                return None
            if db is not None:
                await db["users"].update_one(
                    user_lookup_filter(user),
                    {"$set": {"auto_recharge.payment_method_id": payment_method_id}},
                )
            return payment_method_id
        except Exception as e:
            logger.warning("[billing] could not backfill auto-recharge payment method: %s", e)
            return None

    def auto_recharge_public_state(
        self,
        user: Dict[str, Any],
        *,
        backfilled_payment_method_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        first_pack = self.pack_options()[0]["id"] if self.pack_options() else ""
        ar = user.get("auto_recharge") or {}
        return {
            "enabled": bool(ar.get("enabled")),
            "threshold": ar.get("threshold", 10),
            "pack_id": ar.get("pack_id", first_pack),
            "has_card": bool(ar.get("payment_method_id") or backfilled_payment_method_id),
        }

    async def set_auto_recharge(
        self,
        db: Any,
        user: Dict[str, Any],
        *,
        enabled: bool,
        threshold: int,
        pack_id: str,
        require_saved_card: bool = True,
    ) -> Dict[str, Any]:
        if threshold < 1 or threshold > 1000:
            raise HTTPException(400, "threshold must be between 1 and 1000")
        if not self.find_pack(pack_id):
            raise HTTPException(400, f"Unknown pack_id: {pack_id}")

        existing = user.get("auto_recharge") or {}
        if enabled and require_saved_card and not existing.get("payment_method_id"):
            raise HTTPException(
                400,
                "Auto top-up needs a saved card. Buy a credit pack first, then enable auto top-up.",
            )
        new_doc = {
            "enabled": bool(enabled),
            "threshold": int(threshold),
            "pack_id": pack_id,
            "payment_method_id": existing.get("payment_method_id"),
        }
        if db is not None:
            await db["users"].update_one(
                user_lookup_filter(user),
                {"$set": {"auto_recharge": new_doc}},
            )
        return {
            "enabled": new_doc["enabled"],
            "threshold": new_doc["threshold"],
            "pack_id": new_doc["pack_id"],
            "has_card": bool(new_doc["payment_method_id"]),
        }

    async def create_credit_checkout_session(
        self,
        db: Any,
        user: Dict[str, Any],
        pack_id: str,
        *,
        origin: Optional[str] = None,
    ) -> Dict[str, Any]:
        pack = self.find_pack(pack_id)
        if not pack:
            raise HTTPException(400, f"Unknown pack_id: {pack_id}")
        stripe = self._get_stripe()
        if not stripe:
            raise HTTPException(503, "Billing not configured")
        customer_id = await self.ensure_customer(db, user)
        base = origin or self._default_origin()
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{"price": pack["price_id"], "quantity": 1}],
            customer=customer_id,
            success_url=f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/billing",
            metadata=_metadata_for_user(
                user,
                kind="credit_pack",
                pack_id=pack["id"],
                credits=pack["credits"],
            ),
            payment_intent_data={
                "metadata": _metadata_for_user(
                    user,
                    kind="credit_pack",
                    pack_id=pack["id"],
                    credits=pack["credits"],
                ),
                "setup_future_usage": "off_session",
            },
        )
        return {"url": _obj_get(session, "url"), "session_id": _obj_get(session, "id")}

    async def create_subscription_checkout_session(
        self,
        db: Any,
        user: Dict[str, Any],
        *,
        origin: Optional[str] = None,
    ) -> Dict[str, Any]:
        stripe = self._get_stripe()
        if not stripe:
            raise HTTPException(503, "Billing not configured")
        if not self.price_id:
            raise HTTPException(503, "Subscription price_id not configured")
        customer_id = await self.ensure_customer(db, user)
        base = origin or self._default_origin()
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": self.price_id}],
            customer=customer_id,
            success_url=f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/billing",
            metadata=_metadata_for_user(user, kind="metered_subscription"),
            subscription_data={
                "metadata": _metadata_for_user(user, kind="metered_subscription")
            },
        )
        return {"url": _obj_get(session, "url"), "session_id": _obj_get(session, "id")}

    async def create_setup_intent(self, db: Any, user: Dict[str, Any]) -> Dict[str, Any]:
        stripe = self._get_stripe()
        if not stripe:
            raise HTTPException(503, "Billing not configured")
        customer_id = await self.ensure_customer(db, user)
        intent = stripe.SetupIntent.create(
            customer=customer_id,
            payment_method_types=["card"],
            usage="off_session",
            metadata=_metadata_for_user(user, kind="auto_recharge_card"),
        )
        return {"client_secret": _obj_get(intent, "client_secret")}

    async def create_portal_session(
        self,
        user: Dict[str, Any],
        *,
        return_url: str,
    ) -> Dict[str, Any]:
        stripe = self._get_stripe()
        if not stripe:
            raise HTTPException(503, "Billing not configured")
        customer_id = user.get("stripe_customer_id")
        if not customer_id:
            raise HTTPException(400, "No Stripe customer yet")
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )
        return {"url": _obj_get(session, "url")}

    async def sync_checkout_session(
        self,
        db: Any,
        user: Dict[str, Any],
        session_id: str,
    ) -> Dict[str, Any]:
        if not session_id:
            raise HTTPException(400, "session_id required")
        stripe = self._get_stripe()
        if not stripe:
            raise HTTPException(503, "Billing not configured")
        session = stripe.checkout.Session.retrieve(session_id)
        session_dict = (
            session.to_dict_recursive()
            if hasattr(session, "to_dict_recursive")
            else session.to_dict()
            if hasattr(session, "to_dict")
            else dict(session)
        )
        customer_id = session_dict.get("customer") or user.get("stripe_customer_id") or ""
        subscription_id = session_dict.get("subscription") or ""
        mode = session_dict.get("mode") or ""
        metadata = session_dict.get("metadata") or {}
        expected_identity = user_identity(user)
        session_identity = (
            metadata.get("auth_user_id")
            or metadata.get("logto_user_id")
            or session_dict.get("client_reference_id")
            or expected_identity
        )
        if expected_identity and session_identity and session_identity != expected_identity:
            raise HTTPException(403, "Checkout session belongs to a different user")

        if mode == "subscription" or subscription_id:
            fields = self._subscription_update_fields(
                {
                    "id": subscription_id,
                    "customer": customer_id,
                    "status": session_dict.get("subscription_status") or "active",
                },
                customer_id=customer_id,
                subscription_id=subscription_id,
            )
            if db is not None:
                await db["users"].update_one(user_lookup_filter(user), {"$set": fields})
            updated = {**user, **fields}
            return {"status": "ok", "subscription": self.subscription_state(updated)}

        return {"status": "ignored", "mode": mode}

    async def maybe_trigger_auto_recharge(self, db: Any, user: Dict[str, Any]) -> None:
        if db is None:
            return
        identity = user_identity(user)
        if not identity:
            return
        fresh = await db["users"].find_one(user_lookup_filter(user))
        if not fresh:
            return
        user = fresh
        ar = user.get("auto_recharge") or {}
        if not ar.get("enabled"):
            return
        payment_method_id = ar.get("payment_method_id")
        customer_id = user.get("stripe_customer_id")
        if not payment_method_id or not customer_id:
            return

        threshold = int(ar.get("threshold", 10))
        free_credits = int(user.get("free_credits", 0))
        credits_used = int(user.get("credits_used", 0))
        remaining = max(0, free_credits - credits_used)
        if remaining >= threshold:
            return

        pack = self.find_pack(ar.get("pack_id", ""))
        if not pack:
            return

        stripe = self._get_stripe()
        if not stripe:
            return

        now = int(time.time())
        query = user_lookup_filter(user)
        if "$or" in query:
            query = {"$and": [query, {"$or": [
                {"auto_recharge.in_flight_until": {"$exists": False}},
                {"auto_recharge.in_flight_until": {"$lt": now}},
            ]}]}
        else:
            query = {
                **query,
                "$or": [
                    {"auto_recharge.in_flight_until": {"$exists": False}},
                    {"auto_recharge.in_flight_until": {"$lt": now}},
                ],
            }

        cas = await db["users"].update_one(
            query,
            {"$set": {"auto_recharge.in_flight_until": now + self.auto_recharge_cooldown_sec}},
        )
        if cas.modified_count == 0:
            return

        try:
            stripe.PaymentIntent.create(
                amount=pack["price_cents"],
                currency="usd",
                customer=customer_id,
                payment_method=payment_method_id,
                off_session=True,
                confirm=True,
                metadata=_metadata_for_user(
                    user,
                    kind="auto_recharge",
                    pack_id=pack["id"],
                    credits=pack["credits"],
                ),
            )
            logger.info("[billing] auto-recharge fired: user=%s pack=%s", identity, pack["id"])
        except Exception as e:
            logger.warning("[billing] auto-recharge failed for %s: %s", identity, e)

    # ── Credits summary ───────────────────────────────────

    def credits_summary(self, user: Dict[str, Any]) -> Dict[str, Any]:
        """Return credit balance for a user."""
        free = user.get("free_credits", 0)
        used = user.get("credits_used", 0)
        subscription = self.subscription_state(user)
        return {
            "free_credits": free,
            "credits_used": used,
            "remaining": max(0, free - used),
            "remaining_credits": max(0, free - used),
            "has_subscription": bool(subscription["allows_access"]),
            "billing_mode": "subscription" if self.subscription_required else "credits",
            "subscription": subscription,
            "credits_purchased": user.get("credits_purchased", 0),
            "pack_options": self.pack_options(),
            "auto_recharge": self.auto_recharge_public_state(user),
        }

    def _subscription_update_fields(
        self,
        data: Dict[str, Any],
        *,
        customer_id: str = "",
        subscription_id: str = "",
    ) -> Dict[str, Any]:
        status = data.get("status") or ("active" if subscription_id else "")
        raw_subscription_id = subscription_id or data.get("id") or ""
        stored_subscription_id = (
            None if status in TERMINAL_SUBSCRIPTION_STATUSES else raw_subscription_id
        )
        return {
            "stripe_customer_id": customer_id or data.get("customer") or "",
            "stripe_subscription_id": stored_subscription_id,
            "stripe_subscription_status": status,
            "stripe_subscription_cancel_at_period_end": bool(
                data.get("cancel_at_period_end")
            ),
            "stripe_subscription_current_period_end": data.get("current_period_end"),
            "stripe_subscription_price_id": (
                ((((data.get("items") or {}).get("data") or [{}])[0]).get("price") or {}).get("id")
                if isinstance(data.get("items"), dict)
                else data.get("price_id")
            ),
        }

    # ── Stripe webhook handler ────────────────────────────

    async def handle_webhook(
        self,
        request: Request,
        db: Any,
        webhook_secret: str = "",
    ) -> Dict[str, Any]:
        """Handle Stripe webhook events.

        Processes checkout.session.completed and customer.subscription.created.
        """
        stripe = self._get_stripe()
        if not stripe:
            return {"status": "billing_disabled"}

        payload = await request.body()
        sig_header = request.headers.get("stripe-signature", "")
        secret = webhook_secret or ""

        try:
            event = stripe.Webhook.construct_event(payload, sig_header, secret)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Webhook error: {e}")

        event_type = event["type"]
        data = event["data"]["object"]

        def _object_to_dict(raw: Any) -> Dict[str, Any]:
            if hasattr(raw, "to_dict_recursive"):
                return raw.to_dict_recursive()
            if hasattr(raw, "to_dict"):
                return raw.to_dict()
            return dict(raw)

        data = _object_to_dict(data)

        if event_type == "checkout.session.completed":
            metadata = data.get("metadata", {}) or {}
            kind = metadata.get("kind", "")
            auth_user_id = metadata.get("auth_user_id", "")
            logto_user_id = metadata.get("logto_user_id", "")
            customer_id = data.get("customer", "")
            subscription_id = data.get("subscription", "")

            if kind == "credit_pack":
                credits = int(metadata.get("credits", "0") or 0)
                if credits > 0 and (auth_user_id or logto_user_id) and db is not None:
                    await db["users"].update_one(
                        _user_filter_from_metadata(metadata),
                        {
                            "$inc": {
                                "free_credits": credits,
                                "credits_purchased": credits,
                            },
                            "$set": {"stripe_customer_id": customer_id} if customer_id else {},
                        },
                    )
                    logger.info(
                        "[billing] +%d credits to user %s (pack)",
                        credits,
                        auth_user_id or logto_user_id,
                    )
                return {"status": "ok", "event": event_type, "kind": "credit_pack"}

            if (
                kind == "metered_subscription"
                or data.get("mode") == "subscription"
                or subscription_id
            ) and (auth_user_id or logto_user_id) and db is not None:
                await db["users"].update_one(
                    _user_filter_from_metadata(metadata),
                    {"$set": self._subscription_update_fields(
                        {
                            "id": subscription_id,
                            "customer": customer_id,
                            "status": "active",
                        },
                        customer_id=customer_id,
                        subscription_id=subscription_id,
                    )},
                )
                logger.info(
                    "[billing] Linked Stripe customer %s to user %s",
                    customer_id, auth_user_id or logto_user_id,
                )
                return {"status": "ok", "event": event_type, "kind": "subscription"}

            return {"status": "ignored", "event": event_type, "reason": "unknown kind"}

        elif event_type == "setup_intent.succeeded":
            metadata = data.get("metadata", {}) or {}
            user_id = metadata.get("auth_user_id") or metadata.get("logto_user_id", "")
            payment_method_id = data.get("payment_method", "")
            if user_id and payment_method_id and db is not None:
                await db["users"].update_one(
                    _user_filter_from_metadata(metadata),
                    {"$set": {"auto_recharge.payment_method_id": payment_method_id}},
                )
                logger.info("[billing] saved payment method %s for user %s", payment_method_id, user_id)
            return {"status": "ok", "event": event_type}

        elif event_type == "payment_intent.succeeded":
            metadata = data.get("metadata", {}) or {}
            kind = metadata.get("kind", "")
            user_id = metadata.get("auth_user_id") or metadata.get("logto_user_id", "")

            if kind == "credit_pack":
                payment_method_id = data.get("payment_method", "")
                if user_id and payment_method_id and db is not None:
                    await db["users"].update_one(
                        _user_filter_from_metadata(metadata),
                        {"$set": {"auto_recharge.payment_method_id": payment_method_id}},
                    )
                    logger.info(
                        "[billing] saved pack payment method %s for user %s",
                        payment_method_id,
                        user_id,
                    )
                return {"status": "ok", "event": event_type, "kind": "credit_pack_card"}

            if kind == "auto_recharge":
                credits = int(metadata.get("credits", "0") or 0)
                if credits > 0 and user_id and db is not None:
                    await db["users"].update_one(
                        _user_filter_from_metadata(metadata),
                        {
                            "$inc": {
                                "free_credits": credits,
                                "credits_purchased": credits,
                            }
                        },
                    )
                    logger.info("[billing] +%d credits via auto-recharge for %s", credits, user_id)
            return {"status": "ok", "event": event_type}

        elif event_type in SUBSCRIPTION_UPDATE_EVENTS:
            customer_id = data.get("customer", "")
            subscription_id = data.get("id", "")
            fields = self._subscription_update_fields(
                data,
                customer_id=customer_id,
                subscription_id=subscription_id,
            )
            if customer_id and db is not None:
                result = await db["users"].update_one(
                    {"stripe_customer_id": customer_id},
                    {"$set": fields},
                )
                if getattr(result, "matched_count", 0) == 0:
                    metadata = data.get("metadata", {}) or {}
                    if metadata.get("auth_user_id") or metadata.get("logto_user_id"):
                        update = {"$set": fields}
                        insert_fields = _user_insert_from_metadata(metadata)
                        if insert_fields:
                            update["$setOnInsert"] = insert_fields
                        await db["users"].update_one(
                            _user_filter_from_metadata(metadata),
                            update,
                            upsert=True,
                        )
            return {"status": "ok", "event": event_type}

        elif event_type == "customer.subscription.deleted":
            customer_id = data.get("customer", "")
            fields = {
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": None,
                "stripe_subscription_status": "canceled",
                "stripe_subscription_cancel_at_period_end": False,
                "stripe_subscription_current_period_end": data.get("current_period_end"),
                "stripe_subscription_ended_at": data.get("ended_at") or int(time.time()),
            }
            if customer_id and db is not None:
                result = await db["users"].update_one(
                    {"stripe_customer_id": customer_id},
                    {"$set": fields},
                )
                if getattr(result, "matched_count", 0) == 0:
                    metadata = data.get("metadata", {}) or {}
                    if metadata.get("auth_user_id") or metadata.get("logto_user_id"):
                        update = {"$set": fields}
                        insert_fields = _user_insert_from_metadata(metadata)
                        if insert_fields:
                            update["$setOnInsert"] = insert_fields
                        await db["users"].update_one(
                            _user_filter_from_metadata(metadata),
                            update,
                            upsert=True,
                        )
            return {"status": "ok", "event": event_type}

        elif event_type in ("invoice.payment_failed", "invoice.payment_action_required"):
            customer_id = data.get("customer", "")
            subscription_id = data.get("subscription", "")
            if customer_id and db is not None:
                await db["users"].update_one(
                    {"stripe_customer_id": customer_id},
                    {"$set": {
                        "stripe_subscription_id": subscription_id,
                        "stripe_subscription_status": "past_due",
                    }},
                )
            return {"status": "ok", "event": event_type}

        elif event_type in ("invoice.paid", "invoice.payment_succeeded"):
            customer_id = data.get("customer", "")
            subscription_id = data.get("subscription", "")
            if customer_id and subscription_id and db is not None:
                await db["users"].update_one(
                    {"stripe_customer_id": customer_id},
                    {"$set": {
                        "stripe_subscription_id": subscription_id,
                        "stripe_subscription_status": "active",
                    }},
                )
            return {"status": "ok", "event": event_type}

        return {"status": "ignored", "event": event_type}

"""
Stripe metered billing with free credits for MCP-first servers.

Flow:
1. Free credits deducted first (configurable per product).
2. After credits exhausted, Stripe metered billing kicks in.
3. If no payment method on file, returns 402 with Stripe Checkout URL.
"""

import logging
from typing import Any, Dict, Optional, Set

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

__all__ = ["StripeBilling"]


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
    ):
        self.stripe_secret_key = stripe_secret_key
        self.price_id = price_id
        self.meter_event = meter_event
        self.free_credits = free_credits
        self.tool_costs = tool_costs or {}
        self.read_only_tools = read_only_tools or set()
        self.success_url = success_url
        self.cancel_url = cancel_url

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

        user_id = user.get("logto_user_id", "")
        free_credits = user.get("free_credits", 0)
        credits_used = user.get("credits_used", 0)
        remaining = free_credits - credits_used
        stripe_customer_id = user.get("stripe_customer_id")
        stripe_subscription_id = user.get("stripe_subscription_id")

        # Case 1: free credits remaining
        if remaining >= cost:
            if db is not None:
                await db["users"].update_one(
                    {"logto_user_id": user_id},
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
        if stripe_subscription_id and stripe_customer_id:
            stripe = self._get_stripe()
            if stripe:
                try:
                    stripe.billing.MeterEvent.create(
                        event_name=self.meter_event,
                        payload={
                            "stripe_customer_id": stripe_customer_id,
                            "value": str(cost),
                        },
                    )
                    logger.info(
                        "[billing] Reported %d units to Stripe meter for %s",
                        cost, tool_name,
                    )
                    return {
                        "cost": cost,
                        "source": "stripe_metered",
                        "remaining_credits": 0,
                    }
                except Exception as e:
                    logger.error("[billing] Stripe meter event failed: %s", e)

        # Case 3: no credits, no subscription -> 402
        origin = None
        if request:
            origin = (
                request.headers.get("origin")
                or str(request.base_url).rstrip("/")
            )
        setup_url = self._get_checkout_url(user_id, stripe_customer_id, origin)
        raise HTTPException(
            status_code=402,
            detail={
                "error": "Payment required",
                "message": f"You have no remaining credits. "
                f"This tool costs {cost} credits.",
                "setup_url": setup_url,
                "tool": tool_name,
                "cost": cost,
            },
        )

    # ── Checkout URL ──────────────────────────────────────

    def _get_checkout_url(
        self,
        user_id: str,
        stripe_customer_id: Optional[str] = None,
        origin: Optional[str] = None,
    ) -> str:
        stripe = self._get_stripe()
        base = origin or self.success_url.rsplit("/", 1)[0] if self.success_url else ""
        if not base:
            base = "http://localhost:3000"

        if not stripe or not self.price_id:
            return f"{base}/billing/success"

        try:
            params: Dict[str, Any] = {
                "mode": "subscription",
                "line_items": [{"price": self.price_id}],
                "success_url": f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
                "cancel_url": self.cancel_url or f"{base}/",
                "metadata": {"logto_user_id": user_id},
            }
            if stripe_customer_id:
                params["customer"] = stripe_customer_id

            session = stripe.checkout.Session.create(**params)
            return session.url
        except Exception as e:
            logger.error("[billing] Failed to create Checkout session: %s", e)
            return f"{base}/billing/success"

    # ── Credits summary ───────────────────────────────────

    def credits_summary(self, user: Dict[str, Any]) -> Dict[str, Any]:
        """Return credit balance for a user."""
        free = user.get("free_credits", 0)
        used = user.get("credits_used", 0)
        return {
            "free_credits": free,
            "credits_used": used,
            "remaining": max(0, free - used),
            "has_subscription": bool(user.get("stripe_subscription_id")),
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

        if event_type == "checkout.session.completed":
            logto_user_id = data.get("metadata", {}).get("logto_user_id", "")
            customer_id = data.get("customer", "")
            subscription_id = data.get("subscription", "")
            if logto_user_id and db is not None:
                await db["users"].update_one(
                    {"logto_user_id": logto_user_id},
                    {
                        "$set": {
                            "stripe_customer_id": customer_id,
                            "stripe_subscription_id": subscription_id,
                        }
                    },
                )
                logger.info(
                    "[billing] Linked Stripe customer %s to user %s",
                    customer_id, logto_user_id,
                )
            return {"status": "ok", "event": event_type}

        elif event_type == "customer.subscription.created":
            customer_id = data.get("customer", "")
            subscription_id = data.get("id", "")
            if customer_id and db is not None:
                await db["users"].update_one(
                    {"stripe_customer_id": customer_id},
                    {"$set": {"stripe_subscription_id": subscription_id}},
                )
            return {"status": "ok", "event": event_type}

        return {"status": "ignored", "event": event_type}

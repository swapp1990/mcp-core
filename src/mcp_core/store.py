"""App Store billing through RevenueCat, and one plan resolver over Stripe and the store.

``PlanCatalog.resolve(user)`` reads the Stripe fields, ``users.store_subscription``
(written only by ``RevenueCatBilling.reconcile``), direct grants and the dev
override. The highest-ranked plan wins, so no billing source overwrites another.

    catalog = PlanCatalog([Plan("free", 0), Plan("pro", 1, store_entitlements={"Pro"})])
    core = MCPCore(..., plan_catalog=catalog,
                   revenuecat=RevenueCatBilling(secret_key=..., webhook_authorization=...))
    core.install_store_routes(app)
    state = await require_plan(core, user, "pro")
"""
from __future__ import annotations

import hmac
import inspect
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request
from pymongo.errors import DuplicateKeyError

from .auth import user_identity, user_lookup_filter

logger = logging.getLogger(__name__)

__all__ = [
    "Plan",
    "PlanCatalog",
    "PlanState",
    "RevenueCatBilling",
    "RevenueCatClient",
    "RevenueCatError",
    "ids_from_env",
    "install_store_routes",
    "require_plan",
    "store_access_active",
    "store_projection",
]

APP_STORE_SHIM_PREFIX = "app_store:"
STORE_ACCESS_STATUSES = frozenset({"active", "grace"})
STRIPE_ACCESS_STATUSES = frozenset({"active", "trialing", "past_due"})
# WFY's flat app_store_* fields grant on status alone, exactly as its resolve_tier does (compat, removed in 0.7).
_COMPAT_ACCESS_STATUSES = frozenset({"active", "trialing", "past_due"})
EVENTS_COLLECTION = "store_billing_events"
SANDBOX_ALLOWLIST_ENV = "REVENUECAT_SANDBOX_ALLOWLIST"
_STALE_PROCESSING = timedelta(minutes=5)
_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)

OnChange = Callable[[Dict[str, Any], Dict[str, Any]], Any]


# ── Small helpers ─────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def parse_date(value: Any) -> Optional[datetime]:
    """ISO strings (RevenueCat, WFY), epoch seconds (Stripe), epoch ms, or a stored datetime."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _aware(parsed).astimezone(timezone.utc)


def _iso(value: Any) -> Any:
    return _aware(value).isoformat() if isinstance(value, datetime) else value


def split_ids(value: Any) -> FrozenSet[str]:
    """A comma-separated string or an iterable of ids, stripped, empties dropped."""
    if value is None:
        return frozenset()
    items = value.split(",") if isinstance(value, str) else value
    return frozenset(str(item).strip() for item in items if str(item).strip())


def ids_from_env(*names: str) -> FrozenSet[str]:
    """The first non-empty env var among ``names``, split on commas."""
    for name in names:
        raw = os.getenv(name, "")
        if raw.strip():
            return split_ids(raw)
    return frozenset()


def is_app_store_shim(subscription_id: Any) -> bool:
    return str(subscription_id or "").startswith(APP_STORE_SHIM_PREFIX)


def _identity_candidates(user: Mapping[str, Any]) -> set:
    values = {str(user.get(key) or "").strip() for key in ("auth_user_id", "logto_user_id", "auth_subject")}
    values.discard("")
    expanded = set(values)
    for value in values:
        if ":" not in value:
            expanded.update({f"logto:{value}", f"supabase:{value}"})
    return expanded


def store_projection(user: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """``users.store_subscription``, or WFY's flat ``app_store_*`` fields when it is absent (compat, removed in 0.7)."""
    user = user or {}
    current = user.get("store_subscription")
    if isinstance(current, dict) and current:
        return current
    status = str(user.get("app_store_subscription_status") or "").strip().lower()
    if not status:
        return None
    return {
        "provider": user.get("app_store_provider") or "revenuecat",
        "app_user_id": user.get("app_store_app_user_id") or "",
        "plan": str(user.get("app_store_subscription_tier") or "").strip().lower(),
        "status": status,
        "entitlement_id": user.get("app_store_entitlement_id") or "",
        "product_id": user.get("app_store_product_id") or "",
        "expires_at": parse_date(user.get("app_store_expires_at")),
        "verified_at": parse_date(user.get("app_store_synced_at")),
        "compat": True,
    }


def store_access_active(projection: Optional[Mapping[str, Any]], now: Optional[datetime] = None) -> bool:
    if not projection:
        return False
    status = str(projection.get("status") or "").lower()
    if projection.get("compat"):
        return status in _COMPAT_ACCESS_STATUSES
    if status not in STORE_ACCESS_STATUSES:
        return False
    end = parse_date(projection.get("grace_period_expires_at" if status == "grace" else "expires_at"))
    return end is None or end > (now or _now())


def _public_projection(projection: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not projection:
        return None
    return {key: _iso(value) for key, value in projection.items()}


# ── Plans ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Plan:
    """One plan; ``rank`` orders them, and the id sets say which purchases grant it."""

    name: str
    rank: int = 0
    display_name: str = ""
    stripe_price_ids: Iterable[str] = frozenset()
    store_entitlements: Iterable[str] = frozenset()
    store_product_ids: Iterable[str] = frozenset()
    credits_per_period: int = 0
    period: str = "billing"  # or "calendar_month"

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", str(self.name).strip().lower())
        for attr in ("stripe_price_ids", "store_entitlements", "store_product_ids"):
            object.__setattr__(self, attr, split_ids(getattr(self, attr)))
        if self.period not in {"billing", "calendar_month"}:
            raise ValueError("Plan.period must be 'billing' or 'calendar_month'")
        if not self.name:
            raise ValueError("Plan needs a name")


@dataclass(frozen=True)
class PlanState:
    plan: str
    rank: int
    source: str = "none"  # stripe | store | direct | dev | none
    status: str = ""
    expires_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan": self.plan,
            "rank": self.rank,
            "source": self.source,
            "status": self.status,
            "expires_at": _iso(self.expires_at),
        }


class PlanCatalog:
    """Per-product plans, and how Stripe prices, store entitlements and grants map onto them."""

    def __init__(
        self,
        plans: Iterable[Plan],
        *,
        unknown_paid_plan: str = "",
        direct_grants: Optional[Mapping[str, Any]] = None,
        dev_force_plan: str = "",
        upgrade_url: str = "",
    ) -> None:
        self.plans: List[Plan] = sorted(plans, key=lambda plan: plan.rank)
        if not self.plans:
            raise ValueError("PlanCatalog needs at least one plan")
        self._by_name = {plan.name: plan for plan in self.plans}
        if len(self._by_name) != len(self.plans):
            raise ValueError("PlanCatalog plan names must be unique")
        self.base = self.plans[0]
        self.unknown_paid_plan = (unknown_paid_plan or "").strip().lower()
        if self.unknown_paid_plan and self.unknown_paid_plan not in self._by_name:
            raise ValueError(f"unknown_paid_plan {self.unknown_paid_plan!r} is not a plan")
        self.direct_grants: Dict[str, FrozenSet[str]] = {}
        for name, ids in (direct_grants or {}).items():
            key = str(name).strip().lower()
            if key not in self._by_name:
                raise ValueError(f"direct_grants names unknown plan {key!r}")
            self.direct_grants[key] = split_ids(ids)
        self.dev_force_plan = (dev_force_plan or "").strip().lower()
        self.upgrade_url = upgrade_url or ""
        # MCPCore sets these: the dev override needs the dev auth bypass; Stripe access follows billing's statuses.
        self.dev_bypass = False
        self.stripe_access_statuses = set(STRIPE_ACCESS_STATUSES)

    def get(self, name: str) -> Optional[Plan]:
        return self._by_name.get(str(name or "").strip().lower())

    @property
    def unknown_paid(self) -> Optional[Plan]:
        return self._by_name.get(self.unknown_paid_plan) if self.unknown_paid_plan else None

    def plan_for_stripe_price(self, price_id: str) -> Optional[Plan]:
        matches = [plan for plan in self.plans if price_id and price_id in plan.stripe_price_ids]
        return matches[-1] if matches else None

    def plan_for_store(self, entitlement_id: str, product_id: str) -> Optional[Plan]:
        matches = [
            plan for plan in self.plans
            if (entitlement_id and entitlement_id in plan.store_entitlements)
            or (product_id and product_id in plan.store_product_ids)
        ]
        return matches[-1] if matches else None

    def public(self) -> List[Dict[str, Any]]:
        return [
            {"name": plan.name, "rank": plan.rank, "display_name": plan.display_name or plan.name.title()}
            for plan in self.plans
        ]

    # ── Resolution ──

    def resolve(self, user: Optional[Mapping[str, Any]], *, now: Optional[datetime] = None) -> PlanState:
        user = user or {}
        forced = self._by_name.get(self.dev_force_plan) if self.dev_bypass else None
        if forced is not None:
            return PlanState(forced.name, forced.rank, "dev", "active")
        best = PlanState(self.base.name, self.base.rank)
        for state in (self._direct_state(user), self._stripe_state(user), self._store_state(user, now or _now())):
            if state is not None and state.rank > best.rank:
                best = state
        return best

    def _direct_state(self, user: Mapping[str, Any]) -> Optional[PlanState]:
        candidates = _identity_candidates(user)
        granted = [self._by_name[name] for name, ids in self.direct_grants.items() if ids & candidates]
        if not granted:
            return None
        plan = max(granted, key=lambda item: item.rank)
        return PlanState(plan.name, plan.rank, "direct", "active")

    def _stripe_state(self, user: Mapping[str, Any]) -> Optional[PlanState]:
        subscription_id = str(user.get("stripe_subscription_id") or "")
        if not subscription_id or is_app_store_shim(subscription_id):
            return None
        status = str(user.get("stripe_subscription_status") or "active")
        if status not in self.stripe_access_statuses:
            return None
        plan = self.plan_for_stripe_price(str(user.get("stripe_subscription_price_id") or "")) or self.unknown_paid
        if plan is None:
            return None
        return PlanState(plan.name, plan.rank, "stripe", status, parse_date(user.get("stripe_subscription_current_period_end")))

    def _store_state(self, user: Mapping[str, Any], now: datetime) -> Optional[PlanState]:
        projection = store_projection(user)
        if not store_access_active(projection, now):
            return None
        plan = (
            self.plan_for_store(str(projection.get("entitlement_id") or ""), str(projection.get("product_id") or ""))
            or self.get(str(projection.get("plan") or ""))
            or self.unknown_paid
        )
        if plan is None:
            return None
        return PlanState(plan.name, plan.rank, "store", str(projection.get("status") or ""), parse_date(projection.get("expires_at")))

    # ── Gate ──

    def required_detail(
        self,
        required: Plan,
        state: PlanState,
        *,
        feature: str = "",
        message: Optional[str] = None,
        upgrade_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """WFY's 402 body, so web and iOS clients parse it unchanged."""
        url = self.upgrade_url if upgrade_url is None else upgrade_url
        label = required.display_name or required.name.title()
        text = message or f"{feature or 'This feature'} requires the {label} plan." + (f" {url}" if url else "")
        return {
            "code": "subscription_required",
            "legacy_code": f"{required.name}_required",
            "message": text,
            "required_tier": required.name,
            "current_tier": state.plan if state.rank > self.base.rank else None,
            "upgrade_url": url,
            "error": {
                "code": "subscription_required",
                "message": text,
                "required_tier": required.name,
                "upgrade_url": url,
            },
        }

    def require(
        self,
        user: Optional[Mapping[str, Any]],
        name: str,
        *,
        feature: str = "",
        message: Optional[str] = None,
        upgrade_url: Optional[str] = None,
    ) -> PlanState:
        """Synchronous gate on the stored state: the PlanState, or HTTP 402."""
        required = self.get(name)
        if required is None:
            raise ValueError(f"Unknown plan {name!r}")
        state = self.resolve(user)
        if state.rank >= required.rank:
            return state
        raise HTTPException(
            status_code=402,
            detail=self.required_detail(required, state, feature=feature, message=message, upgrade_url=upgrade_url),
        )


# ── RevenueCat ────────────────────────────────────────────


class RevenueCatError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RevenueCatClient:
    """RevenueCat REST API v1 with a legacy secret key; a v2 client only needs these two calls."""

    def __init__(
        self,
        secret_key: str = "",
        *,
        base_url: str = "https://api.revenuecat.com",
        timeout: float = 15.0,
        http_client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.secret_key = (secret_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self._client_factory = http_client_factory or (lambda: httpx.AsyncClient(timeout=timeout))

    @property
    def configured(self) -> bool:
        return bool(self.secret_key)

    async def _request(self, method: str, app_user_id: str) -> httpx.Response:
        if not self.secret_key:
            raise RevenueCatError("RevenueCat secret key is not configured", 503)
        url = f"{self.base_url}/v1/subscribers/{quote(app_user_id, safe='')}"
        headers = {"Authorization": f"Bearer {self.secret_key}", "Accept": "application/json"}
        try:
            async with self._client_factory() as client:
                return await client.request(method, url, headers=headers)
        except httpx.HTTPError as exc:
            raise RevenueCatError(f"RevenueCat unreachable ({exc.__class__.__name__})") from exc

    async def get_subscriber(self, app_user_id: str) -> Dict[str, Any]:
        """The v1 ``subscriber`` object. The v1 GET creates the customer when it doesn't exist."""
        resp = await self._request("GET", app_user_id)
        if resp.status_code >= 400:
            raise RevenueCatError(f"RevenueCat subscriber lookup failed with HTTP {resp.status_code}", resp.status_code)
        try:
            body = resp.json() or {}
        except ValueError as exc:
            raise RevenueCatError("RevenueCat returned a non-JSON subscriber response", resp.status_code) from exc
        subscriber = body.get("subscriber") if isinstance(body, dict) else None
        return subscriber if isinstance(subscriber, dict) else {}

    async def delete_subscriber(self, app_user_id: str) -> bool:
        """True if deleted, False if RevenueCat had no such customer."""
        resp = await self._request("DELETE", app_user_id)
        if resp.status_code == 404:
            return False
        if resp.status_code >= 400:
            raise RevenueCatError(f"RevenueCat subscriber delete failed with HTTP {resp.status_code}", resp.status_code)
        return True


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


class RevenueCatBilling:
    """Webhook, reconcile and customer deletion for one product's RevenueCat project.

    ``app_user_id`` is always the Logto ``sub`` of the signed-in caller (the apps call
    ``Purchases.logIn(sub)``), never a value from a request body.
    """

    def __init__(
        self,
        secret_key: str = "",
        webhook_authorization: str = "",
        *,
        sandbox_allowlist: Any = None,
        accept_sandbox: bool = False,
        on_change: Optional[OnChange] = None,
        client: Optional[Any] = None,
        events_collection: str = EVENTS_COLLECTION,
        legacy_event_collection: str = "",
    ) -> None:
        self.client = client or RevenueCatClient(secret_key)
        self.webhook_authorization = (webhook_authorization or "").strip()
        if sandbox_allowlist is None:
            sandbox_allowlist = os.getenv(SANDBOX_ALLOWLIST_ENV, "")
        self.sandbox_allowlist = frozenset(item.lower() for item in split_ids(sandbox_allowlist))
        self.accept_sandbox = bool(accept_sandbox)
        self.on_change = on_change
        self.events_collection = events_collection
        self.legacy_event_collection = legacy_event_collection
        self._core: Any = None

    def _bind(self, core: Any) -> None:
        self._core = core

    @property
    def configured(self) -> bool:
        return bool(getattr(self.client, "configured", False))

    # ── Identity ──

    @staticmethod
    def app_user_id(user: Optional[Mapping[str, Any]]) -> str:
        user = user or {}
        for key in ("logto_user_id", "auth_subject"):
            value = str(user.get(key) or "").strip()
            if value:
                return value
        identity = str(user_identity(dict(user)) or "")
        return identity.split(":", 1)[1] if ":" in identity else identity

    def sandbox_allowed(self, user: Optional[Mapping[str, Any]]) -> bool:
        """Sandbox purchases count in production only for allowlisted app user ids or emails."""
        if self.accept_sandbox:
            return True
        if not self.sandbox_allowlist:
            return False
        user = user or {}
        candidates = {value.lower() for value in _identity_candidates(user)}
        app_user_id = self.app_user_id(user)
        if app_user_id:
            candidates.add(app_user_id.lower())
        email = str(user.get("email") or "").strip().lower()
        if email:
            candidates.add(email)
        return bool(candidates & self.sandbox_allowlist)

    # ── Mapping ──

    def project(
        self,
        subscriber: Mapping[str, Any],
        user: Optional[Mapping[str, Any]],
        catalog: PlanCatalog,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Map a v1 subscriber onto ``users.store_subscription``; the highest-ranked entitled plan wins."""
        now = now or _now()
        entitlements = subscriber.get("entitlements") if isinstance(subscriber.get("entitlements"), dict) else {}
        subscriptions = subscriber.get("subscriptions") if isinstance(subscriber.get("subscriptions"), dict) else {}
        candidates: List[Tuple[str, str, Dict[str, Any]]] = [
            (str(ent_id), str(item.get("product_identifier") or ""), item)
            for ent_id, item in entitlements.items() if isinstance(item, dict)
        ]
        referenced = {product_id for _, product_id, _ in candidates}
        candidates += [
            ("", str(product_id), item)
            for product_id, item in subscriptions.items()
            if isinstance(item, dict) and str(product_id) not in referenced
        ]
        sandbox_ok = self.sandbox_allowed(user)
        app_user_id = self.app_user_id(user)
        best: Optional[Dict[str, Any]] = None
        best_key: Optional[Tuple[bool, int, datetime]] = None
        skipped_sandbox = 0
        for entitlement_id, product_id, item in candidates:
            plan = catalog.plan_for_store(entitlement_id, product_id) or catalog.unknown_paid
            if plan is None:
                continue
            sub = subscriptions.get(product_id) if isinstance(subscriptions.get(product_id), dict) else {}
            sandbox = bool(sub.get("is_sandbox"))
            if sandbox and not sandbox_ok:
                skipped_sandbox += 1
                continue
            expires = parse_date(item.get("expires_date"))
            grace = parse_date(item.get("grace_period_expires_date") or sub.get("grace_period_expires_date"))
            if sub.get("refunded_at"):
                status = "refunded"
            elif expires is None or expires > now:
                status = "active"
            elif grace is not None and grace > now:
                status = "grace"
            else:
                status = "expired"
            entitled = status in STORE_ACCESS_STATUSES
            key = (entitled, plan.rank if entitled else -1, expires or _FAR_FUTURE)
            if best_key is None or key > best_key:
                best_key = key
                best = {
                    "plan": plan.name if entitled else catalog.base.name,
                    "status": status,
                    "entitlement_id": entitlement_id or None,
                    "product_id": product_id or None,
                    "store": sub.get("store"),
                    "environment": "sandbox" if sandbox else "production",
                    "period_start": parse_date(item.get("purchase_date") or sub.get("purchase_date")),
                    "expires_at": expires,
                    "grace_period_expires_at": grace,
                    "will_renew": status == "active" and expires is not None and not sub.get("unsubscribe_detected_at"),
                    "billing_issue": bool(sub.get("billing_issues_detected_at")),
                }
        if skipped_sandbox:
            logger.info("[store] ignored %d sandbox purchase(s) for %s: not on the sandbox allowlist", skipped_sandbox, app_user_id)
        projection = {
            "provider": "revenuecat",
            "app_user_id": app_user_id,
            "plan": catalog.base.name,
            "status": "none",
            "entitlement_id": None,
            "product_id": None,
            "store": None,
            "environment": None,
            "period_start": None,
            "expires_at": None,
            "grace_period_expires_at": None,
            "will_renew": False,
            "billing_issue": False,
        }
        projection.update(best or {})
        projection["verified_at"] = now
        return projection

    # ── Reconcile ──

    def _catalog(self) -> PlanCatalog:
        core = self._core
        if core is None:
            raise RuntimeError("RevenueCatBilling must be passed to MCPCore(revenuecat=...)")
        catalog = getattr(core, "plans", None)
        if catalog is None:
            raise RuntimeError("RevenueCat reconcile needs MCPCore(plan_catalog=...)")
        return catalog

    async def reconcile(self, user: Dict[str, Any]) -> Dict[str, Any]:
        """Re-read the subscriber and write ``users.store_subscription``; the only writer of store state."""
        catalog = self._catalog()
        app_user_id = self.app_user_id(user)
        if not app_user_id:
            raise RevenueCatError("This account has no RevenueCat app user id", 422)
        subscriber = await self.client.get_subscriber(app_user_id)
        now = _now()
        after = self.project(subscriber, user, catalog, now=now)
        before = user.get("store_subscription") or None
        db = self._core.db
        if db is not None:
            # No upsert: a late webhook must not recreate a deleted account's record.
            await db["users"].update_one(user_lookup_filter(user), {"$set": {"store_subscription": after}})
            await self._grant_period_credits(db, user, after, catalog, now)
        if self.on_change is not None:
            changed = not before or (before.get("plan"), before.get("status")) != (after["plan"], after["status"])
            result = self.on_change({**user, "store_subscription": after}, {**after, "previous": before, "changed": changed})
            if inspect.isawaitable(result):
                await result
        return after

    async def sync(self, user: Dict[str, Any]) -> Dict[str, Any]:
        """``reconcile`` for a client-facing route: RevenueCat failures become a retryable 503."""
        try:
            return await self.reconcile(user)
        except RevenueCatError as exc:
            logger.warning("[store] RevenueCat sync failed for %s: %s", user_identity(user), exc)
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "billing_verification_unavailable",
                    "message": "Purchase status could not be verified right now.",
                    "retryable": True,
                },
            ) from None

    async def _grant_period_credits(
        self, db: Any, user: Mapping[str, Any], after: Mapping[str, Any], catalog: PlanCatalog, now: datetime
    ) -> bool:
        plan = catalog.get(str(after.get("plan") or ""))
        if plan is None or plan.credits_per_period <= 0 or after.get("status") not in STORE_ACCESS_STATUSES:
            return False
        if plan.period == "calendar_month":
            period_key = now.strftime("%Y-%m")
        else:
            start = after.get("period_start")
            if not isinstance(start, datetime):
                return False
            period_key = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        # The key comes only from the subscriber API, so webhook, sync and retries converge on one grant.
        grant_id = f"revenuecat:{after.get('entitlement_id') or after.get('product_id')}:{period_key}"
        metadata = {"auth_user_id": user_identity(dict(user)), "logto_user_id": user.get("logto_user_id")}
        return await self._core.billing._grant_credits_once(db, metadata, grant_id, plan.credits_per_period)

    async def delete_customer(self, app_user_id: str) -> Optional[bool]:
        """Delete the RevenueCat customer; None when no secret key is configured."""
        if not self.configured:
            return None
        return await self.client.delete_subscriber(app_user_id)

    # ── Webhook ──

    def authorize_webhook(self, header: str) -> None:
        expected = self.webhook_authorization
        if not expected:
            raise HTTPException(status_code=503, detail="RevenueCat webhook is not configured")
        provided = (header or "").strip()
        candidates = [provided]
        if provided[:7].lower() == "bearer ":
            candidates.append(provided[7:].strip())
        matched = False
        for candidate in candidates:
            matched = hmac.compare_digest(candidate.encode(), expected.encode()) or matched
        if not matched:
            raise HTTPException(status_code=401, detail="Invalid webhook authorization")

    @staticmethod
    def event_candidates(event: Mapping[str, Any]) -> List[str]:
        values = [
            event.get("app_user_id"),
            event.get("original_app_user_id"),
            *_as_list(event.get("aliases")),
            *_as_list(event.get("transferred_from")),
            *_as_list(event.get("transferred_to")),
        ]
        candidates: List[str] = []
        for value in values:
            text = str(value or "").strip()
            if text and not text.startswith("$RCAnonymousID:") and text not in candidates:
                candidates.append(text)
        return candidates

    async def _find_user(self, db: Any, candidate: str) -> Optional[Dict[str, Any]]:
        provider = getattr(getattr(self._core, "auth", None), "provider_name", "logto") or "logto"
        return await db["users"].find_one({"$or": [
            {"auth_user_id": f"{provider}:{candidate}"},
            {"auth_user_id": candidate},
            {"logto_user_id": candidate},
        ]})

    async def _claim_event(self, db: Any, log_id: str, event: Mapping[str, Any], environment: str) -> str:
        """``claimed``, ``duplicate`` (already handled) or ``busy`` (another delivery is processing it)."""
        events = db[self.events_collection]
        event_id = str(event.get("id"))
        if self.legacy_event_collection:
            legacy = await db[self.legacy_event_collection].find_one(
                {"event_id": event_id, "environment": event.get("environment") or "", "status": "processed"}
            )
            if legacy:
                return "duplicate"
        now = _now()
        try:
            await events.insert_one({
                "_id": log_id,
                "provider": "revenuecat",
                "event_id": event_id,
                "environment": environment,
                "event_type": event.get("type"),
                "app_user_id": event.get("app_user_id"),
                "status": "processing",
                "attempts": 1,
                "received_at": now,
                "updated_at": now,
            })
            return "claimed"
        except DuplicateKeyError:
            pass
        existing = await events.find_one({"_id": log_id}) or {}
        status = existing.get("status")
        if status in {"processed", "ignored"}:
            return "duplicate"
        updated = _aware(existing.get("updated_at"))
        if status == "processing" and updated is not None and now - updated < _STALE_PROCESSING:
            return "busy"
        claimed = await events.find_one_and_update(
            {"_id": log_id, "status": status, "attempts": existing.get("attempts")},
            {"$set": {"status": "processing", "updated_at": now}, "$inc": {"attempts": 1}},
        )
        return "claimed" if claimed else "busy"

    async def handle_webhook(self, request: Request) -> Dict[str, Any]:
        self.authorize_webhook(request.headers.get("authorization", ""))
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Webhook body is not JSON")
        event = body.get("event") if isinstance(body, dict) and isinstance(body.get("event"), dict) else {}
        event_type = str(event.get("type") or "")
        if event_type == "TEST":
            return {"ok": True, "handled": False, "status": "test"}
        event_id = str(event.get("id") or "").strip()
        if not event_id:
            raise HTTPException(status_code=422, detail="RevenueCat event id is required")
        db = getattr(self._core, "db", None)
        if db is None:
            raise HTTPException(status_code=503, detail="Database unavailable")
        environment = str(event.get("environment") or "PRODUCTION").strip().lower()
        log_id = f"revenuecat:{environment}:{event_id}"
        claim = await self._claim_event(db, log_id, event, environment)
        if claim == "duplicate":
            return {"ok": True, "handled": False, "status": "duplicate"}
        if claim == "busy":
            raise HTTPException(status_code=409, detail="This RevenueCat event is already being processed")

        events = db[self.events_collection]
        try:
            reconciled, reason = await self._process_event(db, event, environment)
        except Exception as exc:
            logger.exception("[store] RevenueCat event %s failed", log_id)
            await events.update_one(
                {"_id": log_id},
                {"$set": {"status": "failed", "error": exc.__class__.__name__, "updated_at": _now()}},
            )
            raise HTTPException(status_code=500, detail="RevenueCat event could not be processed") from None
        status = "processed" if reconciled else "ignored"
        await events.update_one(
            {"_id": log_id},
            {"$set": {"status": status, "reason": reason, "reconciled": reconciled, "processed_at": _now(), "updated_at": _now()}},
        )
        result = {"ok": True, "handled": bool(reconciled), "status": status, "event_type": event_type, "reconciled": reconciled}
        if reason:
            result["reason"] = reason
        return result

    async def _process_event(self, db: Any, event: Mapping[str, Any], environment: str) -> Tuple[int, str]:
        seen = set()
        reconciled = sandbox_skipped = 0
        for candidate in self.event_candidates(event):
            user = await self._find_user(db, candidate)
            if user is None or user.get("_id") in seen:
                continue
            seen.add(user.get("_id"))
            if environment == "sandbox" and not self.sandbox_allowed(user):
                sandbox_skipped += 1
                continue
            await self.reconcile(user)
            reconciled += 1
        if reconciled:
            return reconciled, ""
        if sandbox_skipped:
            logger.info("[store] ignored sandbox event %s: no allowlisted account", event.get("id"))
            return 0, "sandbox"
        return 0, "no_account"


# ── Gate and routes ───────────────────────────────────────


def _stale(projection: Mapping[str, Any], within_s: float) -> bool:
    verified = parse_date(projection.get("verified_at"))
    return verified is None or (_now() - verified).total_seconds() > within_s


async def require_plan(
    core: Any,
    user: Dict[str, Any],
    name: str,
    *,
    fresh_within_s: Optional[float] = None,
    feature: str = "",
    message: Optional[str] = None,
    upgrade_url: Optional[str] = None,
) -> PlanState:
    """The caller's PlanState if it reaches plan ``name``, else HTTP 402 with WFY's body.

    ``fresh_within_s`` re-verifies a store projection older than that before deciding.
    """
    catalog: Optional[PlanCatalog] = getattr(core, "plans", None)
    if catalog is None:
        raise RuntimeError("require_plan needs MCPCore(plan_catalog=...)")
    required = catalog.get(name)
    if required is None:
        raise ValueError(f"Unknown plan {name!r}")
    state = catalog.resolve(user)
    revenuecat: Optional[RevenueCatBilling] = getattr(core, "revenuecat", None)
    projection = (user or {}).get("store_subscription")
    if (
        fresh_within_s is not None
        and revenuecat is not None
        and revenuecat.configured
        and isinstance(projection, dict)
        and projection
        and (state.source == "store" or state.rank < required.rank)
        and _stale(projection, fresh_within_s)
    ):
        user = {**user, "store_subscription": await revenuecat.sync(user)}
        state = catalog.resolve(user)
    if state.rank >= required.rank:
        return state
    raise HTTPException(
        status_code=402,
        detail=catalog.required_detail(required, state, feature=feature, message=message, upgrade_url=upgrade_url),
    )


def install_store_routes(
    app: FastAPI,
    core: Any,
    *,
    sync_path: Optional[str] = "/api/billing/app-store/sync",
    webhook_path: Optional[str] = "/api/billing/revenuecat/webhook",
) -> None:
    """RevenueCat webhook and client sync routes; pass ``None`` to skip one and keep a product's own."""
    revenuecat: Optional[RevenueCatBilling] = getattr(core, "revenuecat", None)
    if revenuecat is None:
        raise RuntimeError("install_store_routes needs MCPCore(revenuecat=...)")

    if webhook_path:
        @app.post(webhook_path, include_in_schema=False)
        async def revenuecat_webhook(request: Request) -> Dict[str, Any]:
            return await revenuecat.handle_webhook(request)

    if sync_path:
        @app.post(sync_path)
        async def store_sync(request: Request) -> Dict[str, Any]:
            payload = await core.auth.verify_token(request)
            if payload is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            user = await core.auth.get_or_create_user(core.db, payload)
            projection = await revenuecat.sync(user)
            return core.billing.credits_summary({**user, "store_subscription": projection})

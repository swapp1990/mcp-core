"""
mcp-core: Auth, billing, and logging infrastructure for MCP-first servers.

Usage:
    from mcp_core import MCPCore

    core = MCPCore(
        product_name="myapp",
        logto_endpoint="https://your-tenant.logto.app",   # or self-hosted: https://auth.example.com
        logto_api_resource="https://api.example.com",
        mongodb_uri="mongodb+srv://...",
        db_name="myapp",
        stripe_secret_key="sk_test_...",
        stripe_price_id="price_...",
        stripe_meter_event="tool_calls",
        free_credits=25,
        tool_costs={"browse": 0, "generate": 2},
        read_only_tools={"browse"},
    )

    # In your tool handler:
    user = await core.auth_and_bill(request, "narrate_text")
"""

import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Set

from fastapi import FastAPI, Request

from .auth import LogtoAuth, SupabaseAuth, user_identity
from .billing import StripeBilling
from .dcr import LogtoDCR
from .echo import with_echo
from .health import HealthCheck
from .mcp_mount import mount_mcp
from .routes import install_routes
from .tool_logging import ToolLogger

__all__ = [
    "MCPCore", "LogtoAuth", "SupabaseAuth", "StripeBilling", "HealthCheck",
    "ToolLogger", "LogtoDCR", "mount_mcp", "with_echo", "user_identity",
]
try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("mcp-core-auth")
except Exception:
    __version__ = "0.0.0+unknown"

logger = logging.getLogger(__name__)


class MCPCore:
    """Facade that wires auth, billing, logging, and health together.

    All parameters can also be provided via environment variables
    with MCP_CORE_ prefix (e.g. MCP_CORE_PRODUCT_NAME).
    Constructor args take precedence over env vars.
    """

    def __init__(
        self,
        product_name: str = "",
        auth_provider: str = "",
        # Logto auth
        logto_endpoint: str = "",
        logto_api_resource: str = "",
        # Supabase auth
        supabase_url: str = "",
        supabase_anon_key: str = "",
        supabase_auth_base_url: str = "",
        supabase_api_resource: str = "",
        free_credits: int = 0,
        dev_auth_bypass: bool = False,
        dev_user_id: str = "local-dev-user",
        reject_m2m: bool = True,
        # MongoDB
        mongodb_uri: str = "",
        db_name: str = "",
        # Stripe billing
        stripe_secret_key: str = "",
        stripe_price_id: str = "",
        stripe_meter_event: str = "mcp_tool_calls",
        stripe_webhook_secret: str = "",
        billing_success_url: str = "",
        billing_cancel_url: str = "",
        billing_buy_url: str = "",
        stripe_portal_configuration_id: str = "",
        credit_packs: Optional[List[Dict[str, Any]]] = None,
        auto_recharge_cooldown_sec: int = 120,
        subscription_required: bool = False,
        subscription_plan_name: str = "",
        subscription_price_label: str = "",
        subscription_allowed_statuses: Optional[Set[str]] = None,
        # Tools
        tool_costs: Optional[Dict[str, int]] = None,
        read_only_tools: Optional[Set[str]] = None,
        # MCP OAuth
        mcp_logto_app_id: str = "",
        mcp_logto_app_secret: str = "",
        mcp_supabase_client_id: str = "",
        mcp_supabase_client_secret: str = "",
        oauth_scopes: Optional[List[str]] = None,
        # Logto Management API (enables real RFC 7591 DCR when provided)
        logto_mgmt_app_id: str = "",
        logto_mgmt_app_secret: str = "",
        logto_mgmt_api_resource: str = "",
        # Set this when Management tokens are issued on a different origin than
        # `logto_endpoint` (Logto OSS self-host with admin on a separate port).
        # Cloud deployments leave this empty.
        logto_mgmt_token_endpoint: str = "",
        # When set, /oauth/authorize bounces unauthenticated users here for
        # product-branded inline sign-in instead of showing Logto's hosted
        # page. The product is responsible for implementing this URL as a
        # page that (1) accepts ?return_to=<url>, (2) validates it is
        # same-origin, (3) runs the product's sign-in UI, (4) redirects
        # the browser back to return_to on success. Logto's session cookie
        # must be emitted with `Domain=.<apex>` so the retry carries it
        # to /oauth/authorize. Leave empty to keep using Logto's hosted UI.
        branded_sign_in_url: str = "",
    ):
        def _env(key: str, default: str = "") -> str:
            return os.getenv(f"MCP_CORE_{key}", default)

        self.product_name = product_name or _env("PRODUCT_NAME", "mcp-server")
        _read_only = read_only_tools or set()
        _free = free_credits or int(_env("FREE_CREDITS", "30"))

        _logto_endpoint = logto_endpoint or _env("LOGTO_ENDPOINT")
        _logto_api_resource = logto_api_resource or _env("LOGTO_API_RESOURCE")
        _supabase_url = supabase_url or _env("SUPABASE_URL")
        _supabase_anon_key = supabase_anon_key or _env("SUPABASE_ANON_KEY")
        _supabase_auth_base_url = (
            supabase_auth_base_url or _env("SUPABASE_AUTH_BASE_URL")
        )
        _supabase_api_resource = (
            supabase_api_resource
            or _env("SUPABASE_API_RESOURCE")
            or _logto_api_resource
        )
        _auth_provider = (
            auth_provider or _env("AUTH_PROVIDER")
        ).strip().lower()
        if not _auth_provider:
            if _supabase_url and _supabase_anon_key:
                _auth_provider = "supabase"
            elif _logto_endpoint:
                _auth_provider = "logto"
            else:
                _auth_provider = "logto"
        if _auth_provider not in {"logto", "supabase", "none"}:
            raise ValueError(
                "auth_provider must be one of: logto, supabase, none"
            )
        self.auth_provider = _auth_provider

        # Auth
        _dev_bypass = dev_auth_bypass or _env("DEV_AUTH_BYPASS") == "1"
        if _auth_provider == "supabase":
            self.auth = SupabaseAuth(
                supabase_url=_supabase_url,
                anon_key=_supabase_anon_key,
                auth_base_url=_supabase_auth_base_url,
                api_resource=_supabase_api_resource,
                free_credits=_free,
                dev_bypass=_dev_bypass,
                dev_user_id=dev_user_id,
                read_only_tools=_read_only,
                reject_m2m=reject_m2m,
            )
        else:
            self.auth = LogtoAuth(
                endpoint=_logto_endpoint,
                api_resource=_logto_api_resource,
                free_credits=_free,
                dev_bypass=_dev_bypass,
                dev_user_id=dev_user_id,
                read_only_tools=_read_only,
                reject_m2m=reject_m2m,
            )

        # Billing
        self.billing = StripeBilling(
            stripe_secret_key=stripe_secret_key or _env("STRIPE_SECRET_KEY"),
            price_id=stripe_price_id or _env("STRIPE_PRICE_ID"),
            meter_event=stripe_meter_event or _env("STRIPE_METER_EVENT", "mcp_tool_calls"),
            free_credits=_free,
            tool_costs=tool_costs or {},
            read_only_tools=_read_only,
            success_url=billing_success_url or _env("BILLING_SUCCESS_URL"),
            cancel_url=billing_cancel_url or _env("BILLING_CANCEL_URL"),
            buy_url=billing_buy_url or _env("BILLING_BUY_URL"),
            portal_configuration_id=(
                stripe_portal_configuration_id
                or _env("STRIPE_PORTAL_CONFIGURATION_ID")
            ),
            credit_packs=credit_packs or [],
            auto_recharge_cooldown_sec=int(
                auto_recharge_cooldown_sec
                or _env("AUTO_RECHARGE_COOLDOWN_SEC", "120")
            ),
            subscription_required=(
                bool(subscription_required)
                or _env("SUBSCRIPTION_REQUIRED") == "1"
                or _env("BILLING_MODE").strip().lower()
                in {"subscription", "subscription_required", "all_access"}
            ),
            subscription_plan_name=(
                subscription_plan_name or _env("SUBSCRIPTION_PLAN_NAME", "Pro")
            ),
            subscription_price_label=(
                subscription_price_label or _env("SUBSCRIPTION_PRICE_LABEL")
            ),
            subscription_allowed_statuses=subscription_allowed_statuses,
        )

        # MongoDB
        self._mongodb_uri = mongodb_uri or _env("MONGODB_URI")
        self._db_name = db_name or _env("DB_NAME", self.product_name)
        self._db: Any = None  # set in connect() or injected directly

        # Logging
        self.tool_logger = ToolLogger(
            db=None,  # set after connect()
            product_name=self.product_name,
        )

        # Health
        self.health = HealthCheck(product_name=self.product_name)

        # MCP OAuth config
        if _auth_provider == "supabase":
            self._mcp_app_id = (
                mcp_supabase_client_id
                or _env("MCP_SUPABASE_CLIENT_ID")
            )
            self._mcp_app_secret = (
                mcp_supabase_client_secret
                or _env("MCP_SUPABASE_CLIENT_SECRET")
            )
        else:
            self._mcp_app_id = mcp_logto_app_id or _env("MCP_LOGTO_APP_ID")
            self._mcp_app_secret = (
                mcp_logto_app_secret or _env("MCP_LOGTO_APP_SECRET")
            )
        self._webhook_secret = stripe_webhook_secret or _env("STRIPE_WEBHOOK_SECRET")
        self._oauth_scopes = oauth_scopes
        self._branded_sign_in_url = (
            branded_sign_in_url or _env("BRANDED_SIGN_IN_URL", "")
        )

        # Real DCR via Logto Management API (optional). If mgmt creds are
        # provided, every /oauth/register call creates a fresh Logto app with
        # the caller's redirect_uris baked in — fixing the dynamic-port
        # loopback case that fake DCR can't handle.
        _mgmt_id = logto_mgmt_app_id or _env("LOGTO_MGMT_APP_ID")
        _mgmt_secret = logto_mgmt_app_secret or _env("LOGTO_MGMT_APP_SECRET")
        self.dcr: Optional[LogtoDCR] = None
        if (
            _auth_provider == "logto"
            and _mgmt_id
            and _mgmt_secret
            and getattr(self.auth, "endpoint", "")
        ):
            self.dcr = LogtoDCR(
                logto_endpoint=self.auth.endpoint,
                mgmt_app_id=_mgmt_id,
                mgmt_app_secret=_mgmt_secret,
                mgmt_api_resource=(
                    logto_mgmt_api_resource
                    or _env("LOGTO_MGMT_API_RESOURCE", "")
                ),
                mgmt_token_endpoint=(
                    logto_mgmt_token_endpoint
                    or _env("LOGTO_MGMT_TOKEN_ENDPOINT", "")
                ),
                app_name_prefix=f"mcp-dcr-{self.product_name}",
            )

    # ── Database ──────────────────────────────────────────

    @property
    def db(self) -> Any:
        return self._db

    @db.setter
    def db(self, value: Any) -> None:
        self._db = value
        self.tool_logger.db = value

    async def connect_db(self) -> Any:
        """Connect to MongoDB using configured URI. Returns the database."""
        if not self._mongodb_uri:
            logger.warning("[mcp-core] No MONGODB_URI — running without DB")
            return None
        import motor.motor_asyncio

        client = motor.motor_asyncio.AsyncIOMotorClient(self._mongodb_uri)
        self.db = client[self._db_name]
        logger.info("[mcp-core] Connected to MongoDB: %s", self._db_name)
        return self.db

    # ── Main middleware ─────���──────────────────────────────

    async def auth_and_bill(
        self, request: Request, tool_name: str
    ) -> Dict[str, Any]:
        """Combined auth + billing check. The main entry point for tool handlers.

        Returns user dict. Raises HTTPException on auth/billing failure.

        On paid-tool calls the post-deduction billing result is attached to
        the returned user as ``user["_billing"]`` = ``{"cost", "source",
        "remaining_credits"}``. Tool handlers that want to surface a usage
        block in their response can read it directly; callers that don't
        care keep working unchanged (non-breaking addition).
        """
        user = await self.auth.require_auth(request, tool_name, self.db)
        if user is None:
            # Read-only tool, no auth provided
            return {
                "auth_user_id": "anonymous",
                "logto_user_id": "anonymous",
                "free_credits": 0,
                "credits_used": 0,
            }
        billing_info = await self.billing.check_and_deduct(
            self.db, user, tool_name, request
        )
        if billing_info is not None:
            user["_billing"] = billing_info
        return user

    # ── Logging shortcut ──────────────────────────────────

    async def log_tool_call(
        self,
        request: Request,
        tool: str,
        user: Optional[Dict[str, Any]] = None,
        duration_ms: int = 0,
        status: str = "ok",
        error: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log a tool call to the audit trail."""
        user_id = user_identity(user)
        cost = self.billing.get_tool_cost(tool)
        await self.tool_logger.log(
            request=request,
            tool=tool,
            user_id=user_id,
            duration_ms=duration_ms,
            status=status,
            cost=cost,
            error=error,
            meta=meta,
        )

    # ── FastAPI integration ───────────────────────────────

    def install_routes(self, app: FastAPI, *, billing_routes: bool = True) -> None:
        """Register standard routes: /health, /api/billing/credits, webhook, OAuth metadata."""
        install_routes(app, self, billing_routes=billing_routes)

    def mount_mcp(
        self,
        app: FastAPI,
        *,
        name: str,
        description: str = "",
        tags=("mcp",),
        legacy_sse: bool = True,
        mount_path_legacy: str = "/mcp",
        mount_path_v2: str = "/mcp/v2",
        instructions: str = "",
        require_auth: bool = True,
        public_discovery: bool = False,
        anonymous_tools: Optional[Iterable[str]] = None,
        image_result_tools: Optional[Iterable[str]] = None,
        ui_widget: Optional[Dict[str, Any]] = None,
        tool_titles: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Mount MCP transports (legacy SSE + stateless HTTP).

        See `mcp_core.mcp_mount.mount_mcp` for full docs. This method
        forwards `self` as `core` so callers don't repeat it.

        ``public_discovery`` + ``anonymous_tools`` open the handshake /
        ``tools/list`` surface (and the named free tools) to anonymous
        callers so directory scanners can enumerate capabilities, while
        paid ``tools/call`` stays gated. Defaults preserve strict auth.
        """
        return mount_mcp(
            app,
            core=self,
            name=name,
            description=description,
            tags=tags,
            legacy_sse=legacy_sse,
            mount_path_legacy=mount_path_legacy,
            mount_path_v2=mount_path_v2,
            instructions=instructions,
            require_auth=require_auth,
            public_discovery=public_discovery,
            anonymous_tools=anonymous_tools,
            image_result_tools=image_result_tools,
            ui_widget=ui_widget,
            tool_titles=tool_titles,
        )

    def mcp_auth_config(self) -> Any:
        """Return an AuthConfig for fastapi-mcp.

        Requires fastapi-mcp to be installed (it's a peer dependency).
        """
        if not self._mcp_app_id:
            return None
        try:
            from fastapi_mcp.types import AuthConfig
        except ImportError:
            from fastapi_mcp import AuthConfig
        if getattr(self.auth, "provider_name", "") == "supabase":
            return AuthConfig(
                issuer=self.auth.auth_base_url,
                oauth_metadata_url=self.auth.oauth_metadata_url,
                authorize_url=self.auth.authorize_url,
                client_id=self._mcp_app_id,
                client_secret=self._mcp_app_secret,
                audience=self.auth.api_resource,
                default_scope=" ".join(
                    self._oauth_scopes
                    or ["openid", "profile", "email"]
                ),
                setup_proxies=True,
                setup_fake_dynamic_registration=True,
            )
        if not getattr(self.auth, "endpoint", ""):
            return None
        return AuthConfig(
            issuer=f"{self.auth.endpoint}/oidc",
            oauth_metadata_url=(
                f"{self.auth.endpoint}/oidc/.well-known/openid-configuration"
            ),
            authorize_url=f"{self.auth.endpoint}/oidc/auth",
            client_id=self._mcp_app_id,
            client_secret=self._mcp_app_secret,
            audience=self.auth.api_resource,
            default_scope=" ".join(
                self._oauth_scopes
                or ["openid", "profile", "email"]
            ),
            setup_proxies=True,
            # Keep fake DCR on so fastapi-mcp advertises `registration_endpoint`
            # in the auth-server metadata (MCP SDK refuses servers without it).
            # When real DCR is enabled, mcp-core's /oauth/register route is
            # registered first via install_routes(), so FastAPI's first-match
            # router dispatches there and fastapi-mcp's fake handler never runs.
            setup_fake_dynamic_registration=True,
        )

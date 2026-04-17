"""
mcp-core: Auth, billing, and logging infrastructure for MCP-first servers.

Usage:
    from mcp_core import MCPCore

    core = MCPCore(
        product_name="voiceforge",
        logto_endpoint="https://fo9pu9.logto.app",
        logto_api_resource="https://api.voiceforge.app",
        mongodb_uri="mongodb+srv://...",
        db_name="voicegen",
        stripe_secret_key="sk_test_...",
        stripe_price_id="price_...",
        stripe_meter_event="voice_tool_calls",
        free_credits=25,
        tool_costs={"browse_voices": 0, "narrate_text": 2},
        read_only_tools={"browse_voices"},
    )

    # In your tool handler:
    user = await core.auth_and_bill(request, "narrate_text")
"""

import logging
import os
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, Request

from .auth import LogtoAuth
from .billing import StripeBilling
from .health import HealthCheck
from .routes import install_routes
from .tool_logging import ToolLogger

__all__ = ["MCPCore", "LogtoAuth", "StripeBilling", "HealthCheck", "ToolLogger"]
__version__ = "0.1.0"

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
        # Logto auth
        logto_endpoint: str = "",
        logto_api_resource: str = "",
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
        # Tools
        tool_costs: Optional[Dict[str, int]] = None,
        read_only_tools: Optional[Set[str]] = None,
        # MCP OAuth
        mcp_logto_app_id: str = "",
        mcp_logto_app_secret: str = "",
        oauth_scopes: Optional[List[str]] = None,
    ):
        def _env(key: str, default: str = "") -> str:
            return os.getenv(f"MCP_CORE_{key}", default)

        self.product_name = product_name or _env("PRODUCT_NAME", "mcp-server")
        _read_only = read_only_tools or set()
        _free = free_credits or int(_env("FREE_CREDITS", "30"))

        # Auth
        self.auth = LogtoAuth(
            endpoint=logto_endpoint or _env("LOGTO_ENDPOINT"),
            api_resource=logto_api_resource or _env("LOGTO_API_RESOURCE"),
            free_credits=_free,
            dev_bypass=dev_auth_bypass or _env("DEV_AUTH_BYPASS") == "1",
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
        self._mcp_app_id = mcp_logto_app_id or _env("MCP_LOGTO_APP_ID")
        self._mcp_app_secret = mcp_logto_app_secret or _env("MCP_LOGTO_APP_SECRET")
        self._webhook_secret = stripe_webhook_secret or _env("STRIPE_WEBHOOK_SECRET")
        self._oauth_scopes = oauth_scopes

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
        """
        user = await self.auth.require_auth(request, tool_name, self.db)
        if user is None:
            # Read-only tool, no auth provided
            return {
                "logto_user_id": "anonymous",
                "free_credits": 0,
                "credits_used": 0,
            }
        await self.billing.check_and_deduct(self.db, user, tool_name, request)
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
        user_id = (user or {}).get("logto_user_id", "")
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

    def install_routes(self, app: FastAPI) -> None:
        """Register standard routes: /health, /api/billing/credits, webhook, OAuth metadata."""
        install_routes(app, self)

    def mcp_auth_config(self) -> Any:
        """Return an AuthConfig for fastapi-mcp.

        Requires fastapi-mcp to be installed (it's a peer dependency).
        """
        if not self.auth.endpoint or not self._mcp_app_id:
            return None
        try:
            from fastapi_mcp.types import AuthConfig
        except ImportError:
            from fastapi_mcp import AuthConfig

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
            setup_fake_dynamic_registration=True,
        )

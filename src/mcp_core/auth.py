"""
Logto JWT validation and user provisioning for MCP-first servers.

Validates JWTs issued by Logto using JWKS endpoint.
Creates user records in MongoDB on first auth (race-condition-safe upsert).
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

__all__ = ["LogtoAuth"]


class LogtoAuth:
    """Logto JWT validation and user provisioning.

    Args:
        endpoint: Logto tenant URL (e.g. "https://fo9pu9.logto.app").
        api_resource: Logto API resource / audience (e.g. "https://api.voiceforge.app").
        free_credits: Credits granted to new users on first auth.
        dev_bypass: Accept "Bearer dev-bypass" as a valid token (local dev only).
        dev_user_id: User ID returned for dev-bypass tokens.
        read_only_tools: Tool names that don't require authentication.
        reject_m2m: Reject machine-to-machine tokens (sub == client_id) for paid tools.
    """

    def __init__(
        self,
        endpoint: str = "",
        api_resource: str = "",
        free_credits: int = 30,
        dev_bypass: bool = False,
        dev_user_id: str = "local-dev-user",
        read_only_tools: Optional[Set[str]] = None,
        reject_m2m: bool = True,
    ):
        self.endpoint = endpoint.rstrip("/") if endpoint else ""
        self.api_resource = api_resource
        self.free_credits = free_credits
        self.dev_bypass = dev_bypass
        self.dev_user_id = dev_user_id
        self.read_only_tools = read_only_tools or set()
        self.reject_m2m = reject_m2m

        self._jwks_client: Optional[PyJWKClient] = None
        self._jwks_last_init: float = 0.0

    # ── JWKS ──────────────────────────────────────────────

    def _get_jwks_client(self) -> Optional[PyJWKClient]:
        if not self.endpoint:
            return None
        # Refresh JWKS client every hour
        if self._jwks_client and (time.time() - self._jwks_last_init) < 3600:
            return self._jwks_client
        jwks_url = f"{self.endpoint}/oidc/jwks"
        try:
            self._jwks_client = PyJWKClient(jwks_url, cache_keys=True)
            self._jwks_last_init = time.time()
            logger.info("[auth] JWKS client initialized: %s", jwks_url)
            return self._jwks_client
        except Exception as e:
            logger.error("[auth] Failed to initialize JWKS client: %s", e)
            return None

    # ── Token extraction ──────────────────────────────────

    @staticmethod
    def _extract_bearer_token(request: Request) -> Optional[str]:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None

    # ── Token validation ──────────────────────────────────

    async def verify_token(self, request: Request) -> Optional[Dict[str, Any]]:
        """Validate JWT from request Authorization header.

        Returns decoded payload if valid, None if no token provided.
        Raises HTTPException(401) if token is invalid/expired.
        """
        token = self._extract_bearer_token(request)
        if not token:
            return None

        # Dev bypass
        if self.dev_bypass and token == "dev-bypass":
            return {"sub": self.dev_user_id, "email": "dev@localhost"}

        jwks_client = self._get_jwks_client()
        if not jwks_client:
            logger.warning("[auth] Auth not configured, allowing request through")
            return {"sub": "anonymous", "dev_mode": True}

        try:
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256", "ES384", "ES512"],
                audience=self.api_resource,
                issuer=f"{self.endpoint}/oidc",
                options={"verify_exp": True},
                leeway=30,
            )
            return payload
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token expired")
        except (jwt.InvalidTokenError, jwt.exceptions.PyJWKClientError) as e:
            raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    # ── User provisioning ─────────────────────────────────

    async def get_or_create_user(
        self, db: Any, token_payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Get or create user record in MongoDB from token payload.

        Uses find_one_and_update with upsert to avoid race conditions.
        Rejects M2M tokens if reject_m2m is True.
        """
        sub = token_payload.get("sub", "")
        client_id = token_payload.get("client_id", "")

        # Reject M2M tokens (sub == client_id means it's an app, not a user)
        if self.reject_m2m and sub and client_id and sub == client_id:
            raise HTTPException(
                status_code=403,
                detail="Machine-to-machine tokens cannot call paid tools. "
                "Use a per-user OAuth token.",
            )

        if db is None:
            return self._ephemeral_user(token_payload)

        if not sub:
            raise HTTPException(status_code=401, detail="Token missing 'sub' claim")

        result = await db["users"].find_one_and_update(
            {"logto_user_id": sub},
            {
                "$setOnInsert": {
                    "logto_user_id": sub,
                    "email": token_payload.get("email", ""),
                    "free_credits": self.free_credits,
                    "credits_used": 0,
                    "stripe_customer_id": None,
                    "stripe_subscription_id": None,
                    "created_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
            return_document=True,  # motor uses True, not ReturnDocument enum
        )
        return result

    def _ephemeral_user(self, token_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Return an in-memory user dict when no DB is available."""
        return {
            "logto_user_id": token_payload.get("sub", "anonymous"),
            "email": token_payload.get("email", ""),
            "free_credits": self.free_credits,
            "credits_used": 0,
            "stripe_customer_id": None,
            "stripe_subscription_id": None,
        }

    # ── Require auth for tool ─────────────────────────────

    async def require_auth(
        self, request: Request, tool_name: str, db: Any = None
    ) -> Optional[Dict[str, Any]]:
        """Validate auth for a tool call.

        Returns user dict for paid tools, None for read-only tools without a token.
        Raises HTTPException(401) if a paid tool is called without valid auth.
        """
        if tool_name in self.read_only_tools:
            payload = await self.verify_token(request)
            if payload and db:
                return await self.get_or_create_user(db, payload)
            return None

        payload = await self.verify_token(request)
        if payload is None:
            raise HTTPException(
                status_code=401,
                detail=f"Authentication required for {tool_name}. "
                "Provide a valid Bearer token.",
            )
        if db is not None:
            return await self.get_or_create_user(db, payload)
        return self._ephemeral_user(payload)

    # ── OAuth metadata ────────────────────────────────────

    def oauth_protected_resource_metadata(
        self, scopes: Optional[list] = None, base_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """RFC 9728 /.well-known/oauth-protected-resource response.

        Args:
            scopes: Supported OAuth scopes.
            base_url: When set (typically from the request), use the server's
                own URL as the authorization server. This is required when
                fastapi-mcp's ``setup_proxies=True`` proxies OAuth routes
                through the server itself.
        """
        if base_url:
            auth_servers = [base_url.rstrip("/")]
        elif self.endpoint:
            auth_servers = [f"{self.endpoint}/oidc"]
        else:
            auth_servers = []

        return {
            "resource": self.api_resource,
            "authorization_servers": auth_servers,
            "scopes_supported": scopes
            or ["openid", "profile", "email"],
            "bearer_methods_supported": ["header"],
        }

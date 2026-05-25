"""
JWT validation and user provisioning for MCP-first servers.

The public auth surface is provider-aware:

- LogtoAuth validates Logto JWTs through JWKS.
- SupabaseAuth validates Supabase access tokens through Supabase Auth.

Both providers normalize users into a shared identity shape so billing,
logging, and downstream apps can move away from Logto-specific storage keys
without breaking existing Logto deployments.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Set

import httpx
import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

__all__ = [
    "BaseAuth",
    "LogtoAuth",
    "SupabaseAuth",
    "auth_user_id",
    "user_identity",
    "user_lookup_filter",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def auth_user_id(provider: str, subject: str) -> str:
    """Return the stable provider-qualified user id stored by mcp-core."""
    provider = (provider or "").strip()
    subject = (subject or "").strip()
    if not provider or not subject:
        return subject
    return f"{provider}:{subject}"


def user_identity(user: Optional[Dict[str, Any]]) -> str:
    """Return the best stable identity from a mcp-core user dict."""
    if not user:
        return ""
    return (
        user.get("auth_user_id")
        or user.get("logto_user_id")
        or user.get("auth_subject")
        or ""
    )


def user_lookup_filter(user: Dict[str, Any]) -> Dict[str, Any]:
    """Build a MongoDB filter matching a user by any known identity key."""
    clauses = []
    if user.get("_id") is not None:
        clauses.append({"_id": user["_id"]})
    if user.get("auth_user_id"):
        clauses.append({"auth_user_id": user["auth_user_id"]})
    if user.get("logto_user_id"):
        clauses.append({"logto_user_id": user["logto_user_id"]})
    identity = user_identity(user)
    if not clauses and identity:
        clauses.append({"auth_user_id": identity})
    if not clauses:
        clauses.append({"auth_user_id": ""})
    return clauses[0] if len(clauses) == 1 else {"$or": clauses}


def _name_from_claims(claims: Dict[str, Any]) -> str:
    email = claims.get("email") or ""
    metadata = claims.get("user_metadata") or {}
    return (
        claims.get("name")
        or claims.get("username")
        or claims.get("user_name")
        or metadata.get("full_name")
        or metadata.get("name")
        or metadata.get("user_name")
        or (email.split("@")[0] if email else "")
    )


def _picture_from_claims(claims: Dict[str, Any]) -> str:
    metadata = claims.get("user_metadata") or {}
    return (
        claims.get("picture")
        or claims.get("avatar_url")
        or metadata.get("avatar_url")
        or metadata.get("picture")
        or ""
    )


class BaseAuth:
    """Shared auth flow for provider-specific token validators."""

    provider_name = "base"
    legacy_user_field = ""

    def __init__(
        self,
        *,
        api_resource: str = "",
        free_credits: int = 30,
        dev_bypass: bool = False,
        dev_user_id: str = "local-dev-user",
        read_only_tools: Optional[Set[str]] = None,
        reject_m2m: bool = True,
    ) -> None:
        self.api_resource = api_resource
        self.free_credits = free_credits
        self.dev_bypass = dev_bypass
        self.dev_user_id = dev_user_id
        self.read_only_tools = read_only_tools or set()
        self.reject_m2m = reject_m2m

    @staticmethod
    def _extract_bearer_token(request: Request) -> Optional[str]:
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token.strip()
        return None

    def _dev_payload(self) -> Dict[str, Any]:
        return {
            "sub": self.dev_user_id,
            "email": "dev@localhost",
            "name": "Local Dev",
            "auth_provider": self.provider_name,
            "dev_bypass": True,
        }

    async def verify_token(self, request: Request) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def _normalize_claims(self, token_payload: Dict[str, Any]) -> Dict[str, Any]:
        sub = token_payload.get("sub", "")
        provider = token_payload.get("auth_provider") or self.provider_name
        normalized = {
            "auth_provider": provider,
            "auth_subject": sub,
            "auth_user_id": token_payload.get("auth_user_id")
            or auth_user_id(provider, sub),
            "email": token_payload.get("email", ""),
            "name": _name_from_claims(token_payload),
            "picture": _picture_from_claims(token_payload),
        }
        if self.legacy_user_field:
            normalized[self.legacy_user_field] = sub
        return normalized

    async def get_or_create_user(
        self, db: Any, token_payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Get or create a MongoDB user record from provider-normalized claims."""
        sub = token_payload.get("sub", "")
        client_id = token_payload.get("client_id", "")

        if self.reject_m2m and sub and client_id and sub == client_id:
            raise HTTPException(
                status_code=403,
                detail="Machine-to-machine tokens cannot call paid tools. "
                "Use a per-user OAuth token.",
            )

        if not sub:
            raise HTTPException(status_code=401, detail="Token missing 'sub' claim")

        profile = self._normalize_claims(token_payload)

        if db is None:
            return self._ephemeral_user(profile)

        users = db["users"]
        now = _now()
        set_fields = {
            "auth_provider": profile["auth_provider"],
            "auth_subject": profile["auth_subject"],
            "auth_user_id": profile["auth_user_id"],
            "last_login_at": now,
            "updated_at": now,
        }
        for key in ("email", "name", "picture", self.legacy_user_field):
            if key and profile.get(key):
                set_fields[key] = profile[key]

        set_on_insert = {
            "free_credits": self.free_credits,
            "credits_used": 0,
            "stripe_customer_id": None,
            "stripe_subscription_id": None,
            "created_at": now,
        }

        # Upgrade existing Logto-era records in place before the neutral upsert.
        if self.legacy_user_field and profile.get(self.legacy_user_field):
            existing = await users.find_one(
                {self.legacy_user_field: profile[self.legacy_user_field]}
            )
            if existing is not None:
                await users.update_one({"_id": existing["_id"]}, {"$set": set_fields})
                return await users.find_one({"_id": existing["_id"]})

        result = await users.find_one_and_update(
            {"auth_user_id": profile["auth_user_id"]},
            {"$set": set_fields, "$setOnInsert": set_on_insert},
            upsert=True,
            return_document=True,
        )
        return result

    def _ephemeral_user(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        user = {
            **profile,
            "free_credits": self.free_credits,
            "credits_used": 0,
            "stripe_customer_id": None,
            "stripe_subscription_id": None,
        }
        return user

    async def require_auth(
        self, request: Request, tool_name: str, db: Any = None
    ) -> Optional[Dict[str, Any]]:
        """Validate auth for a tool call.

        Returns a user dict for authenticated calls and ``None`` for anonymous
        read-only calls. Raises HTTPException(401) when auth is required.
        """
        if tool_name in self.read_only_tools:
            payload = await self.verify_token(request)
            if payload and db is not None:
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
        profile = self._normalize_claims(payload)
        return self._ephemeral_user(profile)

    def oauth_protected_resource_metadata(
        self, scopes: Optional[list] = None, base_url: Optional[str] = None
    ) -> Dict[str, Any]:
        if base_url:
            auth_servers = [base_url.rstrip("/")]
        else:
            auth_servers = self.authorization_servers()
        return {
            "resource": self.api_resource,
            "authorization_servers": auth_servers,
            "scopes_supported": scopes or ["openid", "profile", "email"],
            "bearer_methods_supported": ["header"],
        }

    def authorization_servers(self) -> list[str]:
        return []


class LogtoAuth(BaseAuth):
    """Logto JWT validation and user provisioning."""

    provider_name = "logto"
    legacy_user_field = "logto_user_id"

    def __init__(
        self,
        endpoint: str = "",
        api_resource: str = "",
        free_credits: int = 30,
        dev_bypass: bool = False,
        dev_user_id: str = "local-dev-user",
        read_only_tools: Optional[Set[str]] = None,
        reject_m2m: bool = True,
    ) -> None:
        super().__init__(
            api_resource=api_resource,
            free_credits=free_credits,
            dev_bypass=dev_bypass,
            dev_user_id=dev_user_id,
            read_only_tools=read_only_tools,
            reject_m2m=reject_m2m,
        )
        self.endpoint = endpoint.rstrip("/") if endpoint else ""
        self._jwks_client: Optional[PyJWKClient] = None
        self._jwks_last_init: float = 0.0

    def _get_jwks_client(self) -> Optional[PyJWKClient]:
        if not self.endpoint:
            return None
        if self._jwks_client and (time.time() - self._jwks_last_init) < 3600:
            return self._jwks_client
        jwks_url = f"{self.endpoint}/oidc/jwks"
        try:
            self._jwks_client = PyJWKClient(jwks_url, cache_keys=True)
            self._jwks_last_init = time.time()
            logger.info("[auth] Logto JWKS client initialized: %s", jwks_url)
            return self._jwks_client
        except Exception as e:
            logger.error("[auth] Failed to initialize Logto JWKS client: %s", e)
            return None

    async def verify_token(self, request: Request) -> Optional[Dict[str, Any]]:
        token = self._extract_bearer_token(request)
        if not token:
            return None

        if self.dev_bypass and token == "dev-bypass":
            return self._dev_payload()

        jwks_client = self._get_jwks_client()
        if not jwks_client:
            logger.warning("[auth] Auth not configured, allowing request through")
            return {
                "sub": "anonymous",
                "email": "",
                "auth_provider": self.provider_name,
                "dev_mode": True,
            }

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
            payload["auth_provider"] = self.provider_name
            return payload
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token expired")
        except (jwt.InvalidTokenError, jwt.exceptions.PyJWKClientError) as e:
            raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    def authorization_servers(self) -> list[str]:
        return [f"{self.endpoint}/oidc"] if self.endpoint else []


class SupabaseAuth(BaseAuth):
    """Supabase Auth access-token validation and user provisioning."""

    provider_name = "supabase"

    def __init__(
        self,
        supabase_url: str = "",
        anon_key: str = "",
        auth_base_url: str = "",
        api_resource: str = "",
        free_credits: int = 30,
        dev_bypass: bool = False,
        dev_user_id: str = "local-dev-user",
        read_only_tools: Optional[Set[str]] = None,
        reject_m2m: bool = True,
        timeout: float = 10.0,
        http_client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.supabase_url = supabase_url.rstrip("/") if supabase_url else ""
        self.auth_base_url = (
            auth_base_url.rstrip("/")
            if auth_base_url
            else f"{self.supabase_url}/auth/v1" if self.supabase_url else ""
        )
        self.anon_key = anon_key
        self.timeout = timeout
        self._client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=self.timeout)
        )
        super().__init__(
            api_resource=api_resource or self.supabase_url,
            free_credits=free_credits,
            dev_bypass=dev_bypass,
            dev_user_id=dev_user_id,
            read_only_tools=read_only_tools,
            reject_m2m=reject_m2m,
        )

    async def verify_token(self, request: Request) -> Optional[Dict[str, Any]]:
        token = self._extract_bearer_token(request)
        if not token:
            return None

        if self.dev_bypass and token == "dev-bypass":
            return self._dev_payload()

        if not self.auth_base_url or not self.anon_key:
            raise HTTPException(
                status_code=503,
                detail="Supabase auth is not configured",
            )

        try:
            async with self._client_factory() as client:
                resp = await client.get(
                    f"{self.auth_base_url}/user",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "apikey": self.anon_key,
                    },
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503,
                detail="Auth provider unavailable",
            ) from exc

        if resp.status_code in {401, 403}:
            raise HTTPException(
                status_code=401,
                detail="Invalid access token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=503,
                detail="Auth provider unavailable",
            )

        payload = self._supabase_claims(resp.json())
        if not payload.get("sub"):
            raise HTTPException(
                status_code=401,
                detail="Invalid access token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        payload["auth_provider"] = self.provider_name
        return payload

    @staticmethod
    def _supabase_claims(user: Dict[str, Any]) -> Dict[str, Any]:
        metadata = user.get("user_metadata") or {}
        app_metadata = user.get("app_metadata") or {}
        email = user.get("email") or metadata.get("email") or ""
        return {
            "sub": user.get("id") or user.get("sub"),
            "email": email,
            "name": (
                metadata.get("full_name")
                or metadata.get("name")
                or metadata.get("user_name")
                or (email.split("@")[0] if email else "")
            ),
            "picture": metadata.get("avatar_url") or metadata.get("picture") or "",
            "provider": app_metadata.get("provider") or "",
            "providers": app_metadata.get("providers") or [],
            "user_metadata": metadata,
            "app_metadata": app_metadata,
        }

    def authorization_servers(self) -> list[str]:
        return [self.auth_base_url] if self.auth_base_url else []

    @property
    def oauth_metadata_url(self) -> str:
        return f"{self.auth_base_url}/.well-known/openid-configuration"

    @property
    def authorize_url(self) -> str:
        return f"{self.auth_base_url}/oauth/authorize"

"""
Real RFC 7591 Dynamic Client Registration backed by Logto's Management API.

Logto does not expose its own /oidc/register (confirmed 404 on the token
endpoint). fastapi-mcp's setup_fake_dynamic_registration=True only echoes
back a preconfigured client_id, so clients that use dynamic redirect URIs
(like Claude Code, which opens a random localhost port per session) get
rejected by Logto with invalid_redirect_uri.

This module implements real DCR: on each /oauth/register call it creates a
fresh Logto application via the Management API with the client-supplied
redirect_uris baked in. The returned client_id/secret can then complete
the normal authorize + token flow against Logto directly.

─── On tenant accumulation (deferred cleanup) ──────────────────────────
Every /oauth/register call creates a NEW Logto application — we never
reuse or update an existing one. An MCP client that disconnects and
reconnects produces a new app each time (Claude Code does this whenever
it picks a fresh loopback port). Over weeks of heavy use the tenant can
accumulate hundreds of `<app_name_prefix>: ...` apps.

This is correctness-safe (old apps stay valid for their original clients)
but causes tenant bloat: slower Logto admin UI, and eventual collision
with tenant-level app-count limits on some Logto plans.

No cleanup job is shipped in this module. When it becomes a real problem,
the intended fix is a nightly cron that lists applications via the
Management API and deletes any whose name starts with the product's
`app_name_prefix` and whose `lastSignInAt` (or similar activity marker,
if exposed) is older than ~7 days. Ordering matters: delete inactive
apps only after the client has stopped using them, or the next tool call
will fail auth until the client re-registers. The cron belongs outside
this library — in each product's ops scripts — since the retention
policy is product-specific.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

__all__ = ["LogtoDCR"]


class LogtoDCR:
    def __init__(
        self,
        logto_endpoint: str,
        mgmt_app_id: str,
        mgmt_app_secret: str,
        mgmt_api_resource: str = "",
        app_name_prefix: str = "mcp-dcr",
        timeout: float = 10.0,
        http_client_factory: Optional[Any] = None,
    ) -> None:
        if not logto_endpoint or not mgmt_app_id or not mgmt_app_secret:
            raise ValueError(
                "LogtoDCR requires logto_endpoint, mgmt_app_id, mgmt_app_secret"
            )
        self.endpoint = logto_endpoint.rstrip("/")
        self.mgmt_app_id = mgmt_app_id
        self.mgmt_app_secret = mgmt_app_secret
        self.mgmt_api_resource = mgmt_api_resource or f"{self.endpoint}/api"
        self.app_name_prefix = app_name_prefix
        self.timeout = timeout
        self._client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=self.timeout)
        )
        self._token: Optional[str] = None
        self._token_exp: float = 0.0
        self._token_lock = asyncio.Lock()

    async def _fetch_mgmt_token(self) -> str:
        async with self._client_factory() as client:
            resp = await client.post(
                f"{self.endpoint}/oidc/token",
                auth=(self.mgmt_app_id, self.mgmt_app_secret),
                data={
                    "grant_type": "client_credentials",
                    "resource": self.mgmt_api_resource,
                    "scope": "all",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            logger.error(
                "[dcr] Management token fetch failed: %s %s",
                resp.status_code, resp.text[:500],
            )
            raise HTTPException(502, "DCR backend token error")
        data = resp.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 3600)) - 60
        return self._token

    async def _get_token(self, force: bool = False) -> str:
        async with self._token_lock:
            if not force and self._token and time.time() < self._token_exp:
                return self._token
            return await self._fetch_mgmt_token()

    async def _create_app(self, token: str, payload: Dict[str, Any]) -> httpx.Response:
        async with self._client_factory() as client:
            return await client.post(
                f"{self.endpoint}/api/applications",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

    async def register(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Create a fresh Logto app for this DCR request.

        Accepts an RFC 7591 request body and returns an RFC 7591 response.
        Raises HTTPException on validation or upstream failure.
        """
        redirect_uris = body.get("redirect_uris")
        if not redirect_uris or not isinstance(redirect_uris, list):
            raise HTTPException(400, "redirect_uris is required")

        client_name = body.get("client_name") or "MCP Client"
        auth_method = body.get("token_endpoint_auth_method", "none")
        # Public (PKCE) clients → Native. Confidential clients → Traditional.
        app_type = "Native" if auth_method == "none" else "Traditional"

        payload = {
            "name": f"{self.app_name_prefix}: {client_name}",
            "type": app_type,
            "oidcClientMetadata": {
                "redirectUris": redirect_uris,
                "postLogoutRedirectUris": [],
            },
        }

        token = await self._get_token()
        resp = await self._create_app(token, payload)
        if resp.status_code == 401:
            # Stale token — force refresh and retry once.
            token = await self._get_token(force=True)
            resp = await self._create_app(token, payload)
        if resp.status_code not in (200, 201):
            logger.error(
                "[dcr] Logto app creation failed: %s %s",
                resp.status_code, resp.text[:500],
            )
            raise HTTPException(502, f"DCR upstream error: {resp.status_code}")

        app = resp.json()
        now = int(time.time())
        result: Dict[str, Any] = {
            "client_id": app["id"],
            "client_id_issued_at": now,
            "redirect_uris": redirect_uris,
            "grant_types": body.get(
                "grant_types", ["authorization_code", "refresh_token"]
            ),
            "response_types": body.get("response_types", ["code"]),
            "token_endpoint_auth_method": auth_method,
            "client_name": app.get("name", client_name),
        }
        if app_type != "Native" and app.get("secret"):
            result["client_secret"] = app["secret"]
            result["client_secret_expires_at"] = 0
        return result

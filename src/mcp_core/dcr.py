"""
Real RFC 7591 Dynamic Client Registration backed by Logto's Management API.

Logto does not expose its own /oidc/register (confirmed 404 on the token
endpoint). fastapi-mcp's setup_fake_dynamic_registration=True only echoes
back a preconfigured client_id, so clients that use dynamic redirect URIs
(like Claude Code, which opens a random localhost port per session) get
rejected by Logto with invalid_redirect_uri.

This module implements real DCR: /oauth/register creates or reuses a Logto
application via the Management API with the client-supplied redirect_uris
baked in. The returned client_id/secret can then complete the normal
authorize + token flow against Logto directly.

Native (public/PKCE) clients with the same product prefix + client_name are
reused. Loopback redirect URIs are replaced with the current request's set
so random Claude/Cursor ports do not accumulate. Non-loopback URIs are kept.
Traditional (confidential) clients still create a new app because the
client_secret is only returned at creation time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

__all__ = ["LogtoDCR"]

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _is_loopback(uri: str) -> bool:
    host = (urlparse(str(uri or "")).hostname or "").lower()
    return host in _LOOPBACK_HOSTS


def merge_redirect_uris(existing: List[str], incoming: List[str]) -> List[str]:
    """Keep non-loopback URIs; replace loopback URIs with this request's set."""
    existing = [str(uri) for uri in (existing or []) if uri]
    incoming = [str(uri) for uri in (incoming or []) if uri]
    kept = [uri for uri in existing if not _is_loopback(uri)]
    incoming_other = [uri for uri in incoming if not _is_loopback(uri)]
    incoming_loopback = [uri for uri in incoming if _is_loopback(uri)]
    merged: List[str] = []
    for uri in kept + incoming_other + incoming_loopback:
        if uri not in merged:
            merged.append(uri)
    return merged


class LogtoDCR:
    def __init__(
        self,
        logto_endpoint: str,
        mgmt_app_id: str,
        mgmt_app_secret: str,
        mgmt_api_resource: str = "",
        mgmt_token_endpoint: str = "",
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
        # In Logto Cloud, Management-API tokens are issued on the same host
        # as user-facing OIDC. In OSS self-hosted, the admin tenant runs on a
        # separate endpoint (ADMIN_ENDPOINT env var) and only that tenant's
        # /oidc/token will accept the M2M credentials provisioned for the
        # Management API role. Callers pass the admin URL explicitly in that
        # case; otherwise we default to the same host as `endpoint`.
        self.mgmt_token_endpoint = (
            mgmt_token_endpoint.rstrip("/")
            if mgmt_token_endpoint
            else f"{self.endpoint}/oidc/token"
        )
        self.app_name_prefix = app_name_prefix
        self.timeout = timeout
        self._client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=self.timeout)
        )
        self._token: Optional[str] = None
        self._token_exp: float = 0.0
        self._token_lock = asyncio.Lock()
        self._reuses_by_name = True

    async def _fetch_mgmt_token(self) -> str:
        async with self._client_factory() as client:
            resp = await client.post(
                self.mgmt_token_endpoint,
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

    async def _mgmt_request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        async with self._client_factory() as client:
            resp = await client.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=json,
                params=params,
            )
        if resp.status_code == 401:
            token = await self._get_token(force=True)
            async with self._client_factory() as client:
                resp = await client.request(
                    method,
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=json,
                    params=params,
                )
        return resp

    async def _create_app(self, token: str, payload: Dict[str, Any]) -> httpx.Response:
        return await self._mgmt_request(
            "POST",
            f"{self.endpoint}/api/applications",
            token=token,
            json=payload,
        )

    async def _list_apps(self, token: str) -> List[Dict[str, Any]]:
        apps: List[Dict[str, Any]] = []
        page = 1
        while page <= 50:
            resp = await self._mgmt_request(
                "GET",
                f"{self.endpoint}/api/applications",
                token=token,
                params={"page": page, "page_size": 100},
            )
            if resp.status_code != 200:
                logger.warning(
                    "[dcr] list applications failed: %s %s",
                    resp.status_code, resp.text[:300],
                )
                break
            payload = resp.json()
            batch = payload if isinstance(payload, list) else payload.get("data") or []
            if not isinstance(batch, list):
                break
            apps.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                break
            page += 1
        return apps

    async def _patch_app(self, token: str, app_id: str, payload: Dict[str, Any]) -> httpx.Response:
        return await self._mgmt_request(
            "PATCH",
            f"{self.endpoint}/api/applications/{app_id}",
            token=token,
            json=payload,
        )

    def _registration_result(
        self,
        app: Dict[str, Any],
        *,
        redirect_uris: List[str],
        body: Dict[str, Any],
        auth_method: str,
        app_type: str,
        client_name: str,
    ) -> Dict[str, Any]:
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

    async def register(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Reuse a Native Logto app of the same name, or create one.

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
        app_name = f"{self.app_name_prefix}: {client_name}"

        token = await self._get_token()
        if app_type == "Native":
            try:
                existing = next(
                    (
                        app for app in await self._list_apps(token)
                        if app.get("name") == app_name and app.get("type") == "Native"
                    ),
                    None,
                )
            except Exception:
                logger.exception("[dcr] reuse lookup failed; creating a new app")
                existing = None
            if existing and existing.get("id"):
                current = (
                    (existing.get("oidcClientMetadata") or {}).get("redirectUris")
                    or []
                )
                merged = merge_redirect_uris(current, redirect_uris)
                if merged != list(current):
                    patch = await self._patch_app(
                        token,
                        str(existing["id"]),
                        {
                            "oidcClientMetadata": {
                                "redirectUris": merged,
                                "postLogoutRedirectUris": (
                                    (existing.get("oidcClientMetadata") or {}).get(
                                        "postLogoutRedirectUris"
                                    )
                                    or []
                                ),
                            }
                        },
                    )
                    if patch.status_code not in (200, 204):
                        logger.warning(
                            "[dcr] redirect patch failed: %s %s",
                            patch.status_code, patch.text[:300],
                        )
                    else:
                        existing = patch.json() or existing
                logger.info("[dcr] reusing native app %s (%s)", existing["id"], app_name)
                return self._registration_result(
                    existing,
                    redirect_uris=merged if merged else redirect_uris,
                    body=body,
                    auth_method=auth_method,
                    app_type=app_type,
                    client_name=client_name,
                )

        payload = {
            "name": app_name,
            "type": app_type,
            "oidcClientMetadata": {
                "redirectUris": redirect_uris,
                "postLogoutRedirectUris": [],
            },
        }
        resp = await self._create_app(token, payload)
        if resp.status_code not in (200, 201):
            logger.error(
                "[dcr] Logto app creation failed: %s %s",
                resp.status_code, resp.text[:500],
            )
            raise HTTPException(502, f"DCR upstream error: {resp.status_code}")

        app = resp.json()
        return self._registration_result(
            app,
            redirect_uris=redirect_uris,
            body=body,
            auth_method=auth_method,
            app_type=app_type,
            client_name=client_name,
        )

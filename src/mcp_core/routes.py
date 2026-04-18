"""
Standard routes that every MCP-first server needs.

install_routes(app, core) adds:
  GET  /health
  GET  /api/billing/credits
  POST /api/stripe/webhook
  GET  /.well-known/oauth-protected-resource
  + RFC 6749 error-shape enforcement on all OAuth/.well-known paths
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

__all__ = ["install_routes", "install_oauth_error_handler"]


_OAUTH_PATH_PREFIXES = (
    "/oauth/",
    "/.well-known/oauth-",
    "/.well-known/openid-",
)

# Map HTTP status -> RFC 6749 error code.
_OAUTH_ERROR_CODES = {
    400: "invalid_request",
    401: "invalid_client",
    403: "access_denied",
    404: "invalid_request",
    405: "invalid_request",
    422: "invalid_request",
}


def _is_oauth_path(path: str) -> bool:
    return any(path.startswith(p) for p in _OAUTH_PATH_PREFIXES)


def install_oauth_error_handler(app: FastAPI) -> None:
    """Reshape every 4xx/5xx on OAuth-scoped paths to RFC 6749 format.

    FastAPI's default error body is {"detail": "..."} which breaks strict OAuth
    clients (the MCP SDK parses with Zod expecting {"error": "...", ...}).
    This handler intercepts HTTPExceptions raised on /oauth/* and .well-known
    paths and emits the spec-correct shape regardless of which code path
    produced the error (including router-level 405s).
    """

    @app.exception_handler(StarletteHTTPException)
    async def _oauth_http_handler(request: Request, exc: StarletteHTTPException):
        if not _is_oauth_path(request.url.path):
            detail = exc.detail if exc.detail is not None else ""
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": detail},
                headers=dict(exc.headers or {}),
            )
        code = _OAUTH_ERROR_CODES.get(
            exc.status_code,
            "server_error" if exc.status_code >= 500 else "invalid_request",
        )
        desc = (
            exc.detail if isinstance(exc.detail, str) else str(exc.detail or "")
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": code, "error_description": desc},
            headers=dict(exc.headers or {}),
        )


def install_routes(app: FastAPI, core: Any) -> None:
    """Register standard infrastructure routes on a FastAPI app."""
    install_oauth_error_handler(app)

    # Real DCR via Logto Management API — registered BEFORE fastapi-mcp mounts
    # its proxies, so when setup_fake_dynamic_registration=False the only
    # /oauth/register route is this one.
    if getattr(core, "dcr", None) is not None:
        @app.post("/oauth/register")
        async def oauth_register(request: Request):
            try:
                body = await request.json()
            except Exception:
                body = {}
            return await core.dcr.register(body)

    @app.get("/health")
    async def health():
        return await core.health.run()

    @app.get("/api/billing/credits")
    async def get_credits(request: Request):
        payload = await core.auth.verify_token(request)
        if payload is None:
            from fastapi import HTTPException
            raise HTTPException(401, "Authentication required")
        user = await core.auth.get_or_create_user(core.db, payload)
        return core.billing.credits_summary(user)

    @app.post("/api/stripe/webhook")
    async def stripe_webhook(request: Request):
        return await core.billing.handle_webhook(
            request, core.db, core._webhook_secret
        )

    @app.get("/.well-known/oauth-protected-resource")
    async def oauth_metadata(request: Request):
        # When MCP OAuth proxy is configured, point authorization_servers
        # to this server's own URL so MCP clients discover the proxied
        # OAuth routes (setup_proxies=True in fastapi-mcp).
        base_url = None
        if core._mcp_app_id:
            base = str(request.base_url).rstrip("/")
            proto = request.headers.get("x-forwarded-proto")
            if proto and base.startswith("http://"):
                base = f"{proto}://{base[7:]}"
            base_url = base
        return core.auth.oauth_protected_resource_metadata(
            scopes=core._oauth_scopes,
            base_url=base_url,
        )

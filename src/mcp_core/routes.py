"""
Standard routes that every MCP-first server needs.

install_routes(app, core) adds:
  GET  /health
  GET  /api/billing/credits
  POST /api/stripe/webhook
  GET  /.well-known/oauth-protected-resource
"""

from typing import Any

from fastapi import FastAPI, Request

__all__ = ["install_routes"]


def install_routes(app: FastAPI, core: Any) -> None:
    """Register standard infrastructure routes on a FastAPI app."""

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

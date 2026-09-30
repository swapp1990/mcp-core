"""One account across products: delete it in Logto, and purge each product's data when Logto says so.

A product deletes the shared Logto user (``delete_logto_user``); Logto then sends its
``User.Deleted`` webhook to every product's ``install_account_deletion_webhook`` route,
and each one removes its own records for that ``logto:<sub>``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import FastAPI, HTTPException, Request

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "logto-signature-sha-256"

OnDeleted = Callable[[str, Any], Awaitable[None]]


def verify_logto_signature(body: bytes, signature: str, signing_key: str) -> bool:
    """Logto signs each webhook body with HMAC-SHA256 using the hook's signing key."""
    if not signature or not signing_key:
        return False
    expected = hmac.new(signing_key.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def deleted_user_id(payload: Dict[str, Any]) -> str:
    """The deleted user's id. User.Deleted sends ``data: null``, so it comes from the Management API context."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    from_path = re.fullmatch(r"/(?:api/)?users/([^/?#]+)", str(payload.get("path") or ""))
    return str(data.get("id") or params.get("userId") or (from_path.group(1) if from_path else ""))


async def purge_user_record(db: Any, sub: str) -> int:
    """Remove the product's own user document, which also holds its credits and Stripe ids."""
    result = await db["users"].delete_many(
        {"$or": [{"auth_user_id": f"logto:{sub}"}, {"logto_user_id": sub}]}
    )
    return int(getattr(result, "deleted_count", 0))


async def delete_logto_user(dcr: Any, sub: str) -> bool:
    """Delete the shared Logto user; True if it existed. Every product's webhook then purges its data."""
    if dcr is None:
        raise RuntimeError("Logto Management API is not configured (LOGTO_MGMT_APP_ID / LOGTO_MGMT_APP_SECRET)")
    resp = await dcr._mgmt_request(
        "DELETE", f"{dcr.endpoint}/api/users/{sub}", token=await dcr._get_token()
    )
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return True


def install_account_deletion_webhook(
    app: FastAPI,
    get_db: Callable[[], Any],
    signing_key: str,
    on_deleted: Optional[OnDeleted] = None,
    path: str = "/api/logto/webhook",
) -> None:
    """Receive Logto's User.Deleted hook; ``on_deleted(sub, db)`` removes product data beyond ``users``."""

    @app.post(path, include_in_schema=False)
    async def logto_account_webhook(request: Request) -> Dict[str, Any]:
        body = await request.body()
        if not verify_logto_signature(body, request.headers.get(SIGNATURE_HEADER, ""), signing_key):
            raise HTTPException(status_code=401, detail="Invalid Logto webhook signature")
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            raise HTTPException(status_code=400, detail="Webhook body is not JSON")
        if payload.get("event") != "User.Deleted":
            return {"status": "ignored", "event": payload.get("event")}
        sub = deleted_user_id(payload)
        if not sub:
            return {"status": "ignored", "reason": "no user id"}
        db = get_db()
        removed = await purge_user_record(db, sub) if db is not None else 0
        if on_deleted is not None:
            await on_deleted(sub, db)
        logger.info("[accounts] Logto user %s deleted; removed %d user record(s)", sub, removed)
        return {"status": "ok", "user_records_removed": removed}

"""One account across products: delete it in Logto, and purge each product's data when Logto says so.

A product deletes the shared Logto user (``delete_logto_user``, or the
``install_account_routes`` route); Logto then sends its ``User.Deleted`` webhook to
every product's ``install_account_deletion_webhook`` route, and each one removes its
own records for that ``logto:<sub>``. A ``deleted_accounts`` tombstone stops a
still-valid access token from recreating the ``users`` record afterwards.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional

from fastapi import FastAPI, HTTPException, Request

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "logto-signature-sha-256"
DELETED_ACCOUNTS = "deleted_accounts"

OnDeleted = Callable[[str, Any], Awaitable[None]]
BeforeDelete = Callable[[Dict[str, Any], Any], Awaitable[Optional[Dict[str, Any]]]]
StoreCustomerDeleter = Callable[[str], Awaitable[Optional[bool]]]


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


async def record_deleted_account(db: Any, sub: str, provider: str = "logto") -> None:
    """Tombstone ``<provider>:<sub>``; tokens issued before ``deleted_at`` can no longer create a user record."""
    key = f"{provider}:{sub}"
    await db[DELETED_ACCOUNTS].update_one(
        {"_id": key},
        {"$set": {"sub": sub, "auth_user_id": key, "deleted_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def _delete_store_customer(deleter: Optional[StoreCustomerDeleter], sub: str) -> str:
    if deleter is None:
        return "skipped"
    try:
        deleted = await deleter(sub)
    except Exception as exc:
        logger.warning("[accounts] RevenueCat customer for %s not deleted: %s", sub, exc)
        return "failed"
    if deleted is None:
        return "skipped"
    return "deleted" if deleted else "not_found"


async def purge_account(
    db: Any,
    sub: str,
    *,
    on_deleted: Optional[OnDeleted] = None,
    delete_store_customer: Optional[StoreCustomerDeleter] = None,
) -> Dict[str, Any]:
    """Remove this product's records for ``logto:<sub>``; the tombstone is written first so nothing recreates them."""
    removed = 0
    if db is not None:
        await record_deleted_account(db, sub)
        removed = await purge_user_record(db, sub)
    if on_deleted is not None:
        await on_deleted(sub, db)
    # A store failure is logged and never blocks the purge.
    store_customer = await _delete_store_customer(delete_store_customer, sub)
    return {"user_records_removed": removed, "store_customer": store_customer}


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
    delete_store_customer: Optional[StoreCustomerDeleter] = None,
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
        result = await purge_account(
            get_db(), sub, on_deleted=on_deleted, delete_store_customer=delete_store_customer
        )
        logger.info(
            "[accounts] Logto user %s deleted; removed %d user record(s), store customer %s",
            sub, result["user_records_removed"], result["store_customer"],
        )
        return {"status": "ok", **result}


def install_account_routes(
    app: FastAPI,
    core: Any,
    *,
    path: str = "/api/account/delete",
    before_delete: Optional[BeforeDelete] = None,
    pat_prefixes: Iterable[str] = (),
) -> None:
    """``POST path``: purge this product's data, then delete the shared Logto account.

    Only a real access token for this product may call it: personal access tokens
    (``pat_prefixes``) and machine tokens are refused.
    """
    prefixes = tuple(prefix for prefix in pat_prefixes if prefix)

    @app.post(path)
    async def delete_account(request: Request) -> Dict[str, Any]:
        token = core.auth._extract_bearer_token(request)
        if not token:
            raise HTTPException(status_code=401, detail="Authentication required")
        if prefixes and token.startswith(prefixes):
            raise HTTPException(
                status_code=403,
                detail="Personal access tokens can't delete the account. Sign in on the website or app.",
            )
        # The provider's own verifier: instance-level wrappers that accept product PATs don't apply here.
        payload = await type(core.auth).verify_token(core.auth, request)
        if not payload or not payload.get("sub"):
            raise HTTPException(status_code=401, detail="Authentication required")
        sub = str(payload["sub"])
        if payload.get("client_id") and payload.get("client_id") == sub:
            raise HTTPException(status_code=403, detail="Machine-to-machine tokens can't delete an account.")
        # Nothing is deleted unless the shared account can be deleted afterwards.
        if getattr(core, "dcr", None) is None:
            raise HTTPException(status_code=503, detail="Account identity deletion is not configured.")

        db = core.db
        user = await core.auth.get_or_create_user(db, payload)
        extra = await before_delete(user, db) if before_delete is not None else None
        await core.purge_account(sub)
        try:
            identity_deleted = await core.delete_logto_account(sub)
        except Exception as exc:
            logger.error("[accounts] Logto delete failed for %s after purge: %s", sub, exc)
            raise HTTPException(status_code=502, detail="Could not delete the sign-in identity.") from None
        logger.info("[accounts] account %s deleted by its owner", sub)
        return {**(extra if isinstance(extra, dict) else {}), "deleted": True, "identity_deleted": identity_deleted}

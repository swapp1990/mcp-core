"""Long-lived MCP PAT lifecycle.

Products supply prefix/label; mint/list/revoke and the verify_token wrap
live here so WriteForYou and DesignForYou stop shipping copies.
"""
from __future__ import annotations

import copy
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from .auth import user_identity

DEFAULT_TOKEN_TTL_DAYS = 365
MAX_TOKEN_TTL_DAYS = 3650


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


class PatStore:
    def __init__(
        self,
        db_provider: Callable[[], Any],
        *,
        token_prefix: str,
        token_id_prefix: str = "mpt_",
        default_name: str = "MCP token",
        collection_name: str = "mcp_access_tokens",
    ):
        self._db_provider = db_provider
        self.token_prefix = token_prefix
        self.token_id_prefix = token_id_prefix
        self.default_name = default_name
        self.collection_name = collection_name
        self._memory: dict[str, dict[str, Any]] = {}
        self._indexes_ready = False

    async def _collection(self) -> Any:
        db = self._db_provider()
        if db is None:
            return None
        collection = db[self.collection_name]
        if not self._indexes_ready:
            await collection.create_index("token_hash", unique=True)
            await collection.create_index([("auth_user_id", 1), ("revoked_at", 1)])
            await collection.create_index("expires_at")
            self._indexes_ready = True
        return collection

    def _public(self, doc: dict[str, Any]) -> dict[str, Any]:
        def _iso(value: Any) -> str | None:
            if isinstance(value, datetime):
                return value.isoformat()
            return str(value) if value else None

        return {
            "id": doc.get("_id"),
            "name": doc.get("name", ""),
            "token_prefix": doc.get("token_prefix", ""),
            "created_at": _iso(doc.get("created_at")),
            "expires_at": _iso(doc.get("expires_at")),
            "last_used_at": _iso(doc.get("last_used_at")),
            "revoked_at": _iso(doc.get("revoked_at")),
        }

    async def create_token(self, user: dict[str, Any], *, name: str, expires_in_days: int | None) -> dict[str, Any]:
        now = _utcnow()
        days = max(1, min(int(expires_in_days or DEFAULT_TOKEN_TTL_DAYS), MAX_TOKEN_TTL_DAYS))
        token = f"{self.token_prefix}{secrets.token_urlsafe(36)}"
        token_id = f"{self.token_id_prefix}{uuid4().hex}"
        subject = user.get("auth_subject") or user.get("logto_user_id") or ""
        doc = {
            "_id": token_id,
            "token_hash": _token_hash(token),
            "token_prefix": token[:18],
            "name": (name or self.default_name).strip()[:120],
            "auth_provider": user.get("auth_provider") or "logto",
            "auth_subject": subject,
            "auth_user_id": user_identity(user),
            "email": user.get("email") or "",
            "created_at": now,
            "expires_at": now + timedelta(days=days),
            "last_used_at": None,
            "revoked_at": None,
        }
        if doc["auth_provider"] == "logto" and subject:
            doc["logto_user_id"] = subject
        collection = await self._collection()
        if collection is None:
            self._memory[token_id] = copy.deepcopy(doc)
        else:
            await collection.insert_one(copy.deepcopy(doc))
        return {"token": token, "token_record": self._public(doc)}

    async def verify_token(self, token: str) -> dict[str, Any] | None:
        if not token.startswith(self.token_prefix):
            return None
        token_hash = _token_hash(token)
        collection = await self._collection()
        if collection is None:
            doc = next((copy.deepcopy(item) for item in self._memory.values() if item.get("token_hash") == token_hash), None)
        else:
            doc = await collection.find_one({"token_hash": token_hash})
        if doc is None:
            raise HTTPException(status_code=401, detail="Invalid MCP token")
        if doc.get("revoked_at"):
            raise HTTPException(status_code=401, detail="MCP token has been revoked")
        payload = {
            "sub": doc.get("auth_subject") or "",
            "auth_provider": doc.get("auth_provider") or "logto",
            "auth_subject": doc.get("auth_subject") or "",
            "auth_user_id": doc.get("auth_user_id") or "",
            "email": doc.get("email") or "",
            "mcp_token_id": doc.get("_id") or "",
        }
        if payload["auth_provider"] == "logto" and payload["auth_subject"]:
            payload["logto_user_id"] = payload["auth_subject"]
        return payload


def install_pat_auth(core: Any, store: PatStore) -> None:
    if getattr(core.auth, "_mcp_core_pat_installed", False):
        return
    original = core.auth.verify_token

    async def verify_token_with_pat(request: Request):
        token = core.auth._extract_bearer_token(request)
        if token and token.startswith(store.token_prefix):
            return await store.verify_token(token)
        return await original(request)

    core.auth.verify_token = verify_token_with_pat
    core.auth._mcp_core_pat_installed = True


def pat_router(store: PatStore, *, env_var: str, require_primary: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/mcp_tokens", tags=["mcp-token-management"])

    @router.get("")
    async def list_tokens(request: Request):
        user = await require_primary(request)
        return {"tokens": await store.list_tokens(user)} if hasattr(store, "list_tokens") else {"tokens": []}

    @router.post("")
    async def create_token(request: Request):
        user = await require_primary(request)
        body = await request.json()
        created = await store.create_token(
            user,
            name=str((body or {}).get("name") or store.default_name),
            expires_in_days=(body or {}).get("expires_in_days"),
        )
        created["env_var"] = env_var
        return created

    return router

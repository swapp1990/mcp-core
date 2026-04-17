"""
Audit logging for MCP tool calls.

Writes one document per tool invocation to MongoDB.
Failures are logged but never propagated -- logging must not break tool calls.
"""

import logging
import time
from typing import Any, Dict, Optional

from fastapi import Request

logger = logging.getLogger(__name__)

__all__ = ["ToolLogger"]


class ToolLogger:
    """MCP tool call audit logger.

    Args:
        db: Motor database instance (or None to disable).
        product_name: Product identifier written to every log entry.
        collection: MongoDB collection name for tool logs.
    """

    def __init__(
        self,
        db: Any = None,
        product_name: str = "",
        collection: str = "tool_logs",
    ):
        self.db = db
        self.product_name = product_name
        self.collection = collection

    async def log(
        self,
        request: Request,
        tool: str,
        user_id: str = "",
        duration_ms: int = 0,
        status: str = "ok",
        cost: int = 0,
        error: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Write one audit row. Never raises."""
        if self.db is None:
            return

        doc = {
            "ts": time.time(),
            "product": self.product_name,
            "tool": tool,
            "user_id": user_id,
            "duration_ms": duration_ms,
            "status": status,
            "cost": cost,
            "error": error[:500] if error else "",
            "meta": meta or {},
            "ip": request.client.host if request.client else "",
        }
        try:
            await self.db[self.collection].insert_one(doc)
        except Exception as e:
            logger.warning("[tool_log] Failed to write: %s", e)

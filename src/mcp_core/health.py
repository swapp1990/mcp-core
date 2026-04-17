"""
Health check builder for MCP-first servers.

Standard response shape so monitoring tools can parse any server the same way.
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Tuple, Union

logger = logging.getLogger(__name__)

__all__ = ["HealthCheck"]

# A check is either a sync callable or an async callable.
CheckFn = Union[Callable[[], Any], Callable[[], Awaitable[Any]]]


class HealthCheck:
    """Composable health check builder.

    Usage:
        health = HealthCheck(product_name="voiceforge")
        health.add_check("db", lambda: db.command("ping"))
        health.add_check("tts", lambda: httpx.get(url))

        @app.get("/health")
        async def health_endpoint():
            return await health.run()
    """

    def __init__(self, product_name: str = ""):
        self.product_name = product_name
        self._checks: List[Tuple[str, CheckFn]] = []

    def add_check(self, name: str, fn: CheckFn) -> "HealthCheck":
        """Register a named health check function."""
        self._checks.append((name, fn))
        return self

    async def run(self, timeout: float = 5.0) -> Dict[str, Any]:
        """Execute all checks and return standard health response.

        Each check gets `timeout` seconds before it's marked as failed.
        """
        checks: Dict[str, Dict[str, Any]] = {}
        all_ok = True

        for name, fn in self._checks:
            t0 = time.time()
            try:
                result = fn()
                if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                    await asyncio.wait_for(result, timeout=timeout)
                ms = int((time.time() - t0) * 1000)
                checks[name] = {"ok": True, "ms": ms}
            except asyncio.TimeoutError:
                ms = int((time.time() - t0) * 1000)
                checks[name] = {"ok": False, "ms": ms, "error": "timeout"}
                all_ok = False
            except Exception as e:
                ms = int((time.time() - t0) * 1000)
                checks[name] = {"ok": False, "ms": ms, "error": str(e)[:200]}
                all_ok = False

        return {
            "status": "ok" if all_ok else "degraded",
            "product": self.product_name,
            "checks": checks,
        }

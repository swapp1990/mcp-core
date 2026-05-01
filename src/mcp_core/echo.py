"""Tool-response echo helper.

MCP clients (Claude Code's expanded view, Inspector) render `_meta` as
diagnostic context next to the tool result. Tool handlers call
`with_echo(result, **fields)` to surface the resolved inputs / derived
prompts / chosen provider that the user otherwise can't see — only the
*declared* tool args are visible client-side, not what the server
actually built from them.

Opt-in per call. Pass only what's safe to expose: never raw secrets,
JWTs, or large blobs. Caller decides what's worth showing.
"""
from __future__ import annotations

from typing import Any


def with_echo(result: Any, **echo: Any) -> Any:
    """Merge an `_meta.echo` block into a dict result.

    No-op (returns result unchanged) when the result isn't a dict, so
    callers can wrap unconditionally without type-narrowing. None values
    are dropped to keep the echo terse.
    """
    if not isinstance(result, dict):
        return result
    payload = {k: v for k, v in echo.items() if v is not None}
    if not payload:
        return result
    meta = result.setdefault("_meta", {})
    meta["echo"] = {**meta.get("echo", {}), **payload}
    return result


__all__ = ["with_echo"]

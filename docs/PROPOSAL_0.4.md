# mcp-core 0.4.0 proposal (unreleased)

Status: **proposed, not published.** WriteForYou still pins `mcp-core-auth==0.3.8`.
Do not bump DesignForYou or cut a PyPI release until both consumers agree.

## Why

WriteForYou and DesignForYou each ship ~300 LOC of identical PAT mint/list/revoke
plus a fork of `_apply_tool_titles` because 0.3.8 calls `asyncio.run` under
uvicorn and silently drops tool titles. Billing/tier plumbing is duplicated
the same way.

## Shipped in this working tree (not on PyPI)

- `mcp_core.tokens.PatStore` / `install_pat_auth` / `pat_router` — product
  supplies `token_prefix`, `env_var`, and `default_name`.
- `_apply_tool_titles` lists tools from a dedicated thread when already inside
  a running loop (the WriteForYou loop-safe patch).
- Version string set to `0.4.0` in `pyproject.toml`. **No tag, no publish.**

## Still to land before a real 0.4.0 cut

- PlanCatalog + `require_plan(core, user, "pro")` reading Stripe **and**
  store-billing fields (deletes the `app_store:` fake-Stripe shim).
- RevenueCat adapter + webhook.
- Discovery pack (`server.json` / `connect.md` / `mcp-registry-auth`).
- Cross-provider identity linking (`auth_user_id` canonical).
- Tool-activity middleware (generic; product keeps summarizers).
- One 401 challenge path; billing-route extensibility so products stop
  opting out of `install_routes`.
- Delete `SupabaseAuth` only after DesignForYou confirms Logto-only.
- Consumer smoke: `mount_mcp` inside a live event loop.
- Unreleased-code CI gate + lockstep pin check.
- Fix `docs/integration-guide.md` install name (`mcp-core-auth`).

## Consumer follow-up (WriteForYou, after publish)

Pin `mcp-core-auth==0.4.0` in the same PR that deletes
`mcp_token_auth.py`, `routes/mcp_tokens.py`, and the `_apply_tool_titles_loopsafe`
monkeypatch. DesignForYou is a separate change and is **not** touched here.

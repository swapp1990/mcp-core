# Changelog

## 0.6.0 (unreleased)

Store billing and shared-account deletion ([PROPOSAL_0.6.md](docs/PROPOSAL_0.6.md), part B). Every 0.5.1 API keeps working.

### Added
- `mcp_core.store`: `Plan`, `PlanCatalog`, `PlanState`, `require_plan`, `RevenueCatBilling`, `RevenueCatClient` (REST v1), `RevenueCatError`, `ids_from_env`.
- `MCPCore(plan_catalog=..., revenuecat=...)`, `core.plans`, `core.revenuecat`, `core.install_store_routes(app, sync_path=..., webhook_path=...)`.
- RevenueCat webhook: the configured `Authorization` value, exactly or as `Bearer <value>`, checked with a constant-time compare (401 on mismatch, 503 when unset). Idempotent event log `store_billing_events` keyed `revenuecat:<environment>:<event id>`, which retries failed or stale events. TRANSFER re-reads both sides. `legacy_event_collection` honors ActForYou's `acting_subscription_events` for one release.
- Sandbox allowlist (`sandbox_allowlist=`, default env `REVENUECAT_SANDBOX_ALLOWLIST`): sandbox purchases count only for listed app user ids or emails.
- `on_change(user, state)` after every reconcile, with `state["previous"]` and `state["changed"]`.
- Optional store credits per period (`Plan(credits_per_period=..., period="billing" | "calendar_month")`), granted once through `billing_grant_ids`.
- `core.install_account_routes(app, path="/api/account/delete", before_delete=None, pat_prefixes=())`: refuses PATs, machine tokens and other audiences; runs `before_delete(user, db)`, purges, then deletes the Logto user.
- `core.purge_account(sub)`, `accounts.purge_account`, `accounts.record_deleted_account`, `BaseAuth.check_not_deleted(db, payload)`.
- `User.Deleted` handling also deletes the RevenueCat customer (`DELETE /v1/subscribers/{sub}`) when a secret key is configured. A 404 is fine; a failure is logged.

### Changed
- `get_or_create_user` returns 401 for a token issued before the account's `deleted_accounts` tombstone, instead of recreating the user with fresh free credits. Deletion writes the tombstone.
- `subscription_state` ignores `stripe_subscription_id` values starting with `app_store:`, and grants access from an active store projection (`store_subscription`, or WFY's flat `app_store_*` fields). It also reports `source`. Store subscribers are never metered on Stripe.
- With a catalog, subscription mode gates on `core.plans.resolve(user)`, and `credits_summary` adds `plan`, `plans` and `store_subscription`.

### Fixed
- `checkout.session.completed` (and checkout sync) no longer overwrites `stripe_subscription_price_id` with `None`.

### Compat, removed in 0.7
- The `app_store:` shim reads above.

# Platform contract: one account, separate credits

Every swapp1990 product (WriteForYou, DesignForYou, VideoGen, LetMeActForYou,
SnapForYou, VN Creator, JobsForYou, and any new app) follows these rules. A
person has **one account** across all of them; each product keeps **its own
credits**.

Companion docs: [integration-guide.md](./integration-guide.md) for wiring a
server, [client-auth-matrix.md](./client-auth-matrix.md) for MCP clients.

## Identity

| Rule | Value |
|---|---|
| Identity provider | Self-hosted Logto ([deploy/logto](../deploy/logto/README.md)) |
| Sign-in host (every web and iOS client) | `https://auth.designforyou.swapp1990.org` |
| Token issuer (every backend) | `https://auth.designforyou.swapp1990.org/oidc` |
| User key in every product database | `logto:<sub>` in `users.auth_user_id` |
| Sign-in methods offered | Google, Apple, email (one-time code first, password optional) |
| Name on the Google and Apple sign-in screens | `Swapp1990`, never one product's name |

`auth.swapp1990.org` serves the same tenant, but Logto keeps its session
cookie per host and Google only accepts the designforyou callback. No client
or backend may point at it. mcp-core builds the expected issuer from
`LOGTO_ENDPOINT`, so a backend with the other host rejects every token.

Google and Apple show the name of whatever owns the credential, so both are
owned by a neutral `Swapp1990` identity rather than a product:

- **Google:** one OAuth client in Cloud project `designforyou-auth`, branded
  `Swapp1990` with homepage `https://swapp1990.org/` and privacy policy
  `https://swapp1990.org/privacy/`.
- **Apple:** one Services ID, `com.swapp1990.accounts.siwa` ("Swapp1990"),
  grouped under the App ID `com.swapp1990.accounts`. That App ID must never
  ship: Apple shows the App Store name of the primary App ID once it is live,
  which is how every product showed "LMWFY" while the connector used
  WriteForYou's `com.swapnilsawant.lmwfy.siwa`.

Accounts that share a verified email are linked into one Logto user
(`automaticAccountLinking`). Products never merge users themselves.

## Credits

- **One product, one database.** mcp-core stores credits, subscription state
  and the Stripe customer as fields on `users`, so two products sharing a
  database share one balance per person. `connect_db()` records the owning
  product in `_mcp_core_meta` and logs an error when a second product connects.
- Each product grants its own sign-up credits the first time a person uses it.
- Stripe customers and RevenueCat entitlements are per product.
- All products share one Stripe account, so every purchase mcp-core creates carries
  `product=<product_name>` in its metadata, and a product's webhook ignores events
  tagged for another product. Credits for a purchase are granted once per Stripe
  session or payment intent (`users.billing_grant_ids`), so retried events add nothing.

## Store billing (App Store via RevenueCat)

- Each product has its own RevenueCat project. The iPhone app calls
  `Purchases.logIn(<Logto sub>)`, so the RevenueCat app user id is always the
  signed-in caller's `sub`. The server never takes it from a request body.
- `users.store_subscription` holds the store state, and only
  `core.revenuecat.reconcile(user)` writes it. Reconcile re-reads the subscriber
  from RevenueCat's REST API, so a webhook only says *who* to re-read. No product
  writes Stripe fields for an App Store purchase.
- One `PlanCatalog` per product maps Stripe prices and store entitlements or
  product ids to plans. `require_plan(core, user, "pro")` reads Stripe, store and
  direct grants, and the highest plan wins. A 402 carries `code: subscription_required`,
  `required_tier`, `current_tier` and `upgrade_url`.
- The RevenueCat webhook checks the configured `Authorization` value with a
  constant-time compare, and keeps an idempotent event log
  (`store_billing_events`, keyed `revenuecat:<environment>:<event id>`).
- Sandbox purchases grant access in production only to accounts on
  `REVENUECAT_SANDBOX_ALLOWLIST` (app user ids or emails, for App Review and test
  accounts). Sandbox events for anyone else are logged and ignored.

| Variable | Meaning |
|---|---|
| `REVENUECAT_SECRET_API_KEY` | This product's RevenueCat secret key (v1) |
| `REVENUECAT_WEBHOOK_AUTH` / `REVENUECAT_WEBHOOK_AUTHORIZATION` | `Authorization` value set on the RevenueCat webhook (each product keeps its existing name) |
| `REVENUECAT_SANDBOX_ALLOWLIST` | Comma-separated app user ids or emails whose sandbox purchases count |

## Client behavior

| | Web | iOS (Expo) |
|---|---|---|
| `prompt` | `consent` only. Never `login`: it disables single sign-on | `consent` only |
| Refresh tokens | `offline_access` scope | `offline_access` scope |
| Token storage | `localStorage` (survives closing the tab) | SecureStore |
| Browser session | Shared Logto session, so one sign-in covers every site | Ephemeral, so one app never signs in as another's account |
| Sign-out | Ends the Logto session (`end_session`) | Revokes the refresh token and clears the device |
| Rejected token (401) | Show the signed-out state; never look signed in while calls fail | Same |

Web apps get these defaults from [`@swapp1990/auth-web`](../clients/web/README.md),
and iOS apps from [`@swapp1990/auth-expo`](../clients/expo/README.md), instead of
hand-writing Logto calls. Product sign-in pickers may call Logto with
`direct_sign_in` for a specific method, but must offer all three methods.

## Account deletion

"Delete account" in any product deletes the shared Logto user
(`await core.delete_logto_account(sub)`). Logto's `User.Deleted` webhook fans out to
every product backend, and each backend removes its own data for that `logto:<sub>`:
`core.install_account_deletion_webhook(app, on_deleted=...)` verifies the
`logto-signature-sha-256` header with `LOGTO_WEBHOOK_SIGNING_KEY`, deletes the
product's `users` record and calls `on_deleted(sub, db)` for anything else. Each
product has its own Logto hook pointing at its `/api/logto/webhook`. The UI must say
the account is removed from every swapp1990 product.

- The delete button calls `POST /api/account/delete` on the product host
  (`core.install_account_routes(app, before_delete=...)`). Only a real access token
  for that product may call it: personal access tokens and machine tokens are refused.
  The route purges the product's data first and then deletes the Logto user.
- Deletion also records a `deleted_accounts` tombstone. An access token issued
  before the deletion gets 401 instead of recreating a `users` record with fresh
  free credits.
- When the product has a RevenueCat secret, deletion also deletes the person's
  RevenueCat customer. A 404 is fine. A failure is logged and never blocks the purge.

## Configuration names

Backends pass these to `MCPCore` explicitly:

| Variable | Meaning |
|---|---|
| `LOGTO_ENDPOINT` | Sign-in host above (also sets the expected issuer) |
| `LOGTO_API_RESOURCE` | This product's API resource indicator |
| `LOGTO_APP_ID` | This product's Logto application |
| `LOGTO_MGMT_APP_ID`, `LOGTO_MGMT_APP_SECRET`, `LOGTO_MGMT_TOKEN_ENDPOINT`, `LOGTO_MGMT_API_RESOURCE` | Management API access (DCR, account deletion) |
| `DB_NAME` | This product's own database |
| `LOGTO_WEBHOOK_SIGNING_KEY` | Signing key of this product's Logto `User.Deleted` hook |

## Adding a new product

1. Register its Logto application and API resource with the deploy/logto
   config script, not by hand in the console.
2. Give it its own `DB_NAME`.
3. Pin a released `mcp-core-auth` version from PyPI (not a git commit).
4. Use `@swapp1990/auth-web` for web sign-in, `@swapp1990/auth-expo` for iOS, and the client defaults above elsewhere.

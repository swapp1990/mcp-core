# mcp-core 0.6: iOS sign-in, store billing, Logto as code

Status: **proposal, 2026-09-30.** Nothing here is scheduled. It covers three of the [0.4 "still to land"](./PROPOSAL_0.4.md) items (PlanCatalog + `require_plan`, the RevenueCat adapter, the `app_store:` shim) and adds a shared Expo sign-in package and Logto configuration beyond the sign-in page.

## 1. Goals

1. Both iPhone apps (WriteForYou `lmwfy-ios`, LetMeActForYou `letmeactforyou-ios`) meet the iOS column of the [platform contract](./platform-contract.md#client-behavior) by importing one package, `@swapp1990/auth-expo`, instead of hand-writing Logto calls.
2. App Store (RevenueCat) subscriptions become a first-class mcp-core billing source next to Stripe. One `require_plan(core, user, "pro")` reads both. No product writes fake Stripe fields.
3. Every Logto object a product depends on (apps, redirect URIs, API resources, M2M apps, `User.Deleted` hooks, connectors) lives in a reviewed file, with a `plan`/`apply` tool that never prints secrets and never deletes anything it was not told to.

Out of scope: Android, native Sign in with Apple (see Q9), cross-provider identity linking, RevenueCat API v2.

## 2. Current state

### 2.1 iOS sign-in

| Contract row | WriteForYou iOS (`writeforyou/tracks/writer-ios`) | LetMeActForYou iOS (`directforyou/tracks/letmeactforyou-ios`) |
|---|---|---|
| Client | Hand-written PKCE on `expo-web-browser` + `expo-crypto`, ~570 lines (`src/writeforyou/auth.js:45-572`) | `@logto/rn` 1.2.0 `LogtoProvider` (`App.js:8-13`) |
| Logto app | Native `7vsb81xbjs53sf63hh266`, redirect `lmwfy://callback/` (`src/writeforyou/config.js:7-13`, `auth.js:16-23`) | Native `lwg600ndwbb83omoig9t4`, redirect `letmeactforyou://callback` (`src/logto.js:3-6`) |
| `prompt` | `consent`, explicit (`auth.js:158-159`) | SDK default `consent` (`@logto/rn` `lib/client.js`, `prompt: [Prompt.Consent]`) |
| Ephemeral session | Yes (`auth.js:405-408`) | Yes, explicit (`logto.js:13-16`) |
| `offline_access` | Explicit (`config.js:12`) | Not listed (`logto.js:12`), but `@logto/js` adds it as a reserved scope |
| Token storage | **Plain `AsyncStorage`** (`auth.js:3,14,109-127`). `expo-secure-store` is not a dependency | `@logto/rn` `SecureStorage`: AES key in SecureStore, ciphertext in AsyncStorage |
| Refresh | Single-flight, splits transient from rejected (`auth.js:200-233, 499-545`) | SDK: `getAccessToken` is memoized, so concurrent refreshes share one call |
| API audience | `https://writer.swapp1990.org` (`config.js:11`) | **`https://api.designforyou.app`**, DesignForYou's resource (`logto.js:5`, every profile in `eas.json`). ActForYou accepts it only through a second verifier, `ACTING_NATIVE_LOGTO_API_RESOURCE` (`actforyou/routes/acting_v1.py:30-38, 96-105`) |
| Sign-out | Revokes the refresh token, clears storage (`auth.js:475-493`) | `signOut()` revokes and clears; also RevenueCat `logOut` (`PersonalityProvider.js:482-492`) |
| 401 from the API | **Shows an error, stays signed in** (`src/writeforyou/api.js:106-113, 225`) | **Retries once with the same cached token, then parks the intent as `awaiting_auth`; stays signed in** (`actingApi.js:187-190`, `PersonalityProvider.js:328-332`) |
| Delete account | `POST /api/account/delete` (`api.js:193-195`). The backend deletes the Logto user with its own Management API code (`writer-api/services/api/routes/account.py:14-94`). Copy says "your WriteForYou account" (`WriteForYouApp.js:4835-4849`) | `POST /api/v1/acting/account/deletion` (`actingApi.js:260-268`): an **app-scoped** purge queue plus tombstone (`routes/acting_v1.py:202-224`, `acting/deletion.py:88-124`). **The shared Logto account survives.** Copy says LetMeActForYou data only (`AccountScreen.js:22`) |
| Apple | Web flow through the shared `apple` connector, `direct_sign_in=social:apple` (`auth.js:137-141`) | Same connector through `directSignIn` (`logto.js:24-34`) |
| RevenueCat app user | `Purchases.logIn(<Logto sub>)` (`PurchasesContext.js:90`) | `Purchases.logIn(<Logto sub>)` (`revenuecat.js:58-65`, `PersonalityProvider.js:176`) |
| Over-the-air updates | `expo-updates`, `runtimeVersion: appVersion`; 2.0.0 (50) | **None**: every JS change needs a build; 1.0.0 (18) |

In short: WFY violates the storage and 401 rows. LMAFY violates the audience, 401 and deletion rows. The two apps share no code beyond the Logto host.

### 2.2 Store billing

**WriteForYou** (`writeforyou/tracks/writer-api/services/api`, pinned to `mcp-core-auth==0.4.0`, `requirements.txt:14`):
- The client calls `POST /api/billing/app-store/sync` (`routes/billing.py:167-203`). The server derives the RevenueCat id from the token, never from the client (`billing.py:90-105`), then calls `GET https://api.revenuecat.com/v1/subscribers/{id}` (`billing.py:107-125`).
- The webhook `POST /api/billing/revenuecat/webhook` (`billing.py:205-261`) compares the `Authorization` header, stripping `Bearer ` (`:213-220`), and re-reads the subscriber. It keeps **no event log** and does **no environment check**. It never reads `transferred_from`/`transferred_to`, which is where RevenueCat puts the ids on a TRANSFER event (verify against a sandbox transfer), so a transfer is likely acknowledged as `no_account`. ActForYou would answer the same event with 422 (`revenuecat_webhook.py:24-25`).
- Normalization maps `premium` and `lmwfy_monthly/yearly` to Plus, and Pro only through `REVENUECAT_PRO_*` env vars (`entitlements.py:115-140, 330-342`).
- **The shim** (`entitlements.py:400-419`) writes `stripe_subscription_id = "app_store:<id>"`, `stripe_subscription_status = "active"` and a fake price, so that mcp-core's Stripe-only gate (`billing.py:216-237, 363-398`) lets the user through. `billing.py:131-141` clears it on expiry. Side effect: someone with both a Stripe and an Apple subscription has the real Stripe id overwritten, and loses access when the Apple subscription lapses.
- Tiers come from `resolve_tier` (`entitlements.py:187-231`), with 25 call sites across 6 route files. It includes a Stripe self-heal (`:159-184`) because mcp-core's `checkout.session.completed` handler writes `stripe_subscription_price_id = None` (mcp-core `billing.py:957-972` → `:883-887`).
- There are three env names for one secret (`billing.py:79-88`).

**ActForYou** (`directforyou/tracks/actforyou`, `mcp-core-auth==0.5.1`, no Stripe):
- The webhook `POST /api/webhooks/revenuecat` (`routes/revenuecat_webhook.py:13-44`) does an exact-match `Authorization` compare (`:15-18`). It skips events outside `REVENUECAT_EXPECTED_ENVIRONMENT` (`:26-28`) and keeps an idempotent log in `acting_subscription_events` keyed `{environment}:{event_id}` (`:31-38`).
- Reconcile (`acting/revenuecat.py:21-52`) uses the v1 subscriber API and the entitlement `LetMeActForYou Pro`. It writes `acting_accounts.pro`, and a per-calendar-month take allowance that resets once per `period_key` (`:42-50`, policy in `acting/access.py:31-42`).
- A Pro take needs a projection verified in the last 300 s (`acting/access.py:90-96, 177-178`). The client calls `POST /api/v1/acting/billing/reconcile` (`routes/acting_v1.py:180-188`).

**mcp-core** has Stripe only. `subscription_state` reads `stripe_*` fields and nothing else (`billing.py:216-234`). Grant-once credits exist for Stripe objects: `_grant_credits_once`, `users.billing_grant_ids` (`billing.py:139-156`).

### 2.3 Logto tenant (snapshot refreshed 2026-09-30 after the Phase 2 work)

- 22 applications, plus DCR-created `mcp-dcr-*` apps. Each web product has its own SPA app: DesignForYou Web, VideoGen Web, ActForYou web, JobsForYou Web and Autonomous Writer. Each MCP server has its own `… MCP-DCR` M2M app (DesignForYou, ActForYou, JobsForYou and Writer). The `Autonomous Writer` SPA still lists `lmwfy://callback(/)`.
- 9 API resources, including `https://actforyou.swapp1990.org` ("ActForYou API"), which LMAFY must adopt as its audience. `https://api.designforyou.app` is accepted on the acting API only for shipped build 18.
- **User.Deleted hooks (one per product):**
  - Enabled: ActForYou, DesignForYou, JobsForYou and VideoGen.
  - Created but still disabled: WriteForYou and SnapForYou, until their receivers deploy.
  - Hooks were created in the console/API by hand, so part C must import them.
- **Connectors:** `aws-ses-mail`, `google-universal` and `apple-universal`, plus a leftover **`mock-social-connector`**. The Apple connector uses the neutral Services ID `com.swapp1990.accounts.siwa` (f687a9a) with an empty `scope`, so Apple returns no email and email-based account linking can't match Apple users.
- Tooling: `logto_config.py` can snapshot, but `apply` only diffs the sign-in experience (`logto_config.py:116-144`). The snapshot reads one page of 100 (`:79, :94`). `bootstrap-apps.py` creates objects but never patches them (`:174-189`), and it **prints a new M2M secret to stdout** (`:188, :320`).

## 3. Part A: `@swapp1990/auth-expo`

### 3.1 Decision: wrap `@logto/rn`, don't port WFY's hand-rolled client

`@logto/rn` already gives us PKCE, ephemeral sessions, SecureStore-backed storage, memoized refresh with rotation, and a revoking `signOut`. LMAFY uses it in production today. The package pins the contract options itself, as `auth-web` does over `@logto/browser` (`clients/web/src/index.js:45-147`), so no app can drift. WFY's ~570 lines are replaced, not moved.

### 3.2 API (mirrors auth-web)

```js
import { createAuth } from "@swapp1990/auth-expo";
import { AuthProvider, useAuth } from "@swapp1990/auth-expo/react";

export const auth = createAuth({
  appId: "lwg600ndwbb83omoig9t4",
  resource: "https://actforyou.swapp1990.org",
  redirectUri: "letmeactforyou://callback",
  scopes: [],                                   // product scopes, added to the base set
  deleteAccountUrl: "https://actforyou.swapp1990.org/api/account/delete",
  legacySession: undefined,                     // WFY only, see 3.5
});

await auth.signIn(method?)       // "google" | "apple" | "email" | undefined (Logto page, all three) -> AuthUser
await auth.getAccessToken()      // string; null (and onExpire fires) when Logto rejects the session;
                                 // throws AuthUnavailable on network/5xx, keeping the session
auth.expire()                    // the product API answered 401: clear tokens, fire onExpire
auth.onExpire(listener)          // -> unsubscribe
await auth.authFetch(url, init)  // adds the bearer; a 401 calls expire() and throws AuthExpired
await auth.isAuthenticated(); await auth.getUser()   // AuthUser {id, name, email, picture}
await auth.signOut()             // revoke refresh token + clear SecureStore; no browser round trip
await auth.deleteAccount({ body, headers })          // POST deleteAccountUrl with the bearer, then signOut()
```

`AuthProvider`/`useAuth()` exposes `{ready, signedIn, expired, user, signIn, signOut, getAccessToken, expire, deleteAccount}`, the same shape as `auth-web/react`, so screens gate on `signedIn`, never on "has a token".

Fixed defaults (callers cannot override them): `endpoint = https://auth.designforyou.swapp1990.org`, `prompt: consent`, `preferEphemeralSession: true`, scopes ⊇ `openid profile email offline_access`, `resources: [resource]`. `signIn("email")` sends `firstScreen: identifier:sign_in`, `identifiers: ["email"]`, and social methods send `directSignIn: {method: "social", target}`. These match what both apps send today.

Two behaviours neither app has today:
- **Config fingerprint.** `createAuth` stores `appId|resource|scopes` in SecureStore. When it changes, the app clears its tokens and signs the person out once. The alternative is a refresh token that fails for a new audience (Logto binds refresh tokens to the resources in the original grant; expected `invalid_target`, verify on simulator). This is how LMAFY's audience change reaches users.
- **Cancel is not failure.** When `authSessionResult.type` is `cancel` or `dismiss`, the package throws `AuthCancelled`, not `auth_session_failed`. LMAFY currently can't tell the two apart (`PersonalityProvider.js:426-427`).

`Client` is injectable (as in auth-web) for unit tests and WFY's `LOCAL_PLAYTEST` mode.

### 3.3 Backend pieces (mcp-core, server-only)

- `core.install_account_routes(app, path="/api/account/delete", before_delete=None)`. It authenticates the caller, runs the product's `before_delete(user, db)` (e.g. WFY wipes stories), then calls `core.delete_logto_account(sub)` (`accounts.py:49-59`). It returns `{deleted, identity_deleted}`. Logto's `User.Deleted` hook then fans out to every product, including this one. WFY's hand-rolled Management API code (`routes/account.py:14-66`) goes away.
- Contract update: the iOS column names `@swapp1990/auth-expo` as the way to get these defaults, and "delete account" is always `POST /api/account/delete` on the product host.

### 3.4 Shipping

Same channel as auth-web: `clients/expo`, `npm test && npm pack`, then `gh release create auth-expo-v0.1.0 swapp1990-auth-expo-0.1.0.tgz`. Apps install `https://github.com/swapp1990/mcp-core/releases/download/auth-expo-v0.1.0/swapp1990-auth-expo-0.1.0.tgz`. `package-lock.json` pins the tarball's integrity hash, and EAS fetches it at build time, which needs the repo or release to be public (Q8). Peer dependencies: `@logto/rn ^1.2`, `expo-secure-store`, `expo-web-browser`, `expo-crypto`, `@react-native-async-storage/async-storage`, `react` (optional, for `/react`). The source is plain ESM like auth-web. CI runs `expo export --platform ios` on a fixture app so a Metro/`exports` break fails before an app build does.

### 3.5 Migration per app

**LetMeActForYou, first (smallest diff, and the audience fix is a security issue: today's tokens are valid for DesignForYou's API):**
1. Prerequisites (server only, no build): the resource `https://actforyou.swapp1990.org` already exists. Deploy ActForYou `POST /api/account/delete` with `install_account_routes`, `before_delete` calling today's app-scoped purge.
2. Build 19: replace `LogtoProvider`/`useLogto` with `auth-expo`. `resource` becomes `https://actforyou.swapp1990.org` in `logto.js` and in all three `eas.json` profiles. Route `actingApi.request` through `authFetch`, so the 401 retry at `actingApi.js:187-190` becomes `expire()` and the gate re-opens. `deleteAccount` calls the new route. Copy at `AccountScreen.js:22` says the account is removed from every swapp1990 product.
3. Existing build-18 users keep working, because ActForYou still accepts the old audience. Once build 18's share of `/api/v1/acting` traffic is near zero (log the `aud` claim for a week), remove `ACTING_NATIVE_LOGTO_API_RESOURCE` and the second verifier (`acting_v1.py:30-38, 96-105`).

**WriteForYou:**
1. Optional interim step, OTA to 2.0.0 (JS only): on `WriterApiError.isAuth()` call `signOut()`, and fix the deletion copy. This closes the 401 row without a build.
2. Version 2.0.1: add `@logto/rn`, `expo-secure-store` and `auth-expo`. SecureStore is a native module, so this **needs a new binary**; bump `version` so `runtimeVersion: appVersion` keeps the 2.0.0 OTA channel separate. Delete `auth.js`, keep `useWriterAuth` as a thin adapter over `useAuth` so the 5,000-line `WriteForYouApp.js` barely changes, and route `api.js` through `authFetch`.
3. `legacySession: () => AsyncStorage.getItem("@writeforyou/logto.session.v1")` runs once: it writes the stored refresh and ID tokens into the SDK's storage and removes the plain-text key. Same app id and resource, so the first `getAccessToken` refreshes silently. If the import fails, the person signs in once.
4. The backend (C/B steps) replaces `routes/account.py` with `install_account_routes`. The path is unchanged, so 2.0.0 binaries keep working.

### 3.6 Testing

- **Unit** (`node --test`, fake client, like `clients/web/test/auth.test.js`): the fixed sign-in options per method; scopes always include `offline_access`; dead session → `null` + `onExpire`; transient → throws and keeps tokens; fingerprint change clears; `authFetch` 401 → `expire`; `deleteAccount` sends the bearer, then signs out, and doesn't sign out on a 5xx; `legacySession` imports once and deletes the key.
- **Static check per app** (extend `writer-ios/scripts/verify-logto-auth.js`, add one to LMAFY): the app id, redirect URI and resource in the build config must match `deploy/logto/tenant.json` (Part C). A redirect URI missing in Logto is then caught before an EAS build.
- **Simulator, real Logto** (dev client, `eas build --profile ios-simulator` or `npx expo run:ios`), with Maestro flows (LMAFY already has `e2e/maestro`) on a disposable Logto test user: email one-time code, Google, Apple. Decode the token and assert `aud` is the product resource. Force a refresh with `client.clearAccessToken()`. After sign-out, the old refresh token must fail at `/oidc/token`. A backend 401 must show the signed-out UI. Delete account: the Logto user is gone (Management API `GET /api/users/{id}` → 404), and each product's `users` record is purged by its hook.
- **TestFlight on a real device** before submission, including upgrading from the currently shipped build (LMAFY 18 → 19 signs out once; WFY 2.0.0 → 2.0.1 stays signed in).

## 4. Part B: store billing in mcp-core

### 4.1 Shape

New module `mcp_core/store.py`: `PlanCatalog`, `Plan`, `RevenueCatBilling`, `require_plan`, `install_store_routes`. `MCPCore(..., plan_catalog=..., revenuecat=...)` wires them in. Products pass every setting explicitly (contract: backends pass config to `MCPCore`), so the droplets keep their existing env names.

```python
catalog = PlanCatalog(
    plans=[
        Plan("free", rank=0),
        Plan("plus", rank=1, stripe_price_ids=ids("STRIPE_PLUS_PRICE_ID", "STRIPE_PRICE_ID"),
             store_entitlements={"premium"}, store_product_ids={"lmwfy_monthly", "lmwfy_yearly"}),
        Plan("pro", rank=2, stripe_price_ids=ids("STRIPE_PRO_PRICE_ID")),
    ],
    unknown_paid_plan="plus",               # active subscription on an unmapped price/entitlement
    direct_grants={"pro": ids("WRITER_DIRECT_PRO_USER_IDS")},
)
core = MCPCore(..., plan_catalog=catalog, revenuecat=RevenueCatBilling(
    secret_key=os.getenv("REVENUECAT_SECRET_API_KEY", ""),       # legacy v1 secret key
    webhook_authorization=os.getenv("REVENUECAT_WEBHOOK_AUTH", ""),
    accept_sandbox=True,                                          # App Review buys in sandbox (Q2)
))
core.install_store_routes(app, sync_path="/api/billing/app-store/sync",
                          webhook_path="/api/billing/revenuecat/webhook")
state = await require_plan(core, user, "pro")                    # PlanState or HTTP 402
```

### 4.2 PlanCatalog and `require_plan`

- `core.plans.resolve(user) -> PlanState(plan, rank, source, status, expires_at)`. `source` is one of `stripe` (active status and a mapped `stripe_subscription_price_id`), `store` (`users.store_subscription`), `direct` or `dev` (`DEV_FORCE_TIER` under the dev bypass only). The highest rank wins, so a person with both Stripe Plus and Apple Pro is Pro, and neither source overwrites the other.
- `async require_plan(core, user, name, *, fresh_within_s=None)` raises 402 with WFY's current body (`code: subscription_required`, `legacy_code: pro_required`, `required_tier`, `current_tier`, `upgrade_url`; `entitlements.py:494-509`), so web and iOS clients parse it unchanged. `fresh_within_s` re-verifies a stale store projection first; ActForYou passes 300.
- When a catalog is present, `StripeBilling.check_and_deduct` in subscription mode gates on `resolve(user).rank >= 1` instead of `subscription_state` alone. `credits_summary` adds `plan`, `plans` (the public catalog) and `store_subscription`.
- Stripe fix in the same release: `checkout.session.completed` no longer writes `stripe_subscription_price_id = None` (`billing.py:957-972`). WFY's self-heal (`entitlements.py:159-184`) can then go.
- Compat, removed in 0.7: `subscription_state` ignores `stripe_subscription_id` values starting `app_store:`, and `resolve` reads WFY's flat `app_store_subscription_*` fields when `store_subscription` is absent.

### 4.3 RevenueCat adapter

**Stored state** (one subdocument on the product's own `users` record, keyed like everything else by `auth_user_id = logto:<sub>`): `store_subscription: {provider: "revenuecat", app_user_id, plan, status (active|grace|expired|refunded|none), entitlement_id, product_id, store, environment, period_start, expires_at, will_renew, billing_issue, verified_at}`.

**`app_user_id`** is always the Logto `sub` of the authenticated caller (both apps `Purchases.logIn(sub)`). It is never taken from a request body, as WFY enforces today.

**Client** (`RevenueCatClient`, v1): `GET /v1/subscribers/{id}` with `Authorization: Bearer <secret>`. Both products hold legacy v1 secret keys, which can't call API v2. v2 is a second client behind the same interface later (Q7). Note: the v1 GET creates the customer if it doesn't exist, so only signed-in callers and webhook-matched users trigger a lookup.

**`reconcile(user)`** is the only writer, whether called from client sync, webhook or freshness check:
1. Fetch the subscriber. For each entitlement, active means `expires_date` is null or in the future, or `grace_period_expires_date` is in the future (status `grace`). A `refunded_at` on the matching subscription means `refunded`.
2. Map `(entitlement_id, product_identifier)` through the catalog; keep the highest rank. Record `is_sandbox` as `environment`; when `accept_sandbox=False`, sandbox entitlements are ignored.
3. `$set store_subscription`, using the same upsert shape as the Stripe handlers.
4. If the plan has `credits_per_period`, grant through `_grant_credits_once` with `grant_id = f"revenuecat:{entitlement}:{period_key}"`, where `period_key` is the entitlement's `purchase_date` (`period="billing"`) or `YYYY-MM` (`period="calendar_month"`). The key comes only from the subscriber API, never from an event, so webhook, sync and retries converge on one grant.
5. `on_change(user, before, after)` fires when the plan or status changes (ActForYou uses it, 4.5).

**Webhook** (`install_store_routes`):
1. Accept the configured value exactly, or as `Bearer <value>` (constant-time compare, covering both products' conventions). A wrong or missing value returns 401. An unset value returns 503.
2. `TEST` → 200. `event.id` is required.
3. Event log `store_billing_events`, `_id = "revenuecat:{environment}:{event_id}"`, status `processing|processed|failed`, attempts. A duplicate that was processed returns 200 `duplicate`. A stale `processing` or a `failed` event is retried.
4. Candidates: `app_user_id`, `original_app_user_id`, `aliases`, `transferred_from`, `transferred_to`, minus `$RCAnonymousID:*`. TRANSFER reconciles **both** sides, so an Apple ID restored on a second Logto account removes access from the first.
5. Reconcile each candidate that has a `users` record (unknown ids are acknowledged). Mark `processed`. A RevenueCat or Mongo error marks the event `failed` and returns 500, so RevenueCat retries.

**Account deletion:** the `User.Deleted` handler already removes `users`. Whether to also `DELETE /v1/subscribers/{sub}` in the product's RevenueCat project is Q4.

### 4.4 Per-product configuration

| | WriteForYou | ActForYou |
|---|---|---|
| Plans | free / plus / pro | free / pro |
| Stripe | Plus + Pro prices | none |
| Store → plan | `premium`, `lmwfy_monthly/yearly` → plus; Pro via `REVENUECAT_PRO_*` (unset?) | `LetMeActForYou Pro` → pro |
| Credits per period | none (tier only) | Pro takes: `ACTING_PRO_ALLOWANCE_MODE` + `ACTING_PRO_MONTHLY_TAKE_COUNT`, calendar month |
| Freshness | none | 300 s |
| Sync route (kept) | `POST /api/billing/app-store/sync` | `POST /api/v1/acting/billing/reconcile` |
| Webhook route (kept) | `POST /api/billing/revenuecat/webhook` | `POST /api/webhooks/revenuecat` |
| Secret / webhook env | `REVENUECAT_SECRET_API_KEY` (+2 aliases) / `REVENUECAT_WEBHOOK_AUTH` | `REVENUECAT_SECRET_API_KEY` / `REVENUECAT_WEBHOOK_AUTHORIZATION` |

The routes keep their paths, so the RevenueCat dashboard URLs and the shipped binaries don't change.

### 4.5 Migration without double-granting

**ActForYou (first: already on 0.5.1, one entitlement, no Stripe):**
1. Pin 0.6.0 and configure `RevenueCatBilling(accept_sandbox=True)`. The webhook's environment filter becomes noise reduction only (Q2).
2. Replace `routes/revenuecat_webhook.py` and `acting/revenuecat.py` with `install_store_routes` on the same paths. `billing_reconcile` calls `core.revenuecat.reconcile(user)`.
3. The take allowance stays in `acting_accounts`. `on_change` writes `acting_accounts.pro` and applies today's allowance rule, which resets only when `period_key` changes. No counter moves, so nothing can be granted twice.
4. For one release, the new event log also checks `acting_subscription_events`, so a RevenueCat retry of an already-processed event is a `duplicate`. Re-processing would be harmless anyway, because reconcile is idempotent.
5. Optional later step (Q1): move Pro takes to mcp-core credits (`credits_per_period`, `period="calendar_month"`). Before the switch, seed `billing_grant_ids` with `revenuecat:LetMeActForYou Pro:<current YYYY-MM>` for every account whose `pro_allowance.period_key` is the current month. The first reconcile after the switch then grants nothing extra.

**WriteForYou (0.4.0 → 0.6.0, so it also picks up 0.5's product-tagged Stripe credits and deletion webhook):**
1. Deploy 0.6.0 with the compat reads while WFY's own routes still write the shim. Both paths agree.
2. Switch to `install_store_routes` and `catalog`. `resolve_tier` becomes a one-line wrapper over `core.plans.resolve`, so the 25 call sites stay for now. `require_pro` becomes `require_plan(..., "pro")`. The store now writes `store_subscription` only.
3. Backfill: `py -m mcp_core.store migrate --db autonomous_writer_v2 --dry-run`, then without `--dry-run`. It copies `app_store_*` to `store_subscription`. It unsets `stripe_*` **only where `stripe_subscription_id` starts with `app_store:`**, then re-reconciles each migrated user from RevenueCat. It is idempotent and prints counts only.
4. 0.7 drops the compat reads; WFY deletes `entitlements.py:306-420` and `billing.py:79-261`. There are no store credits in WFY, so "double-granting" here means double or lost access. The highest-rank rule and the prefix-guarded backfill cover both.

### 4.6 Tests

In `tests/test_store.py` (mongomock-motor plus `httpx.MockTransport` for RevenueCat):
- Mapping: `premium` → plus, Pro product → pro, expired, grace, refunded, sandbox with the flag on and off, unmapped active → `unknown_paid_plan`.
- Stripe Plus plus Apple Pro → pro. An Apple sync leaves the `stripe_*` fields byte-identical (the shim bug as a regression test).
- Webhook: 401 on a missing or wrong header, 503 when unconfigured, `TEST` ack, the same event twice → one RevenueCat call, TRANSFER reconciles both ids, a RevenueCat 5xx → 500 and then a successful retry.
- Credits: two webhooks plus one sync for one renewal → one grant. The next period → a second grant. Calendar-month mode. The seeded migration key → no grant.
- `require_plan`: the 402 body matches WFY's current one field for field, and `fresh_within_s` triggers a reconcile when stale.
- Migration: dry-run counts, a second run is a no-op, users with a real Stripe id keep it.
- Consumers: WFY `tests/test_subscription_billing.py` and ActForYou `tests/` pass unchanged against 0.6.0 (same routes, same bodies).
- Live, gated (`tests/live`, `RUN_LIVE_TESTS=1`): a real v1 subscriber read for a sandbox test user in each RevenueCat project.

## 5. Part C: Logto configuration as code

### 5.1 Desired-state file: `deploy/logto/tenant.json` (committed, no secrets)

```json
{
  "version": 1,
  "unmanaged": { "applicationNamePrefixes": ["mcp-dcr-"], "applicationIds": ["m-default"],
                 "resourceIndicators": ["https://default.logto.app/api"] },
  "signInExperience": "sign-in-experience.json",
  "resources": [
    { "indicator": "https://actforyou.swapp1990.org", "name": "ActForYou API",
      "accessTokenTtl": 3600, "scopes": [] }
  ],
  "applications": [
    { "id": "lwg600ndwbb83omoig9t4", "name": "LetMeActForYou iOS (Native)", "type": "Native",
      "redirectUris": ["letmeactforyou://callback", "letmeactforyou://callback/"],
      "postLogoutRedirectUris": ["letmeactforyou://callback", "letmeactforyou://callback/"],
      "customClientMetadata": { "rotateRefreshToken": true, "refreshTokenTtlInDays": 90 } },
    { "name": "ActForYou MCP-DCR", "type": "MachineToMachine",
      "roles": ["Logto Management API access"], "secretEnv": "actforyou:MCP_LOGTO_APP_SECRET" }
  ],
  "hooks": [
    { "name": "ActForYou User.Deleted", "events": ["User.Deleted"], "enabled": true,
      "url": "https://actforyou.swapp1990.org/api/logto/webhook",
      "signingKeyEnv": "actforyou:LOGTO_WEBHOOK_SIGNING_KEY" }
  ],
  "connectors": [
    { "connectorId": "google-universal", "public": { "clientId": "…", "scope": "…", "prompts": ["select_account"] },
      "secretEnv": { "clientSecret": "LOGTO_GOOGLE_CLIENT_SECRET" } }
  ]
}
```

Matching: applications by `id` once they exist (client builds embed the id, so a rename must never create a new app) and by `name` before that. Resources match by `indicator`, hooks by `name`, connectors by `connectorId`. List fields (`redirectUris`, `events`, `scopes`, `roles`) are sets. `secretEnv`/`signingKeyEnv` name *where* a secret goes, as `<product>:<VAR>`, never the value.

### 5.2 Commands (`logto_config.py`, stdlib + httpx, reusing `Mgmt`)

| Command | Does |
|---|---|
| `import --env-file …` (or `--from-snapshot logto-state.json`) | Writes `tenant.json` from live state through a field **allowlist**, skips `unmanaged`, and sorts stably. Acceptance: `plan` right after `import` shows no changes |
| `plan --env-file …` | Prints `+ create`, `~ update field: old → new` and `? unknown in live (kept)`. Secret fields show only `<secret: same>` / `<secret: differs>`, by comparing sha256 of the live value and the env value, never the values themselves. Exit code 2 when changes are pending |
| `apply --env-file … --yes [--prune] [--secrets-out FILE]` | Applies the plan in dependency order: resources → scopes → apps → roles → connectors → hooks → sign-in experience |
| `test-hook NAME` | `POST /api/hooks/{id}/test`, then shows `recent-logs` status codes |
| `snapshot` | Kept as-is, paginated |

Rules:
- **No secrets on stdout, in `tenant.json` or in the snapshot.** Objects are read through per-type allowlists (never a denylist). `secret`, `signingKey`, connector configs outside `public`, and hook `config.headers` are never copied into anything printable.
- A create that returns a secret (M2M app secret, hook signing key) needs `--secrets-out`. `apply` writes `<product>:<VAR>=<value>` there with 0600 permissions (the path must match a gitignored `deploy/logto/*.secrets.env`) and prints only `wrote actforyou:LOGTO_WEBHOOK_SIGNING_KEY (sha256 3f1a…)`. Without the flag, the create is refused. `bootstrap-apps.py` loses its secret printing (`:188, :320`) and becomes a thin `apply` for a fresh tenant.
- **Removals need `--prune`:** a live object absent from the file, and also entries removed from a managed list (a redirect URI, a hook event). Without it they appear under `?`/`-` and are left alone. `unmanaged` objects (DCR clients, `m-default`, the Management API resource) are never deleted, even with `--prune`.
- Hooks are created `enabled: false`. The operator copies the signing key into that product's `.env.prod` and deploys, then a second `apply` enables the hook and `test-hook` checks it. So no hook ever fires at a backend that can't verify it.
- Pagination: `page`/`page_size=100` until the list is exhausted (DCR apps grow with every client install). Logto's image is pinned (`svhd/logto:1.38.0`); the tests replay recorded 1.38 responses.

### 5.3 First desired changes (from the 2026-09-29 snapshot)

1. Create resource `https://actforyou.swapp1990.org` (blocks LMAFY build 19 and ActForYou's MCP audience).
2. Create `ActForYou User.Deleted` (its route is live on 0.5.1, `acting_server.py:97`). Add the WFY, DesignForYou and VideoGen hooks as each backend installs the 0.5+ route.
3. `--prune` candidates, after checking no shipped build uses them: `mock-social-connector`, and `lmwfy://callback(/)` on the `Autonomous Writer` SPA (WFY iOS uses its own Native app).
4. Record `customClientMetadata` (refresh TTL/rotation) for both Native apps, so a change is reviewed (Q10).

### 5.4 Tests

Unit tests with a fake `Mgmt` fed from a fixture tenant. The fixture contains planted secrets (`secret`, `signingKey`, `clientSecret`, `privateKey`, hook headers), and every command's stdout, `tenant.json` and snapshot are asserted not to contain them. Other cases: import→plan is empty; create/update/prune ordering; `--prune` absent means no DELETE call; `unmanaged` is never deleted; secret creation refused without `--secrets-out`; pagination across 3 pages. One live read-only `plan` against the real tenant (gated) before each `apply`.

## 6. Staged plan

| # | Step | Repo | Build? |
|---|---|---|---|
| 1 | C: `import` + paginated `snapshot` + read-only `plan`; commit `tenant.json` | mcp-core | no |
| 2 | C: `apply` for creates only (disabled hook → key → enabled). The ActForYou resource and every product's hook already exist (made by hand on 2026-09-30), so the first `apply` should be a no-op. | mcp-core + droplet env | no |
| 3 | B: mcp-core 0.6.0 (`store.py`, catalog, `require_plan`, compat reads, price-clobber fix, `install_account_routes`); tag `v0.6.0`, PyPI | mcp-core | no |
| 4 | B: ActForYou pins 0.6.0, store routes on the same paths, `on_change` keeps the allowance; adds `POST /api/account/delete` | actforyou | no |
| 5 | A: `auth-expo` 0.1.0 + tests; release `auth-expo-v0.1.0` | mcp-core | no |
| 6 | A: LMAFY build 19: auth-expo, audience `actforyou.swapp1990.org`, 401 → signed out, shared-account deletion + copy | letmeactforyou-ios | **yes** (no OTA in this app) |
| 7 | A (optional): WFY OTA to 2.0.0: 401 → sign out, deletion copy | lmwfy-ios | no (EAS Update) |
| 8 | B: WFY pins 0.6.0 (from 0.5.1, which is live with its deletion webhook since autonomous-writer #33), store routes, backfill | writer-api | no |
| 9 | A: WFY 2.0.1: auth-expo + SecureStore + legacy-session import | lmwfy-ios | **yes** (new native module) |
| 10 | Clean-up: drop `ACTING_NATIVE_LOGTO_API_RESOURCE` once build 18 traffic is gone; C `--prune` pass; 0.7 drops the compat reads and the WFY shim code | all | no |

Steps 1–5 are independent of each other except 3 → 4 and 2 → 6. Each product repin follows the CLAUDE.md release checklist (bump, tag, PyPI, repin, deploy).

## 7. Risks

- **The audience change signs LMAFY users out once** (refresh tokens are bound to the old resource). This is intended, via the fingerprint, and step 10 waits on real traffic, not on a date.
- **WFY session import may not work** with `@logto/rn` storage internals. The fallback is one sign-in; the import path is covered on the simulator before submission.
- **Backfill ordering:** stopping shim writes before the compat read is deployed would cut Apple subscribers off. The order is fixed in 4.5, and the migrate tool refuses to run unless the connected product's `users` has a `_mcp_core_meta` owner, and prints counts first.
- **Sandbox purchases grant production access** when `accept_sandbox=True`. App Review needs this, but so does anyone with a TestFlight build (Q2).
- **Deleting from one app deletes everywhere.** LMAFY build 18 promises app-only deletion, so its old endpoint must keep that meaning until build 19's copy ships (Q3).
- **v1 API lifetime:** the v1 GET creates customers and v1 may be retired. It is isolated behind `RevenueCatClient`.
- **Hook secrets:** a signing key written to the wrong file, or a hook enabled before the backend has its key. Covered by `--secrets-out` path checks and create-disabled-then-enable.
- **Tarball distribution:** EAS must reach the release asset, and a re-uploaded asset under the same tag fails lockfile integrity. Tags are immutable; bump the version instead.

## 8. Open questions for the owner

**Decided 2026-09-30 (Swapnil):**
- **Q1:** ActForYou's Pro allowance stays in `acting_accounts`; mcp-core only answers "is this person Pro".
- **Q2:** sandbox entitlements count in production only for an allowlist of reviewer and test accounts.
- **Q3:** "Delete account" in any app deletes the one shared account (decided at the start of the unification).
- **Q4:** on `User.Deleted`, each product also deletes the person's RevenueCat customer.
- **Q6:** the Apple connector now requests `name email` (applied 2026-09-30).
- **Q8:** `swapp1990/mcp-core` is public.
- Still open: Q7 (v1 vs v2 RevenueCat keys; default stays v1 for 0.6, behind `RevenueCatClient`) and Q10 (refresh-token TTL; default stays Logto's 14 days).

1. ActForYou Pro takes: keep the allowance in `acting_accounts` (recommended for 0.6), or move it to mcp-core credits? Should an Apple Pro subscriber also get MCP casting credits?
2. Sandbox: accept sandbox entitlements in production (needed for App Review and TestFlight), or accept them only for an allowlist of reviewer and test accounts?
3. Shared deletion: confirm that "Delete account" in either iPhone app deletes the one swapp1990 account. Until LMAFY 19 ships, should `/api/v1/acting/account/deletion` stay app-scoped (recommended)?
4. On `User.Deleted`, should each product also delete the person's RevenueCat customer (`DELETE /v1/subscribers/{sub}`)?
5. (Settled in Phase 2: per-product SPA apps and per-product `MCP-DCR` M2M apps. The management calls still use `m-default`.)
6. Apple connector scope: set `scope: "name email"` so Apple sign-ins carry an email and link to the same person's Google/email account?
7. Rotate both RevenueCat projects to API v2 secret keys (needs project ids), or stay on v1 for 0.6?
8. Is `swapp1990/mcp-core` public, or at least its release assets? EAS needs to download the tarball.
9. Keep web-flow Sign in with Apple through the shared connector (today), or pursue the native Apple sheet later?
10. Native refresh-token policy: what TTL (Logto's default is 14 days, so someone away for two weeks is signed out) and rotation?
11. (Settled: the `jobalerts` callbacks were removed from the `Autonomous Writer` SPA when Job Alerts got its own app.)

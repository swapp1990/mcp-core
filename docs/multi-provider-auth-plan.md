# Multi-Provider Auth Plan

Planning reference for adding provider-agnostic auth to `mcp-core`, with
Logto preserved and Supabase added as the first alternate provider.

Status: reference plan only. No implementation is implied by this document.
Last updated: 2026-05-25.

Read alongside:

- `src/mcp_core/auth.py`
- `src/mcp_core/__init__.py`
- `src/mcp_core/routes.py`
- `src/mcp_core/dcr.py`
- `src/mcp_core/billing.py`
- `docs/oauth-mcp-integration.md`
- Melo Supabase example: `D:\MyProjects\Claude\Melo\auth.js`,
  `D:\MyProjects\Claude\Melo\backend\auth.py`
- First downstream app: `D:\MyProjects\Claude\autonomous-writer\writer-v2`

---

## 1. Goals

`mcp-core` should support multiple auth-backed services without forcing each
downstream app to rewrite token validation, user provisioning, billing identity,
or MCP OAuth wiring.

The first two providers are:

| Provider | Hosted/cloud | Self-hosted | Browser UX | MCP OAuth |
|----------|--------------|-------------|------------|-----------|
| Logto | Supported today | Supported today | Hosted UI or branded bounce | Supported today via mcp-core proxy + Logto DCR |
| Supabase | New target | New target by URL/key config | Inline Google via Supabase Auth, plus more providers later | New target, using Supabase Auth OAuth Server where possible |

Success means:

1. Existing Logto users and apps keep working with no config changes.
2. New apps can opt into Supabase with a small env/config diff.
3. Cloud vs self-hosted is selected by endpoint/key values, not separate code.
4. Billing and audit logs work with either provider.
5. MCP clients can still discover auth, complete OAuth, call tools, and get
   per-user billing.
6. The implementation is driven by unit, integration, and E2E tests.

---

## 2. Non-goals

- Do not remove Logto.
- Do not force every downstream app to migrate storage immediately.
- Do not make Supabase-specific assumptions part of the generic user model.
- Do not hard-code Writer V2 details in `mcp-core`.
- Do not rely on Supabase cloud-only behavior when a self-hosted Supabase URL
  can express the same deployment.

---

## 3. Current State

`mcp-core` currently has a single concrete auth class:

- `LogtoAuth` validates JWTs by JWKS.
- Users are provisioned into MongoDB with `logto_user_id`.
- Billing deducts credits by querying `{"logto_user_id": user_id}`.
- Tool logs write `user_id` from `user["logto_user_id"]`.
- MCP OAuth routes are Logto-specific:
  - `/oauth/authorize` injects Logto `resource`.
  - `/oauth/token` injects Logto `resource`.
  - `LogtoDCR` creates Logto apps through the Management API.
  - `mcp_auth_config()` returns Logto issuer and endpoints.

Melo demonstrates the desired Supabase browser and backend pattern:

- Frontend loads `@supabase/supabase-js`.
- Inline Google uses Google Identity Services and
  `supabase.auth.signInWithIdToken`.
- Backend verifies access tokens by calling:
  `GET {SUPABASE_URL}/auth/v1/user`
  with both `Authorization: Bearer <token>` and `apikey`.
- Backend normalizes the Supabase user to:
  `{sub, email, name, picture, provider, providers}`.
- Storage uses neutral `auth_user_id` while retaining legacy `logto_user_id`
  fallbacks.

Writer V2 currently depends on Logto in both places:

- Server: `_build_core()` passes Logto env vars into `MCPCore`.
- Client: `@logto/react`, `@logto/browser`, custom Logto Experience API forms.
- Many server routes read `user["logto_user_id"]`.
- Admin credit top-up accepts only `logto_user_id`.

---

## 4. Target Architecture

Introduce an auth provider boundary inside `mcp-core`.

The outer `MCPCore` facade should keep the current simple usage style:

```python
core = MCPCore(
    product_name="my-product",
    auth_provider="supabase",
    supabase_url="https://project-ref.supabase.co",
    supabase_anon_key="...",
    mongodb_uri="...",
    db_name="my-product",
)
```

Logto remains supported:

```python
core = MCPCore(
    product_name="my-product",
    auth_provider="logto",
    logto_endpoint="https://tenant.logto.app",
    logto_api_resource="https://api.my-product.app",
)
```

And existing callers should still work:

```python
core = MCPCore(
    product_name="my-product",
    logto_endpoint="https://tenant.logto.app",
    logto_api_resource="https://api.my-product.app",
)
```

### Provider contract

The implementation can use a protocol/base class rather than over-abstracting.
The useful boundary is:

```python
class AuthProvider:
    provider_name: str
    read_only_tools: set[str]
    dev_bypass: bool
    dev_user_id: str

    async def verify_token(request: Request) -> dict | None: ...
    async def get_or_create_user(db: Any, token_payload: dict) -> dict: ...
    async def require_auth(request: Request, tool_name: str, db: Any = None) -> dict | None: ...
    def oauth_protected_resource_metadata(scopes: list | None = None, base_url: str | None = None) -> dict: ...
    def mcp_auth_config(...): ...
```

Keep shared behavior in a base helper where it is truly shared:

- Bearer token extraction.
- Dev bypass.
- Read-only tool behavior.
- Neutral user document creation.
- OAuth protected-resource metadata shape.

Keep provider-specific behavior in provider classes:

- Logto JWKS and issuer/audience validation.
- Logto resource injection and DCR.
- Supabase remote token verification.
- Supabase OAuth Server metadata and consent behavior.

---

## 5. Config Surface

Constructor arguments should take precedence over `MCP_CORE_*` env vars.

### Generic

| Constructor | Env | Default | Notes |
|-------------|-----|---------|-------|
| `auth_provider` | `MCP_CORE_AUTH_PROVIDER` | inferred | `logto`, `supabase`, or `none` |
| `free_credits` | `MCP_CORE_FREE_CREDITS` | `30` | unchanged |
| `dev_auth_bypass` | `MCP_CORE_DEV_AUTH_BYPASS` | `0` | unchanged |
| `dev_user_id` | `MCP_CORE_DEV_USER_ID` | `local-dev-user` | currently constructor-only in practice |
| `oauth_scopes` | optional later | `openid profile email` | provider-compatible defaults |

Provider inference:

1. Explicit `auth_provider` wins.
2. If Supabase URL/key are present, use Supabase.
3. Else if Logto endpoint/resource are present, use Logto.
4. Else no external auth, but dev bypass can still work when enabled.

### Logto

Keep existing names:

- `MCP_CORE_LOGTO_ENDPOINT`
- `MCP_CORE_LOGTO_API_RESOURCE`
- `MCP_CORE_MCP_LOGTO_APP_ID`
- `MCP_CORE_MCP_LOGTO_APP_SECRET`
- `MCP_CORE_LOGTO_MGMT_APP_ID`
- `MCP_CORE_LOGTO_MGMT_APP_SECRET`
- `MCP_CORE_LOGTO_MGMT_API_RESOURCE`
- `MCP_CORE_LOGTO_MGMT_TOKEN_ENDPOINT`
- `MCP_CORE_BRANDED_SIGN_IN_URL`

### Supabase

New names:

- `MCP_CORE_SUPABASE_URL`
- `MCP_CORE_SUPABASE_ANON_KEY`
- `MCP_CORE_SUPABASE_AUTH_BASE_URL`
- `MCP_CORE_MCP_SUPABASE_CLIENT_ID`
- `MCP_CORE_MCP_SUPABASE_CLIENT_SECRET`
- `MCP_CORE_SUPABASE_VERIFY_STRATEGY`

Defaults:

- `supabase_auth_base_url = f"{supabase_url}/auth/v1"`
- `supabase_verify_strategy = "remote_user"`

The URL values should work for both cloud and self-hosted Supabase.

---

## 6. User Identity Model

This is the main migration risk.

Today, `logto_user_id` is both the provider subject and the product user key.
That cannot remain the only key once multiple providers exist.

New user documents should include:

```json
{
  "auth_provider": "supabase",
  "auth_subject": "provider-native-subject",
  "auth_user_id": "supabase:provider-native-subject",
  "email": "person@example.com",
  "name": "Person Name",
  "picture": "https://...",
  "free_credits": 30,
  "credits_used": 0,
  "stripe_customer_id": null,
  "stripe_subscription_id": null,
  "created_at": "...",
  "last_login_at": "..."
}
```

Compatibility:

- Logto users should also get `auth_provider`, `auth_subject`, and
  `auth_user_id` on next auth.
- Existing queries by `logto_user_id` should continue during the transition.
- Public return dicts should include both:
  - `auth_user_id`
  - `logto_user_id` when present, for older downstream code

Shared helper:

```python
def user_identity(user: dict) -> str:
    return user.get("auth_user_id") or user.get("logto_user_id") or ""
```

Billing should use that helper and update by:

1. `_id` when the user document has `_id`.
2. Else `auth_user_id`.
3. Else `logto_user_id`.

Stripe metadata should move from only `logto_user_id` to:

```json
{
  "auth_user_id": "...",
  "auth_provider": "...",
  "auth_subject": "..."
}
```

For webhook compatibility, continue reading old `logto_user_id` metadata.

---

## 7. Supabase Provider Behavior

### Token verification

MVP should use remote verification because it works for Supabase cloud and
self-hosted deployments without depending on asymmetric JWT keys:

```http
GET {SUPABASE_AUTH_BASE_URL}/user
Authorization: Bearer <access-token>
apikey: <anon-or-publishable-key>
```

Expected behavior:

- No bearer token returns `None`.
- `dev-bypass` works when enabled.
- 401/403 from Supabase becomes HTTP 401 with `WWW-Authenticate: Bearer`.
- Network errors or 5xx become HTTP 503.
- A response missing a stable user id becomes HTTP 401.

Normalize Supabase users like Melo:

```python
{
    "sub": user["id"] or user["sub"],
    "email": user["email"] or user_metadata["email"] or "",
    "name": user_metadata["full_name"] or user_metadata["name"] or user_metadata["user_name"] or email_prefix,
    "picture": user_metadata["avatar_url"] or user_metadata["picture"] or "",
    "provider": app_metadata["provider"] or "",
    "providers": app_metadata["providers"] or [],
}
```

### Future JWKS verification

Supabase supports JWKS for asymmetric signing keys, but self-hosted or older
projects may not expose usable asymmetric keys. Add JWKS verification only
after the remote-user path is covered and stable.

When added, make it opt-in or auto-detected with a safe fallback.

---

## 8. MCP OAuth Behavior

Logto keeps the current code path:

- Logto DCR through Management API when configured.
- Fake DCR kept enabled for fastapi-mcp metadata compatibility.
- `/oauth/authorize` and `/oauth/token` resource injection remain Logto-only.

Supabase should not use Logto resource-injection routes.

For Supabase, prefer the Supabase Auth OAuth Server:

- issuer: `{SUPABASE_AUTH_BASE_URL}`
- discovery: provider metadata under Supabase Auth
- authorize/token/register endpoints from discovery where possible
- consent handled by a downstream app page when Supabase requires it

Important implementation note:

Supabase OAuth Server and MCP-auth documentation should be rechecked
immediately before coding. The feature is documented as beta and endpoint
requirements may change.

---

## 9. `mcp-core` TDD Plan

Add failing tests before implementation.

### Provider selection

- `MCPCore(auth_provider="logto")` builds Logto auth.
- Existing Logto-only constructor still builds Logto auth.
- `MCPCore(auth_provider="supabase")` builds Supabase auth.
- Supabase env vars infer Supabase provider.
- Constructor args override env vars.
- Unknown provider raises a clear configuration error.

### Supabase token verification

Use `httpx.MockTransport` or injected async client factory.

- Missing token returns `None`.
- Dev bypass returns local claims.
- Valid Supabase `/user` response returns normalized claims.
- 401/403 returns HTTP 401.
- Network failure returns HTTP 503.
- Missing `id`/`sub` returns HTTP 401.
- Provider sends `apikey` and bearer headers.

### User provisioning

- Supabase first auth creates a user with neutral identity fields.
- Second auth is idempotent and does not reset credits.
- Logto users also get neutral identity fields.
- Existing Logto users found by `logto_user_id` are upgraded in place.
- Concurrent upserts produce one user document.

### Billing and logging

- Billing deducts by `auth_user_id`.
- Billing still deducts legacy Logto users by `logto_user_id`.
- Stripe checkout metadata includes neutral identity.
- Stripe webhook links subscription by `auth_user_id`.
- Legacy webhook metadata with `logto_user_id` still works.
- Tool logging uses neutral identity.

### Routes and MCP metadata

- Logto `/oauth/authorize` behavior remains unchanged.
- Supabase mode does not install Logto resource-injection proxies.
- Supabase protected-resource metadata advertises the expected auth server.
- `mcp_auth_config()` returns provider-specific issuer and endpoints.
- Bearer gate still returns `WWW-Authenticate` with resource metadata.

### Test commands

```powershell
cd D:\MyProjects\Claude\mcp-core
py -m pytest tests\test_auth.py tests\test_billing.py tests\test_integration.py tests\test_mcp_mount.py -q
py -m pytest -q
```

Optional live tests later:

```powershell
$env:RUN_LIVE_SUPABASE_TESTS="1"
py -m pytest tests\live -q
```

---

## 10. Writer V2 Downstream Plan

Writer V2 is the first real downstream app to consume Supabase cloud auth.

### Server changes

Files likely involved:

- `writer-v2/server/main.py`
- `writer-v2/server/.env.example`
- `writer-v2/server/requirements.txt`
- `writer-v2/server/routes/*.py`
- `writer-v2/server/tests/*`

Server tasks:

1. Add env config:
   - `AUTH_PROVIDER=supabase`
   - `SUPABASE_URL`
   - `SUPABASE_ANON_KEY`
   - MCP Supabase OAuth fields as needed
2. Build `MCPCore` with provider selection.
3. Add a local helper:
   ```python
   def auth_user_id(user: dict) -> str:
       return user.get("auth_user_id") or user.get("logto_user_id") or ""
   ```
4. Replace route-level `user["logto_user_id"]` reads with the helper.
5. Update admin credit top-up to accept `auth_user_id`, while still accepting
   `logto_user_id`.
6. Keep `ALLOW_DEV_AUTH_BYPASS=1` behavior unchanged for tests.

### Client changes

Files likely involved:

- `writer-v2/client/src/index.js`
- `writer-v2/client/src/App.js`
- `writer-v2/client/src/auth.js`
- `writer-v2/client/src/api/_fetch.js`
- `writer-v2/client/src/components/auth/*`
- `writer-v2/client/package.json`

Client tasks:

1. Add a provider-neutral auth adapter used by the app:
   - `isAuthenticated`
   - `isLoading`
   - `getAccessToken`
   - `signIn`
   - `signOut`
   - optional `fetchUserInfo`
2. Keep Logto adapter for existing deployments.
3. Add Supabase adapter:
   - `@supabase/supabase-js`
   - Google Identity Services inline button
   - `supabase.auth.signInWithIdToken({ provider: "google", token, nonce })`
   - redirect OAuth provider fallback for non-Google providers
4. Update auth modal to render provider-specific sign-in options.
5. Keep dev bypass path independent of the provider adapter.

### Consent page

Writer already has `/consent` for Logto. Supabase OAuth Server may require a
provider-specific consent flow for MCP clients.

Plan:

1. Hide provider details behind a consent adapter.
2. In Supabase mode, fetch authorization details for the incoming request.
3. Show client name and scopes.
4. Approve or deny.
5. Redirect to the provider-returned URL.

---

## 11. Writer V2 TDD and E2E Plan

### Server tests

Add tests before implementation:

- `_build_core()` passes Supabase config when `AUTH_PROVIDER=supabase`.
- Mocked Supabase bearer token can call `/api/mcp/get_credits`.
- `generate_story` stores `user_id` from neutral identity.
- `list_stories` isolates two Supabase users.
- Admin top-up works with `auth_user_id`.
- Existing dev bypass tests keep passing.

### Client unit tests

- `useMcpFetch` sends Supabase access token.
- `useMcpFetch` still sends `Bearer dev-bypass` in dev bypass mode.
- Supabase session-expired state surfaces a sign-in-required error.
- Logto adapter keeps existing token behavior.

### Playwright E2E

Default fake suite:

- Still runs with dev bypass and no live auth dependency.
- Existing story creation/revision E2E remains unchanged.

Mocked Supabase auth E2E:

- Landing page shows inline Google option in Supabase mode.
- Mock Google credential signs in through a mocked Supabase client.
- Authenticated dashboard loads.
- A tool request includes `Authorization: Bearer <supabase-token>`.

Supabase consent E2E:

- `/consent?authorization_id=...` loads provider details.
- Approve redirects to provider URL.
- Deny redirects or surfaces provider error cleanly.

Opt-in live smoke:

- Supabase Auth settings/discovery endpoint reachable.
- Google provider enabled.
- Inline Google button renders without Logto redirect.
- MCP protected-resource metadata points at Supabase/provider-compatible auth.

Commands:

```powershell
cd D:\MyProjects\Claude\autonomous-writer\writer-v2
cd server; py -3 -m pytest tests\ -x -q
cd ..
npx playwright test
```

---

## 12. Rollout Order

1. Add `mcp-core` provider boundary tests.
2. Implement provider boundary with Logto unchanged.
3. Add Supabase auth tests.
4. Implement Supabase token verification and neutral user provisioning.
5. Update billing/logging identity helpers.
6. Add Supabase MCP metadata/config tests.
7. Implement Supabase MCP config path.
8. Run full `mcp-core` test suite.
9. Update Writer server to consume neutral identity.
10. Update Writer client auth adapter and Supabase UI.
11. Add and run Writer unit/E2E tests.
12. Run opt-in live Supabase cloud smoke.
13. Only then consider production deploy.

---

## 13. Open Questions

1. Should `auth_user_id` be exactly `provider:subject`, or should it be a
   stable hash to avoid leaking provider ids into downstream records?
2. Should `mcp-core` expose `user_identity(user)` publicly?
3. Should Supabase remote-user verification cache successful token payloads
   for a short TTL, or always call Supabase?
4. Should Supabase JWKS verification be added in the first pass, or deferred
   until after Writer is live?
5. Does Writer need email/password auth after moving to Supabase, or is inline
   Google the first supported production path?
6. Will Writer MCP auth use Supabase OAuth Server directly, or should
   `mcp-core` proxy parts of the flow like it does for Logto?

---

## 14. External References to Recheck

These docs are likely to matter during implementation and should be verified
again before coding because auth provider behavior changes over time:

- Supabase Auth JWT verification:
  `https://supabase.com/docs/guides/auth/jwts`
- Supabase Auth with Google:
  `https://supabase.com/docs/guides/auth/social-login/auth-google`
- Supabase OAuth 2.1 Server:
  `https://supabase.com/docs/guides/auth/oauth-server`
- Supabase MCP authentication:
  `https://supabase.com/docs/guides/auth/oauth-server/mcp-authentication`
- Logto docs for JWT, API resources, and Management API should also be
  rechecked before touching the Logto path.


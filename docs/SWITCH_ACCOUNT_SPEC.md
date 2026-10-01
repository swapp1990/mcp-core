# Switch account across every swapp1990 website

Status: **spec, 2026-10-01.** Not started. Owner decisions are in [section 10](#10-owner-decisions); all are answered.

## 1. Goal

One **Switch account** action in any swapp1990 website lets the person pick a different account on Logto's page. After that, every other swapp1990 website they open is on the new account, with no second sign-in. **Sign out** works the same way: sign out in one site, and every site shows signed out.

Success means:

1. Switching takes one click, then Logto's page: Google (with its account chooser), Apple or email.
2. Every other web app shows the new account the next time it is loaded or focused. No app keeps acting as the old account, and nothing is billed to the old account from a stale tab.
3. Apps where nothing changed make no extra Logto round trips on normal page loads.

Out of scope: the iOS apps (their browser sessions are ephemeral on purpose; see the [platform contract](./platform-contract.md#client-behavior)), MCP clients (each one holds its own grant; switching means reconnecting the connector), and keeping two accounts signed in side by side.

## 2. Current behavior (measured 2026-10-01)

All of this was read from the live Logto 1.38.0 (`/opt/apps/logto-docker`), its database and the deployed web apps.

| Fact | Evidence | Consequence |
|---|---|---|
| Sign-in uses `prompt=consent`, so an existing Logto session signs straight in | auth-web `signIn`; Logto logs: VideoGen sign-ins at 18:15 and 18:27 had no `Interaction.SignIn.*` steps, reusing Writer's 06:45 Google sign-in | Single sign-on works; there is no way to pick another account without signing out first |
| Logto's logout page always posts `logout=yes` | `packages/core/static/html/logout.html` | Ending the session from any app destroys the whole Logto session (every app's entry in it) |
| At logout, oidc-provider keeps grants that include `offline_access` | `oidc-provider/lib/actions/end_session.js:164-173` (`persistsLogout`) | Every web app asks for `offline_access`, so **other apps' refresh tokens survive a sign-out** and keep renewing as the old account |
| Access tokens live 7 days for Writer, VideoGen, DesignForYou and JobsForYou APIs (1 hour for ActForYou and SnapForYou) | `resources.access_token_ttl` | Revoking tokens server-side would not stop a stale app for up to a week; the switch has to happen in each client |
| Backends verify JWTs locally with mcp-core | `mcp_core.auth` JWKS | No backend can tell that a token's session ended |
| `prompt=none` works | `/oidc/auth?...&prompt=none` with no session → 303 to the callback with `error=login_required` | A top-level silent sign-in can pick up whatever account the Logto session now holds |
| Logto pages send `X-Frame-Options: SAMEORIGIN` | `curl -I /oidc/auth` | A hidden-iframe silent check is not reliable; use a top-level redirect |
| Back-channel logout is enabled in Logto, but no app sets `backchannelLogoutUri` | Logto config `features.backchannelLogout`, `applications.oidc_client_metadata` | Available for later server-side hardening (section 9) |
| The Google connector sends `prompts: ["select_account"]` | `connectors` row `qinjb62m13jn` | Picking "Continue with Google" during a switch shows Google's account chooser |
| Logto session TTL is 14 days | Logto `defaults.sessionTtl` | Sets the lifetime of the shared hint cookie (section 4) |
| Every web app lives on a `*.swapp1990.org` host; Logto is `auth.designforyou.swapp1990.org` | — | All of them are same-site, so one cookie on `Domain=swapp1990.org` is readable by every app |

### Web clients today

| App | Host | Sign-in client |
|---|---|---|
| WriteForYou web | writer.swapp1990.org | `@swapp1990/auth-web` 0.1.0 |
| VideoGen | videogen.swapp1990.org | `@swapp1990/auth-web` 0.1.0 |
| DesignForYou | designforyou.swapp1990.org | `@swapp1990/auth-web` 0.1.0 |
| JobsForYou | jobalerts.swapp1990.org | `@swapp1990/auth-web` 0.1.0 |
| ActForYou web | actforyou.swapp1990.org | Hand-written PKCE in `site/app.js` |
| SnapForYou | snapforyou.swapp1990.org | `@logto/browser` 3.0.14 from jsDelivr in `app/index.html` |

## 3. Design overview

Three pieces:

1. **Switch account** ends the Logto session and reopens Logto's sign-in page in the same tab.
2. **A shared account hint**: a cookie on `Domain=swapp1990.org` that says which account the Logto session belongs to, or that the person signed out. Every app writes it when it signs in or out.
3. **Reconcile**: on page load and when a tab regains focus, each app compares the hint with the account it is holding. On a mismatch it drops its tokens and signs in silently as the new account. A focused tab does this with an automatic reload.

The hint carries no authority. Tokens still come only from Logto, so a wrong or forged hint can at worst cause one silent sign-in or a local sign-out (section 8).

## 4. The account hint cookie

| Attribute | Value |
|---|---|
| Name | `swapp1990_account` |
| Value | `v1.<sub>` when signed in, `v1.-` after a sign-out. `<sub>` is the Logto user id (not personal data; it is already the identity key `logto:<sub>`) |
| Domain | `swapp1990.org` (only when `location.hostname` ends in `.swapp1990.org`; otherwise the hint is off, e.g. localhost dev) |
| Path | `/` |
| Max-Age | `1209600` (14 days, the Logto session TTL). Safari caps script-set cookies at 7 days; an expired hint reads as "unknown", which is safe |
| Secure, SameSite | `Secure; SameSite=Lax` |
| HttpOnly | No (it is written and read by page script) |

States as read by an app:

| Hint | Meaning |
|---|---|
| absent or unparseable | Unknown (first visit after rollout, expired, or another browser profile). Do nothing; write our own value if we are signed in |
| `v1.-` | Someone signed out in some app |
| `v1.<sub>` | The Logto session belongs to `<sub>` |

Writes:

- After `completeSignIn()` succeeds: write `v1.<sub>` from the new ID token.
- On page load when signed in and the hint is absent: write `v1.<sub>`.
- In `signOut()` and `switchAccount()`, before redirecting to Logto: write `v1.-`.
- `expire()` (401 or a dead refresh token) does **not** write the hint: one app's token failing says nothing about the shared session.

## 5. Flows

### 5.1 Switch account (in the app where the person clicks it)

1. Save `{ returnTo }` in `sessionStorage["swapp1990.switch"]`.
2. Write hint `v1.-`.
3. `logto.signOut(location.origin)`: this revokes this app's refresh token, clears local tokens and goes to Logto `end_session`, which destroys the Logto session (`logout=yes`) and returns to the site root. Only the bare origin is registered as a post-logout URI on every app (a deep path got a 400 on VideoGen; fixed in 4f7f2d4).
4. On the root, auth-web init sees `swapp1990.switch`, removes it, and calls `signIn(undefined, returnTo)`. There is no Logto session, so Logto shows its page. Google shows its account chooser.
5. On the callback, `completeSignIn()` writes `v1.<new sub>` and the app returns to `returnTo`.

If the person backs out of Logto's page, they stay signed out everywhere. That is the expected result, and the switch flag is already gone, so there is no loop.

Why sign out first instead of `prompt=login`: the platform contract forbids `login`, and how oidc-provider merges a different account into an existing session isn't documented. Ending the session gives a known clean state for one extra redirect.

### 5.2 Reconcile on page load (any app, any page)

The app holds account `mine` (the `sub` in its stored ID token) or nothing.

| Hint | Holding `mine` | Holding nothing |
|---|---|---|
| unknown | Write `v1.<mine>`. Done | Done |
| `v1.<mine>` | Done | Silent sign-in (5.4) |
| `v1.<other>` | Drop tokens (revoke the refresh token, best effort), then silent sign-in | Silent sign-in |
| `v1.-` | Drop tokens. Show signed out | Done |

"Holding nothing" + `v1.<sub>` means the person signed in on another site and this one has never seen them, so single sign-on just happens on page load (Q3, agreed).

Whenever a silent sign-in changes who the app shows (a first-visit sign-in or a switch picked up from another site), the app tells the person: a toast "Signed in as `<email>`" for about 5 seconds, plus the account menu showing the email or avatar at all times while signed in. auth-web reports this as `init()` → `{ announced: true, user }`; each app renders the toast in its own style.

The UI must not render as signed in until reconcile finishes. `AuthProvider` keeps `ready: false` until then.

### 5.3 Reconcile on focus (an already-open tab)

On `visibilitychange` to visible, `focus`, and the `storage` event (another tab of the same app changed tokens), read the hint again:

- It matches `mine`, or is unknown: done.
- `v1.-`: drop tokens and show signed out. Nothing can be lost by signing out.
- `v1.<other>`: **reload automatically as the new account** (Q2):
  1. Mark the session `switchedElsewhere`. `getAccessToken()` returns `null` from now on, so no request goes out as the old account and nothing is billed to it.
  2. If another tab of the same app has already signed in as `<other>` (its tokens in this origin's `localStorage` now say `<other>`), call `location.reload()`. No Logto round trip is needed.
  3. Otherwise, run the silent sign-in (5.4) with `returnTo` set to the current path and query. The tab lands back on the same page as the new account, and the "Signed in as `<email>`" toast (5.2) says what happened.

  Unsaved work in the tab is lost, which the owner accepted. auth-web never suppresses the browser's own "Leave site?" prompt: an app that already registers `beforeunload` for unsaved edits still gets it. If the person chooses to stay, the tab keeps `switchedElsewhere` (no API calls) and tries again on its next focus.

### 5.4 Silent sign-in

A top-level redirect to `/oidc/auth` with `prompt=none`, the app's normal redirect URI and PKCE.

- The Logto session exists: Logto returns a code at once; `completeSignIn()` exchanges it and writes the hint. The person sees a short redirect flash, not a sign-in page.
- No session (`error=login_required`, or `consent_required` / `interaction_required`): `completeSignIn()` must treat this as "signed out", not throw. It clears local state, writes `v1.-` only when the error is `login_required`, and lands on `returnTo` signed out.
- Loop guard: record `sessionStorage["swapp1990.silent"] = <hint value>` before redirecting. Never run a second silent sign-in for the same hint value in the same tab.

### 5.5 Sign out

`signOut()` writes `v1.-`, then runs the existing flow (revoke, clear, `end_session` to the root). Other apps sign out on their next load or focus (5.2, 5.3).

This is a behavior change. Today, signing out of VideoGen leaves Writer signed in, because Writer's refresh token survives. See Q1.

## 6. `@swapp1990/auth-web` 0.2.0

New and changed API (`clients/web/src/index.js`):

```js
const auth = createAuth({
  appId, resource,
  accountHint: true,             // default true; turns the hint off where the host isn't *.swapp1990.org
  hintDomain: "swapp1990.org",   // override only for tests
});

await auth.init();               // NEW. Runs 5.1 step 4 and 5.2. Returns { signedIn, user, redirecting }
auth.switchAccount(returnTo?);   // NEW. Section 5.1
auth.signOut();                  // CHANGED. Writes v1.- and always returns to location.origin (no path, no trailing slash)
auth.completeSignIn();           // CHANGED. Writes the hint; treats login_required as signed out
auth.watchAccount(listener);     // NEW. Section 5.3; runs the sign-out or reload itself, then calls listener({ type: "signed-out" | "reloading" }); returns unsubscribe
auth.getAccessToken();           // CHANGED. Returns null while switchedElsewhere, until the reload
```

`clients/web/src/react.js`: `AuthProvider` calls `init()` and `watchAccount()`. Context gains `switchAccount` and `announced` (for the toast). The reload in 5.3 happens inside auth-web, so apps render nothing for it. `ready` stays false until `init()` resolves.

Unexported helpers worth their own tests: `readHint()`, `writeHint(value)`, `subFromStoredIdToken()`, `revokeRefreshToken()` (POST `/oidc/token/revocation` with `client_id`; `@logto/browser` has no public revoke outside `signOut`).

Package hygiene: still a tarball on a GitHub release (`auth-web-v0.2.0`); apps pin the URL. `node --test` covers the state machine with a fake Logto client, a fake `document.cookie` and fake `sessionStorage`.

## 7. Work per app

Each app: bump to auth-web 0.2.0, put **Switch account** next to **Sign out** in its existing account menu, show the "Signed in as" toast and the email in the account menu, then commit → push → deploy.

| App | Extra work |
|---|---|
| VideoGen | `frontend/src/auth.jsx` wraps auth-web in its own `LogtoBridge`; pass through `init`/`watchAccount` and drop its local `resolvePostLogoutRedirectUri`. Its `apiFetch` already handles `getAccessToken()` returning null |
| WriteForYou web | Check that the legacy `/signin` handoff page still lands correctly after `init()` |
| DesignForYou | Check that DesignForYou Web's post-logout URIs include `https://designforyou.swapp1990.org/` (tenant.json drift noted at the 0.6 import) |
| JobsForYou | None beyond the common steps |
| ActForYou web | Hand-written client. Either port `site/app.js` to auth-web (it has no bundler, so it would need a small build or a vendored ESM copy), or implement the hint protocol by hand (about 60 lines: sections 4, 5.2–5.5). Q4 |
| SnapForYou | Raw `@logto/browser` from jsDelivr. Same choice as ActForYou. Q4 |

Logto changes: none required for the auth-web apps. They sign out to `location.origin`, and as of 2026-10-01 every SPA registers `https://<host>` without the slash, except SnapForYou (`https://snapforyou.swapp1990.org/` only), which signs out with its own client. Writer registers only the no-slash form, so a client that adds a path or a trailing slash gets Logto's 400. `logto_config.py plan` should show no drift afterwards.

## 8. Edge cases and safety

- **Forged or stale hint.** Any `*.swapp1990.org` page can write the cookie; all of them are ours. The worst a bad value can do is one silent sign-in (which returns the real Logto session's account) or a local sign-out. It can never sign anyone in as another person.
- **Two tabs of the same app.** They share `localStorage`. The tab that switched writes the new tokens; the other tab sees the `storage` event and re-renders as the new account (5.3 step 3). Reconcile compares against the account the tab rendered with, not only the stored one.
- **Old account's tokens.** Each app revokes its own old refresh token when it reconciles. An app that isn't opened keeps a dangling refresh token until its TTL (14 days by default for SPAs). Its 7-day access tokens remain valid server-side but are no longer held by any client. Section 9 covers hardening.
- **Billing safety.** Between the focus and the reload, a stale tab sends no authenticated calls (`getAccessToken()` returns null), so no credits move on the wrong account.
- **Cancel during a switch.** Signed out everywhere, no loop (5.1).
- **Account deleted elsewhere.** Unchanged: the refresh fails, `sessionIsDead` runs `expire()`.
- **Same account signs in again.** The hint value is unchanged, so nothing happens.
- **Local dev (`localhost`, `127.0.0.1`).** No hint; behavior as today.
- **Chrome profiles, private windows, other browsers.** Each has its own cookies, `localStorage` and Logto session, and its own Google sign-in. Account A in one Chrome profile and account B in another stay signed in side by side, and a switch or sign-out in one never touches the other. This is the supported way to use two accounts at once.

## 9. Later (not in this spec)

- **Shorter access tokens.** 7 days is long for tokens nobody can revoke. Moving Writer, VideoGen, DesignForYou and JobsForYou to 1 hour (like ActForYou) shrinks the window. It costs one refresh an hour per app; check the iOS apps' refresh handling first.
- **Back-channel logout.** Set `backchannelLogoutUri` per app to an mcp-core route that records `(sub, sid, ended_at)` and makes the backend reject older tokens for that session. That gives server-side enforcement on top of the client-side switch.
- **iOS "Switch account".** Sign out (revoke and clear) then sign in. The ephemeral session already shows Logto's page every time.

## 10. Owner decisions

Answered by Swapnil on 2026-10-01 unless marked open.

- **Q1. Sign out signs you out everywhere: yes.** Same mechanism as the switch (5.5).
- **Q2. An already-open tab after a switch elsewhere: reload automatically.** It reloads as the new account when it regains focus (5.3). Unsaved work in that tab is lost; the browser's own "Leave site?" prompt still applies where an app sets one.
- **Q3. Auto sign-in on first visit: yes, and the person must see it.** A toast "Signed in as `<email>`" plus the email in the account menu (5.2).
- **Q4. ActForYou web and SnapForYou: implement the hint protocol by hand.** No auth-web port for now. The protocol goes in the platform contract so it stays checkable.
- **Q5. End-to-end tests: Logto one-time tokens for two dedicated test users.** See section 13.

## 11. Rollout

1. **auth-web 0.2.0** with unit tests (state machine, hint read/write, the `login_required` callback, the loop guard). Release `auth-web-v0.2.0`.
2. **Pilot on VideoGen and WriteForYou web.** Deploy both. Verify with two accounts (section 12).
3. **DesignForYou and JobsForYou.**
4. **ActForYou web and SnapForYou** (per Q4).
5. **Contract.** Add the hint protocol and the Switch account row to [platform-contract.md](./platform-contract.md#client-behavior). Add "every app's post-logout URIs include its site root" to `logto_config.py plan` checks.

The end-to-end suite (section 13) lands with step 2 and gains one spec per app in steps 3–4.

## 12. Acceptance checks

Run with accounts A and B in one browser profile, after each rollout step:

1. Signed in as A on Writer and VideoGen. In VideoGen, click Switch account and choose B. VideoGen shows B.
2. Reload Writer: it shows B after one redirect, with no Logto page. Logto logs show no `Interaction.SignIn.*` for Writer.
3. With a Writer tab already open before step 1, refocus it: it reloads on the same page as B within about 2 seconds, with the "Signed in as" toast. Between the focus and the reload, no API call carries A's token.
4. Sign out in Writer. Reload VideoGen: signed out. Click Sign in: Logto's page appears.
5. Normal reloads with no switch: Logto access logs show zero `/oidc/auth` requests from the reload.
6. Start a switch and back out on Logto's page: every app shows signed out, and no redirect loop happens (no more than one `/oidc/auth` per app).
7. VideoGen on `localhost`: behaves as today; no cookie is written.
8. First visit to a site while signed in elsewhere: the "Signed in as `<email>`" toast appears and the account menu shows the email.

## 13. End-to-end tests

### 13.1 Usual practice for apps with social and email-code sign-in

1. **Never automate Google or Apple sign-in pages.** They detect bots, change without notice, and their terms forbid scripted logins. Test the provider buttons by hand, once per release.
2. **Use dedicated test users in the real identity provider**, signed in by a method the test controls. That means a password, or a sign-in token issued by the provider's admin API. Test the email-code screen itself separately, with a test inbox, only when that screen is what's under test.
3. **Sign in once per account in a setup step, then reuse the saved browser state.** In Playwright that's a setup project writing one `storageState` file per account, which the tests then load.
4. **Keep test users identifiable and cheap**: a fixed address pattern, a marker on the user, excluded from metrics, and no paid generation in the suite.

### 13.2 What this suite uses

Logto 1.38 has **one-time tokens** (magic links): Management API `POST /api/one-time-tokens` returns a single-use token for an email. Adding `one_time_token=<token>&login_hint=<email>` to the `/oidc/auth` request signs that email in with no email sent and no sign-in page. This is Logto's own feature, so the suite runs the real authorization, real tokens and the real Logto session cookie.

- **Test users:** `e2e-switch-a@swapp1990.org` and `e2e-switch-b@swapp1990.org`, with `customData.e2e = true`. They're created once through the Management API and reused; `beforeAll` resets their product state.
- **Token minting:** a fixture calls the Management API with the existing M2M app (`LOGTO_MGMT_APP_ID` / `LOGTO_MGMT_APP_SECRET`, token endpoint `https://auth.swapp1990.org:8443/oidc/token`). Tokens expire in 5 minutes and are single-use. Secrets come from the environment, never from the repo.
- **Signing in from a test:** click the app's real Sign in button. A Playwright `page.route` on `**/oidc/auth?*` appends `one_time_token` and `login_hint` to that one request. The app's own PKCE state and callback run unchanged, and the app ships no test hooks.
- **Saved state:** a setup project signs in as A and as B on the Logto origin and saves `storageState`; each test starts from it. Every test still checks its starting account through the API, not the DOM.
- **Assertions through APIs:** the account shown is checked by calling each app's `/api/me` (or its equivalent) with the token the page holds; Logto access logs are read for the "no extra `/oidc/auth`" and "no `Interaction.SignIn.*`" checks (section 12, checks 2 and 5).
- **One `test()` per step in `test.describe.serial`:** sign in as A on two apps → switch on VideoGen to B → reload Writer → refocus an open stale tab (auto reload) → sign out → cancel path. Each step reports its own result.
- **Runs against production**, the deployed commit, after each rollout step, because cross-app behavior only exists on the real `*.swapp1990.org` hosts. The local `localhost` check (section 12, check 7) runs in each app's own suite.

**Probe, 2026-10-01: works.** `e2e-switch-a@swapp1990.org` (Logto user `0jnje2luox4r`, `customData.e2e = true`) was created through the Management API and given a 300-second token. Headless Chromium clicked VideoGen's real Sign in button, and the `page.route` handler answered the first `/oidc/auth` with a 302 to the same URL plus `one_time_token` and `login_hint`. Logto ran `PUT /api/experience` → `verification/one-time-token/verify` 200 → `identification` 204 → `submit` 200 without showing a page. VideoGen then exchanged the code, and `/api/billing/credits` returned 200 on `/studio`. Side effect: VideoGen created a user record for the test user with the 50 free credits, so test users get each product's sign-up grant. The suite's `beforeAll` resets that state.

Still to settle:

- `e2e-switch-b` doesn't exist yet; the suite creates it the same way.
- The token minting script reads the M2M secret on the droplet and prints only the token. The suite needs that secret in its own environment, or a small minting command it calls over SSH.
- The Google and Apple buttons stay on the manual release checklist.

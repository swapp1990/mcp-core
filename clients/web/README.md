# @swapp1990/auth-web

Sign-in for swapp1990 web apps: one shared Logto account, with the
[platform contract](../../docs/platform-contract.md) defaults baked in.

- Logto host and issuer `https://auth.designforyou.swapp1990.org`
- `prompt=consent` only, so a session from another swapp1990 app signs the person straight in
- Google, Apple or email (`signIn("google" | "apple" | "email")`), or Logto's page with all three (`signIn()`)
- Tokens in `localStorage` (the Logto browser SDK default), refreshed with `offline_access`
- A rejected session clears the tokens and fires `onExpire`, so the app shows signed out instead of failing silently
- **One account across sites (0.2.0).** Sign-out and Switch account end the shared Logto session, and a cookie on `swapp1990.org` tells every site who is signed in, so each site follows a switch or a sign-out made in another. Spec: [SWITCH_ACCOUNT_SPEC.md](../../docs/SWITCH_ACCOUNT_SPEC.md)

## Install

Pinned to a GitHub release of mcp-core (no npm registry):

```bash
npm install @logto/browser https://github.com/swapp1990/mcp-core/releases/download/auth-web-v0.2.0/swapp1990-auth-web-0.2.0.tgz
```

## Use

```js
import { createAuth } from "@swapp1990/auth-web";
import { AuthProvider, useAuth } from "@swapp1990/auth-web/react";

export const auth = createAuth({
  appId: "<Logto app id>",
  resource: "https://<product>.swapp1990.org",
  scopes: ["<product>:read"],
});

// Every API call: send auth.getAccessToken(); on a 401 call auth.expire().
```

### Account switching (0.2.0)

`AuthProvider` does all of this for React apps. Without React:

```js
const { signedIn, user, redirecting, announced } = await auth.init(); // once per page load, before any signed-in UI
if (redirecting) return;                       // the page is leaving for Logto
const stop = auth.watchAccount();              // open tabs follow a switch or sign-out made elsewhere
if (announced) toast(`Signed in as ${user.email}`); // a silent sign-in just changed the account

await auth.switchAccount();  // ends the shared session and opens Logto's page to pick another account
await auth.signOut();        // signs out of every swapp1990 site; always returns to the site root
```

- Call `init()` on every page, including the site root: a pending switch finishes there.
- Register the site root (`https://<host>`, no path) as the Logto app's post-logout redirect URI. Nothing else works: Logto answers `end_session` with a 400 for any other path.
- Show who is signed in at all times (name or email in the account menu), plus a short "Signed in as <email>" toast when `announced` is true.
- The hint is skipped off `*.swapp1990.org` (localhost dev behaves like 0.1.0). Pass `accountHint: false` to turn it off.
- Breaking since 0.1.0: `signOut()` takes no argument.

`/callback` calls `auth.completeSignIn()` and then `window.location.replace(auth.consumeReturnTo())`.

## Release

Bump `version`, then from this folder:

```bash
npm test && npm pack
gh release create auth-web-v<version> swapp1990-auth-web-<version>.tgz --repo swapp1990/mcp-core
```

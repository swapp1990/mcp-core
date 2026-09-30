# @swapp1990/auth-web

Sign-in for swapp1990 web apps: one shared Logto account, with the
[platform contract](../../docs/platform-contract.md) defaults baked in.

- Logto host and issuer `https://auth.designforyou.swapp1990.org`
- `prompt=consent` only, so a session from another swapp1990 app signs the person straight in
- Google, Apple or email (`signIn("google" | "apple" | "email")`), or Logto's page with all three (`signIn()`)
- Tokens in `localStorage` (the Logto browser SDK default), refreshed with `offline_access`
- A rejected session clears the tokens and fires `onExpire`, so the app shows signed out instead of failing silently

## Install

Pinned to a GitHub release of mcp-core (no npm registry):

```bash
npm install @logto/browser https://github.com/swapp1990/mcp-core/releases/download/auth-web-v0.1.0/swapp1990-auth-web-0.1.0.tgz
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

`/callback` calls `auth.completeSignIn()` and then `window.location.replace(auth.consumeReturnTo())`.

## Release

Bump `version`, then from this folder:

```bash
npm test && npm pack
gh release create auth-web-v<version> swapp1990-auth-web-<version>.tgz --repo swapp1990/mcp-core
```

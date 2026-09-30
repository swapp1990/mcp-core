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

`auth.swapp1990.org` serves the same tenant, but Logto keeps its session
cookie per host and Google only accepts the designforyou callback. No client
or backend may point at it. mcp-core builds the expected issuer from
`LOGTO_ENDPOINT`, so a backend with the other host rejects every token.

Accounts that share a verified email are linked into one Logto user
(`automaticAccountLinking`). Products never merge users themselves.

## Credits

- **One product, one database.** mcp-core stores credits, subscription state
  and the Stripe customer as fields on `users`, so two products sharing a
  database share one balance per person. `connect_db()` records the owning
  product in `_mcp_core_meta` and logs an error when a second product connects.
- Each product grants its own sign-up credits the first time a person uses it.
- Stripe customers and RevenueCat entitlements are per product.

## Client behavior

| | Web | iOS (Expo) |
|---|---|---|
| `prompt` | `consent` only. Never `login`: it disables single sign-on | `consent` only |
| Refresh tokens | `offline_access` scope | `offline_access` scope |
| Token storage | `localStorage` (survives closing the tab) | SecureStore |
| Browser session | Shared Logto session, so one sign-in covers every site | Ephemeral, so one app never signs in as another's account |
| Sign-out | Ends the Logto session (`end_session`) | Revokes the refresh token and clears the device |
| Rejected token (401) | Show the signed-out state; never look signed in while calls fail | Same |

Web apps get these defaults from [`@swapp1990/auth-web`](../clients/web/README.md)
instead of hand-writing Logto calls. Product sign-in pickers may call Logto with
`direct_sign_in` for a specific method, but must offer all three methods.

## Account deletion

"Delete account" in any product deletes the shared Logto user. Logto's
`User.Deleted` webhook fans out to every product backend, and each backend
removes its own data for that `logto:<sub>`. The UI must say the account is
removed from every swapp1990 product.

## Configuration names

Backends pass these to `MCPCore` explicitly:

| Variable | Meaning |
|---|---|
| `LOGTO_ENDPOINT` | Sign-in host above (also sets the expected issuer) |
| `LOGTO_API_RESOURCE` | This product's API resource indicator |
| `LOGTO_APP_ID` | This product's Logto application |
| `LOGTO_MGMT_APP_ID`, `LOGTO_MGMT_APP_SECRET`, `LOGTO_MGMT_TOKEN_ENDPOINT`, `LOGTO_MGMT_API_RESOURCE` | Management API access (DCR, account deletion) |
| `DB_NAME` | This product's own database |

## Adding a new product

1. Register its Logto application and API resource with the deploy/logto
   config script, not by hand in the console.
2. Give it its own `DB_NAME`.
3. Pin a released `mcp-core-auth` version from PyPI (not a git commit).
4. Use `@swapp1990/auth-web` for web sign-in, and the client defaults above elsewhere.

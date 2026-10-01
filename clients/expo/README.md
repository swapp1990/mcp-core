# @swapp1990/auth-expo

Sign-in for swapp1990 iPhone apps (Expo): one shared Logto account, with the
[platform contract](../../docs/platform-contract.md) defaults baked in. It is a thin
wrapper over `@logto/rn`, and the native twin of [`@swapp1990/auth-web`](../web/README.md).

- Logto host and issuer `https://auth.designforyou.swapp1990.org`
- `prompt=consent` only, never `login`
- An ephemeral browser session (`preferEphemeralSession`), so one app never signs in as another app's account
- Google, Apple or email (`signIn("google" | "apple" | "email")`), or Logto's page with all three (`signIn()`)
- Tokens in SecureStore (the `@logto/rn` store: key in the keychain, ciphertext in AsyncStorage), refreshed with `offline_access`
- A rejected session or a 401 from the product API clears the tokens and fires `onExpire`, so the app shows the signed-out state instead of failing quietly
- Sign-out revokes the refresh token and clears the device, with no browser round trip
- `deleteAccount()` calls the product backend, which deletes the shared Logto user, then signs out

Callers can't change the prompt, the ephemeral session or the base scopes.

## Install

The package ships as a GitHub release asset of mcp-core, not from an npm registry. Install the
native peers with `expo install` first, so they match the app's Expo SDK:

```bash
npx expo install @logto/rn expo-secure-store expo-web-browser expo-crypto @react-native-async-storage/async-storage
npm install https://github.com/swapp1990/mcp-core/releases/download/auth-expo-v0.1.0/swapp1990-auth-expo-0.1.0.tgz
```

`package-lock.json` records the tarball's integrity hash, and EAS downloads the tarball at build
time. Adding `expo-secure-store` to an app that lacks it needs a new binary; an OTA update can't
ship it.

## Use

```js
import { createAuth } from "@swapp1990/auth-expo";
import { AuthProvider, useAuth } from "@swapp1990/auth-expo/react";

export const auth = createAuth({
  appId: "<Logto Native app id>",
  resource: "https://<product>.swapp1990.org",       // the token audience
  redirectUri: "<scheme>://callback",               // registered on the Logto app
  scopes: [],                                       // product scopes, added to the base set
  deleteAccountUrl: "https://<product>.swapp1990.org/api/account/delete",
});

<AuthProvider auth={auth}><App /></AuthProvider>;

const { ready, signedIn, expired, user, signIn, signOut, authFetch, deleteAccount } = useAuth();
```

| Call | Result |
|---|---|
| `signIn(method?)` | `AuthUser {id, name, email, picture}`. Throws `AuthCancelled` when the person closes the browser |
| `getAccessToken()` | The token. `null` (and `onExpire` fires) when Logto rejects the session. Throws `AuthUnavailable` on network or server errors and keeps the session |
| `authFetch(url, init)` | `fetch` with the bearer. A 401 calls `expire()` and throws `AuthExpired` |
| `expire()` | Clears the tokens and fires `onExpire`. Call it when a non-`fetch` transport gets a 401 |
| `onExpire(cb)`, `onChange(cb)` | Return an unsubscribe function. `onChange` receives `"signedIn"`, `"signedOut"` or `"expired"` |
| `isAuthenticated()`, `getUser()` | From the stored ID token |
| `signOut()` | Revokes the refresh token and clears the device. Offline, it still clears the device |
| `deleteAccount(url? \| {url, body, headers})` | POSTs with the bearer (`body` as JSON), then signs out and returns the response body. A non-2xx throws `DeleteAccountFailed` and keeps the session |

Gate screens on `signedIn`, never on having a token. `expired` is true when a saved session stopped working, so the app can say "sign in again".

`auth.client()` returns the `@logto/rn` client, for raw ID-token claims such as `username`.
Pass `Client` to `createAuth` to replace it (tests, playtest modes). For a fake signed-in user,
render your own `AuthContext.Provider` from `@swapp1990/auth-expo/react`.

**Config changes sign the person out once.** `createAuth` stores `endpoint|appId|resource|scopes` in
SecureStore. When they differ from the last launch, it clears the tokens and fires `onExpire`,
because Logto binds a refresh token to the resources of its original grant. The first launch
with this package counts as a change, so any session `@logto/rn` stored before is dropped.

## Migrating LetMeActForYou (`letmeactforyou-ios`)

The audience moves from DesignForYou's API to `https://actforyou.swapp1990.org`, so every build-18
user signs in once on build 19. Change `EXPO_PUBLIC_LOGTO_API_RESOURCE` in all three `eas.json`
profiles too.

```js
// src/logto.js
import { AuthExpired, createAuth } from "@swapp1990/auth-expo";

export const auth = createAuth({
  appId: process.env.EXPO_PUBLIC_LOGTO_APP_ID || "lwg600ndwbb83omoig9t4",
  resource: process.env.EXPO_PUBLIC_LOGTO_API_RESOURCE || "https://actforyou.swapp1990.org",
  redirectUri: process.env.EXPO_PUBLIC_LOGTO_REDIRECT_URI || "letmeactforyou://callback",
  deleteAccountUrl: "https://actforyou.swapp1990.org/api/account/delete",
});

// App.js: <LogtoProvider config={LOGTO_CONFIG}> becomes
<AuthProvider auth={auth}><PersonalityApp /></AuthProvider>

// PersonalityProvider.js: useLogto() becomes useAuth().
//   isInitialized -> ready, isAuthenticated -> signedIn, (await getIdTokenClaims()).sub -> user.id
//   signIn(logtoSignInOptions(method)) -> signIn(method); treat AuthCancelled as "not signed in", not an error
//   deleteAccount: await auth.deleteAccount(); then the existing logout() clean-up (RevenueCat, cache)

// actingApi.js request(): authenticated calls go through authFetch, and AuthExpired is never retried.
const response = await (getAccessToken ? auth.authFetch : fetch)(apiPath(path), { method, signal, headers, body });
// in the catch block, before the retry:
if (error instanceof AuthExpired) throw error;
```

Change the delete copy in `AccountScreen.js` to say the account is removed from every swapp1990
product. `LetMeActForYouApp.js` (not mounted) still sends this token to DesignForYou, which will
reject the new audience.

## Migrating WriteForYou (`writer-ios`)

The app id, resource and redirect stay the same, so the session in AsyncStorage is imported
once and the person stays signed in. `@logto/rn` and `expo-secure-store` are new native modules,
so this ships as a new binary (2.0.1).

```js
// src/writeforyou/auth.js replaces the hand-rolled PKCE client
import { useState } from "react";
import { AuthCancelled, AuthExpired, createAuth } from "@swapp1990/auth-expo";
import { AuthProvider as SharedProvider, useAuth } from "@swapp1990/auth-expo/react";

export const auth = createAuth({
  appId: "7vsb81xbjs53sf63hh266",
  resource: "https://writer.swapp1990.org",
  redirectUri: "lmwfy://callback/",
  deleteAccountUrl: "https://writer.swapp1990.org/api/account/delete",
  legacySession: "@writeforyou/logto.session.v1",  // imported once, then the plain-text key is removed
});

export const AuthProvider = ({ children }) => <SharedProvider auth={auth}>{children}</SharedProvider>;

// Keeps WriteForYouApp.js's field names.
export function useWriterAuth() {
  const a = useAuth();
  const [authError, setAuthError] = useState("");
  const signIn = (method) => a.signIn(method).catch((error) => {
    if (!(error instanceof AuthCancelled)) setAuthError(error.message);
    throw error;
  });
  return {
    user: a.user && { id: a.user.id, email: a.user.email, user_metadata: { email: a.user.email, full_name: a.user.name } },
    loading: !a.ready,
    isAuthenticated: a.signedIn,
    authError,
    signInWithGoogle: () => signIn("google"),
    signInWithApple: () => signIn("apple"),
    signInWithEmail: () => signIn("email"),
    signOut: a.signOut,
    getAccessToken: a.getAccessToken,
  };
}

// api.js apiFetch: a 401 now signs the person out instead of leaving them "signed in".
let res;
try {
  res = await auth.authFetch(`${WRITER_API_BASE_URL}${path}`, options);
} catch (error) {
  if (error instanceof AuthExpired) throw new WriterApiError("Session expired. Sign in again.", { status: 401, code: "session_expired" });
  throw new WriterApiError("Network connection failed. Check your connection and retry.", { status: 0, code: "network_error" });
}

// deleteAccount: auth.deleteAccount() (same /api/account/delete path, then signed out)
```

`LOCAL_PLAYTEST` renders its own `AuthContext.Provider` value instead of `AuthProvider`.

## Test

```bash
npm test
```

The tests run on Node's test runner against fakes of `@logto/rn`, `expo-secure-store` and
AsyncStorage (`test/support`). They need no install.

`npm run check:metro` packs the tarball, installs it into `fixture/` and runs
`expo export --platform ios`, so a Metro or `exports` break fails before an app build does. It
installs Expo 54 into the fixture (network, several hundred MB). To reuse an existing Expo 54
app's modules instead, without writing to them:

```bash
npm run check:metro -- --node-modules <path to an Expo 54 app>/node_modules
```

## Release

Bump `version`, then from this folder:

```bash
npm test && npm pack
gh release create auth-expo-v<version> swapp1990-auth-expo-<version>.tgz --repo swapp1990/mcp-core
```

Tags are immutable: a re-uploaded asset under the same tag breaks every lockfile that pinned it, so
bump the version instead.

import LogtoClient, { LogtoClientError, LogtoRequestError } from "@logto/browser";

/** Sign-in host and token issuer for every swapp1990 product. */
export const LOGTO_ENDPOINT = "https://auth.designforyou.swapp1990.org";

const BASE_SCOPES = ["openid", "profile", "email", "offline_access"];

/** Only same-site paths, and never back into the sign-in flow itself. */
export function safeReturnPath(value) {
  const path = value || "/";
  if (!path.startsWith("/") || path.startsWith("//") || /^\/(signin|callback)(\/|\?|$)/.test(path)) return "/";
  return path;
}

/** Logto refused the saved session: nothing to retry, the person has to sign in again. */
export function sessionIsDead(err) {
  if (err instanceof LogtoClientError) return true;
  if (err instanceof LogtoRequestError) return /^oidc\.(invalid_|access_denied)/.test(err.code);
  return false;
}

export function userFromClaims(claims) {
  if (!claims) return null;
  const email = String(claims.email ?? "");
  return {
    id: String(claims.sub ?? ""),
    name: String(claims.name ?? "") || email.split("@")[0],
    email,
    picture: String(claims.picture ?? ""),
  };
}

function readSession(key) {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

/**
 * One product's sign-in, with the platform defaults baked in (mcp-core docs/platform-contract.md).
 * `resource` is the product's API resource indicator; `scopes` adds product scopes to the base set.
 */
export function createAuth({
  appId,
  resource,
  scopes = [],
  endpoint = LOGTO_ENDPOINT,
  callbackPath = "/callback",
  returnKey = "swapp1990.return-to",
  legacyReturnKeys = [],
  Client = LogtoClient,
} = {}) {
  if (!appId) throw new Error("createAuth needs the product's Logto appId");
  const config = {
    endpoint,
    appId,
    resources: resource ? [resource] : [],
    scopes: [...new Set([...BASE_SCOPES, ...scopes])],
  };
  let client = null;
  const logto = () => (client ??= new Client(config, true));
  const listeners = new Set();
  let callbackRun = null;

  function expire() {
    void Promise.resolve(logto().clearAllTokens()).catch(() => {});
    listeners.forEach((listener) => listener());
  }

  return {
    config,
    client: logto,

    /** `method` picks Google, Apple or the email screen; omit it to show Logto's page with all three. */
    async signIn(method, returnTo = "/") {
      try {
        sessionStorage.setItem(returnKey, safeReturnPath(returnTo));
      } catch {
        // Without storage the callback lands on "/".
      }
      await logto().signIn({
        redirectUri: `${window.location.origin}${callbackPath}`,
        // Never "login": it would skip a session the person already has in another swapp1990 app.
        prompt: "consent",
        ...(method === "email"
          ? { firstScreen: "identifier:sign_in", identifiers: ["email"] }
          : method
            ? { directSignIn: { method: "social", target: method } }
            : {}),
      });
    },

    /** Finishes the redirect; the code is single-use, so repeat calls share the first run. */
    completeSignIn() {
      callbackRun ??= (async () => {
        const url = window.location.href;
        if (await logto().isSignInRedirected(url)) await logto().handleSignInCallback(url);
      })();
      return callbackRun;
    },

    consumeReturnTo() {
      const keys = [returnKey, ...legacyReturnKeys];
      const value = keys.map(readSession).find(Boolean) || "";
      try {
        keys.forEach((key) => sessionStorage.removeItem(key));
      } catch {
        // Nothing stored.
      }
      return safeReturnPath(value);
    },

    /** Resolves to null (and fires onExpire) when Logto rejects the saved session; other failures throw. */
    async getAccessToken() {
      try {
        return await logto().getAccessToken(resource);
      } catch (err) {
        if (sessionIsDead(err)) {
          expire();
          return null;
        }
        throw err;
      }
    },

    /** Drops the local session; call it when the product API answers 401. */
    expire,

    onExpire(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },

    isAuthenticated: () => logto().isAuthenticated(),

    async getUser() {
      return userFromClaims(await logto().getIdTokenClaims().catch(() => null));
    },

    /** Ends the Logto session too; `postLogoutRedirect` must be registered on the Logto app. */
    signOut(postLogoutRedirect = window.location.origin) {
      return logto().signOut(postLogoutRedirect);
    },
  };
}

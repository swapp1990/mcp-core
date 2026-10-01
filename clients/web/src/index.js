import LogtoClient, { LogtoClientError, LogtoRequestError } from "@logto/browser";
import { HINT_DOMAIN, formatHint, hintCookieString, hintDomainFor, parseHint, readHintCookie } from "./hint.js";

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

function writeSession(key, value) {
  try {
    if (value == null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch {
    // Without storage the flag is lost; callers fall back to signed out.
  }
}

const SWITCH_KEY = "swapp1990.switch";
const SILENT_KEY = "swapp1990.silent";
const ANNOUNCE_KEY = "swapp1990.announce";

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
  accountHint = true,
  hintDomain = HINT_DOMAIN,
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
  // The account this page is showing, and whether another site switched it out from under us.
  let renderedSub = null;
  let switchedElsewhere = false;

  const hintOn = () => accountHint && hintDomainFor(window.location.hostname, hintDomain) !== null;
  const readHint = () => (hintOn() ? parseHint(readHintCookie(document.cookie)) : { kind: "unknown", raw: null });
  function writeHint(sub) {
    if (!hintOn()) return;
    document.cookie = hintCookieString(formatHint(sub), hintDomainFor(window.location.hostname, hintDomain), window.location.protocol === "https:");
  }
  const currentPath = () => `${window.location.pathname ?? "/"}${window.location.search ?? ""}`;
  const onCallbackPage = () => String(window.location.pathname ?? "").replace(/\/+$/, "") === callbackPath;

  async function storedSub() {
    try {
      return (await logto().getIdTokenClaims())?.sub ?? null;
    } catch {
      return null;
    }
  }

  /** Best effort: the old account's refresh token must not outlive the switch. */
  async function revokeRefreshToken() {
    try {
      const token = await logto().getRefreshToken();
      if (!token) return;
      await globalThis.fetch(`${endpoint}/oidc/token/revocation`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ client_id: appId, token }),
      });
    } catch {
      // Revocation failing never blocks the switch.
    }
  }

  async function dropTokens() {
    await revokeRefreshToken();
    await Promise.resolve(logto().clearAllTokens()).catch(() => {});
    renderedSub = null;
  }

  async function getUser() {
    return userFromClaims(await logto().getIdTokenClaims().catch(() => null));
  }

  function expire() {
    void Promise.resolve(logto().clearAllTokens()).catch(() => {});
    listeners.forEach((listener) => listener());
  }

  async function signIn(method, returnTo = "/") {
    writeSession(returnKey, safeReturnPath(returnTo));
    await logto().signIn({
      redirectUri: `${window.location.origin}${callbackPath}`,
      // Never "login": it would skip a session the person already has in another swapp1990 app.
      // Never "none" either: Logto then withholds the refresh token and refuses apps the session has not visited.
      prompt: "consent",
      ...(method === "email"
        ? { firstScreen: "identifier:sign_in", identifiers: ["email"] }
        : method
          ? { directSignIn: { method: "social", target: method } }
          : {}),
    });
  }

  /** Signs in as whatever account the Logto session holds; once per hint value per tab, so a stale hint cannot loop. */
  async function adoptSessionAccount(raw, returnTo) {
    if (readSession(SILENT_KEY) === raw) return false;
    writeSession(SILENT_KEY, raw);
    writeSession(ANNOUNCE_KEY, "1");
    await signIn(undefined, returnTo);
    return true;
  }

  async function reconcile(signedIn) {
    const hint = readHint();
    const mine = signedIn ? await storedSub() : null;
    if (hint.kind === "unknown") {
      if (mine) writeHint(mine);
      return { signedIn };
    }
    if (hint.kind === "signed-out") {
      writeSession(SILENT_KEY, null);
      if (signedIn) await dropTokens();
      return { signedIn: false };
    }
    if (signedIn && mine === hint.sub) {
      writeSession(SILENT_KEY, null);
      return { signedIn: true };
    }
    if (signedIn) await dropTokens();
    return (await adoptSessionAccount(hint.raw, currentPath())) ? { redirecting: true } : { signedIn: false };
  }

  return {
    config,
    client: logto,

    /** `method` picks Google, Apple or the email screen; omit it to show Logto's page with all three. */
    signIn,

    /** Finishes the redirect; the code is single-use, so repeat calls share the first run. */
    completeSignIn() {
      callbackRun ??= (async () => {
        const url = window.location.href;
        if (!(await logto().isSignInRedirected(url))) return;
        await logto().handleSignInCallback(url);
        const sub = await storedSub();
        if (sub) {
          renderedSub = sub;
          writeHint(sub);
        }
      })();
      return callbackRun;
    },

    /**
     * Run once per page load, before showing any signed-in UI. Finishes a pending account switch, then makes
     * this page match the account in the shared hint. `redirecting` means the page is leaving for Logto.
     * `announced` means a silent sign-in just happened and the app should say who is signed in.
     */
    async init() {
      const pending = readSession(SWITCH_KEY);
      if (pending) {
        writeSession(SWITCH_KEY, null);
        let returnTo = "/";
        try {
          returnTo = JSON.parse(pending).returnTo;
        } catch {
          // A damaged flag falls back to "/".
        }
        await signIn(undefined, returnTo);
        return { signedIn: false, user: null, redirecting: true, announced: false };
      }
      let signedIn = await logto().isAuthenticated();
      if (!onCallbackPage() && hintOn()) {
        const outcome = await reconcile(signedIn);
        if (outcome.redirecting) return { signedIn: false, user: null, redirecting: true, announced: false };
        signedIn = outcome.signedIn;
      }
      renderedSub = signedIn ? await storedSub() : null;
      const announced = signedIn && readSession(ANNOUNCE_KEY) === "1";
      if (!onCallbackPage()) writeSession(ANNOUNCE_KEY, null);
      return { signedIn, user: signedIn ? await getUser() : null, redirecting: false, announced };
    },

    /** Ends the Logto session for every swapp1990 site, then shows Logto's sign-in page to pick another account. */
    async switchAccount(returnTo = currentPath()) {
      writeSession(SWITCH_KEY, JSON.stringify({ returnTo: safeReturnPath(returnTo) }));
      writeSession(SILENT_KEY, null);
      writeSession(ANNOUNCE_KEY, null);
      writeHint(null);
      renderedSub = null;
      await logto().signOut(window.location.origin);
    },

    /**
     * Keeps an open page on the account in the shared hint. A sign-out elsewhere signs this page out; a different
     * account reloads it as that account. Returns an unsubscribe function.
     */
    watchAccount(listener = () => {}) {
      if (!hintOn() || typeof document === "undefined") return () => {};
      let busy = false;
      const check = async () => {
        if (busy) return;
        busy = true;
        try {
          const hint = readHint();
          if (hint.kind === "unknown") return;
          if (hint.kind === "signed-out") {
            if (renderedSub === null) return;
            await dropTokens();
            expire();
            listener({ type: "signed-out" });
            return;
          }
          if (hint.sub === renderedSub) return;
          // From here no request leaves this page as the old account.
          switchedElsewhere = true;
          listener({ type: "reloading" });
          // Another tab of this app may already hold the new account; then a plain reload picks it up.
          if ((await storedSub()) === hint.sub) {
            window.location.reload();
            return;
          }
          await revokeRefreshToken();
          if (!(await adoptSessionAccount(hint.raw, currentPath()))) window.location.reload();
        } finally {
          busy = false;
        }
      };
      const onVisible = () => {
        if (document.visibilityState === "visible") void check();
      };
      const onFocus = () => void check();
      document.addEventListener("visibilitychange", onVisible);
      window.addEventListener("focus", onFocus);
      window.addEventListener("storage", onFocus);
      return () => {
        document.removeEventListener("visibilitychange", onVisible);
        window.removeEventListener("focus", onFocus);
        window.removeEventListener("storage", onFocus);
      };
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
      if (switchedElsewhere) return null;
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

    getUser,

    /** Ends the Logto session for every swapp1990 site. Always returns to the site root, the only post-logout URI the apps register. */
    async signOut() {
      writeHint(null);
      writeSession(SILENT_KEY, null);
      writeSession(ANNOUNCE_KEY, null);
      renderedSub = null;
      await logto().signOut(window.location.origin);
    },
  };
}

import { LogtoClient } from "@logto/rn";
import AsyncStorage from "@react-native-async-storage/async-storage";
import * as SecureStore from "expo-secure-store";

/** Sign-in host and token issuer for every swapp1990 product. */
export const LOGTO_ENDPOINT = "https://auth.designforyou.swapp1990.org";

const BASE_SCOPES = ["openid", "profile", "email", "offline_access"];
const SOCIAL_METHODS = new Set(["google", "apple"]);

/** Sign-in was closed or dismissed by the person, not refused by Logto. */
export class AuthCancelled extends Error {
  constructor(type = "cancel") {
    super("Sign-in was cancelled.");
    this.name = "AuthCancelled";
    this.code = "auth_cancelled";
    this.type = type;
  }
}

/** The session is gone (Logto or the product API rejected it); the person has to sign in again. */
export class AuthExpired extends Error {
  constructor() {
    super("The session expired. Sign in again.");
    this.name = "AuthExpired";
    this.code = "auth_expired";
  }
}

/** Logto could not be reached or failed (network, 5xx); the session is kept. */
export class AuthUnavailable extends Error {
  constructor(cause) {
    super("Sign-in service is unavailable. Try again.");
    this.name = "AuthUnavailable";
    this.code = "auth_unavailable";
    this.cause = cause;
  }
}

export class DeleteAccountFailed extends Error {
  constructor(status, body) {
    super(`Account deletion failed (HTTP ${status}).`);
    this.name = "DeleteAccountFailed";
    this.code = "delete_account_failed";
    this.status = status;
    this.body = body;
  }
}

/** Logto refused the saved session: nothing to retry. Matches by name so a duplicated SDK copy still counts. */
export function sessionIsDead(err) {
  if (err?.name === "LogtoClientError") return true;
  if (err?.name === "LogtoRequestError") return /^oidc\.(invalid_|access_denied)/.test(String(err.code));
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

/** The per-method Logto parameters; `undefined` shows Logto's page with every method. */
export function signInOptions(method) {
  if (method === undefined) return {};
  if (method === "email") return { firstScreen: "identifier:sign_in", identifiers: ["email"] };
  if (SOCIAL_METHODS.has(method)) return { directSignIn: { method: "social", target: method } };
  throw new TypeError(`Unknown sign-in method "${method}"; use "google", "apple" or "email"`);
}

/**
 * One product's native sign-in, with the platform defaults baked in (mcp-core docs/platform-contract.md).
 * `resource` is the product's API resource indicator; `scopes` adds product scopes to the base set.
 */
export function createAuth({
  appId,
  resource,
  redirectUri,
  scopes = [],
  endpoint = LOGTO_ENDPOINT,
  deleteAccountUrl,
  legacySession,
  Client = LogtoClient,
} = {}) {
  if (!appId) throw new Error("createAuth needs the product's Logto appId");
  if (!resource) throw new Error("createAuth needs the product's API resource");
  if (!redirectUri) throw new Error("createAuth needs the app's redirectUri");
  if (/^https?:\/\/auth\.swapp1990\.org(\/|$)/.test(endpoint)) {
    // Same tenant, but another cookie host and issuer: every backend would reject its tokens.
    throw new Error(`Use ${LOGTO_ENDPOINT}, not auth.swapp1990.org`);
  }
  const config = {
    endpoint,
    appId,
    resources: [resource],
    scopes: [...new Set([...BASE_SCOPES, ...scopes])],
    // Logto issues the offline_access refresh token only with consent; never "login" (platform contract).
    prompt: "consent",
    // Keeps one app's Logto cookies out of another's sign-in.
    preferEphemeralSession: true,
  };
  const fingerprint = [config.endpoint, appId, resource, config.scopes.join(" ")].join("|");
  const fingerprintKey = `swapp1990.auth-expo.${appId}.config`;

  let client = null;
  const logto = () => (client ??= new Client(config));
  const listeners = new Set();
  const emit = (event, user = null) => listeners.forEach((listener) => listener(event, user));

  let started = null;
  const ready = () => (started ??= init());

  async function init() {
    const sdk = logto();
    try {
      if ((await SecureStore.getItemAsync(fingerprintKey)) !== fingerprint) {
        // Refresh tokens are bound to the original grant's resources, so a changed config can't reuse them.
        const hadSession = await sdk.isAuthenticated();
        await sdk.clearAllTokens();
        await SecureStore.setItemAsync(fingerprintKey, fingerprint);
        if (hadSession) emit("expired");
      }
    } catch {
      // SecureStore unavailable: the check runs on the next launch.
    }
    if (legacySession) await importLegacySession(sdk);
  }

  async function importLegacySession(sdk) {
    const raw = await AsyncStorage.getItem(legacySession).catch(() => null);
    if (!raw) return;
    try {
      const saved = JSON.parse(raw);
      const refreshToken = saved?.refresh_token ?? saved?.refreshToken;
      const idToken = saved?.id_token ?? saved?.idToken;
      if (refreshToken && idToken && !(await sdk.getRefreshToken())) {
        await sdk.setRefreshToken(refreshToken);
        await sdk.setIdToken(idToken);
      }
    } catch {
      // Unreadable: the person signs in once.
    }
    await AsyncStorage.removeItem(legacySession).catch(() => {});
  }

  function expire() {
    emit("expired");
    return Promise.resolve(logto().clearAllTokens()).catch(() => {});
  }

  async function getUser() {
    await ready();
    return userFromClaims(await logto().getIdTokenClaims().catch(() => null));
  }

  async function getAccessToken() {
    await ready();
    try {
      return await logto().getAccessToken(resource);
    } catch (err) {
      if (sessionIsDead(err)) {
        await expire();
        return null;
      }
      throw new AuthUnavailable(err);
    }
  }

  async function authFetch(url, init = {}) {
    const token = await getAccessToken();
    if (!token) throw new AuthExpired();
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${token}`);
    const response = await globalThis.fetch(url, { ...init, headers });
    if (response.status === 401) {
      await expire();
      throw new AuthExpired();
    }
    return response;
  }

  async function signOut() {
    await ready();
    const sdk = logto();
    try {
      await sdk.signOut();
    } catch {
      // Offline (no OIDC discovery): clear the device anyway; the refresh token lapses on its TTL.
      await Promise.resolve(sdk.clearAllTokens()).catch(() => {});
    }
    emit("signedOut");
  }

  return {
    config,
    client: logto,

    /** `method` picks Google, Apple or the email screen; omit it to show Logto's page with all three. */
    async signIn(method) {
      const options = signInOptions(method);
      await ready();
      const sdk = logto();
      try {
        await sdk.signIn({ redirectUri, prompt: "consent", ...options });
      } catch (err) {
        const type = sdk.authSessionResult?.type;
        if (err?.code === "auth_session_failed" && (type === "cancel" || type === "dismiss")) {
          throw new AuthCancelled(type);
        }
        throw err;
      }
      const user = await getUser();
      emit("signedIn", user);
      return user;
    },

    /** Resolves to null (and fires onExpire) when Logto rejects the session; throws AuthUnavailable otherwise. */
    getAccessToken,

    /** Drops the local session; call it when a product API answers 401. */
    expire,

    onExpire(listener) {
      const wrapped = (event) => event === "expired" && listener();
      listeners.add(wrapped);
      return () => listeners.delete(wrapped);
    },

    /** Fires ("signedIn", user) | ("signedOut") | ("expired") after the session changes. */
    onChange(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },

    async isAuthenticated() {
      await ready();
      return logto().isAuthenticated();
    },

    getUser,

    /** Revokes the refresh token and clears the device; no browser round trip. */
    signOut,

    /** Adds the bearer; a 401 expires the session and throws AuthExpired. */
    authFetch,

    /** POSTs to the product's delete route (the backend deletes the shared Logto user), then signs out. */
    async deleteAccount(options = {}) {
      const { url = deleteAccountUrl, body, headers } = typeof options === "string" ? { url: options } : options;
      if (!url) throw new Error("deleteAccount needs a URL (createAuth deleteAccountUrl)");
      const response = await authFetch(url, {
        method: "POST",
        headers: {
          Accept: "application/json",
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
          ...headers,
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new DeleteAccountFailed(response.status, payload);
      await signOut();
      return payload;
    },
  };
}

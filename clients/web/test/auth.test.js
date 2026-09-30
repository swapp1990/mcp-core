import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { LogtoClientError, LogtoRequestError } from "@logto/browser";
import { LOGTO_ENDPOINT, createAuth, safeReturnPath } from "../src/index.js";

class MemoryStorage {
  #items = new Map();
  getItem(key) {
    return this.#items.has(key) ? this.#items.get(key) : null;
  }
  setItem(key, value) {
    this.#items.set(key, String(value));
  }
  removeItem(key) {
    this.#items.delete(key);
  }
}

class FakeClient {
  static last = null;
  constructor(config) {
    this.config = config;
    this.signInCalls = [];
    this.cleared = 0;
    this.tokenResult = async () => "token-1";
    FakeClient.last = this;
  }
  async signIn(options) {
    this.signInCalls.push(options);
  }
  async getAccessToken(resource) {
    this.resource = resource;
    return this.tokenResult();
  }
  async clearAllTokens() {
    this.cleared += 1;
  }
  async isAuthenticated() {
    return true;
  }
  async getIdTokenClaims() {
    return { sub: "u1", email: "reader@example.com", name: "" };
  }
  async isSignInRedirected() {
    return true;
  }
  async handleSignInCallback() {
    this.callbacks = (this.callbacks ?? 0) + 1;
  }
  async signOut(redirect) {
    this.signedOutTo = redirect;
  }
}

beforeEach(() => {
  globalThis.window = { location: { origin: "https://app.example.com", href: "https://app.example.com/callback?code=x" } };
  globalThis.sessionStorage = new MemoryStorage();
});

const make = (extra = {}) => createAuth({ appId: "app1", resource: "https://api.example.com", Client: FakeClient, ...extra });

test("defaults to the shared designforyou host and adds offline_access", () => {
  const auth = make({ scopes: ["writer:read"] });
  assert.equal(auth.config.endpoint, LOGTO_ENDPOINT);
  assert.equal(LOGTO_ENDPOINT, "https://auth.designforyou.swapp1990.org");
  assert.deepEqual(auth.config.scopes, ["openid", "profile", "email", "offline_access", "writer:read"]);
  assert.deepEqual(auth.config.resources, ["https://api.example.com"]);
});

test("sign-in asks for consent only, never login", async () => {
  const auth = make();
  await auth.signIn("google", "/library");
  const [call] = FakeClient.last.signInCalls;
  assert.equal(call.prompt, "consent");
  assert.equal(call.redirectUri, "https://app.example.com/callback");
  assert.deepEqual(call.directSignIn, { method: "social", target: "google" });
});

test("email opens Logto's email screen; no method shows every option", async () => {
  const auth = make();
  await auth.signIn("email");
  await auth.signIn();
  const [email, any] = FakeClient.last.signInCalls;
  assert.equal(email.firstScreen, "identifier:sign_in");
  assert.deepEqual(email.identifiers, ["email"]);
  assert.equal(any.directSignIn, undefined);
  assert.equal(any.firstScreen, undefined);
});

test("return path survives the redirect and falls back to legacy keys", async () => {
  const auth = make({ legacyReturnKeys: ["old.key"] });
  await auth.signIn("apple", "/story/1");
  assert.equal(auth.consumeReturnTo(), "/story/1");
  assert.equal(auth.consumeReturnTo(), "/");
  sessionStorage.setItem("old.key", "/articles");
  assert.equal(auth.consumeReturnTo(), "/articles");
});

test("return paths stay on this site and out of the sign-in flow", () => {
  assert.equal(safeReturnPath("https://evil.example/x"), "/");
  assert.equal(safeReturnPath("//evil.example"), "/");
  assert.equal(safeReturnPath("/callback?code=1"), "/");
  assert.equal(safeReturnPath("/signin"), "/");
  assert.equal(safeReturnPath("/write?x=1"), "/write?x=1");
});

test("a dead session clears tokens, returns null and notifies listeners", async () => {
  const auth = make();
  let expired = 0;
  auth.onExpire(() => (expired += 1));
  auth.client().tokenResult = async () => {
    throw new LogtoClientError("not_authenticated");
  };
  assert.equal(await auth.getAccessToken(), null);
  assert.equal(expired, 1);
  assert.equal(FakeClient.last.cleared, 1);
});

test("a rejected refresh token counts as a dead session", async () => {
  const auth = make();
  let expired = 0;
  auth.onExpire(() => (expired += 1));
  auth.client().tokenResult = async () => {
    throw new LogtoRequestError("oidc.invalid_grant", "grant request is invalid");
  };
  assert.equal(await auth.getAccessToken(), null);
  assert.equal(expired, 1);
});

test("network failures are not treated as sign-out", async () => {
  const auth = make();
  let expired = 0;
  auth.onExpire(() => (expired += 1));
  auth.client().tokenResult = async () => {
    throw new TypeError("Failed to fetch");
  };
  await assert.rejects(auth.getAccessToken(), TypeError);
  assert.equal(expired, 0);
});

test("the callback runs once however often it is called", async () => {
  const auth = make();
  await Promise.all([auth.completeSignIn(), auth.completeSignIn()]);
  assert.equal(FakeClient.last.callbacks, 1);
});

test("sign-out ends the Logto session and returns to the origin", async () => {
  const auth = make();
  await auth.signOut();
  assert.equal(FakeClient.last.signedOutTo, "https://app.example.com");
});

test("the user comes from ID token claims", async () => {
  const auth = make();
  assert.deepEqual(await auth.getUser(), { id: "u1", name: "reader", email: "reader@example.com", picture: "" });
});

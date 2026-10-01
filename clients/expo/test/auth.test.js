import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { items as asyncItems } from "@react-native-async-storage/async-storage";
import { LogtoClient, LogtoClientError, LogtoError, LogtoRequestError, storage } from "@logto/rn";
import { items as secureItems } from "expo-secure-store";
import {
  AuthCancelled,
  AuthExpired,
  AuthUnavailable,
  DeleteAccountFailed,
  LOGTO_ENDPOINT,
  createAuth,
} from "../src/index.js";

const RESOURCE = "https://actforyou.swapp1990.org";
let fetchCalls;
let fetchResponse;

beforeEach(() => {
  storage.clear();
  secureItems.clear();
  asyncItems.clear();
  LogtoClient.instances.length = 0;
  fetchCalls = [];
  fetchResponse = () => new Response(JSON.stringify({ ok: true }), { status: 200 });
  globalThis.fetch = async (url, init) => {
    fetchCalls.push({ url, init });
    return fetchResponse();
  };
});

const make = (extra = {}) =>
  createAuth({ appId: "app1", resource: RESOURCE, redirectUri: "letmeactforyou://callback", ...extra });

async function signedIn(extra) {
  const auth = make(extra);
  await auth.signIn("google");
  return { auth, client: auth.client() };
}

test("pins the shared host, consent, ephemeral sessions and offline_access", () => {
  const auth = make({ scopes: ["acting:write"], prompt: "login", preferEphemeralSession: false });
  auth.client();
  const { config } = LogtoClient.last;
  assert.equal(LOGTO_ENDPOINT, "https://auth.designforyou.swapp1990.org");
  assert.equal(config.endpoint, LOGTO_ENDPOINT);
  assert.equal(config.prompt, "consent");
  assert.equal(config.preferEphemeralSession, true);
  assert.deepEqual(config.scopes, ["openid", "profile", "email", "offline_access", "acting:write"]);
  assert.deepEqual(config.resources, [RESOURCE]);
});

test("refuses the other Logto host and missing product settings", () => {
  assert.throws(() => make({ endpoint: "https://auth.swapp1990.org" }), /auth\.swapp1990\.org/);
  assert.throws(() => make({ appId: "" }), /appId/);
  assert.throws(() => make({ resource: "" }), /resource/);
  assert.throws(() => make({ redirectUri: "" }), /redirectUri/);
});

test("social sign-in asks for consent and goes straight to the provider", async () => {
  const auth = make();
  const user = await auth.signIn("google");
  await auth.signIn("apple");
  const [google, apple] = LogtoClient.last.signInCalls;
  assert.equal(google.prompt, "consent");
  assert.equal(google.redirectUri, "letmeactforyou://callback");
  assert.deepEqual(google.directSignIn, { method: "social", target: "google" });
  assert.deepEqual(apple.directSignIn, { method: "social", target: "apple" });
  assert.deepEqual(user, { id: "u1", name: "reader", email: "reader@example.com", picture: "" });
});

test("email opens Logto's email screen; no method shows every option", async () => {
  const auth = make();
  await auth.signIn("email");
  await auth.signIn();
  const [email, any] = LogtoClient.last.signInCalls;
  assert.equal(email.prompt, "consent");
  assert.equal(email.firstScreen, "identifier:sign_in");
  assert.deepEqual(email.identifiers, ["email"]);
  assert.equal(email.directSignIn, undefined);
  assert.equal(any.prompt, "consent");
  assert.equal(any.directSignIn, undefined);
  assert.equal(any.firstScreen, undefined);
  await assert.rejects(auth.signIn("facebook"), TypeError);
});

test("closing the browser is AuthCancelled, not a sign-in failure", async () => {
  const auth = make();
  auth.client().sessionResult = { type: "cancel" };
  await assert.rejects(auth.signIn("apple"), (err) => err instanceof AuthCancelled && err.type === "cancel");
  auth.client().sessionResult = { type: "dismiss" };
  await assert.rejects(auth.signIn("apple"), AuthCancelled);
  auth.client().sessionResult = { type: "locked" };
  await assert.rejects(auth.signIn("apple"), (err) => err.code === "auth_session_failed");
});

test("sign-in notifies change listeners with the user", async () => {
  const auth = make();
  const events = [];
  auth.onChange((event, user) => events.push([event, user?.id]));
  await auth.signIn("google");
  assert.deepEqual(events, [["signedIn", "u1"]]);
  assert.equal(await auth.isAuthenticated(), true);
});

test("the access token is requested for the product resource", async () => {
  const { auth, client } = await signedIn();
  assert.equal(await auth.getAccessToken(), "token-1");
  assert.equal(client.resource, RESOURCE);
});

test("a dead session clears tokens, returns null and notifies listeners", async () => {
  const { auth, client } = await signedIn();
  let expired = 0;
  auth.onExpire(() => (expired += 1));
  client.tokenResult = async () => {
    throw new LogtoClientError("not_authenticated");
  };
  const before = client.cleared;
  assert.equal(await auth.getAccessToken(), null);
  assert.equal(expired, 1);
  assert.equal(client.cleared, before + 1);
  assert.equal(await auth.isAuthenticated(), false);
});

test("a rejected refresh token counts as a dead session", async () => {
  const { auth, client } = await signedIn();
  let expired = 0;
  const off = auth.onExpire(() => (expired += 1));
  client.tokenResult = async () => {
    throw new LogtoRequestError("oidc.invalid_grant", "grant request is invalid");
  };
  assert.equal(await auth.getAccessToken(), null);
  off();
  assert.equal(await auth.getAccessToken(), null);
  assert.equal(expired, 1);
});

test("network and server failures throw AuthUnavailable and keep the session", async () => {
  const { auth, client } = await signedIn();
  let expired = 0;
  auth.onExpire(() => (expired += 1));
  const before = client.cleared;
  for (const failure of [new TypeError("Network request failed"), new LogtoError("unexpected_response_error")]) {
    client.tokenResult = async () => {
      throw failure;
    };
    await assert.rejects(auth.getAccessToken(), (err) => err instanceof AuthUnavailable && err.cause === failure);
  }
  assert.equal(expired, 0);
  assert.equal(client.cleared, before);
  assert.equal(await auth.isAuthenticated(), true);
});

test("authFetch sends the bearer and keeps the caller's request", async () => {
  const { auth } = await signedIn();
  const signal = new AbortController().signal;
  const response = await auth.authFetch("https://api.example.com/me", {
    method: "POST",
    headers: { "Idempotency-Key": "k1" },
    body: "{}",
    signal,
  });
  assert.equal(response.status, 200);
  const [{ url, init }] = fetchCalls;
  assert.equal(url, "https://api.example.com/me");
  assert.equal(init.method, "POST");
  assert.equal(init.signal, signal);
  assert.equal(init.headers.get("Authorization"), "Bearer token-1");
  assert.equal(init.headers.get("Idempotency-Key"), "k1");
});

test("a 401 from the product API expires the session", async () => {
  const { auth, client } = await signedIn();
  const events = [];
  auth.onChange((event) => events.push(event));
  fetchResponse = () => new Response("{}", { status: 401 });
  const before = client.cleared;
  await assert.rejects(auth.authFetch("https://api.example.com/me"), AuthExpired);
  assert.deepEqual(events, ["expired"]);
  assert.equal(client.cleared, before + 1);
  assert.equal(await auth.isAuthenticated(), false);
});

test("authFetch without a live session throws AuthExpired and sends nothing", async () => {
  const { auth, client } = await signedIn();
  client.tokenResult = async () => {
    throw new LogtoClientError("not_authenticated");
  };
  await assert.rejects(auth.authFetch("https://api.example.com/me"), AuthExpired);
  assert.equal(fetchCalls.length, 0);
});

test("deleteAccount posts with the bearer, then signs out", async () => {
  const { auth, client } = await signedIn({ deleteAccountUrl: "https://actforyou.swapp1990.org/api/account/delete" });
  const events = [];
  auth.onChange((event) => events.push(event));
  fetchResponse = () => new Response(JSON.stringify({ deleted: true, identity_deleted: true }), { status: 200 });
  const result = await auth.deleteAccount({ body: { confirmation: "delete_my_account" }, headers: { "Idempotency-Key": "r1" } });
  assert.deepEqual(result, { deleted: true, identity_deleted: true });
  const [{ url, init }] = fetchCalls;
  assert.equal(url, "https://actforyou.swapp1990.org/api/account/delete");
  assert.equal(init.method, "POST");
  assert.equal(init.headers.get("Authorization"), "Bearer token-1");
  assert.equal(init.headers.get("Content-Type"), "application/json");
  assert.equal(init.headers.get("Idempotency-Key"), "r1");
  assert.equal(init.body, JSON.stringify({ confirmation: "delete_my_account" }));
  assert.equal(client.signOutCalls, 1);
  assert.deepEqual(events, ["signedOut"]);
  assert.equal(await auth.isAuthenticated(), false);
});

test("deleteAccount keeps the session when the backend fails", async () => {
  const { auth, client } = await signedIn();
  fetchResponse = () => new Response(JSON.stringify({ detail: "down" }), { status: 503 });
  await assert.rejects(
    auth.deleteAccount("https://writer.swapp1990.org/api/account/delete"),
    (err) => err instanceof DeleteAccountFailed && err.status === 503 && err.body.detail === "down",
  );
  assert.equal(fetchCalls[0].url, "https://writer.swapp1990.org/api/account/delete");
  assert.equal(fetchCalls[0].init.body, undefined);
  assert.equal(client.signOutCalls, 0);
  assert.equal(await auth.isAuthenticated(), true);
  await assert.rejects(make().deleteAccount(), /URL/);
});

test("sign-out revokes through the SDK and notifies listeners", async () => {
  const { auth, client } = await signedIn();
  const events = [];
  auth.onChange((event) => events.push(event));
  await auth.signOut();
  assert.equal(client.signOutCalls, 1);
  assert.deepEqual(events, ["signedOut"]);
  assert.equal(await auth.isAuthenticated(), false);
});

test("sign-out still clears the device when Logto is unreachable", async () => {
  const { auth, client } = await signedIn();
  client.signOutError = new TypeError("Network request failed");
  await auth.signOut();
  assert.equal(await auth.isAuthenticated(), false);
});

test("a changed config signs the person out once", async () => {
  const first = await signedIn();
  assert.equal(await first.auth.isAuthenticated(), true);

  const sameConfig = make();
  assert.equal(await sameConfig.isAuthenticated(), true);

  const newAudience = make({ resource: "https://other.swapp1990.org" });
  let expired = 0;
  newAudience.onExpire(() => (expired += 1));
  assert.equal(await newAudience.isAuthenticated(), false);
  assert.equal(expired, 1);
  assert.equal(await make({ resource: "https://other.swapp1990.org" }).isAuthenticated(), false);
});

test("the first launch with auth-expo drops a session made without it", async () => {
  storage.set("app1:idToken", "old-id");
  storage.set("app1:refreshToken", "old-refresh");
  const auth = make();
  let expired = 0;
  auth.onExpire(() => (expired += 1));
  assert.equal(await auth.isAuthenticated(), false);
  assert.equal(expired, 1);
});

test("a legacy session is imported once and its plain-text key removed", async () => {
  const key = "@writeforyou/logto.session.v1";
  asyncItems.set(key, JSON.stringify({ access_token: "a", refresh_token: "legacy-refresh", id_token: "legacy-id" }));
  const auth = make({ legacySession: key });
  let expired = 0;
  auth.onExpire(() => (expired += 1));
  assert.equal(await auth.isAuthenticated(), true);
  assert.equal(expired, 0);
  assert.equal(await auth.client().getRefreshToken(), "legacy-refresh");
  assert.equal(asyncItems.has(key), false);

  asyncItems.set(key, JSON.stringify({ refresh_token: "stale-refresh", id_token: "stale-id" }));
  const relaunch = make({ legacySession: key });
  assert.equal(await relaunch.isAuthenticated(), true);
  assert.equal(await relaunch.client().getRefreshToken(), "legacy-refresh");
  assert.equal(asyncItems.has(key), false);
});

test("an unreadable legacy session is removed and the person signs in", async () => {
  const key = "@writeforyou/logto.session.v1";
  asyncItems.set(key, "{not json");
  const auth = make({ legacySession: key });
  assert.equal(await auth.isAuthenticated(), false);
  assert.equal(asyncItems.has(key), false);
});

test("an injected Client replaces the SDK", async () => {
  class Playtest extends LogtoClient {}
  const auth = make({ Client: Playtest });
  assert.ok(auth.client() instanceof Playtest);
});

test("the user comes from ID token claims", async () => {
  const { auth } = await signedIn();
  auth.client().claims = { sub: "u2", email: "actor@example.com", name: "Ana", picture: "https://x/p.png" };
  assert.deepEqual(await auth.getUser(), { id: "u2", name: "Ana", email: "actor@example.com", picture: "https://x/p.png" });
  await auth.signOut();
  assert.equal(await auth.getUser(), null);
});

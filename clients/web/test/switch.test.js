import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { createAuth } from "../src/index.js";
import { HINT_COOKIE, formatHint, hintCookieString, hintDomainFor, parseHint, readHintCookie } from "../src/hint.js";

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

/** document.cookie that keeps name=value pairs and honours Domain (any), Max-Age=0. */
class FakeDocument {
  jar = new Map();
  visibilityState = "visible";
  listeners = {};
  get cookie() {
    return [...this.jar].map(([k, v]) => `${k}=${v}`).join("; ");
  }
  set cookie(line) {
    const [pair, ...attrs] = line.split(";").map((part) => part.trim());
    const [name, ...value] = pair.split("=");
    this.writes.push({ pair, attrs });
    if (attrs.includes("Max-Age=0")) this.jar.delete(name);
    else this.jar.set(name, value.join("="));
  }
  writes = [];
  addEventListener(type, fn) {
    (this.listeners[type] ??= new Set()).add(fn);
  }
  removeEventListener(type, fn) {
    this.listeners[type]?.delete(fn);
  }
  fire(type) {
    for (const fn of this.listeners[type] ?? []) fn();
  }
}

class FakeClient {
  static last = null;
  constructor(config) {
    this.config = config;
    this.signInCalls = [];
    this.cleared = 0;
    this.sub = null;
    this.refreshToken = null;
    FakeClient.last = this;
  }
  async signIn(options) {
    this.signInCalls.push(options);
  }
  async getAccessToken() {
    return "token";
  }
  async getRefreshToken() {
    return this.refreshToken;
  }
  async clearAllTokens() {
    this.cleared += 1;
    this.sub = null;
    this.refreshToken = null;
  }
  async isAuthenticated() {
    return this.sub !== null;
  }
  async getIdTokenClaims() {
    if (!this.sub) throw new Error("not_authenticated");
    return { sub: this.sub, email: `${this.sub}@example.com`, name: "" };
  }
  async isSignInRedirected() {
    return true;
  }
  async handleSignInCallback() {
    this.sub = this.nextSub ?? "u1";
  }
  async signOut(redirect) {
    this.signedOutTo = redirect;
  }
}

let doc;
let fetched;
let reloads;

function setLocation(pathname = "/studio", search = "") {
  const origin = "https://videogen.swapp1990.org";
  globalThis.window.location = {
    origin,
    hostname: "videogen.swapp1990.org",
    protocol: "https:",
    pathname,
    search,
    href: `${origin}${pathname}${search}`,
    reload: () => (reloads.count += 1),
  };
}

beforeEach(() => {
  doc = new FakeDocument();
  fetched = [];
  reloads = { count: 0 };
  const windowListeners = {};
  globalThis.window = {
    addEventListener: (type, fn) => (windowListeners[type] ??= new Set()).add(fn),
    removeEventListener: (type, fn) => windowListeners[type]?.delete(fn),
    fire: (type) => windowListeners[type]?.forEach((fn) => fn()),
  };
  setLocation();
  globalThis.document = doc;
  globalThis.sessionStorage = new MemoryStorage();
  globalThis.fetch = async (url, init) => {
    fetched.push({ url, body: String(init?.body) });
    return { ok: true };
  };
});

const make = (extra = {}) => createAuth({ appId: "app1", resource: "https://api.example.com", Client: FakeClient, ...extra });
const setHint = (raw) => doc.jar.set(HINT_COOKIE, raw);
const hint = () => doc.jar.get(HINT_COOKIE);
const holding = (auth, sub, refreshToken = "rt-1") => {
  auth.client().sub = sub;
  auth.client().refreshToken = refreshToken;
};

// Hint format

test("hint values round-trip and unreadable ones count as unknown", () => {
  assert.deepEqual(parseHint(formatHint("abc123")), { kind: "account", sub: "abc123", raw: "v1.abc123" });
  assert.equal(formatHint(null), "v1.-");
  assert.equal(parseHint("v1.-").kind, "signed-out");
  for (const bad of [null, "", "v1.", "v2.abc", "abc", "v1.%E0%A4%A"]) assert.equal(parseHint(bad).kind, "unknown", String(bad));
  assert.equal(readHintCookie(`a=1; ${HINT_COOKIE}=v1.u1; b=2`), "v1.u1");
  assert.equal(readHintCookie("a=1"), null);
});

test("the cookie is shared across swapp1990.org only", () => {
  assert.equal(hintDomainFor("videogen.swapp1990.org"), "swapp1990.org");
  assert.equal(hintDomainFor("swapp1990.org"), "swapp1990.org");
  assert.equal(hintDomainFor("localhost"), null);
  assert.equal(hintDomainFor("evilswapp1990.org"), null);
  assert.equal(hintDomainFor("swapp1990.org.evil.com"), null);
  const line = hintCookieString("v1.u1", "swapp1990.org", true);
  assert.match(line, /Domain=swapp1990\.org; Path=\/; Max-Age=1209600; SameSite=Lax; Secure$/);
});

// Writing the hint

test("finishing a sign-in records the account", async () => {
  const auth = make();
  setLocation("/callback", "?code=x");
  auth.client().nextSub = "u7";
  await auth.completeSignIn();
  assert.equal(hint(), "v1.u7");
  assert.match(doc.writes.at(-1).attrs.join(";"), /Domain=swapp1990\.org/);
});

test("sign-out writes the signed-out hint and returns to the origin whatever it is given", async () => {
  const auth = make();
  setHint("v1.u1");
  await auth.signOut("/studio");
  assert.equal(hint(), "v1.-");
  assert.equal(FakeClient.last.signedOutTo, "https://videogen.swapp1990.org");
});

test("no hint is written off swapp1990.org or with accountHint false", async () => {
  const auth = make({ accountHint: false });
  holding(auth, "u1");
  await auth.signOut();
  assert.equal(doc.writes.length, 0);

  globalThis.window.location.hostname = "localhost";
  const local = make();
  holding(local, "u1");
  const result = await local.init();
  assert.equal(doc.writes.length, 0);
  assert.equal(result.signedIn, true);
});

// Switch account

test("switch account ends the session, then the next page opens Logto's page for the same path", async () => {
  const auth = make();
  setLocation("/studio", "?tab=2");
  holding(auth, "u1");
  setHint("v1.u1");
  await auth.switchAccount();
  assert.equal(hint(), "v1.-");
  assert.equal(FakeClient.last.signedOutTo, "https://videogen.swapp1990.org");

  // The site root loads after Logto's end-session.
  setLocation("/");
  const next = make();
  const result = await next.init();
  assert.equal(result.redirecting, true);
  const [call] = FakeClient.last.signInCalls;
  assert.equal(call.prompt, "consent");
  assert.equal(call.directSignIn, undefined);
  assert.equal(next.consumeReturnTo(), "/studio?tab=2");

  // Backing out of Logto's page must not start the switch again.
  const after = await make().init();
  assert.equal(after.redirecting, false);
  assert.equal(FakeClient.last.signInCalls.length, 0);
});

// Reconcile on load

test("unknown hint: a signed-in page records its account and nothing else happens", async () => {
  const auth = make();
  holding(auth, "u1");
  const result = await auth.init();
  assert.equal(hint(), "v1.u1");
  assert.deepEqual([result.signedIn, result.redirecting, result.announced], [true, false, false]);
  assert.equal(result.user.email, "u1@example.com");
  assert.equal(FakeClient.last.signInCalls.length, 0);
});

test("unknown hint and signed out: nothing happens", async () => {
  const result = await make().init();
  assert.deepEqual([result.signedIn, result.redirecting], [false, false]);
  assert.equal(doc.writes.length, 0);
});

test("matching hint changes nothing", async () => {
  const auth = make();
  holding(auth, "u1");
  setHint("v1.u1");
  const result = await auth.init();
  assert.equal(result.signedIn, true);
  assert.equal(FakeClient.last.signInCalls.length, 0);
  assert.equal(FakeClient.last.cleared, 0);
  assert.equal(fetched.length, 0);
});

test("a different account in the hint drops this page's tokens and signs in as the new account", async () => {
  const auth = make();
  setLocation("/library", "?page=2");
  holding(auth, "u1", "rt-old");
  setHint("v1.u2");
  const result = await auth.init();
  assert.equal(result.redirecting, true);
  assert.equal(FakeClient.last.cleared, 1);
  assert.equal(fetched.length, 1);
  assert.match(fetched[0].url, /\/oidc\/token\/revocation$/);
  assert.match(fetched[0].body, /client_id=app1/);
  assert.match(fetched[0].body, /token=rt-old/);
  assert.equal(FakeClient.last.signInCalls[0].prompt, "consent");
  assert.equal(auth.consumeReturnTo(), "/library?page=2");
});

test("a signed-out hint signs the page out without a redirect", async () => {
  const auth = make();
  holding(auth, "u1");
  setHint("v1.-");
  const result = await auth.init();
  assert.deepEqual([result.signedIn, result.redirecting], [false, false]);
  assert.equal(FakeClient.last.cleared, 1);
  assert.equal(FakeClient.last.signInCalls.length, 0);
});

test("a page that never saw the person signs in when the hint names an account", async () => {
  setHint("v1.u1");
  const auth = make();
  const result = await auth.init();
  assert.equal(result.redirecting, true);
  assert.equal(FakeClient.last.signInCalls.length, 1);
  assert.equal(fetched.length, 0);
});

test("a silent sign-in runs once per hint value in a tab", async () => {
  setHint("v1.u1");
  const first = make();
  assert.equal((await first.init()).redirecting, true);
  // Logto showed its page and the person came back without signing in.
  const second = make();
  const result = await second.init();
  assert.deepEqual([result.redirecting, result.signedIn], [false, false]);
  assert.equal(FakeClient.last.signInCalls.length, 0);
  // A new hint value is allowed one more attempt.
  setHint("v1.u2");
  assert.equal((await make().init()).redirecting, true);
});

test("the guard is released once the page matches the hint, so a later sign-in elsewhere is followed", async () => {
  setHint("v1.u1");
  assert.equal((await make().init()).redirecting, true);
  const back = make();
  back.client().nextSub = "u1";
  setLocation("/callback", "?code=x&state=s");
  await back.completeSignIn();
  setLocation("/studio");
  assert.equal((await back.init()).signedIn, true);
  setHint("v1.-");
  await back.init();
  setHint("v1.u1");
  const again = make();
  assert.equal((await again.init()).redirecting, true);
});

test("announced is true once, on the page after a silent sign-in", async () => {
  setHint("v1.u1");
  assert.equal((await make().init()).redirecting, true);

  // Callback page: init must not clear the flag.
  setLocation("/callback", "?code=x&state=s");
  const callback = make();
  callback.client().nextSub = "u1";
  assert.equal((await callback.init()).announced, false);
  await callback.completeSignIn();

  setLocation("/studio");
  const landed = await callback.init();
  assert.deepEqual([landed.signedIn, landed.announced], [true, true]);
  assert.equal((await callback.init()).announced, false);
});

test("an interactive sign-in is never announced", async () => {
  const auth = make();
  await auth.signIn(undefined, "/studio");
  setLocation("/callback", "?code=x");
  auth.client().nextSub = "u1";
  await auth.completeSignIn();
  setLocation("/studio");
  assert.equal((await auth.init()).announced, false);
});

test("the callback page is left alone while the sign-in finishes", async () => {
  setHint("v1.u2");
  setLocation("/callback", "?code=x&state=s");
  const result = await make().init();
  assert.deepEqual([result.signedIn, result.redirecting], [false, false]);
  assert.equal(FakeClient.last.signInCalls.length, 0);
  setLocation("/callback/", "?code=x&state=s");
  assert.equal((await make().init()).redirecting, false);
});

// Open tabs

async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

test("a sign-out elsewhere signs an open page out and tells the app", async () => {
  const auth = make();
  holding(auth, "u1");
  setHint("v1.u1");
  await auth.init();
  const events = [];
  let expired = 0;
  auth.onExpire(() => (expired += 1));
  auth.watchAccount((event) => events.push(event.type));
  setHint("v1.-");
  globalThis.window.fire("focus");
  await settle();
  assert.deepEqual(events, ["signed-out"]);
  assert.equal(expired, 1);
  assert.equal(FakeClient.last.sub, null);
  assert.equal(reloads.count, 0);
});

test("a different account elsewhere blocks API calls and signs this tab in as the new account", async () => {
  const auth = make();
  setLocation("/write/3", "?draft=1");
  holding(auth, "u1", "rt-old");
  setHint("v1.u1");
  await auth.init();
  const events = [];
  auth.watchAccount((event) => events.push(event.type));
  assert.equal(await auth.getAccessToken(), "token");

  setHint("v1.u2");
  doc.fire("visibilitychange");
  await settle();
  assert.deepEqual(events, ["reloading"]);
  assert.equal(await auth.getAccessToken(), null);
  assert.match(fetched[0].body, /token=rt-old/);
  assert.equal(FakeClient.last.signInCalls.length, 1);
  assert.equal(auth.consumeReturnTo(), "/write/3?draft=1");
});

test("when another tab of this app already holds the new account, a plain reload is enough", async () => {
  const auth = make();
  holding(auth, "u1");
  setHint("v1.u1");
  await auth.init();
  auth.watchAccount();
  // The other tab signed in as u2 and wrote the shared tokens and the hint.
  auth.client().sub = "u2";
  setHint("v1.u2");
  globalThis.window.fire("storage");
  await settle();
  assert.equal(reloads.count, 1);
  assert.equal(FakeClient.last.signInCalls.length, 0);
  assert.equal(await auth.getAccessToken(), null);
});

test("when the silent sign-in already ran for this hint, the tab reloads into the signed-out state", async () => {
  const auth = make();
  holding(auth, "u1");
  setHint("v1.u1");
  await auth.init();
  auth.watchAccount();
  sessionStorage.setItem("swapp1990.silent", "v1.u2");
  setHint("v1.u2");
  globalThis.window.fire("focus");
  await settle();
  assert.equal(reloads.count, 1);
  assert.equal(FakeClient.last.signInCalls.length, 0);
});

test("a matching or unknown hint leaves an open page alone", async () => {
  const auth = make();
  holding(auth, "u1");
  setHint("v1.u1");
  await auth.init();
  auth.watchAccount();
  globalThis.window.fire("focus");
  doc.jar.delete(HINT_COOKIE);
  globalThis.window.fire("focus");
  await settle();
  assert.equal(reloads.count, 0);
  assert.equal(FakeClient.last.signInCalls.length, 0);
  assert.equal(await auth.getAccessToken(), "token");
});

test("unsubscribing stops the checks", async () => {
  const auth = make();
  holding(auth, "u1");
  setHint("v1.u1");
  await auth.init();
  const stop = auth.watchAccount();
  stop();
  setHint("v1.-");
  globalThis.window.fire("focus");
  await settle();
  assert.equal(FakeClient.last.sub, "u1");
});

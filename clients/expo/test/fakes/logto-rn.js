// Stands in for @logto/rn: same error names and codes, tokens kept per appId across instances.
export class LogtoClientError extends Error {
  constructor(code, data) {
    super(code);
    this.name = "LogtoClientError";
    this.code = code;
    this.data = data;
  }
}

export class LogtoRequestError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "LogtoRequestError";
    this.code = code;
  }
}

export class LogtoError extends Error {
  constructor(code) {
    super(code);
    this.name = "LogtoError";
    this.code = code;
  }
}

export class LogtoNativeClientError extends Error {
  constructor(code) {
    super(code);
    this.name = "LogtoNativeClientError";
    this.code = code;
  }
}

export const storage = new Map();

export class LogtoClient {
  static instances = [];
  static get last() {
    return LogtoClient.instances.at(-1);
  }

  constructor(config) {
    this.config = config;
    this.signInCalls = [];
    this.signOutCalls = 0;
    this.cleared = 0;
    this.tokenResult = async () => "token-1";
    this.sessionResult = { type: "success" };
    this.signOutError = null;
    this.claims = { sub: "u1", email: "reader@example.com", name: "" };
    LogtoClient.instances.push(this);
  }

  #key(item) {
    return `${this.config.appId}:${item}`;
  }
  #get(item) {
    return storage.get(this.#key(item)) ?? null;
  }

  async setRefreshToken(value) {
    storage.set(this.#key("refreshToken"), value);
  }
  async setIdToken(value) {
    storage.set(this.#key("idToken"), value);
  }
  async getRefreshToken() {
    return this.#get("refreshToken");
  }
  async isAuthenticated() {
    return Boolean(this.#get("idToken"));
  }
  async clearAllTokens() {
    this.cleared += 1;
    storage.delete(this.#key("refreshToken"));
    storage.delete(this.#key("idToken"));
  }
  async getIdTokenClaims() {
    if (!this.#get("idToken")) throw new LogtoClientError("not_authenticated");
    return this.claims;
  }
  async getAccessToken(resource) {
    this.resource = resource;
    return this.tokenResult();
  }
  async signIn(options) {
    this.signInCalls.push(options);
    await this.clearAllTokens();
    this.authSessionResult = this.sessionResult;
    if (this.authSessionResult.type !== "success") throw new LogtoNativeClientError("auth_session_failed");
    await this.setIdToken("id-token");
    await this.setRefreshToken("refresh-token");
  }
  async signOut() {
    this.signOutCalls += 1;
    if (this.signOutError) throw this.signOutError;
    await this.clearAllTokens();
  }
}

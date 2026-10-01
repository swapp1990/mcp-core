import type LogtoClient from "@logto/browser";

export declare const LOGTO_ENDPOINT: string;

export type SignInMethod = "google" | "apple" | "email";

export interface AuthUser {
  id: string;
  name: string;
  email: string;
  picture: string;
}

export interface CreateAuthOptions {
  appId: string;
  resource?: string;
  scopes?: string[];
  endpoint?: string;
  callbackPath?: string;
  returnKey?: string;
  legacyReturnKeys?: string[];
  /** Share the signed-in account with other swapp1990 sites through a cookie. Default true; off on other hosts. */
  accountHint?: boolean;
  /** Cookie domain for the hint. Override only in tests. */
  hintDomain?: string;
  Client?: new (config: unknown, enableCache?: boolean) => LogtoClient;
}

export interface InitResult {
  signedIn: boolean;
  user: AuthUser | null;
  /** The page is leaving for Logto (a pending switch or a silent sign-in); render nothing. */
  redirecting: boolean;
  /** A silent sign-in just changed who is signed in: tell the person. */
  announced: boolean;
}

export type AccountEvent = { type: "signed-out" } | { type: "reloading" };

export interface Auth {
  config: { endpoint: string; appId: string; resources: string[]; scopes: string[] };
  client: () => LogtoClient;
  signIn(method?: SignInMethod, returnTo?: string): Promise<void>;
  completeSignIn(): Promise<void>;
  /** Run once per page load before showing signed-in UI. */
  init(): Promise<InitResult>;
  /** Ends the shared session, then shows Logto's sign-in page to pick another account. */
  switchAccount(returnTo?: string): Promise<void>;
  /** Keeps an open page on the shared account; returns an unsubscribe function. */
  watchAccount(listener?: (event: AccountEvent) => void): () => void;
  consumeReturnTo(): string;
  getAccessToken(): Promise<string | null>;
  expire(): void;
  onExpire(listener: () => void): () => void;
  isAuthenticated(): Promise<boolean>;
  getUser(): Promise<AuthUser | null>;
  /** Ends the shared session for every site and returns to the site root. */
  signOut(): Promise<void>;
}

export declare function createAuth(options: CreateAuthOptions): Auth;
export declare function safeReturnPath(value: string | null | undefined): string;
export declare function sessionIsDead(err: unknown): boolean;
export declare function userFromClaims(claims: Record<string, unknown> | null | undefined): AuthUser | null;

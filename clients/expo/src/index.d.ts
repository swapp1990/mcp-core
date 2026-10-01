import type { LogtoClient, LogtoNativeConfig } from "@logto/rn";

export declare const LOGTO_ENDPOINT: string;

export type SignInMethod = "google" | "apple" | "email";
export type AuthEvent = "signedIn" | "signedOut" | "expired";

export interface AuthUser {
  id: string;
  name: string;
  email: string;
  picture: string;
}

export interface AuthConfig {
  endpoint: string;
  appId: string;
  resources: string[];
  scopes: string[];
  prompt: "consent";
  preferEphemeralSession: true;
}

export interface CreateAuthOptions {
  appId: string;
  /** The product's API resource indicator (the token audience). */
  resource: string;
  /** The app's custom-scheme callback, registered on its Logto Native app. */
  redirectUri: string;
  /** Product scopes, added to openid profile email offline_access. */
  scopes?: string[];
  endpoint?: string;
  /** Default target of deleteAccount(). */
  deleteAccountUrl?: string;
  /** AsyncStorage key of a pre-auth-expo session ({refresh_token, id_token}); imported once, then removed. */
  legacySession?: string;
  /** Replaces @logto/rn's LogtoClient (tests, local playtest modes). */
  Client?: new (config: AuthConfig & LogtoNativeConfig) => LogtoClient;
}

export interface DeleteAccountOptions {
  url?: string;
  /** Sent as JSON. */
  body?: unknown;
  headers?: Record<string, string>;
}

export interface Auth {
  config: AuthConfig;
  client: () => LogtoClient;
  signIn(method?: SignInMethod): Promise<AuthUser | null>;
  getAccessToken(): Promise<string | null>;
  expire(): Promise<void>;
  onExpire(listener: () => void): () => void;
  onChange(listener: (event: AuthEvent, user: AuthUser | null) => void): () => void;
  isAuthenticated(): Promise<boolean>;
  getUser(): Promise<AuthUser | null>;
  signOut(): Promise<void>;
  authFetch(url: string, init?: RequestInit): Promise<Response>;
  deleteAccount(options?: string | DeleteAccountOptions): Promise<unknown>;
}

export declare function createAuth(options: CreateAuthOptions): Auth;
export declare function sessionIsDead(err: unknown): boolean;
export declare function signInOptions(method?: SignInMethod): Record<string, unknown>;
export declare function userFromClaims(claims: Record<string, unknown> | null | undefined): AuthUser | null;

export declare class AuthCancelled extends Error {
  code: "auth_cancelled";
  type: "cancel" | "dismiss";
}
export declare class AuthExpired extends Error {
  code: "auth_expired";
}
export declare class AuthUnavailable extends Error {
  code: "auth_unavailable";
}
export declare class DeleteAccountFailed extends Error {
  code: "delete_account_failed";
  status: number;
  body: unknown;
}

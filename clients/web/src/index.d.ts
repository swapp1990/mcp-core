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
  Client?: new (config: unknown, enableCache?: boolean) => LogtoClient;
}

export interface Auth {
  config: { endpoint: string; appId: string; resources: string[]; scopes: string[] };
  client: () => LogtoClient;
  signIn(method?: SignInMethod, returnTo?: string): Promise<void>;
  completeSignIn(): Promise<void>;
  consumeReturnTo(): string;
  getAccessToken(): Promise<string | null>;
  expire(): void;
  onExpire(listener: () => void): () => void;
  isAuthenticated(): Promise<boolean>;
  getUser(): Promise<AuthUser | null>;
  signOut(postLogoutRedirect?: string): Promise<void>;
}

export declare function createAuth(options: CreateAuthOptions): Auth;
export declare function safeReturnPath(value: string | null | undefined): string;
export declare function sessionIsDead(err: unknown): boolean;
export declare function userFromClaims(claims: Record<string, unknown> | null | undefined): AuthUser | null;

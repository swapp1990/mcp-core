import type { Context, ReactNode } from "react";
import type { Auth, AuthUser, DeleteAccountOptions, SignInMethod } from "./index";

export interface AuthValue {
  ready: boolean;
  signedIn: boolean;
  /** The saved session stopped working (refresh rejected, the API returned 401, or the auth config changed). */
  expired: boolean;
  user: AuthUser | null;
  signIn(method?: SignInMethod): Promise<AuthUser | null>;
  signOut(): Promise<void>;
  getAccessToken(): Promise<string | null>;
  authFetch(url: string, init?: RequestInit): Promise<Response>;
  expire(): Promise<void>;
  deleteAccount(options?: string | DeleteAccountOptions): Promise<unknown>;
}

export declare const AuthContext: Context<AuthValue | null>;
export declare function AuthProvider(props: { auth: Auth; children?: ReactNode }): JSX.Element;
export declare function useAuth(): AuthValue;

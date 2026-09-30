import type { Context, ReactNode } from "react";
import type { Auth, AuthUser, SignInMethod } from "./index";

export interface AuthValue {
  ready: boolean;
  signedIn: boolean;
  /** The saved session stopped working (refresh rejected or the API returned 401). */
  expired: boolean;
  user: AuthUser | null;
  signIn(method?: SignInMethod, returnTo?: string): Promise<void>;
  completeSignIn(): Promise<void>;
  consumeReturnTo(): string;
  getAccessToken(): Promise<string | null>;
  signOut(postLogoutRedirect?: string): Promise<void>;
  expire(): void;
}

export declare const AuthContext: Context<AuthValue | null>;
export declare function AuthProvider(props: { auth: Auth; children?: ReactNode }): JSX.Element;
export declare function useAuth(): AuthValue;

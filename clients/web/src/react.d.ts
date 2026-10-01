import type { Context, ReactNode } from "react";
import type { Auth, AuthUser, SignInMethod } from "./index";

export interface AuthValue {
  ready: boolean;
  signedIn: boolean;
  /** The saved session stopped working (refresh rejected or the API returned 401). */
  expired: boolean;
  user: AuthUser | null;
  /** A silent sign-in just changed the account: show "Signed in as <email>", then call dismissAnnouncement(). */
  announced: boolean;
  dismissAnnouncement(): void;
  switchAccount(returnTo?: string): Promise<void>;
  signIn(method?: SignInMethod, returnTo?: string): Promise<void>;
  completeSignIn(): Promise<void>;
  consumeReturnTo(): string;
  getAccessToken(): Promise<string | null>;
  signOut(): Promise<void>;
  expire(): void;
}

export declare const AuthContext: Context<AuthValue | null>;
export declare function AuthProvider(props: { auth: Auth; children?: ReactNode }): JSX.Element;
export declare function useAuth(): AuthValue;

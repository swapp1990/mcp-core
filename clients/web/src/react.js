import { createContext, createElement, useContext, useEffect, useMemo, useState } from "react";

/** Exported so a product can supply its own value (e.g. a local dev-bypass user). */
export const AuthContext = createContext(null);

export function AuthProvider({ auth, children }) {
  const [state, setState] = useState({ ready: false, signedIn: false, expired: false, user: null });

  useEffect(() => {
    let alive = true;
    void (async () => {
      const signedIn = await auth.isAuthenticated();
      const user = signedIn ? await auth.getUser() : null;
      if (alive) setState((s) => ({ ...s, ready: true, signedIn, user }));
    })();
    const off = auth.onExpire(() => setState({ ready: true, signedIn: false, expired: true, user: null }));
    return () => {
      alive = false;
      off();
    };
  }, [auth]);

  const value = useMemo(
    () => ({
      ...state,
      signIn: auth.signIn,
      completeSignIn: auth.completeSignIn,
      consumeReturnTo: auth.consumeReturnTo,
      getAccessToken: auth.getAccessToken,
      signOut: auth.signOut,
      expire: auth.expire,
    }),
    [state, auth],
  );
  return createElement(AuthContext.Provider, { value }, children);
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}

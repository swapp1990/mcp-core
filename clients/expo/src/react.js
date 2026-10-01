import { createContext, createElement, useContext, useEffect, useMemo, useState } from "react";

/** Exported so a product can supply its own value (e.g. a local playtest user). */
export const AuthContext = createContext(null);

export function AuthProvider({ auth, children }) {
  const [state, setState] = useState({ ready: false, signedIn: false, expired: false, user: null });

  useEffect(() => {
    let alive = true;
    // Subscribed before the first read, so a sign-out caused by a config change shows as expired.
    const off = auth.onChange((event, user) => {
      if (!alive) return;
      if (event === "signedIn") setState({ ready: true, signedIn: true, expired: false, user });
      else setState({ ready: true, signedIn: false, expired: event === "expired", user: null });
    });
    void (async () => {
      const signedIn = await auth.isAuthenticated().catch(() => false);
      const user = signedIn ? await auth.getUser() : null;
      if (alive) setState((s) => ({ ...s, ready: true, signedIn, user, expired: signedIn ? false : s.expired }));
    })();
    return () => {
      alive = false;
      off();
    };
  }, [auth]);

  const value = useMemo(
    () => ({
      ...state,
      signIn: auth.signIn,
      signOut: auth.signOut,
      getAccessToken: auth.getAccessToken,
      authFetch: auth.authFetch,
      expire: auth.expire,
      deleteAccount: auth.deleteAccount,
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

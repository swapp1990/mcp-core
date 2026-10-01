import { createContext, createElement, useCallback, useContext, useEffect, useMemo, useState } from "react";

/** Exported so a product can supply its own value (e.g. a local dev-bypass user). */
export const AuthContext = createContext(null);

export function AuthProvider({ auth, children }) {
  const [state, setState] = useState({ ready: false, signedIn: false, expired: false, user: null, announced: false });

  useEffect(() => {
    let alive = true;
    void (async () => {
      const result = await auth.init();
      // A redirecting page is already leaving; keep it blank instead of flashing signed-out UI.
      if (alive && !result.redirecting) {
        setState((s) => ({ ...s, ready: true, signedIn: result.signedIn, user: result.user, announced: result.announced }));
      }
    })();
    const off = auth.onExpire(() => setState({ ready: true, signedIn: false, expired: true, user: null, announced: false }));
    const stopWatching = auth.watchAccount();
    return () => {
      alive = false;
      off();
      stopWatching();
    };
  }, [auth]);

  const dismissAnnouncement = useCallback(() => setState((s) => ({ ...s, announced: false })), []);

  const value = useMemo(
    () => ({
      ...state,
      dismissAnnouncement,
      signIn: auth.signIn,
      completeSignIn: auth.completeSignIn,
      consumeReturnTo: auth.consumeReturnTo,
      getAccessToken: auth.getAccessToken,
      switchAccount: auth.switchAccount,
      signOut: auth.signOut,
      expire: auth.expire,
    }),
    [state, auth, dismissAnnouncement],
  );
  return createElement(AuthContext.Provider, { value }, children);
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}

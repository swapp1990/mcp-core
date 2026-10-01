/** Cookie that tells every swapp1990 site which account the shared Logto session belongs to. */
export const HINT_COOKIE = "swapp1990_account";
export const HINT_DOMAIN = "swapp1990.org";

const HINT_MAX_AGE = 14 * 24 * 60 * 60;
const VERSION = "v1";

/** The cookie domain for this host, or null where the hint can't be shared (localhost, other domains). */
export function hintDomainFor(hostname, domain = HINT_DOMAIN) {
  if (typeof hostname !== "string") return null;
  return hostname === domain || hostname.endsWith(`.${domain}`) ? domain : null;
}

/** `v1.<sub>` while signed in, `v1.-` after a sign-out; null clears to the signed-out value. */
export function formatHint(sub) {
  return `${VERSION}.${sub ? encodeURIComponent(sub) : "-"}`;
}

/** unknown: absent or unreadable; signed-out: someone signed out; account: the session belongs to `sub`. */
export function parseHint(raw) {
  const prefix = `${VERSION}.`;
  if (typeof raw !== "string" || !raw.startsWith(prefix) || raw.length === prefix.length) return { kind: "unknown", raw: null };
  const rest = raw.slice(prefix.length);
  if (rest === "-") return { kind: "signed-out", raw };
  try {
    return { kind: "account", sub: decodeURIComponent(rest), raw };
  } catch {
    return { kind: "unknown", raw: null };
  }
}

export function readHintCookie(cookieString) {
  for (const part of String(cookieString ?? "").split(";")) {
    const [name, ...value] = part.trim().split("=");
    if (name === HINT_COOKIE) return value.join("=");
  }
  return null;
}

export function hintCookieString(raw, domain, secure) {
  return `${HINT_COOKIE}=${raw}; Domain=${domain}; Path=/; Max-Age=${HINT_MAX_AGE}; SameSite=Lax${secure ? "; Secure" : ""}`;
}

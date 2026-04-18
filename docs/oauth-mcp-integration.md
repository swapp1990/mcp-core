# OAuth + MCP Integration: Deep Dive

A technical narrative of the fixes landed in mcp-core on 2026-04-17 to get a third-party MCP client (Claude Code) authenticating end-to-end against a Logto-protected MCP server (writer-v2), with JWT access tokens that mcp-core can decode, validate, and bill against.

This document is the "why" companion to the code. Read it alongside `src/mcp_core/routes.py`, `src/mcp_core/dcr.py`, and `src/mcp_core/__init__.py`.

---

## 1. The stack

```
┌──────────────────────────────────────────────────────────────┐
│  MCP Client (Claude Code, Cursor, mcp-inspector, ...)        │
│  - Speaks MCP over HTTP+SSE                                  │
│  - Does OAuth 2.1 + PKCE; expects RFC 7591 DCR               │
│  - Uses MCP SDK (TypeScript) with Zod schema validation      │
└──────────────────────────────────────────────────────────────┘
                ▲  HTTPS
                │
┌──────────────────────────────────────────────────────────────┐
│  MCP Server (writer-v2, voicegen, ...)                       │
│                                                              │
│  FastAPI ── fastapi-mcp ── mcp-core ── Logto ── MongoDB      │
│              (protocol)    (infra)     (auth)                │
└──────────────────────────────────────────────────────────────┘
```

Ownership boundaries:

| Layer | Owns |
|-------|------|
| **MCP SDK (client)** | Protocol framing, OAuth 2.1 dance, PKCE, token storage, tool-call UI |
| **fastapi-mcp** | MCP JSON-RPC over SSE/HTTP, tool discovery from FastAPI routes, OAuth-proxy scaffolding (`setup_proxies=True`) |
| **mcp-core** | Logto JWT validation, Stripe billing, MongoDB user records, tool audit log, **DCR**, **OAuth proxy overrides** |
| **Logto** | OP (identity provider); issues tokens; exposes Management API for app CRUD |

The fixes in this doc all land at the **mcp-core ↔ Logto** and **mcp-core ↔ MCP client** seams. fastapi-mcp gave us 80% of the protocol; mcp-core fills the 20% of auth infrastructure that `fastapi-mcp` deliberately leaves to the product.

---

## 2. The happy-path flow we're building

```
Client                        mcp-core + fastapi-mcp              Logto
  │                                   │                             │
  │ 1. GET /.well-known/oauth-*       │                             │
  │<──────── metadata (with proxied  │                             │
  │           authorize/token URLs)   │                             │
  │                                   │                             │
  │ 2. POST /oauth/register           │                             │
  │    {redirect_uris: [              │                             │
  │      "http://127.0.0.1:54321/cb"] │                             │
  │   }                               │ ─── Management API ───────▶ │
  │<──── {client_id, client_secret?} ◀── create app w/ those URIs ──│
  │                                   │                             │
  │ 3. Redirect user to               │                             │
  │    /oauth/authorize?resource=…   │                             │
  │    (PKCE code_challenge)          │                             │
  │                                   │───── /oidc/auth ───────────▶│
  │<═══════════════════════════════════════ consent → code ═════════│
  │                                   │                             │
  │ 4. POST /oauth/token              │                             │
  │    grant_type=authorization_code  │                             │
  │    code=…, code_verifier=…        │                             │
  │                                   │───── /oidc/token ──────────▶│
  │                                   │     (resource injected)     │
  │<─── { access_token: JWT, … }  ◀───────── JWT w/ aud = API ──────│
  │                                   │                             │
  │ 5. POST /mcp/messages/            │                             │
  │    Authorization: Bearer <JWT>    │                             │
  │    tool=list_stories              │                             │
  │                                   │                             │
  │                                  verify JWT (JWKS cached)       │
  │                                  auth_and_bill → deduct credit  │
  │                                  call handler → MongoDB         │
  │<─── { stories: […] }              │                             │
```

Every fix below unblocks one specific step. The reverts track false starts.

---

## 3. Fix 1 — Real RFC 7591 Dynamic Client Registration

**Commit:** `cb66534` — *Add real RFC 7591 DCR via Logto Management API*

### The problem

MCP clients are distributed software. They are not pre-registered OAuth apps sitting in a vendor dashboard. When Claude Code spins up an MCP session, it opens a **random loopback port** for the OAuth callback (e.g. `http://127.0.0.1:54321/callback`) and expects the MCP server's auth server to accept that port as a valid redirect URI.

This is what RFC 7591 (Dynamic Client Registration) exists for: the client POSTs its metadata — including `redirect_uris` — to `/oauth/register`, and the auth server returns a fresh `client_id`. The MCP spec **requires** DCR support.

Two things break this in our stack:

1. **Logto has no `/oidc/register`.** Confirmed with a direct probe: the endpoint 404s. Logto expects you to manage applications through its Management API or its web UI, not via the OIDC registration spec.
2. **fastapi-mcp ships a fake DCR.** When `setup_fake_dynamic_registration=True`, any POST to `/oauth/register` returns a hardcoded `client_id` (the one you pre-registered in Logto and configured via `mcp_logto_app_id`). The static client has a fixed set of redirect URIs. Claude Code's dynamic loopback port is not in that set, so the subsequent `/oauth/authorize` with `redirect_uri=http://127.0.0.1:54321/callback` is rejected by Logto with `invalid_redirect_uri`.

Result: the OAuth dance dies at step 3 for any client that doesn't use a pinned redirect URI.

### The fix: real DCR backed by Logto's Management API

`src/mcp_core/dcr.py` implements `LogtoDCR.register(body)`. On every incoming `/oauth/register` POST:

1. Fetch a Management API access token via client-credentials grant (cached until 60s before expiry).
2. POST `/api/applications` with:
   - `type`: `"Native"` if the client sent `token_endpoint_auth_method=none` (public PKCE clients like Claude Code), else `"Traditional"` (confidential).
   - `oidcClientMetadata.redirectUris`: the exact list the client asked for.
3. Return an RFC 7591–shaped response with the fresh `client_id` (and `client_secret` for confidential clients).

Key implementation details:

- **Token cache with mutex** (`asyncio.Lock`): prevents a thundering herd of Management API token requests when many clients register simultaneously.
- **One-shot 401 retry**: if the cached token is stale (e.g. Logto rotated its secret), fetch a fresh one and retry the app creation exactly once.
- **`http_client_factory` injection**: lets tests swap in an `httpx.MockTransport`. See `tests/test_dcr.py`.

### Subtlety: route registration order matters

The `/oauth/register` route is registered in `install_routes()` (see `src/mcp_core/routes.py:82-89`), which runs **before** `mcp.mount_sse()`. FastAPI uses first-match router dispatch, so mcp-core's real DCR handler wins over fastapi-mcp's fake one — even though both are present.

This becomes a recurring tool in the toolkit: **register an override in `install_routes` before the fastapi-mcp mount** to shadow any fastapi-mcp-provided route.

---

## 4. Fix 1b — Keep fake DCR advertised in metadata

**Commit:** `3a7223a` — *Keep fake DCR advertised so MCP SDK accepts the server*

### What we tried first

`cb66534` set `setup_fake_dynamic_registration=self.dcr is None` — the intuitive choice: "if we have real DCR, turn off the fake one." This broke the MCP SDK client.

### Why it broke

The MCP TypeScript SDK validates the auth-server metadata document (`/.well-known/oauth-authorization-server`) with a Zod schema that requires `registration_endpoint`. Without it, the client refuses to proceed:

```
Incompatible auth server: does not support dynamic client registration
```

fastapi-mcp only inserts `registration_endpoint` into its proxied metadata when `setup_fake_dynamic_registration=True`. Turning the flag off satisfied our "don't use the fake handler" instinct but silently stripped the field the SDK was looking for.

### The fix

Leave `setup_fake_dynamic_registration=True` **unconditionally**. The metadata doc keeps advertising `registration_endpoint`. At request time, mcp-core's real handler (registered first in `install_routes`) serves the actual POST. The fake handler is still mounted but unreachable — first-match dispatch.

This is a small example of a broader principle in protocol work: **discovery documents and runtime behavior have separate failure modes.** You need both to be right; you can't trade one off for the other.

---

## 5. Fix 2 — Opaque tokens vs. JWTs (RFC 8707 Resource Indicators)

**Commits:**
- `cc05810` — *Override /oauth/authorize with RFC 8707 resource indicator for Logto*
- `f99432b` — *Proxy /oauth/token and metadata too, so clients always get JWTs*

This one took two commits because the fix had to be applied at two endpoints, and the second endpoint's necessity wasn't visible until we tested with a minimal client.

### The problem

After fixing DCR, the OAuth dance completed — client got back an `access_token` — but every MCP tool call 401'd inside `mcp-core`'s `verify_token`. The token was a string of opaque bytes, not a JWT. PyJWT couldn't decode it. JWKS verification was impossible.

Why did Logto hand us an opaque token? Because of **RFC 8707 Resource Indicators**.

### The RFC 8707 primer

OAuth 2.0 originally had no standard way to say "I want a token for API X." The non-standard `audience=` parameter became common (Auth0, Okta). RFC 8707 standardized the mechanism:

- **`resource=<uri>`** on `/authorize` and `/token` requests tells the OP: *"issue me a token that is valid for this resource server and contains `aud=<uri>`."*
- Without `resource`, OPs are free to issue whatever they like. Some issue a general-purpose JWT. Logto issues an **opaque reference token** that can only be introspected via a separate `/oidc/token/introspection` call.

fastapi-mcp's proxy sends `audience=<api_resource>` (the pre-RFC-8707 convention). Logto treats that as a non-binding hint and, because no `resource` is present, falls back to opaque-token mode.

### The two-endpoint requirement

Logto's specific implementation requires `resource` on **both** the authorize redirect and the token exchange. Our first fix (`cc05810`) only overrode `/oauth/authorize`. Testing revealed:

- Authorize with `resource` → Logto records the resource intent on the authorization code.
- Token exchange **without** `resource` → Logto issues an opaque token anyway.

`f99432b` added the token-endpoint proxy.

### What the fix does

Three endpoints are now overridden by mcp-core (`src/mcp_core/routes.py:98-181`):

**`GET /oauth/authorize`** — pure redirect.
Rewrites the query string: copies `response_type`, `client_id`, `redirect_uri`, `state`, `code_challenge`, `code_challenge_method`, etc.; unions the client's scopes with server defaults; **injects `resource=<api_resource>`**; 307-redirects to `{logto}/oidc/auth`.

**`POST /oauth/token`** — full proxy.
Parses the form body, sets `data.setdefault("resource", api_resource)` (honors the client's resource if they sent one; injects it otherwise), forwards any `Authorization` header (for confidential clients using basic auth), POSTs to `{logto}/oidc/token`, and passes the response back byte-for-byte.

**`GET /.well-known/oauth-authorization-server`** — metadata rewrite.
Fetches Logto's OpenID configuration upstream, overrides `authorization_endpoint` and `token_endpoint` to point at our proxies, adds `registration_endpoint` when real DCR is enabled, and returns the modified doc. This ensures MCP clients that consume the metadata doc end up calling our proxies, not Logto direct.

### `_public_base_url` — the reverse-proxy detail

```python
def _public_base_url(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    proto = request.headers.get("x-forwarded-proto")
    if proto and base.startswith("http://"):
        base = f"{proto}://{base[7:]}"
    return base
```

Production deployments sit behind a reverse proxy (nginx/Caddy) that terminates TLS. FastAPI sees the inbound request as `http://…` on its internal port. If we naively use `request.base_url` to build the `authorization_endpoint` URL we return in metadata, clients end up redirected to `http://writer.swapp1990.org/oauth/authorize` — which fails at the HTTPS-only upstream proxy.

Reading `x-forwarded-proto` (set by the reverse proxy) and upgrading `http://` → `https://` produces the correct public URL. This is infrastructure-facing plumbing that every MCP server needs, which is exactly why it belongs in mcp-core.

### Verification

After `f99432b`, a minimal `test_proxy_auth.py` script completed the full flow with a client that sent **no `resource` parameter at all** — the JWT came back with the correct `aud` claim, and `list_stories` returned real data.

---

## 6. The dead-end — forcing auth on MCP routes via `Depends`

**Commits:**
- `d74c538` — *Gate MCP routes with auth Depends so Authorization reaches tool dispatch*
- `16101d4` — *Revert: remove require_mcp_token dependency (Claude Code doesn't auth on GET /mcp SSE stream open)*

This is a case study in "the fix that worked elsewhere won't always work here."

### The theory

fastapi-mcp internally dispatches tool calls via an ASGI re-entry to routes like `/api/mcp/<tool_name>`. Observations in templategen months earlier showed that when no FastAPI dependency is attached to the mount point, the re-entered request arrives at the tool handler with **no `Authorization` header** — even though the original POST to `/mcp/messages/` had one. The workaround there was to attach `dependencies=[Depends(require_mcp_token)]` to `AuthConfig`, which pulls the validation into FastAPI's dependency graph. That path preserves request context and propagates the header.

`d74c538` applied the same fix to mcp-core.

### Why it broke with Claude Code

The Streamable-HTTP transport has two channels:

1. **`GET /mcp`** — server-sent events stream, opened once per session, kept alive.
2. **`POST /mcp/messages/`** — individual JSON-RPC requests.

Claude Code **does not send an `Authorization` header on the GET that opens the SSE stream.** It sends it on the POSTs that follow. With `Depends(require_mcp_token)` attached to the mount, the SSE stream open returned 401, the client never got a session, and every tool call silently failed before it was even dispatched.

templategen's flow worked differently — probably because of how it was mounted, or because the SSE open there was preceded by a messages POST. The same dependency was correct in one context and wrong in another.

### The revert

`16101d4` pulled the `Depends` back out. Authentication now happens later, inside the individual tool handler's call to `core.auth_and_bill(request, tool_name)`. The stream itself is unauthenticated, which is acceptable because no protected data flows through it until the client sends a message with a token.

**Lesson to preserve**: when migrating voicegen/writer to mcp-core, do *not* re-introduce the `Depends(require_mcp_token)` pattern without first verifying the client's auth-timing behavior. A TODO in the plan doc: document per-client auth-sequence quirks.

---

## 7. Supporting fix — RFC 6749 error shape

**Commit:** `cb66534` (shipped alongside the DCR work) — `install_oauth_error_handler` in `src/mcp_core/routes.py:42-72`.

### The problem

FastAPI's default error body is `{"detail": "message"}`. The MCP TypeScript SDK parses OAuth error responses with a Zod schema that requires RFC 6749 shape:

```json
{ "error": "invalid_request", "error_description": "…" }
```

A 405 "Method Not Allowed" on `/.well-known/oauth-authorization-server` (e.g. from a misrouted GET → POST) returns FastAPI's default shape, which fails Zod parsing. The client surfaces a cryptic `ZodError` and the user sees no useful diagnostic.

### The fix

Register a global `StarletteHTTPException` handler. It only reshapes responses whose path is under `/oauth/` or `/.well-known/oauth-` or `/.well-known/openid-`. Every 4xx/5xx on those prefixes becomes:

```json
{ "error": "<rfc6749_code>", "error_description": "<detail>" }
```

With a status → code map (`_OAUTH_ERROR_CODES`) covering the cases we've seen. All other paths fall through unchanged, so non-OAuth routes keep FastAPI's default `{"detail": "…"}` shape (which is what their clients expect).

This is defense-in-depth: we don't control every code path that might raise an HTTPException on an OAuth route (FastAPI routing, Starlette middleware, our own handlers). One handler normalizes the shape regardless.

---

## 8. What's still on the table

Tracked in `vn-creator/docs/mcp-core-plan.md` under "Credit Accounting":

- **Tool responses don't include credit state.** `generate_story`, `get_story`, etc. return only job/revision metadata. Clients must hit `/api/billing/credits` separately to display a balance. When writer-v2 migrates to mcp-core, `auth_and_bill` should attach `credits_remaining` and `cost_charged` to every paid-tool response — ideally via a response-model decorator so per-tool code doesn't have to remember.
- **Pricing is declared at tool-registration time** (the architectural principle from the plan doc): mcp-core owns the ledger, the product owns the price map. Already true for `tool_costs` config, but the mechanism for surfacing cost in the response isn't wired yet.

Tracked here (mcp-core repo):

- **Auth-header propagation across transports is client-specific.** The `Depends` revert above is not a permanent answer — it's an accommodation of one client's quirk. A proper fix would be: detect whether the current request is an SSE open vs. a JSON-RPC message and gate accordingly.
- **Live-test gap.** `tests/live/` exists but there's no live test that exercises the DCR → authorize → token → tool-call chain against a real Logto tenant. The local `test_proxy_auth.py` that proved the RFC 8707 fix is ad-hoc and not checked in. Promoting it to `tests/live/test_live_oauth_proxy.py` would prevent a future refactor from silently regressing the opaque-token fix.

---

## 9. Quick reference — where each concept lives

| Concept | File | Lines |
|---------|------|-------|
| Real DCR via Logto Management API | `src/mcp_core/dcr.py` | whole file |
| DCR wiring into MCPCore | `src/mcp_core/__init__.py` | 137–154 |
| `/oauth/register` route | `src/mcp_core/routes.py` | 82–89 |
| `/oauth/authorize` proxy (RFC 8707) | `src/mcp_core/routes.py` | 121–143 |
| `/oauth/token` proxy (RFC 8707) | `src/mcp_core/routes.py` | 145–163 |
| `/.well-known/oauth-authorization-server` rewrite | `src/mcp_core/routes.py` | 165–181 |
| RFC 6749 error-shape handler | `src/mcp_core/routes.py` | 42–72 |
| Reverse-proxy proto fix | `src/mcp_core/routes.py` | 114–119 |
| `setup_fake_dynamic_registration=True` (discovery) | `src/mcp_core/__init__.py` | 256–261 |
| DCR tests | `tests/test_dcr.py` | whole file |

---

## 10. References

- **RFC 6749** — OAuth 2.0 (error response shape): https://datatracker.ietf.org/doc/html/rfc6749#section-5.2
- **RFC 7591** — Dynamic Client Registration: https://datatracker.ietf.org/doc/html/rfc7591
- **RFC 8707** — Resource Indicators for OAuth 2.0: https://datatracker.ietf.org/doc/html/rfc8707
- **RFC 9728** — OAuth 2.0 Protected Resource Metadata: https://datatracker.ietf.org/doc/html/rfc9728
- **MCP Authorization spec** — https://modelcontextprotocol.io/specification/draft/basic/authorization
- **Logto Management API** — https://docs.logto.io/integrate-logto/interact-with-management-api
- **fastapi-mcp `AuthConfig`** — source of `setup_proxies`, `setup_fake_dynamic_registration`, `dependencies`.

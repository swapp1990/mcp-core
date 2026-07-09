# Client auth matrix for mcp-core products

How operators (and agents) should verify that **third-party MCP clients** can
authenticate against an mcp-core-backed server. This is product-ops guidance,
not a Python API change.

Companion docs:

- [oauth-mcp-integration.md](./oauth-mcp-integration.md) — protocol / DCR internals  
- [integration-guide.md](./integration-guide.md) — wiring mcp-core into a new server  
- Template human/agent guide: [templates/connect.md.template](./templates/connect.md.template)

---

## Auth model (two paths)

| Path | Who signs in | Who stores secrets | When to use |
|------|--------------|--------------------|-------------|
| **A. OAuth (primary)** | User in a **browser** opened by the MCP client | **The client** (access + refresh tokens) | Claude Code, Cursor, OpenCode, Grok (interactive), any DCR-capable client |
| **B. Long-lived bearer (optional fallback)** | User on the **product website**, then pastes a PAT into env/config | **User** (`Authorization: Bearer …`) | Headless agents, CI, clients that cannot finish OAuth |

### Path A is the normal product experience

1. Client discovers the server (`/.well-known/mcp/server.json` and/or a published MCP URL).  
2. Client reads protected-resource + authorization-server metadata (mcp-core / IdP).  
3. User completes **web login** (Google / email / etc. via Logto or Supabase).  
4. Client stores tokens; subsequent tool calls send `Authorization: Bearer <access_token>`.  
5. No manual “generate API key” step.

There is **no** “user logs into the website once and the client invents a PAT by itself.”  
A website session cookie is not an MCP client credential.

### Path B is product-specific (optional)

mcp-core validates bearer tokens from the IdP (and any product-defined schemes).  
**Minting, listing, and revoking long-lived MCP PATs** is not part of mcp-core today.
If your product implements PAT minting (e.g. a `/connect` page + token store):

- Document it as a **fallback**, not the default.  
- Mint must require a **signed-in website** session.  
- Agents must not invent admin/service/localStorage tokens.

---

## Minimum confidence bar (any mcp-core product)

Run these before claiming “MCP auth works.”

### 1. Server discovery (automated)

```bash
# Built into mcp-core
mcp-core-check-readiness https://your-app.example.com/mcp/v2/
```

Expect: protected-resource metadata, AS discovery, DCR registration path, and an
**unauthenticated** MCP challenge (typically HTTP **401** + `WWW-Authenticate`).

### 2. Unauthenticated control

```bash
curl -s -D - -X POST "https://your-app.example.com/mcp/v2/" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"matrix","version":"1.0"}}}'
```

Expect **401** (or equivalent auth error). If tools work without credentials, auth is broken.

### 3. OAuth with a real client (operator + browser)

Clear any leftover product PAT env vars so the client cannot skip OAuth:

```text
WRITEFORYOU_MCP_TOKEN, PRODUCT_MCP_TOKEN, …  (User + Process env)
```

Then for **each** client you care about:

| Client | Typical operator steps |
|--------|------------------------|
| Claude Code | `claude mcp add --transport http <name> <url>` → `claude mcp login <name>` → approve in browser → `claude mcp list` shows Connected |
| OpenCode | Add remote MCP → `opencode mcp auth <name>` → approve in browser → `opencode mcp list` shows connected |
| Grok | `grok mcp add --transport http <name> <url>` → interactive TUI `/mcps` → select server → **`i`** authorize → browser → **`r`** refresh → `grok mcp doctor <name>` |
| Cursor | Install via deeplink or `mcp.json` → complete OAuth when prompted |
| Codex | Often **bearer-first**; use Path B if OAuth is unsupported |

**Pass:** A free read-only tool succeeds (e.g. credits / list / health) **without** a PAT in the environment.

**Operator model that works well:**

1. Script starts `mcp login` / `mcp auth` and opens the authorize URL.  
2. Human only clicks **Approve** in a browser already signed into the product/IdP.  
3. Script re-checks Connected + one tool call.

### 4. Optional PAT lifecycle (if your product implements mint)

1. User signed in on the website → generate token (shown once).  
2. Configure client with `Authorization: Bearer <token>` or `bearer_token_env_var`.  
3. Same read-only tool succeeds.  
4. Revoke token on website → tool/MCP initialize returns **401**.

---

## Guide-only comprehension check (agents)

To test whether an agent can **learn auth from docs alone** (no recipe coaching in the prompt):

1. Publish a plain-text guide (see template below), e.g.  
   `GET /.well-known/mcp/connect.md` and/or `/connect.md`.  
2. Isolated client profile with **no** preconfigured MCP server and **no** PAT env.  
3. Prompt only: fetch the guide URL; connect this client; prove a read-only tool **or** stop with the exact remaining human step from the guide.  
4. Score whether the agent describes **both** OAuth and (if documented) mint, without inventing admin tokens.

This does **not** replace step 3 (real browser OAuth). It only checks documentation quality.

---

## What products should ship

| Artifact | Owner |
|----------|--------|
| MCP URL (trailing slash if required by your mount) | Product |
| `/.well-known/mcp/server.json` | Product (often generated from public URL) |
| OAuth metadata / DCR (via mcp-core `install_routes` + mount) | mcp-core + product config |
| Human connect UI (optional) | Product |
| **Agent-readable `connect.md`** (recommended) | Product, from [template](./templates/connect.md.template) |
| Client-specific eval harness | Product repo (do not put product-specific scripts in mcp-core) |

---

## Anti-patterns

| Anti-pattern | Why it fails the matrix |
|--------------|-------------------------|
| Pre-seeded PAT in User env while “testing OAuth” | Client skips browser login; false pass |
| Calling tools with admin/service tokens | Wrong principal; bypasses user billing/library |
| Documenting only PAT, not OAuth | Forces every user through secret minting |
| Claiming headless agent completed OAuth alone | OAuth needs a browser consent step |
| Putting product eval scripts into mcp-core | Wrong layer; couple library to one product’s CLIs |

---

## Relation to `mcp-core-check-readiness`

`mcp-core-check-readiness` automates **server-side** discovery and challenge
shape. It does **not**:

- Complete a browser OAuth grant for Claude/Grok/OpenCode  
- Prove a specific desktop client’s token store  
- Exercise product PAT mint UIs  

Use readiness in CI; use the matrix above for release confidence with real clients.

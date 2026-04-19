# Self-hosted Logto for mcp-core

The reference Logto deployment that every mcp-core consumer can stand up on
their own infrastructure. Runs Logto + Postgres in docker-compose. Works
identically in local dev and prod; only the public URLs + port bindings
differ between `docker-compose.local.yml` and `docker-compose.prod.yml`.

## Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | Base: Logto + Postgres. No port bindings, no public URLs. |
| `docker-compose.local.yml` | Dev override: binds 3001/3002, sets `ENDPOINT=http://localhost:3001`. |
| `docker-compose.prod.yml` | Prod override: binds 3011/3012, sets public URLs for nginx fronting. |
| `.env.example` | `POSTGRES_PASSWORD` (required) and optional `LOGTO_IMAGE` pin. |
| `nginx-auth.conf` | Host nginx vhost for `auth.swapp1990.org` (adapt the domain). |
| `verify.py` | End-to-end compatibility check: mcp-core talking to self-host. |
| `deploy.sh` | One-shot prod deploy script (mirrors templategen/writer-v2 pattern). |

## Local dev

```bash
cd deploy/logto
cp .env.example .env
# Set POSTGRES_PASSWORD (generate with: openssl rand -hex 32)

docker compose -f docker-compose.yml -f docker-compose.local.yml up -d

# Wait for boot, then run the end-to-end compatibility check:
py verify.py
```

Admin console is at http://localhost:3002 — the first visit to that URL
becomes the root admin. Close the browser tab after creating the account;
no other "who is the admin" gate exists.

Teardown (wipes the volume):
```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml down -v
```

## Production deploy

First-time server prep (once):
1. DNS: A record `auth.swapp1990.org -> <server IP>`.
2. Host nginx vhost: copy `nginx-auth.conf` to `/etc/nginx/sites-available/`,
   symlink into `sites-enabled/`, then `certbot --nginx -d auth.swapp1990.org`.
3. `docker` + `docker compose v2` installed on the server.

Then from your workstation:
```bash
./deploy.sh
```

The script rsyncs the compose files, generates `.env` if missing, pulls
images, brings the stack up, and smoke-tests the public URL.

## mcp-core compatibility notes

Self-hosted Logto (OSS) is mostly drop-in compatible with Logto Cloud but
has a few surface differences that affect how mcp-core talks to it.

### Admin-tenant endpoint split

OSS runs two logical tenants in one process:
- **default** tenant (port 3001) — end-user sign-ins and app-scoped APIs.
- **admin** tenant (port 3002) — admin console + Management API auth.

Management-API M2M credentials (like the seeded `m-default` app) must be
exchanged for tokens at the **admin** OIDC endpoint, not the user-facing
one. This differs from Cloud, where everything lives on a single host.

`LogtoDCR` accepts an optional `mgmt_token_endpoint` parameter to handle
this:

```python
from mcp_core.dcr import LogtoDCR

dcr = LogtoDCR(
    logto_endpoint="https://auth.swapp1990.org",
    mgmt_app_id="<m2m-app-id>",
    mgmt_app_secret="<m2m-app-secret>",
    mgmt_api_resource="https://default.logto.app/api",
    # OSS-only: Management tokens are issued by the admin tenant.
    mgmt_token_endpoint="https://auth.swapp1990.org:8443/oidc/token",
)
```

### Admin must live on its own origin (not a path prefix)

The admin SPA in Logto OSS hardcodes its API fetches as `/api/*` relative
to the origin root — it does NOT use a base path derived from
`ADMIN_ENDPOINT`. If you serve admin at `auth.example.com/admin`, the
browser's `/api/*` calls land on the *user* tenant and fail.

Workable separations, cleanest first:
- **Different port on the same host** (what this repo uses):
  `ENDPOINT=https://auth.example.com` and
  `ADMIN_ENDPOINT=https://auth.example.com:8443`. Same cert, no new DNS,
  open one firewall port.
- **Subdomain**: `admin-auth.example.com`. Needs a DNS record + cert.

The port-based setup only requires the nginx vhost to have a second
`server { listen 8443 ssl; ... }` block that proxies to the admin
container port. No path rewrites, no Host spoofing.

When `mgmt_token_endpoint` is unset (default), `LogtoDCR` uses
`f"{endpoint}/oidc/token"` — the Cloud behavior — so existing deployments
see no regression.

### ADMIN_ENDPOINT must be container-reachable

Logto performs service-to-service JWKS fetches using `ADMIN_ENDPOINT`
verbatim (see `koa-auth.getAdminTenantTokenValidationSet`). If the container
cannot resolve/connect to that URL from *inside itself*, any Management-API
request 500s with `ECONNREFUSED`.

Two ways this manifests:
- **Local dev:** using port remaps like `3012:3002` makes the container's
  `localhost:3012` unreachable from inside the container. Fix: the local
  override binds `3001:3001`/`3002:3002` 1:1 so `localhost:3001/3002`
  resolves correctly from both host and container.
- **Prod:** `ENDPOINT=https://auth.swapp1990.org` is fine because the
  domain resolves publicly, but the prod override adds
  `extra_hosts: - "auth.swapp1990.org:host-gateway"` to keep the internal
  round-trip local (container -> host nginx -> container) instead of
  depending on external DNS + egress.

### Registration endpoint never advertised

Logto (both Cloud and self-host) does **not** return `registration_endpoint`
in `/oidc/.well-known/openid-configuration`, and `/oidc/register` 404s.
mcp-core's `install_routes()` fills this gap by mounting `/oauth/register`
and advertising it in the proxied metadata doc — works identically on
self-host.

### Management API resource

For the single OSS tenant named `default`, the Management API resource
indicator is `https://default.logto.app/api`. This is a logical string
(not a reachable URL) — it's only used as the `resource`/`audience` value.

## Verify compatibility

`verify.py` is the source of truth for "does self-hosted Logto still work
with mcp-core". It covers:

- OIDC discovery (issuer, jwks_uri, authorize/token endpoints)
- JWKS format + supported JWT algorithms
- Management-API token exchange via admin tenant
- `LogtoDCR.register()` end-to-end creating a Native/PKCE app
- JWT signed by Logto validated by `LogtoAuth._get_jwks_client()`

Run it after any Logto version bump:

```bash
py verify.py                                   # local compose
py verify.py --endpoint https://auth.swapp1990.org \
             --admin    https://auth.swapp1990.org/admin \
             --mgmt-secret <from-logto-admin-console>
```

## Post-deploy config (admin console)

Do these once the stack is up on prod:

1. **SPA apps** — one per frontend (templategen, writer-v2). Redirect URIs:
   `https://designforyou.swapp1990.org/callback`, etc. Note the client IDs.
2. **API resources** — `https://api.designforyou.app` and equivalent for
   writer. Add scopes the products use.
3. **M2M app for DCR** — one per backend that needs Dynamic Client
   Registration. Grant it the "Management API access for default" role.
4. **Google social connector** — create a Google Cloud OAuth 2.0 client,
   redirect URI `https://auth.swapp1990.org/callback/<connector-id>`.
5. **SMTP email connector (Amazon SES)** — host
   `email-smtp.<region>.amazonaws.com`, port 587, STARTTLS, IAM SMTP creds.
6. **Sign-in experience** — enable email + password, plus Google connector.

Then update each product's `auth.jsx` and backend `.env`:
- Frontend: `LOGTO_CONFIG.endpoint = 'https://auth.swapp1990.org'`
- Backend: `LOGTO_ENDPOINT=https://auth.swapp1990.org` and
  `LOGTO_API_RESOURCE=...`, plus `LOGTO_DCR_MGMT_APP_ID/SECRET` and
  `LOGTO_DCR_MGMT_TOKEN_ENDPOINT=https://auth.swapp1990.org/admin/oidc/token`.

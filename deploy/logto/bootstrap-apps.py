"""
Bulk-create the apps / API resources / connectors this org needs in a
freshly-installed self-hosted Logto. Idempotent: re-runs skip existing
records (match by name for apps, indicator for resources).

Usage:
    py bootstrap-apps.py \
        --endpoint https://auth.swapp1990.org \
        --admin https://auth.swapp1990.org:8443 \
        --mgmt-secret "$(ssh ... psql ... 'SELECT secret FROM applications WHERE id=...')"

Prints a summary of all created/found IDs + secrets at the end so the
values can be pasted into each product's .env.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx


# ─── Config ────────────────────────────────────────────────────────────

@dataclass
class Product:
    name: str                       # human label
    spa_app_name: str               # shown in Logto admin UI
    redirect_uris: List[str]
    post_logout_uris: List[str]
    api_resource: str               # RFC 8707 indicator
    api_resource_name: str
    scopes: List[str]               # resource-scoped scopes


TEMPLATEGEN = Product(
    name="templategen",
    spa_app_name="DesignForYou (templategen)",
    redirect_uris=[
        "http://localhost:5176/callback",
        "https://designforyou.swapp1990.org/callback",
    ],
    post_logout_uris=[
        "http://localhost:5176",
        "https://designforyou.swapp1990.org",
    ],
    api_resource="https://api.designforyou.app",
    api_resource_name="DesignForYou API",
    scopes=["designforyou:read", "designforyou:generate"],
)

WRITER = Product(
    name="writer-v2",
    spa_app_name="Autonomous Writer",
    redirect_uris=[
        "http://localhost:3000/callback",
        "https://writer.swapp1990.org/callback",
    ],
    post_logout_uris=[
        "http://localhost:3000",
        "https://writer.swapp1990.org",
    ],
    api_resource="https://api.writer.swapp1990.org",
    api_resource_name="Writer API",
    scopes=["writer:read", "writer:write"],
)

# All products share the seeded m-default M2M app for DCR — it already has
# the admin-tenant 'machine:mapi:default' role that can't be assigned to
# default-tenant apps. Per-product M2M creds would be cleaner but require
# admin-tenant app creation, which means hitting the admin-tenant
# Management API separately. Not worth the complexity for two products.
SHARED_DCR_APP_ID = "m-default"


# ─── Management API client ─────────────────────────────────────────────

class Mgmt:
    def __init__(
        self,
        endpoint: str,
        admin_endpoint: str,
        mgmt_app_id: str,
        mgmt_app_secret: str,
        resource: str = "https://default.logto.app/api",
    ):
        self.endpoint = endpoint.rstrip("/")
        self.admin = admin_endpoint.rstrip("/")
        self.mgmt_app_id = mgmt_app_id
        self.mgmt_app_secret = mgmt_app_secret
        self.resource = resource
        self._token: Optional[str] = None

    def _token_or_fetch(self) -> str:
        if self._token:
            return self._token
        r = httpx.post(
            f"{self.admin}/oidc/token",
            auth=(self.mgmt_app_id, self.mgmt_app_secret),
            data={
                "grant_type": "client_credentials",
                "resource": self.resource,
                "scope": "all",
            },
            timeout=15,
        )
        r.raise_for_status()
        self._token = r.json()["access_token"]
        return self._token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token_or_fetch()}",
            "Content-Type": "application/json",
        }

    def get(self, path: str) -> Any:
        r = httpx.get(f"{self.endpoint}{path}", headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        r = httpx.post(
            f"{self.endpoint}{path}",
            headers=self._headers(),
            json=body,
            timeout=15,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:500]}")
        return r.json()

    def patch(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        r = httpx.patch(
            f"{self.endpoint}{path}",
            headers=self._headers(),
            json=body,
            timeout=15,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"PATCH {path} -> {r.status_code}: {r.text[:500]}")
        return r.json()


# ─── Idempotent creators ───────────────────────────────────────────────

def ensure_api_resource(m: Mgmt, indicator: str, name: str) -> Dict[str, Any]:
    for r in m.get("/api/resources"):
        if r["indicator"] == indicator:
            print(f"  [skip] resource '{name}' exists  id={r['id']}")
            return r
    r = m.post("/api/resources", {
        "name": name,
        "indicator": indicator,
        "accessTokenTtl": 3600,
    })
    print(f"  [new]  resource '{name}'             id={r['id']}")
    return r


def ensure_scopes_on_resource(
    m: Mgmt, resource_id: str, scopes: List[str]
) -> None:
    existing = {s["name"] for s in m.get(f"/api/resources/{resource_id}/scopes")}
    for s in scopes:
        if s in existing:
            continue
        m.post(f"/api/resources/{resource_id}/scopes", {"name": s, "description": s})
        print(f"           +scope '{s}'")


def ensure_app(
    m: Mgmt,
    name: str,
    app_type: str,
    oidc_metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Match by display name. Won't patch redirect URIs on existing apps."""
    for a in m.get("/api/applications"):
        if a["name"] == name:
            print(f"  [skip] app '{name}' exists            id={a['id']}")
            return a
    payload = {"name": name, "type": app_type, "oidcClientMetadata": oidc_metadata}
    r = m.post("/api/applications", payload)
    secret_hint = (r.get("secret") or "")[:8] + "..." if r.get("secret") else "(native, no secret)"
    print(f"  [new]  app '{name}'                  id={r['id']}  secret={secret_hint}")
    return r


def ensure_ses_connector(m: Mgmt) -> None:
    """Add the AWS SES email connector if one isn't already present."""
    import os
    existing = m.get("/api/connectors")
    if any(c.get("connectorId") == "aws-ses-mail" for c in existing):
        print("  [skip] AWS SES connector already present")
        return
    access_key = os.environ.get("SES_ACCESS_KEY_ID")
    secret_key = os.environ.get("SES_SECRET_ACCESS_KEY")
    from_email = os.environ.get("SES_FROM", "swapp19902@gmail.com")
    region = os.environ.get("SES_REGION", "us-west-2")
    if not access_key or not secret_key:
        print("  [skip] SES_ACCESS_KEY_ID / SES_SECRET_ACCESS_KEY not set in env")
        return
    templates = [
        {
            "usageType": ut,
            "subject": f"[DesignForYou] {ut} code",
            "content": "Your verification code is {{code}}. Expires in 10 minutes.",
        }
        for ut in (
            "SignIn", "Register", "ForgotPassword",
            "Generic", "OrganizationInvitation",
            "UserPermissionValidation", "BindNewIdentifier",
            "MfaVerification", "BindMfa",
        )
    ]
    r = m.post("/api/connectors", {
        "connectorId": "aws-ses-mail",
        "config": {
            "accessKeyId": access_key,
            "accessKeySecret": secret_key,
            "region": region,
            "emailAddress": from_email,
            "templates": templates,
        },
    })
    print(f"  [new]  AWS SES connector             id={r['id']}")


def ensure_m2m_role_assignment(m: Mgmt, app_id: str, role_name: str = "machine:mapi:default") -> None:
    """Grant a M2M app the role that lets it call the default-tenant Management API."""
    try:
        roles = m.get(f"/api/applications/{app_id}/roles")
    except RuntimeError:
        roles = []
    if any(r.get("name") == role_name for r in roles):
        print(f"           role '{role_name}' already assigned")
        return
    # Look up the role id
    for r in m.get("/api/roles?type=MachineToMachine"):
        if r["name"] == role_name:
            m.post(
                f"/api/applications/{app_id}/roles",
                {"roleIds": [r["id"]]},
            )
            print(f"           +role '{role_name}'")
            return
    print(f"           [warn] role '{role_name}' not found; app will not have Management API access")


# ─── Main ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--admin", required=True)
    ap.add_argument("--mgmt-id", default="m-default")
    ap.add_argument("--mgmt-secret", required=True)
    args = ap.parse_args()

    m = Mgmt(args.endpoint, args.admin, args.mgmt_id, args.mgmt_secret)

    summary: Dict[str, Dict[str, Any]] = {}

    # Connectors live in default tenant.
    ensure_ses_connector(m)

    for p in (TEMPLATEGEN, WRITER):
        print(f"\n=== {p.name} ===")
        api_r = ensure_api_resource(m, p.api_resource, p.api_resource_name)
        ensure_scopes_on_resource(m, api_r["id"], p.scopes)
        spa = ensure_app(m, p.spa_app_name, "SPA", {
            "redirectUris": p.redirect_uris,
            "postLogoutRedirectUris": p.post_logout_uris,
        })
        summary[p.name] = {
            "spa_app_id": spa["id"],
            "api_resource": p.api_resource,
        }

    print("\n=== paste into .env files ===\n")
    print("# DCR is shared: all products use the seeded m-default app.")
    print(f"# Read the secret from postgres once:")
    print(f"#   docker exec logto-docker-postgres-1 psql -U logto -d logto -t -A \\")
    print(f"#     -c \"SELECT secret FROM applications WHERE id='m-default';\"")
    print()
    for name, v in summary.items():
        print(f"# --- {name} ---")
        print(f"LOGTO_ENDPOINT=https://auth.swapp1990.org")
        print(f"LOGTO_APP_ID={v['spa_app_id']}")
        print(f"LOGTO_API_RESOURCE={v['api_resource']}")
        print(f"LOGTO_MGMT_APP_ID={SHARED_DCR_APP_ID}")
        print(f"LOGTO_MGMT_APP_SECRET=<m-default secret from above>")
        print(f"LOGTO_MGMT_TOKEN_ENDPOINT=https://auth.swapp1990.org:8443/oidc/token")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())

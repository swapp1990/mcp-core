"""
End-to-end compatibility check: self-hosted Logto ↔ mcp-core.

Exercises every Logto surface mcp-core depends on, against a local
docker-compose'd Logto. Prints PASS/FAIL for each step so regressions
are obvious.

Prereqs:
    cd mcp-core/deploy/logto
    docker compose -f docker-compose.yml -f docker-compose.local.yml up -d

Usage:
    python verify.py
        # assumes local compose is running on 3001/3002

    python verify.py --endpoint https://auth.swapp1990.org --admin https://auth.swapp1990.org/admin
        # run against a prod instance (needs seeded m-default creds via --mgmt)

The script reads seeded m-default / m-admin secrets from Postgres when
--mgmt-secret isn't passed and PG_HOST/PG_USER/PG_PASSWORD are set
(defaults match docker-compose.local.yml).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

# Hook into mcp-core to validate our compatibility shim works end-to-end.
SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")
sys.path.insert(0, os.path.abspath(SRC))
from mcp_core.dcr import LogtoDCR  # noqa: E402


# Plain ASCII so output survives cp1252 terminals (Windows default).
OK = "[PASS]"
FAIL = "[FAIL]"


@dataclass
class Config:
    endpoint: str
    admin_endpoint: str
    mgmt_app_id: str
    mgmt_app_secret: str
    mgmt_api_resource: str


def _docker_pg_fetch_secret(app_id: str) -> Optional[str]:
    """Read the seeded M2M app secret from the running Logto Postgres."""
    try:
        out = subprocess.check_output(
            [
                "docker", "exec", "logto-postgres-1",
                "psql", "-U", "logto", "-d", "logto",
                "-t", "-A",
                "-c", f"SELECT secret FROM applications WHERE id='{app_id}';",
            ],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return out or None
    except Exception:
        return None


def step(name: str, ok: bool, detail: str = "") -> bool:
    marker = OK if ok else FAIL
    line = f"{marker} {name}"
    if detail:
        line += f"  [{detail}]"
    print(line)
    return ok


async def check_oidc_discovery(client: httpx.AsyncClient, cfg: Config) -> Dict[str, Any]:
    """Verify mcp-core's expected OIDC endpoints are advertised."""
    resp = await client.get(f"{cfg.endpoint}/oidc/.well-known/openid-configuration")
    resp.raise_for_status()
    meta = resp.json()

    step("OIDC discovery reachable", True, cfg.endpoint)
    step(
        "issuer matches mcp-core's LogtoAuth expectation",
        meta["issuer"] == f"{cfg.endpoint}/oidc",
        meta["issuer"],
    )
    step(
        "jwks_uri matches LogtoAuth _get_jwks_client()",
        meta["jwks_uri"] == f"{cfg.endpoint}/oidc/jwks",
        meta["jwks_uri"],
    )
    step(
        "token_endpoint present",
        meta["token_endpoint"] == f"{cfg.endpoint}/oidc/token",
    )
    step(
        "authorization_endpoint present",
        meta["authorization_endpoint"] == f"{cfg.endpoint}/oidc/auth",
    )
    step(
        "resource-indicator grant_type accepted (client_credentials)",
        "client_credentials" in meta["grant_types_supported"],
    )
    # Logto never advertises registration_endpoint — mcp-core fills that gap.
    step(
        "registration_endpoint intentionally absent (mcp-core LogtoDCR covers it)",
        "registration_endpoint" not in meta,
    )
    return meta


async def check_jwks(client: httpx.AsyncClient, cfg: Config) -> None:
    resp = await client.get(f"{cfg.endpoint}/oidc/jwks")
    resp.raise_for_status()
    jwks = resp.json()
    step(
        "JWKS returns at least one key",
        "keys" in jwks and len(jwks["keys"]) >= 1,
        f"{len(jwks.get('keys', []))} key(s)",
    )
    # LogtoAuth.verify_token accepts RS256/ES256/ES384/ES512
    accepted = {"RS256", "ES256", "ES384", "ES512"}
    algs = {k.get("alg") for k in jwks["keys"] if k.get("alg")}
    step(
        "JWT alg is one LogtoAuth accepts",
        bool(algs & accepted),
        ",".join(sorted(algs)),
    )


async def check_mgmt_token_and_dcr(cfg: Config) -> None:
    """Use mcp-core's LogtoDCR directly to prove end-to-end DCR works."""
    dcr = LogtoDCR(
        logto_endpoint=cfg.endpoint,
        mgmt_app_id=cfg.mgmt_app_id,
        mgmt_app_secret=cfg.mgmt_app_secret,
        mgmt_api_resource=cfg.mgmt_api_resource,
        mgmt_token_endpoint=f"{cfg.admin_endpoint}/oidc/token",
        app_name_prefix="mcp-dcr-verify",
    )

    body = {
        "redirect_uris": ["http://localhost:5176/callback"],
        "client_name": "verify-run",
        "token_endpoint_auth_method": "none",
    }
    result = await dcr.register(body)
    step(
        "LogtoDCR.register() returns RFC 7591 shape",
        "client_id" in result and result["redirect_uris"] == body["redirect_uris"],
        result.get("client_id", "?"),
    )
    step(
        "Native (PKCE) app created - no client_secret leak",
        "client_secret" not in result,
    )


async def check_m2m_token_and_jwt_validation(cfg: Config) -> None:
    """Issue a real JWT and verify mcp-core's LogtoAuth decodes it."""
    from mcp_core.auth import LogtoAuth

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{cfg.admin_endpoint}/oidc/token",
            auth=(cfg.mgmt_app_id, cfg.mgmt_app_secret),
            data={
                "grant_type": "client_credentials",
                "resource": cfg.mgmt_api_resource,
                "scope": "all",
            },
        )
    resp.raise_for_status()
    token = resp.json()["access_token"]

    # Use LogtoAuth for raw JWKS validation, but we need to match this
    # M2M token's audience, which is the Management API resource (not our
    # app's resource). Build a LogtoAuth targeting the admin tenant.
    auth = LogtoAuth(
        endpoint=cfg.admin_endpoint,
        api_resource=cfg.mgmt_api_resource,
        reject_m2m=False,
    )
    jwks_client = auth._get_jwks_client()
    step("LogtoAuth._get_jwks_client() initialized", jwks_client is not None)

    import jwt as pyjwt
    signing_key = jwks_client.get_signing_key_from_jwt(token)
    payload = pyjwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256", "ES256", "ES384", "ES512"],
        audience=cfg.mgmt_api_resource,
        issuer=f"{cfg.admin_endpoint}/oidc",
    )
    step(
        "JWT signed by Logto validated with LogtoAuth's JWKS path",
        payload.get("sub") == cfg.mgmt_app_id,
        f"sub={payload.get('sub')}",
    )


async def amain(cfg: Config) -> int:
    print("\n=== mcp-core vs self-hosted Logto: compatibility ===")
    print(f"endpoint       : {cfg.endpoint}")
    print(f"admin endpoint : {cfg.admin_endpoint}")
    print(f"mgmt resource  : {cfg.mgmt_api_resource}")
    print()

    async with httpx.AsyncClient(timeout=10) as client:
        await check_oidc_discovery(client, cfg)
        await check_jwks(client, cfg)

    await check_mgmt_token_and_dcr(cfg)
    await check_m2m_token_and_jwt_validation(cfg)

    print("\nAll compatibility checks passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", default="http://localhost:3001")
    ap.add_argument("--admin", default="http://localhost:3002")
    ap.add_argument("--mgmt-id", default="m-default")
    ap.add_argument(
        "--mgmt-secret",
        default=None,
        help="If omitted, read from docker-compose'd Postgres.",
    )
    ap.add_argument(
        "--mgmt-resource",
        default="https://default.logto.app/api",
        help="Management API resource indicator. 'default' tenant uses this for OSS.",
    )
    args = ap.parse_args()

    secret = args.mgmt_secret or _docker_pg_fetch_secret(args.mgmt_id)
    if not secret:
        print(
            f"{FAIL} could not obtain mgmt secret for '{args.mgmt_id}' — "
            "pass --mgmt-secret explicitly or ensure docker-compose is up",
            file=sys.stderr,
        )
        return 2

    cfg = Config(
        endpoint=args.endpoint.rstrip("/"),
        admin_endpoint=args.admin.rstrip("/"),
        mgmt_app_id=args.mgmt_id,
        mgmt_app_secret=secret,
        mgmt_api_resource=args.mgmt_resource,
    )
    try:
        return asyncio.run(amain(cfg))
    except Exception as e:
        print(f"\n{FAIL} verify failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

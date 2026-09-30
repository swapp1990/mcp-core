"""
Keep the shared Logto tenant's configuration in the repo instead of the console.

    py logto_config.py snapshot --env-file <path/.env.prod>
        Writes logto-state.json: applications, API resources, connectors
        (no secrets) and the sign-in experience, for review and diffing.

    py logto_config.py apply --env-file <path/.env.prod> [--yes]
        Diffs sign-in-experience.json against the live tenant and, with
        --yes, PATCHes only the fields that differ.

The env file needs LOGTO_ENDPOINT, LOGTO_MGMT_APP_ID, LOGTO_MGMT_APP_SECRET and
LOGTO_MGMT_TOKEN_ENDPOINT (the admin tenant's /oidc/token).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "logto-state.json"
DESIRED_SIE_FILE = HERE / "sign-in-experience.json"

# Connector config keys that are public identifiers; everything else may be a secret.
PUBLIC_CONNECTOR_KEYS = {"clientId", "scope", "prompts", "fromEmail", "region"}


def _load_mgmt_class():
    spec = importlib.util.spec_from_file_location("bootstrap_apps", HERE / "bootstrap-apps.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["bootstrap_apps"] = module
    spec.loader.exec_module(module)
    return module.Mgmt


def _read_env_file(path: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _mgmt(env_file: str):
    env = {**_read_env_file(env_file), **{k: v for k, v in os.environ.items() if k.startswith("LOGTO_")}}
    missing = [k for k in ("LOGTO_ENDPOINT", "LOGTO_MGMT_APP_ID", "LOGTO_MGMT_APP_SECRET", "LOGTO_MGMT_TOKEN_ENDPOINT") if not env.get(k)]
    if missing:
        sys.exit(f"missing in env: {', '.join(missing)}")
    admin = env["LOGTO_MGMT_TOKEN_ENDPOINT"].removesuffix("/oidc/token")
    Mgmt = _load_mgmt_class()
    return Mgmt(
        endpoint=env["LOGTO_ENDPOINT"],
        admin_endpoint=admin,
        mgmt_app_id=env["LOGTO_MGMT_APP_ID"],
        mgmt_app_secret=env["LOGTO_MGMT_APP_SECRET"],
        resource=env.get("LOGTO_MGMT_API_RESOURCE", "https://default.logto.app/api"),
    )


def snapshot(m) -> Dict[str, Any]:
    apps = [
        {
            "id": a["id"],
            "name": a["name"],
            "type": a["type"],
            "isThirdParty": a.get("isThirdParty", False),
            "redirectUris": a.get("oidcClientMetadata", {}).get("redirectUris", []),
            "postLogoutRedirectUris": a.get("oidcClientMetadata", {}).get("postLogoutRedirectUris", []),
        }
        for a in m.get("/api/applications?page_size=100")
    ]
    connectors = [
        {
            "id": c["id"],
            "connectorId": c["connectorId"],
            "target": c.get("target"),
            "type": c.get("type"),
            "platform": c.get("platform"),
            "config": {k: v for k, v in (c.get("config") or {}).items() if k in PUBLIC_CONNECTOR_KEYS},
        }
        for c in m.get("/api/connectors")
    ]
    resources = [
        {"id": r["id"], "name": r["name"], "indicator": r["indicator"]}
        for r in m.get("/api/resources?page_size=100")
    ]
    hooks = [
        {"id": h["id"], "name": h.get("name"), "events": h.get("events"), "url": h.get("config", {}).get("url"), "enabled": h.get("enabled")}
        for h in m.get("/api/hooks")
    ]
    sie = m.get("/api/sign-in-exp")
    for key in ("tenantId", "id"):
        sie.pop(key, None)
    return {
        "applications": sorted(apps, key=lambda a: a["name"]),
        "resources": sorted(resources, key=lambda r: r["indicator"]),
        "connectors": sorted(connectors, key=lambda c: c["id"]),
        "hooks": hooks,
        "signInExperience": sie,
    }


def diff_sign_in_experience(live: Dict[str, Any], desired: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in desired.items() if live.get(key) != value}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["snapshot", "apply"])
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--yes", action="store_true", help="apply the diff (default is a dry run)")
    args = parser.parse_args()

    m = _mgmt(args.env_file)

    if args.command == "snapshot":
        STATE_FILE.write_text(json.dumps(snapshot(m), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {STATE_FILE}")
        return 0

    desired = json.loads(DESIRED_SIE_FILE.read_text(encoding="utf-8"))
    changes = diff_sign_in_experience(m.get("/api/sign-in-exp"), desired)
    if not changes:
        print("sign-in experience already matches sign-in-experience.json")
        return 0
    live = m.get("/api/sign-in-exp")
    for key, value in changes.items():
        print(f"- {key}: {json.dumps(live.get(key))}")
        print(f"+ {key}: {json.dumps(value)}")
    if not args.yes:
        print("dry run; re-run with --yes to apply")
        return 0
    m.patch("/api/sign-in-exp", changes)
    print(f"applied {len(changes)} field(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

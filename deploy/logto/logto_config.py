"""
Keep the shared Logto tenant's configuration in the repo instead of the console.

    py logto_config.py import --env-file <path/.env.prod>
        Writes tenant.json (applications, API resources, hooks, connectors; no
        secrets) from the live tenant, skipping its `unmanaged` objects, and
        refreshes the fields sign-in-experience.json lists.

    py logto_config.py plan --env-file <path/.env.prod>
        Read-only diff of tenant.json and sign-in-experience.json against the
        live tenant: `+` would create, `~` would change, `?`/`-` live only
        (kept). Exit code 2 when they differ, 0 when they match.

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
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "logto-state.json"
TENANT_FILE = HERE / "tenant.json"
DESIRED_SIE_FILE = HERE / "sign-in-experience.json"

# Logto rejects page_size above 100.
PAGE_SIZE = 100
KINDS = ("resources", "applications", "hooks", "connectors")

DEFAULT_UNMANAGED = {
    "applicationNamePrefixes": ["mcp-dcr-"],
    "applicationIds": ["admin-console", "m-default"],
    "resourceIndicators": ["https://default.logto.app/api"],
}

# Connector config keys that are public identifiers; everything else may be a secret.
PUBLIC_CONNECTOR_KEYS = {"clientId", "scope", "prompts", "fromEmail", "region", "emailAddress"}
CLIENT_METADATA_KEYS = (
    "corsAllowedOrigins", "idTokenTtl", "refreshTokenTtl", "refreshTokenTtlInDays",
    "rotateRefreshToken", "alwaysIssueRefreshToken", "allowTokenExchange", "isDeviceFlow",
)
SECRET_NAME = re.compile(r"secret|key|password|token|credential", re.IGNORECASE)
# Hand-written pointers to where a secret lives; not in Logto, so kept across imports.
ANNOTATIONS = ("secretEnv", "signingKeyEnv")


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


def list_all(m, path: str) -> List[Dict[str, Any]]:
    sep = "&" if "?" in path else "?"
    items: List[Dict[str, Any]] = []
    page = 1
    while True:
        batch = m.get(f"{path}{sep}page={page}&page_size={PAGE_SIZE}")
        # An endpoint that ignores `page` would return page 1 forever.
        if page > 1 and batch and batch[0] == items[0]:
            raise RuntimeError(f"GET {path} ignored the page parameter")
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            return items
        page += 1


def _public_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {k: config[k] for k in sorted(config or {}) if k in PUBLIC_CONNECTOR_KEYS and not SECRET_NAME.search(k)}


def _strip_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_secrets(v) for k, v in sorted(value.items()) if not SECRET_NAME.search(k)}
    if isinstance(value, list):
        return [_strip_secrets(v) for v in value]
    return value


def _drop_empty(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in entry.items() if v not in (None, "", [], {})}


# ─── Desired-state projection (allowlists only) ───────────────────────

def project_application(a: Dict[str, Any], roles: List[str]) -> Dict[str, Any]:
    oidc = a.get("oidcClientMetadata") or {}
    ccm = a.get("customClientMetadata") or {}
    client_metadata = {k: ccm[k] for k in CLIENT_METADATA_KEYS if k in ccm}
    if "corsAllowedOrigins" in client_metadata:
        client_metadata["corsAllowedOrigins"] = sorted(client_metadata["corsAllowedOrigins"])
    return _drop_empty({
        "id": a["id"],
        "name": a["name"],
        "type": a["type"],
        "description": a.get("description"),
        "isThirdParty": True if a.get("isThirdParty") else None,
        "redirectUris": sorted(oidc.get("redirectUris") or []),
        "postLogoutRedirectUris": sorted(oidc.get("postLogoutRedirectUris") or []),
        "customClientMetadata": _drop_empty(client_metadata),
        "customData": _strip_secrets(a.get("customData") or {}),
        "roles": sorted(roles),
    })


def project_resource(r: Dict[str, Any], scopes: List[str]) -> Dict[str, Any]:
    return {
        "indicator": r["indicator"],
        "name": r["name"],
        "accessTokenTtl": r.get("accessTokenTtl"),
        **({"isDefault": True} if r.get("isDefault") else {}),
        "scopes": sorted(scopes),
    }


def project_hook(h: Dict[str, Any]) -> Dict[str, Any]:
    # Never signingKey or config.headers.
    events = h.get("events") or ([h["event"]] if h.get("event") else [])
    return {
        "name": h.get("name"),
        "events": sorted(events),
        "url": (h.get("config") or {}).get("url"),
        "enabled": bool(h.get("enabled")),
    }


def project_connector(c: Dict[str, Any]) -> Dict[str, Any]:
    return _drop_empty({
        "id": c["id"],
        "connectorId": c["connectorId"],
        "target": c.get("target"),
        "syncProfile": bool(c.get("syncProfile")),
        "public": _public_config(c.get("config")),
    })


def _is_unmanaged_app(a: Dict[str, Any], unmanaged: Dict[str, Any]) -> bool:
    return a["id"] in unmanaged.get("applicationIds", []) or any(
        a["name"].startswith(p) for p in unmanaged.get("applicationNamePrefixes", [])
    )


def read_live(m, unmanaged: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    apps = []
    for a in list_all(m, "/api/applications"):
        if _is_unmanaged_app(a, unmanaged):
            continue
        roles = []
        if a["type"] == "MachineToMachine":
            roles = [r["name"] for r in list_all(m, f"/api/applications/{a['id']}/roles")]
        apps.append(project_application(a, roles))
    resources = [
        project_resource(r, [s["name"] for s in list_all(m, f"/api/resources/{r['id']}/scopes")])
        for r in list_all(m, "/api/resources")
        if r["indicator"] not in unmanaged.get("resourceIndicators", [])
    ]
    return {
        "resources": sorted(resources, key=lambda r: r["indicator"]),
        "applications": sorted(apps, key=lambda a: (a["name"], a["id"])),
        "hooks": sorted((project_hook(h) for h in list_all(m, "/api/hooks")), key=lambda h: h["name"] or ""),
        # /api/connectors is not paginated.
        "connectors": sorted((project_connector(c) for c in m.get("/api/connectors")), key=lambda c: (c["connectorId"], c["id"])),
    }


# ─── Matching and diffing ─────────────────────────────────────────────

def match(kind: str, desired: Dict[str, Any], live: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    # Apps and connectors match by id once they have one: client builds embed the id.
    if kind in ("applications", "connectors") and desired.get("id"):
        field = "id"
    else:
        field = {"applications": "name", "resources": "indicator", "hooks": "name", "connectors": "connectorId"}[kind]
    return next((entry for entry in live if entry.get(field) == desired.get(field)), None)


def label(kind: str, entry: Dict[str, Any]) -> str:
    if kind == "applications":
        return f"\"{entry.get('name')}\" ({entry.get('type')}) {entry.get('id', '')}".rstrip()
    if kind == "resources":
        return f"{entry.get('indicator')} (\"{entry.get('name')}\")"
    if kind == "hooks":
        return f"\"{entry.get('name')}\""
    return f"{entry.get('connectorId')} {entry.get('id', '')}".rstrip()


def _flatten(entry: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in entry.items():
        if not prefix and key in ("id", *ANNOTATIONS):
            continue
        if isinstance(value, dict):
            out.update(_flatten(value, f"{prefix}{key}."))
        elif value not in (None, "", []):
            out[f"{prefix}{key}"] = value
    return out


def diff_entry(desired: Dict[str, Any], live: Dict[str, Any]) -> List[str]:
    """Field lines: `~ f: old -> new`, `+ f: item` to add, `- f: item` live only."""
    want, have = _flatten(desired), _flatten(live)
    lines = []
    for path in sorted(set(want) | set(have)):
        w, h = want.get(path), have.get(path)
        if isinstance(w, list) or isinstance(h, list):
            w_items = {json.dumps(i, sort_keys=True) for i in (w or [])}
            h_items = {json.dumps(i, sort_keys=True) for i in (h or [])}
            lines += [f"+ {path}: {i}" for i in sorted(w_items - h_items)]
            lines += [f"- {path}: {i}  (live only, kept)" for i in sorted(h_items - w_items)]
        elif w != h:
            old = json.dumps(h) if path in have else "(unset)"
            new = json.dumps(w) if path in want else "(unset)"
            lines.append(f"~ {path}: {old} -> {new}")
    return lines


def diff_sign_in_experience(live: Dict[str, Any], desired: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in desired.items() if live.get(key) != value}


# ─── Commands ─────────────────────────────────────────────────────────

def _load_tenant(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _sie_path(tenant: Dict[str, Any], tenant_path: Path) -> Path:
    return tenant_path.parent / tenant.get("signInExperience", DESIRED_SIE_FILE.name)


def import_tenant(m, existing: Dict[str, Any]) -> Dict[str, Any]:
    unmanaged = existing.get("unmanaged") or DEFAULT_UNMANAGED
    live = read_live(m, unmanaged)
    for kind in KINDS:
        for old in existing.get(kind, []):
            new = match(kind, old, live[kind])
            if new is not None:
                new.update({k: old[k] for k in ANNOTATIONS if k in old})
    return {
        "version": 1,
        "unmanaged": unmanaged,
        "signInExperience": existing.get("signInExperience", DESIRED_SIE_FILE.name),
        **live,
    }


def plan(m, tenant: Dict[str, Any], desired_sie: Dict[str, Any]) -> tuple[List[str], Dict[str, int]]:
    live = read_live(m, tenant.get("unmanaged") or DEFAULT_UNMANAGED)
    out: List[str] = []
    counts = {"create": 0, "change": 0, "live_only": 0}
    for kind in KINDS:
        section: List[str] = []
        matched: List[int] = []
        for desired in tenant.get(kind, []):
            found = match(kind, desired, live[kind])
            if found is None:
                note = "  (id not in live)" if desired.get("id") else ""
                section.append(f"  + {label(kind, desired)}  would create{note}")
                counts["create"] += 1
                continue
            matched.append(id(found))
            lines = diff_entry(desired, found)
            if not lines:
                continue
            changes = [line for line in lines if not line.startswith("-")]
            counts["change"] += 1 if changes else 0
            counts["live_only"] += len(lines) - len(changes)
            section.append(f"  {'~' if changes else '-'} {label(kind, found)}")
            section += [f"      {line}" for line in lines]
        for entry in live[kind]:
            if id(entry) not in matched:
                section.append(f"  ? {label(kind, entry)}  live only, kept")
                counts["live_only"] += 1
        out += [f"{kind}:", *section] if section else [f"{kind}: no drift"]
    live_sie = m.get("/api/sign-in-exp")
    sie_changes = diff_sign_in_experience(live_sie, desired_sie)
    if sie_changes:
        out.append("signInExperience:")
        for key, value in sie_changes.items():
            out.append(f"  ~ {key}: {json.dumps(live_sie.get(key))} -> {json.dumps(value)}")
        counts["change"] += len(sie_changes)
    else:
        out.append("signInExperience: no drift")
    return out, counts


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
        for a in list_all(m, "/api/applications")
    ]
    connectors = [
        {
            "id": c["id"],
            "connectorId": c["connectorId"],
            "target": c.get("target"),
            "type": c.get("type"),
            "platform": c.get("platform"),
            "config": _public_config(c.get("config")),
        }
        for c in m.get("/api/connectors")
    ]
    resources = [
        {"id": r["id"], "name": r["name"], "indicator": r["indicator"]}
        for r in list_all(m, "/api/resources")
    ]
    hooks = [
        {"id": h["id"], "name": h.get("name"), "events": h.get("events"), "url": h.get("config", {}).get("url"), "enabled": h.get("enabled")}
        for h in list_all(m, "/api/hooks")
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


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["import", "plan", "snapshot", "apply"])
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--yes", action="store_true", help="apply: patch the diff (default is a dry run)")
    args = parser.parse_args(argv)

    m = _mgmt(args.env_file)

    if args.command == "snapshot":
        STATE_FILE.write_text(json.dumps(snapshot(m), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {STATE_FILE}")
        return 0

    if args.command == "import":
        tenant = import_tenant(m, _load_tenant(TENANT_FILE))
        _write_json(TENANT_FILE, tenant)
        print(f"wrote {TENANT_FILE}: " + ", ".join(f"{len(tenant[k])} {k}" for k in KINDS))
        sie_path = _sie_path(tenant, TENANT_FILE)
        desired_sie = json.loads(sie_path.read_text(encoding="utf-8"))
        live_sie = m.get("/api/sign-in-exp")
        stale = diff_sign_in_experience(live_sie, desired_sie)
        if stale:
            _write_json(sie_path, {key: live_sie.get(key) for key in desired_sie})
            print(f"updated {sie_path.name} from live: {', '.join(stale)}")
        return 0

    if args.command == "plan":
        tenant = _load_tenant(TENANT_FILE)
        if not tenant:
            sys.exit(f"{TENANT_FILE} not found; run import first")
        desired_sie = json.loads(_sie_path(tenant, TENANT_FILE).read_text(encoding="utf-8"))
        lines, counts = plan(m, tenant, desired_sie)
        print("\n".join(lines))
        if not any(counts.values()):
            print("no drift: tenant.json matches the live tenant. Nothing was written.")
            return 0
        print(f"drift: {counts['create']} to create, {counts['change']} to change, "
              f"{counts['live_only']} live only (kept). Nothing was written.")
        return 2

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

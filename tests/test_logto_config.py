"""
Unit tests for deploy/logto/logto_config.py (import / plan / snapshot / apply).

A fake Mgmt serves a fixture tenant shaped like Logto 1.38 Management API
responses, with planted secrets. No network calls.
"""

import copy
import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

LOGTO_DIR = Path(__file__).resolve().parents[1] / "deploy" / "logto"
_spec = importlib.util.spec_from_file_location("logto_config", LOGTO_DIR / "logto_config.py")
lc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lc)

PLANTED = [
    "PLANTED-app-secret-9f8e7d",
    "PLANTED-m2m-secret-1a2b3c",
    "PLANTED-hook-signing-key-4d5e",
    "PLANTED-hook-header-bearer-6f7a",
    "PLANTED-google-client-secret-8b9c",
    "PLANTED-apple-private-key-0d1e",
    "PLANTED-apple-key-id-2f3a",
    "PLANTED-ses-access-key-id-4b5c",
    "PLANTED-ses-access-key-secret-6d7e",
    "PLANTED-mock-client-secret-8f9a",
    "PLANTED-custom-data-token-0b1c",
]


# ── Fixture tenant (Logto 1.38 response shapes) ────────────

def _app(id_, name, type_, secret="", redirects=(), logouts=(), ccm=None, custom=None, description=None):
    return {
        "tenantId": "default", "id": id_, "name": name, "secret": secret or f"internal-{id_}",
        "description": description, "type": type_,
        "oidcClientMetadata": {"redirectUris": list(redirects), "postLogoutRedirectUris": list(logouts)},
        "customClientMetadata": ccm or {}, "protectedAppMetadata": None,
        "customData": custom or {}, "isThirdParty": False, "createdAt": 1727740800000,
    }


def fixture_tenant():
    desired_sie = json.loads((LOGTO_DIR / "sign-in-experience.json").read_text(encoding="utf-8"))
    return {
        "applications": [
            _app("nativeapp00000000001", "LetMeActForYou iOS (Native)", "Native", secret=PLANTED[0],
                 redirects=["letmeactforyou://callback/", "letmeactforyou://callback"],
                 logouts=["letmeactforyou://callback"],
                 ccm={"refreshTokenTtl": 15552000, "refreshTokenTtlInDays": 180, "rotateRefreshToken": True},
                 custom={"product": "actforyou", "apiToken": PLANTED[10]}, description="iOS app"),
            _app("spaapp00000000000001", "SnapForYou", "SPA",
                 redirects=["https://snapforyou.swapp1990.org/callback"],
                 logouts=["https://snapforyou.swapp1990.org"],
                 ccm={"corsAllowedOrigins": ["https://snapforyou.swapp1990.org", "http://localhost:8791"]}),
            _app("m2mapp00000000000001", "ActForYou MCP-DCR", "MachineToMachine", secret=PLANTED[1]),
            _app("dcrapp00000000000001", "mcp-dcr-actforyou: mcp-readiness-probe", "Native",
                 redirects=["http://127.0.0.1:9/cb"], custom={"product": "actforyou"}),
            _app("m-default", "Default M2M", "MachineToMachine"),
        ],
        "app_roles": {
            "m2mapp00000000000001": [{"id": "role1", "name": "Logto Management API access", "type": "MachineToMachine"}],
        },
        "resources": [
            {"tenantId": "default", "id": "mgmt", "name": "Logto Management API",
             "indicator": "https://default.logto.app/api", "isDefault": False, "accessTokenTtl": 3600},
            {"tenantId": "default", "id": "res1", "name": "ActForYou API",
             "indicator": "https://actforyou.swapp1990.org", "isDefault": False, "accessTokenTtl": 3600},
            {"tenantId": "default", "id": "res2", "name": "Writer API",
             "indicator": "https://writer.swapp1990.org", "isDefault": False, "accessTokenTtl": 604800},
        ],
        "scopes": {
            "res2": [{"id": "s1", "name": "write:story", "description": "w"}, {"id": "s2", "name": "read:story", "description": "r"}],
        },
        "hooks": [
            {"tenantId": "default", "id": "hook1", "name": "ActForYou account deletion", "event": None,
             "events": ["User.Deleted"], "signingKey": PLANTED[2], "enabled": True, "createdAt": 1,
             "config": {"url": "https://actforyou.swapp1990.org/api/logto/webhook",
                        "headers": {"Authorization": PLANTED[3]}}},
            {"tenantId": "default", "id": "hook2", "name": "WriteForYou account deletion", "event": None,
             "events": ["User.Deleted"], "signingKey": "internal-hook2", "enabled": False, "createdAt": 2,
             "config": {"url": "https://writer.swapp1990.org/api/logto/webhook"}},
        ],
        "connectors": [
            {"id": "googleconn01", "connectorId": "google-universal", "target": "google", "type": "Social",
             "platform": "Universal", "syncProfile": False, "metadata": {"target": "google"},
             "config": {"clientId": "123-abc.apps.googleusercontent.com", "clientSecret": PLANTED[4],
                        "prompts": ["select_account"], "scope": "openid profile email"}},
            {"id": "appleconn001", "connectorId": "apple-universal", "target": "apple", "type": "Social",
             "platform": "Universal", "syncProfile": False,
             "config": {"clientId": "com.swapp1990.accounts.siwa", "privateKey": PLANTED[5], "keyId": PLANTED[6]}},
            {"id": "sesconn00001", "connectorId": "aws-ses-mail", "target": "aws-ses", "type": "Email",
             "platform": None, "syncProfile": False,
             "config": {"accessKeyId": PLANTED[7], "accessKeySecret": PLANTED[8],
                        "emailAddress": "noreply@example.com", "region": "us-west-2", "templates": []}},
            {"id": "mockconn0001", "connectorId": "mock-social-connector", "target": "mock-social", "type": "Social",
             "platform": "Web", "syncProfile": False,
             "config": {"clientId": "mock-client", "clientSecret": PLANTED[9]}},
        ],
        "sie": {"tenantId": "default", "id": "default", "color": {"primaryColor": "#6139F6"}, **desired_sie},
    }


class FakeMgmt:
    """Read-only Management API over a fixture tenant; any write fails the test."""

    def __init__(self, tenant, ignore_page=()):
        self.t = tenant
        self.ignore_page = set(ignore_page)
        self.gets = []

    def _page(self, path, items, query):
        if "page" not in query or path in self.ignore_page:
            return items
        size = int(query["page_size"][0])
        assert size <= 100, "Logto rejects page_size above 100"
        start = (int(query["page"][0]) - 1) * size
        return items[start:start + size]

    def get(self, path):
        self.gets.append(path)
        parts = urlsplit(path)
        query = parse_qs(parts.query)
        segs = parts.path.strip("/").split("/")[1:]
        t = self.t
        if segs == ["applications"]:
            items = t["applications"]
        elif segs[0] == "applications" and segs[2:] == ["roles"]:
            items = t["app_roles"].get(segs[1], [])
        elif segs == ["resources"]:
            items = t["resources"]
        elif segs[0] == "resources" and segs[2:] == ["scopes"]:
            items = t["scopes"].get(segs[1], [])
        elif segs == ["hooks"]:
            items = t["hooks"]
        elif segs == ["connectors"]:
            return copy.deepcopy(t["connectors"])
        elif segs == ["sign-in-exp"]:
            return copy.deepcopy(t["sie"])
        else:
            raise AssertionError(f"unexpected GET {path}")
        return copy.deepcopy(self._page(parts.path, items, query))

    def post(self, path, body):
        raise AssertionError(f"write attempted: POST {path}")

    def patch(self, path, body):
        raise AssertionError(f"write attempted: PATCH {path}")

    def delete(self, path):
        raise AssertionError(f"write attempted: DELETE {path}")


@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    """Run logto_config.main against a FakeMgmt with every file under tmp_path."""
    (tmp_path / "sign-in-experience.json").write_text(
        (LOGTO_DIR / "sign-in-experience.json").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(lc, "TENANT_FILE", tmp_path / "tenant.json")
    monkeypatch.setattr(lc, "STATE_FILE", tmp_path / "logto-state.json")
    monkeypatch.setattr(lc, "DESIRED_SIE_FILE", tmp_path / "sign-in-experience.json")
    mgmt = FakeMgmt(fixture_tenant())
    monkeypatch.setattr(lc, "_mgmt", lambda env_file: mgmt)

    class Runner:
        dir = tmp_path

        def __call__(self, *args):
            capsys.readouterr()
            code = lc.main([*args, "--env-file", "unused.env"])
            return code, capsys.readouterr().out

        def tenant(self):
            return json.loads((tmp_path / "tenant.json").read_text(encoding="utf-8"))

        def write_tenant(self, data):
            (tmp_path / "tenant.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    runner = Runner()
    runner.mgmt = mgmt
    return runner


# ── Pagination ─────────────────────────────────────────────

def _many_apps(n):
    return [_app(f"app{i:05d}", f"App {i:05d}", "SPA") for i in range(n)]


def test_list_all_reads_every_page():
    tenant = fixture_tenant()
    tenant["applications"] = _many_apps(250)
    m = FakeMgmt(tenant)
    apps = lc.list_all(m, "/api/applications")
    assert [a["id"] for a in apps] == [f"app{i:05d}" for i in range(250)]
    assert m.gets == [f"/api/applications?page={p}&page_size=100" for p in (1, 2, 3)]


def test_list_all_stops_on_empty_page_after_exact_multiple():
    tenant = fixture_tenant()
    tenant["applications"] = _many_apps(200)
    m = FakeMgmt(tenant)
    assert len(lc.list_all(m, "/api/applications")) == 200
    assert len(m.gets) == 3


def test_list_all_refuses_an_endpoint_that_ignores_page():
    tenant = fixture_tenant()
    tenant["applications"] = _many_apps(100)
    m = FakeMgmt(tenant, ignore_page={"/api/applications"})
    with pytest.raises(RuntimeError, match="ignored the page parameter"):
        lc.list_all(m, "/api/applications")


def test_snapshot_reads_past_the_first_page(cli):
    cli.mgmt.t["applications"] += _many_apps(150)
    assert cli("snapshot")[0] == 0
    state = json.loads((cli.dir / "logto-state.json").read_text(encoding="utf-8"))
    assert len(state["applications"]) == 155


def test_import_reads_past_the_first_page(cli):
    cli.mgmt.t["applications"] += _many_apps(150)
    cli("import")
    assert len(cli.tenant()["applications"]) == 153


# ── Secret redaction ───────────────────────────────────────

def test_no_command_prints_or_writes_a_secret(cli):
    outputs = []
    for command in ("import", "plan", "snapshot"):
        outputs.append(cli(command)[1])
    cli.mgmt.t["sie"]["customCss"] = ".changed {}"
    outputs.append(cli("plan")[1])
    outputs.append(cli("apply")[1])
    files = [p.read_text(encoding="utf-8") for p in cli.dir.glob("*.json")]
    blob = "\n".join(outputs + files)
    leaked = [s for s in PLANTED if s in blob]
    assert leaked == []
    for field in ("clientSecret", "privateKey", "keyId", "accessKeyId", "accessKeySecret", "signingKey", "headers", "apiToken"):
        assert f'"{field}"' not in blob


def test_connector_public_fields_are_kept(cli):
    cli("import")
    connectors = {c["connectorId"]: c for c in cli.tenant()["connectors"]}
    assert connectors["google-universal"]["public"] == {
        "clientId": "123-abc.apps.googleusercontent.com", "prompts": ["select_account"], "scope": "openid profile email"}
    assert connectors["apple-universal"]["public"] == {"clientId": "com.swapp1990.accounts.siwa"}
    assert connectors["aws-ses-mail"]["public"] == {"emailAddress": "noreply@example.com", "region": "us-west-2"}


def test_secret_named_connector_keys_are_dropped_even_if_allowlisted(monkeypatch):
    monkeypatch.setattr(lc, "PUBLIC_CONNECTOR_KEYS", {"clientId", "clientSecret"})
    assert lc._public_config({"clientId": "a", "clientSecret": "b"}) == {"clientId": "a"}


# ── Import shape and unmanaged filtering ───────────────────

def test_import_writes_the_desired_state_shape(cli):
    code, out = cli("import")
    assert code == 0
    assert "2 resources, 3 applications, 2 hooks, 4 connectors" in out
    tenant = cli.tenant()
    assert list(tenant) == ["version", "unmanaged", "signInExperience", "resources", "applications", "hooks", "connectors"]
    assert tenant["signInExperience"] == "sign-in-experience.json"
    native = next(a for a in tenant["applications"] if a["type"] == "Native")
    assert native["redirectUris"] == ["letmeactforyou://callback", "letmeactforyou://callback/"]
    assert native["customClientMetadata"] == {"refreshTokenTtl": 15552000, "refreshTokenTtlInDays": 180, "rotateRefreshToken": True}
    assert native["customData"] == {"product": "actforyou"}
    m2m = next(a for a in tenant["applications"] if a["type"] == "MachineToMachine")
    assert m2m["roles"] == ["Logto Management API access"]
    assert "redirectUris" not in m2m
    writer = next(r for r in tenant["resources"] if r["name"] == "Writer API")
    assert writer["scopes"] == ["read:story", "write:story"]
    assert tenant["hooks"][0] == {"name": "ActForYou account deletion", "events": ["User.Deleted"],
                                  "url": "https://actforyou.swapp1990.org/api/logto/webhook", "enabled": True}


def test_import_skips_unmanaged_objects(cli):
    cli("import")
    tenant = cli.tenant()
    names = [a["name"] for a in tenant["applications"]]
    assert not any(n.startswith("mcp-dcr-") for n in names)
    assert "m-default" not in [a["id"] for a in tenant["applications"]]
    assert "https://default.logto.app/api" not in [r["indicator"] for r in tenant["resources"]]
    assert tenant["unmanaged"] == lc.DEFAULT_UNMANAGED


def test_import_keeps_a_custom_unmanaged_section(cli):
    unmanaged = {**lc.DEFAULT_UNMANAGED, "applicationNamePrefixes": ["mcp-dcr-", "Snap"]}
    cli.write_tenant({"version": 1, "unmanaged": unmanaged})
    cli("import")
    tenant = cli.tenant()
    assert tenant["unmanaged"] == unmanaged
    assert "SnapForYou" not in [a["name"] for a in tenant["applications"]]


def test_plan_ignores_new_unmanaged_objects_in_live(cli):
    cli("import")
    cli.mgmt.t["applications"].append(_app("dcrapp00000000000002", "mcp-dcr-writer: Cursor", "Native"))
    code, out = cli("plan")
    assert code == 0, out
    assert "mcp-dcr" not in out


def test_import_is_byte_stable(cli):
    cli("import")
    first = (cli.dir / "tenant.json").read_bytes()
    cli.mgmt.t["applications"].reverse()
    cli.mgmt.t["hooks"].reverse()
    cli("import")
    assert (cli.dir / "tenant.json").read_bytes() == first


def test_reimport_keeps_secret_env_annotations(cli):
    cli("import")
    tenant = cli.tenant()
    m2m = next(a for a in tenant["applications"] if a["type"] == "MachineToMachine")
    m2m["secretEnv"] = "actforyou:MCP_LOGTO_APP_SECRET"
    tenant["hooks"][0]["signingKeyEnv"] = "actforyou:LOGTO_WEBHOOK_SIGNING_KEY"
    cli.write_tenant(tenant)
    cli("import")
    again = cli.tenant()
    assert next(a for a in again["applications"] if a["type"] == "MachineToMachine")["secretEnv"] == "actforyou:MCP_LOGTO_APP_SECRET"
    assert again["hooks"][0]["signingKeyEnv"] == "actforyou:LOGTO_WEBHOOK_SIGNING_KEY"
    assert cli("plan")[0] == 0


def test_import_refreshes_sign_in_experience_fields_from_live(cli):
    cli.mgmt.t["sie"]["customCss"] = ".live {}"
    code, out = cli("import")
    assert "updated sign-in-experience.json from live: customCss" in out
    sie = json.loads((cli.dir / "sign-in-experience.json").read_text(encoding="utf-8"))
    assert sie["customCss"] == ".live {}"
    assert "color" not in sie
    assert cli("plan")[0] == 0


# ── Plan ───────────────────────────────────────────────────

def test_plan_after_import_has_no_drift(cli):
    cli("import")
    code, out = cli("plan")
    assert code == 0
    assert "no drift: tenant.json matches the live tenant" in out


def test_plan_classifies_drift_and_exits_2(cli):
    cli("import")
    tenant = cli.tenant()
    native = next(a for a in tenant["applications"] if a["type"] == "Native")
    native["redirectUris"] = ["letmeactforyou://callback", "letmeactforyou://new"]
    native["customClientMetadata"]["refreshTokenTtlInDays"] = 90
    tenant["applications"].append({"name": "New Web", "type": "SPA", "redirectUris": ["https://new.example/cb"]})
    next(r for r in tenant["resources"] if r["name"] == "Writer API")["name"] = "Writer API v2"
    tenant["hooks"] = [h for h in tenant["hooks"] if h["name"] != "WriteForYou account deletion"]
    cli.write_tenant(tenant)
    sie = json.loads((cli.dir / "sign-in-experience.json").read_text(encoding="utf-8"))
    sie["customCss"] = ".new {}"
    (cli.dir / "sign-in-experience.json").write_text(json.dumps(sie), encoding="utf-8")

    code, out = cli("plan")
    assert code == 2
    assert '  + "New Web" (SPA)  would create' in out
    assert '  ~ "LetMeActForYou iOS (Native)" (Native) nativeapp00000000001' in out
    assert '      + redirectUris: "letmeactforyou://new"' in out
    assert '      - redirectUris: "letmeactforyou://callback/"  (live only, kept)' in out
    assert "      ~ customClientMetadata.refreshTokenTtlInDays: 180 -> 90" in out
    assert '      ~ name: "Writer API" -> "Writer API v2"' in out
    assert '  ? "WriteForYou account deletion"  live only, kept' in out
    assert '  ~ customCss: null -> ".new {}"' in out
    assert "connectors: no drift" in out
    assert "drift: 1 to create, 3 to change, 2 live only (kept). Nothing was written." in out


def test_plan_counts_live_only_list_entries_as_drift(cli):
    cli("import")
    tenant = cli.tenant()
    spa = next(a for a in tenant["applications"] if a["name"] == "SnapForYou")
    spa["customClientMetadata"]["corsAllowedOrigins"].remove("http://localhost:8791")
    cli.write_tenant(tenant)
    code, out = cli("plan")
    assert code == 2
    assert '  - "SnapForYou" (SPA) spaapp00000000000001' in out
    assert "0 to create, 0 to change, 1 live only (kept)" in out


def test_plan_matches_an_app_by_name_until_it_has_an_id(cli):
    cli("import")
    tenant = cli.tenant()
    for app in tenant["applications"]:
        app.pop("id")
    cli.write_tenant(tenant)
    assert cli("plan")[0] == 0


def test_plan_reports_an_id_that_is_gone_from_live(cli):
    cli("import")
    cli.mgmt.t["applications"] = [a for a in cli.mgmt.t["applications"] if a["name"] != "SnapForYou"]
    code, out = cli("plan")
    assert code == 2
    assert '  + "SnapForYou" (SPA) spaapp00000000000001  would create  (id not in live)' in out


def test_plan_reports_a_live_only_connector(cli):
    cli("import")
    cli.mgmt.t["connectors"].append({"id": "extra0000001", "connectorId": "github-universal", "target": "github",
                                     "syncProfile": False, "config": {"clientId": "gh", "clientSecret": PLANTED[4]}})
    code, out = cli("plan")
    assert code == 2
    assert "  ? github-universal extra0000001  live only, kept" in out
    assert PLANTED[4] not in out


def test_plan_without_tenant_file_exits(cli):
    with pytest.raises(SystemExit, match="run import first"):
        cli("plan")


# ── apply (sign-in experience) keeps working ───────────────

def test_apply_dry_run_then_patch(cli, monkeypatch):
    patched = []
    monkeypatch.setattr(cli.mgmt, "patch", lambda path, body: patched.append((path, body)) or {})
    cli.mgmt.t["sie"]["customCss"] = ".live {}"
    code, out = cli("apply")
    assert code == 0 and "dry run" in out and patched == []
    cli("apply", "--yes")
    assert patched == [("/api/sign-in-exp", {"customCss": None})]

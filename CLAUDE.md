# mcp-core

Shared auth, billing, logging, and MCP-mount layer for WriteForYou and DesignForYou.

- **PyPI distribution name:** `mcp-core-auth`
- **Import package:** `mcp_core` (the import path did not change)

## Who consumes it

Every product follows [docs/platform-contract.md](docs/platform-contract.md) and pins a released `mcp-core-auth` version:

- **WriteForYou**: `D:/MyProjects/Claude/writeforyou/tracks/writer-api/services/api/requirements.txt`
- **DesignForYou**: `D:/MyProjects/Claude/ai-template-gen/backend/requirements.txt` (deployed from that repo; its image is `templategen-backend`)
- **ActForYou (LetMeActForYou)**: `D:/MyProjects/Claude/directforyou/tracks/actforyou` (`deploy/Dockerfile.mcp-core` + `requirements.txt`; own deployment in `/opt/apps/actforyou`)
- **VideoGen**: `D:/MyProjects/Claude/directforyou/tracks/videogen/backend/requirements.txt`
- **VN Creator**: `D:/MyProjects/Claude/vn-creator/backend/requirements.txt` (still on 0.3.1; repinned when it moves to the shared Logto)

## Rename trap

Commit `1043ea0` renamed the dist from `mcp-core` to `mcp-core-auth`. The bare name `mcp-core` on PyPI is an unrelated project. `pip install mcp-core` will not import `mcp_core`. Always install `mcp-core-auth`.

A git-SHA pin older than `1043ea0` installs as `mcp-core`; a later `pip install -U mcp-core-auth` will not upgrade it — two distributions writing the same `mcp_core/` path.

## Release gotcha

Five commits landed after version `0.3.8` was set, with no bump, so `==0.3.8` is not HEAD. Git tags stop at `v0.3.3` (0.3.4–0.3.8 are untagged). Find the 0.3.8 commit with `git log -S'version = "0.3.8"'`.

## Tests

```
py -3 -m pytest tests -q
```

`tests/live/` needs `.env.live` (gated live tests; do not run them as part of the default suite).

## Shipping a change (multi-repo)

1. Bump `pyproject.toml` version.
2. Tag `vX.Y.Z` on that same commit.
3. Publish the `mcp-core-auth` wheel to PyPI.
4. Repin **every** product above and deploy each one.

# Contributing to mcp-core

Thanks for considering a contribution. mcp-core is a small library — issues and PRs are both welcome.

## Reporting bugs

Before filing, please:

1. Reproduce on the latest `main`.
2. Check open issues for duplicates.
3. Include: Python version, mcp-core version, a minimal repro, and the full traceback (not just the last line).

## Proposing changes

For anything more than a one-line fix, open an issue first to discuss the approach. mcp-core has a deliberately narrow scope (auth + billing + MCP mount + tool logging for FastAPI MCP servers), and not every feature fits.

## Pull requests

- One topic per PR. Refactors and bug fixes go in separate PRs.
- Include tests for new behavior. The `tests/` suite runs on mocks; if your change needs live services, add a `tests/live/` test guarded by `RUN_LIVE_TESTS=1`.
- Keep public API changes backward-compatible when possible. If you must break something, call it out in the PR description.
- Match the existing style — no formatter is enforced, but the code aims for short modules with docstrings on public classes and short inline comments only where intent isn't obvious.

## Local development

```bash
git clone https://github.com/swapp1990/mcp-core
cd mcp-core
pip install -e ".[dev]"
pytest                              # mock-based tests, run anywhere
RUN_LIVE_TESTS=1 pytest tests/live  # needs tests/.env.live with real Logto/Mongo/Stripe
```

For OAuth flow changes, the reference deployment is `deploy/logto/` — spin up a local Logto in docker and run `verify.py` to confirm mcp-core still talks to it.

## Auth providers

mcp-core is currently Logto-specific. PRs that add support for additional OIDC providers (Auth0, Keycloak, generic OIDC) are welcome — the cleanest path is extracting a `BaseOIDCProvider` interface from `LogtoAuth` rather than dropping a parallel class. Open an issue first to discuss the shape.

## License

By contributing, you agree that your contributions will be licensed under the same MIT license as the rest of the repo.

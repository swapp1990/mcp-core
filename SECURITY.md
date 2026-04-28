# Security policy

## Reporting a vulnerability

Please **do not** file public GitHub issues for security vulnerabilities.

Email the maintainer directly with:

- A description of the issue and its impact.
- Steps to reproduce, or a proof-of-concept.
- Affected versions, if known.
- Your name / handle for credit (optional).

Contact: report security issues via [GitHub's private vulnerability reporting](https://github.com/swapp1990/mcp-core/security/advisories/new) on this repo.

You should expect an acknowledgement within a few business days. If the report is valid, we'll work on a fix and coordinate disclosure with you. Critical issues that can be exploited remotely will be prioritized.

## Scope

In scope:
- Auth bypass in `LogtoAuth` (token validation, JWKS handling, audience/issuer checks).
- Privilege escalation through DCR (`LogtoDCR`).
- Billing bypass in `StripeBilling` (credit accounting, webhook signature verification).
- Information disclosure via `ToolLogger` or health endpoints.
- Anything that lets an unauthenticated caller invoke a paid tool, or one user act as another.

Out of scope:
- Vulnerabilities in upstream dependencies (Logto, Stripe, FastAPI, MongoDB) — please report those upstream. We'll bump pins once a fix is released.
- Self-inflicted misconfiguration of a downstream deployment (e.g. publishing your `.env` to a public S3 bucket).
- Issues in `examples/` or `deploy/logto/demos/` — those are illustrative, not production code.

## Supported versions

Only the latest tagged minor version receives security fixes. Pre-1.0, the API may change between minors — pin your version and watch the changelog.

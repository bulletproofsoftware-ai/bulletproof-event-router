# Security Policy

This document describes how to report security vulnerabilities in the **event-router** project and the response commitments of the maintainers.

## Supported Versions

| Version Range | Supported |
|---------------|-----------|
| `0.1.x` (initial release line) | Yes — receives security fixes |
| Any pre-release / branch builds | No — use only for testing |

When a new minor or major release ships, the previous minor remains supported for 90 days for security fixes only.

## Reporting a Vulnerability

**Please do NOT open a public GitHub issue for security vulnerabilities.** Public disclosure before a fix is available puts users at risk.

Send vulnerability reports to the security contact for the organization operating this deployment (`security@<your-domain>`), or open a GitHub private security advisory on this repository.

Include the following in your report:

1. **Affected component** — e.g. the dispatch API in `app/main.py`, the workflow-health endpoint, the DLQ retry loop, or a script under `runtime/`
2. **Vulnerability class** — e.g. SSRF, auth bypass, injection, information disclosure, denial of service
3. **Impact** — what an adversary can achieve
4. **Reproduction steps** — a minimal proof of concept
5. **Affected version(s)** — git SHA or release tag
6. **Suggested mitigation** (optional)
7. **Your contact details** — you may report anonymously, but we cannot then acknowledge or credit the report

### Response Targets

| Stage | Target |
|-------|--------|
| Acknowledge receipt | 3 business days |
| Initial severity assessment | 7 business days |
| Fix or documented mitigation for High/Critical | 30 days |
| Public advisory after fix ships | 7 days |

We ask that you allow 90 days before public disclosure, or until a fix ships, whichever comes first.

## Security Model

Understanding what this service does and does not defend against.

### Trust boundaries

- **Event submission (`POST /events`)** — the router accepts events and dispatches them to configured handlers. Treat the submission endpoint as **trusted-network only**. It performs no authentication of its own; put it behind your own authentication layer or bind it to a private interface. Do not expose it to the public internet.
- **Workflow health (`POST /workflows/{name}/health`)** — authenticated with an HMAC signature derived from `WORKFLOW_HEALTH_HMAC_SECRET`. If that variable is blank the endpoint is disabled entirely and returns HTTP 503 (it fails closed rather than accepting unauthenticated writes). Set the secret to enable health reporting.
- **Outbound webhooks** — routing rules dispatch to URLs you configure. The router will request whatever URL a rule names, so routing rules are a privileged configuration surface: anyone who can edit `routing-rules.yaml` can make the router issue requests from inside your network. Restrict write access to the config directory accordingly.

### Secrets handling

- Secrets (`N8N_API_KEY`, `WORKFLOW_HEALTH_HMAC_SECRET`, any downstream DSN) are read from environment variables. Do not commit populated `.env` files; `.env` is gitignored and only `.env.example` is tracked.
- `runtime/launch-bridge.sh` reads the n8n API key from the running container at start time specifically so the secret does not need to be written into scheduler configuration.
- Downstream mirrors (data-plane, economics, runtime-security, metrics) are **disabled by default**. Each is enabled only by setting its DSN or URL explicitly.

### Data at rest

- The dead-letter queue (`DLQ_PATH`, SQLite) stores full event payloads for `DLQ_RETENTION_DAYS` (default 30). If your events carry sensitive data, that file inherits the same sensitivity — protect it with filesystem permissions and include it in your retention and deletion policy.

### Not in scope

- The router does not sanitize event payload contents. Handlers that consume the payload are responsible for their own input validation.
- The router does not provide multi-tenancy or per-caller authorization.

## Security Practices in This Repository

- Dependencies are version-pinned in `requirements.txt`.
- CI compiles every Python file and runs the test suite on each push. Note that the test step is currently advisory: it reports failures as a warning rather than failing the build.
- GitHub Actions are pinned to full commit SHAs.
- No credentials, private hostnames, or environment-specific endpoints are committed; configuration ships as `.example` files with placeholder values.

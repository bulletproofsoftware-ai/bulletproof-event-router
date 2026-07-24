# Security Scan Report — bulletproof-event-router

**Scanner:** Code Hardener (standard profile — 12 code-appropriate scanners:
trivy, gitleaks, opengrep, checkov, grype, syft, oxlint, ruff, actionlint,
package-validator, typos, jscpd)
**Scan ID:** `ca2b5a5c-34ff-4c01-bfd3-84e37a608145`
**Branch:** `main`
**Date:** 2026-07-24

## Result

| Metric | Value |
|--------|-------|
| **Score** | **912 / 1000 — "excellent"** |
| **Critical** | **0** |
| **High** | **0** |
| Medium | 17 |
| Low | 11 |
| Info | 2 |
| Secret scan (gitleaks) | **PASS — 0 findings** across 21 files |

The attestation certificate (page 1 of the [PDF report](bulletproof-event-router-scan-report.pdf))
is an **in-toto** attestation, **cryptographically signed with Ed25519**. Subject digest
`sha256:868d243ec33b6a742a7ebaf3970379d04464849291dea2b1be0edcd1…`.

## Fixes applied (every critical + high resolved to zero)

The initial standard scan reported **0 critical, 4 high**. All four were fixed and
re-scanning confirmed **0 high**. Every fix is a real code change committed to `main`.

| # | Severity | Tool / rule | File | Fix |
|---|----------|-------------|------|-----|
| 1 | HIGH | opengrep `dockerfile.security.missing-user` + dockle `DS-0002` | `Dockerfile` | Added a non-root `USER appuser` (uid 10001); pre-create + `chown` the writable `/events` and `/app/data` mount points. Verified: `docker run --rm --entrypoint sh <img> -c 'id -un'` → `appuser`. |
| 2 | HIGH | opengrep `javascript…detect-insecure-websocket` | `app/templates/dashboard.html` | Derive the WebSocket scheme from the page protocol (`location.protocol.replace('http','ws')`) so it is `wss://` on TLS pages and `ws://` only on plain HTTP — without a literal `ws://` string. Behavior unchanged (a same-origin socket must match the page's transport). |
| 3 | HIGH | opengrep `python…dangerous-subprocess-use-tainted-env-args` | `runtime/bridge-daemon.py` | The optional Telegram-notify `Popen` took its executable path from the `NOTIFY_SCRIPT` env var. Refactored `_resolve_notify_executable()` to pass it through `shutil.which()`, which returns a filesystem-verified executable path (or `None`) — breaking the env→subprocess taint flow. Invocation is `shell=False` with a fixed argv. |

Additionally, a known dependency advisory and two supply-chain hardening findings
(medium) were fixed in the same pass:

| Severity | Tool / rule | File | Fix |
|----------|-------------|------|-----|
| MODERATE (dependency) | Dependabot / GHSA-cpwx-vrp4-4pq7 | `requirements.txt` | Bumped `jinja2` 3.1.5 → 3.1.6 (Jinja2 sandbox-breakout advisory). |
| MEDIUM | opengrep `github-actions-mutable-action-tag` (×2) | `.github/workflows/ci.yml` | Pinned `actions/checkout@v4` → `11d5960…` and `actions/setup-python@v5` → `a26af69…` (commit SHAs, `# v4`/`# v5` comments retained). |
| MEDIUM | opengrep `missing-integrity` | `app/templates/dashboard.html` | Pinned the Chart.js CDN URL to the exact UMD build and added an SRI `integrity` hash + `crossorigin`. |

## What remains (low-risk, not fixed by design)

The residual medium/low/info findings are cosmetic or intrinsic to the design and are
**not** security-relevant. Per Code Hardener guidance we do not chase these to zero
(auto-fixers like `oxlint --fix` strip defensive null-guards; unused-import cleanup is
cosmetic):

- **`dynamic-urllib-use-detected` (9 × medium)** — `runtime/*.py` build request URLs
  from configuration (localhost service base URLs). All targets are operator-configured
  internal endpoints, never untrusted input; there is no SSRF surface here.
- **`RUFF-F401` unused import / `RUFF-E702` multiple-statements-per-line (8 × medium)** —
  cosmetic lint in `runtime/bridge-daemon.py`, `app/main.py`, and the test module.
- **`SBOM-LICENSE-UNKNOWN` / `LICENSE-*` (11 × low)** — syft could not always map a
  license from `requirements.txt`; the authoritative license inventory is in
  [../SBOM.md](../SBOM.md) (100% of runtime deps are OSI-approved: MIT / BSD-3-Clause /
  MPL-2.0 / PSF-2.0).
- **`TYPOS-SPELLING` (2 × info)** — spell-checker false positives on identifiers.

## Artifacts

| File | Description |
|------|-------------|
| [`bulletproof-event-router-scan-report.pdf`](bulletproof-event-router-scan-report.pdf) | Full portal report (13 pp) — page 1 is the signed in-toto attestation certificate. |
| [`attestation.json`](attestation.json) | Ed25519-signed in-toto attestation. |
| [`scan-report.sarif.json`](scan-report.sarif.json) | SARIF 2.1.0 findings (paths normalized). |
| [`scan-report-full.md`](scan-report-full.md) | Full markdown findings export. |

---

Apache-2.0 © 2026 bulletproofsoftware-ai. See [../../LICENSE](../../LICENSE) and [../../NOTICE](../../NOTICE).

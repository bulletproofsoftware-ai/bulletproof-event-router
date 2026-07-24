# Software Bill of Materials — bulletproof-event-router

A machine-readable CycloneDX 1.5 SBOM ships alongside this document:
[`bulletproof-event-router.cyclonedx.json`](bulletproof-event-router.cyclonedx.json).

The component set was resolved from the pinned [`requirements.txt`](../requirements.txt)
for the container's target platform (`python:3.12-slim`, linux/x86-64), so it reflects
exactly what a Docker build installs. Licenses were taken from authoritative PyPI
metadata per (name, version).

## Summary

| | Count |
|---|---|
| **Direct dependencies** | 6 |
| **Transitive dependencies** | 16 |
| **Total components** | **22** |

### License distribution

| License | Components |
|---------|-----------|
| MIT | 10 |
| BSD-3-Clause | 10 |
| MPL-2.0 | 1 |
| PSF-2.0 | 1 |

**100% of runtime dependencies are OSI-approved.** No copyleft-of-concern (GPL/AGPL)
licenses are present. MPL-2.0 (`certifi`) is file-level copyleft, satisfied by
distributing the certificate bundle unmodified.

## Direct dependencies

These are the six lines in `requirements.txt`.

| Package | Version | License |
|---------|---------|---------|
| `fastapi` | 0.115.6 | MIT |
| `httpx` | 0.28.1 | BSD-3-Clause |
| `jinja2` | 3.1.6 | BSD-3-Clause |
| `pydantic` | 2.10.4 | MIT |
| `PyYAML` | 6.0.2 | MIT |
| `uvicorn` | 0.34.0 (with `[standard]` extra) | BSD-3-Clause |

> `jinja2` is pinned to **3.1.6** (not the original 3.1.5) to close
> GHSA-cpwx-vrp4-4pq7 (Jinja2 sandbox-breakout advisory). See
> [scan/scan-report.md](scan/scan-report.md).

## Transitive dependencies

Pulled in by the six direct deps (the `uvicorn[standard]` extra adds `uvloop`,
`httptools`, `watchfiles`, `websockets`, `python-dotenv`; `fastapi` adds `starlette`;
`pydantic` adds `pydantic-core` + `annotated-types` + `typing-extensions`; `httpx`
adds `httpcore`/`h11`/`certifi`/`idna`/`anyio`/`sniffio`).

| Package | Version | License |
|---------|---------|---------|
| `annotated-types` | 0.8.0 | MIT |
| `anyio` | 4.14.2 | MIT |
| `certifi` | 2026.7.22 | MPL-2.0 |
| `click` | 8.4.2 | BSD-3-Clause |
| `h11` | 0.16.0 | MIT |
| `httpcore` | 1.0.9 | BSD-3-Clause |
| `httptools` | 0.8.0 | MIT |
| `idna` | 3.18 | BSD-3-Clause |
| `markupsafe` | 3.0.3 | BSD-3-Clause |
| `pydantic-core` | 2.27.2 | MIT |
| `python-dotenv` | 1.2.2 | BSD-3-Clause |
| `starlette` | 0.41.3 | BSD-3-Clause |
| `typing-extensions` | 4.16.0 | PSF-2.0 |
| `uvloop` | 0.22.1 | MIT |
| `watchfiles` | 1.2.0 | MIT |
| `websockets` | 16.1.1 | BSD-3-Clause |

> Transitive versions are the newest releases compatible with the direct pins at
> resolution time; run `pip download -r requirements.txt` for the exact set on your
> platform. The CycloneDX JSON records the resolved versions above.

## Optional (not in `requirements.txt`)

`runtime/bridge-daemon.py` imports **`psycopg2`** (PostgreSQL client) for its optional
data-plane lineage mirror. It is intentionally **excluded from `requirements.txt`**:
the core event-router service never touches Postgres, and the bridge mirror is off
unless `DATA_PLANE_DSN` is set. Install it separately only if you enable that mirror.

## Base image

- **`python:3.12-slim`** — digest observed at build time:
  `python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de`
- The container adds a non-root `appuser` (uid 10001) and runs as that user.
- No system packages are installed beyond the Debian slim base and the pip wheels above.

---

Apache-2.0 © 2026 bulletproofsoftware-ai. See [LICENSE](../LICENSE) and [NOTICE](../NOTICE).

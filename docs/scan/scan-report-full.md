# Security Scan Report: bulletproof-event-router

**Scan ID:** `ca2b5a5c-34ff-4c01-bfd3-84e37a608145`
**Date:** 2026-07-24T19:58:07.307Z
**Score:** 1000/1000 (excellent)
**Branch:** main | **Commit:** `N/A`
**Profile:** standard

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 0 |
| Medium | 17 |
| Low | 11 |
| Info | 2 |
| **Total (open)** | **30** |

> **Note:** The counts above reflect _open_ findings only.
> 1 scanner(s) were skipped — see "Skipped Scanners" below.

## Scanners Executed

| Scanner | Status | Findings | Duration | Notes |
|---------|--------|----------|----------|-------|
| trivy | pass | 3 | 2.3s |  |
| gitleaks | pass | 0 | 0.5s |  |
| opengrep | pass | 9 | 6.3s |  |
| checkov | pass | 0 | 3.3s |  |
| grype | pass | 0 | 3.1s |  |
| syft | pass | 8 | 1.4s |  |
| package-validator | pass | 0 | 0.1s |  |
| oxlint | skipped | 0 | 0.0s | _skipped: no_matching_files_ |
| ruff | pass | 8 | 0.0s |  |
| actionlint | pass | 0 | 0.0s |  |
| jscpd | pass | 0 | 0.0s |  |
| typos | pass | 2 | 0.0s |  |
| _file_inventory | pass | 0 | 0.0s |  |

## Medium Findings (17)

### [MEDIUM] Multiple statements on one line (semicolon)

- **File:** `tests/test_delayed_dispatch.py:73`
- **Scanner:** ruff
- **Rule:** `RUFF-E702`

**What's wrong:** Multiple statements on one line (semicolon)

**How to fix:** See: https://docs.astral.sh/ruff/rules/multiple-statements-on-one-line-semicolon

**Action:** Plan to fix this issue in your next sprint or release.

---

### [MEDIUM] Multiple statements on one line (semicolon)

- **File:** `tests/test_delayed_dispatch.py:72`
- **Scanner:** ruff
- **Rule:** `RUFF-E702`

**What's wrong:** Multiple statements on one line (semicolon)

**How to fix:** See: https://docs.astral.sh/ruff/rules/multiple-statements-on-one-line-semicolon

**Action:** Plan to fix this issue in your next sprint or release.

---

### [MEDIUM] \`sqlite3\` imported but unused

- **File:** `tests/test_delayed_dispatch.py:6`
- **Scanner:** ruff
- **Rule:** `RUFF-F401`

**What's wrong:** `sqlite3` imported but unused

**How to fix:** Auto-fix available: Remove unused import: `sqlite3` (applicability: safe)

**Action:** Plan to fix this issue in your next sprint or release.

---

### [MEDIUM] \`os\` imported but unused

- **File:** `tests/test_delayed_dispatch.py:5`
- **Scanner:** ruff
- **Rule:** `RUFF-F401`

**What's wrong:** `os` imported but unused

**How to fix:** Auto-fix available: Remove unused import: `os` (applicability: safe)

**Action:** Plan to fix this issue in your next sprint or release.

---

### [MEDIUM] \`contextlib.closing\` imported but unused

- **File:** `runtime/bridge-daemon.py:30`
- **Scanner:** ruff
- **Rule:** `RUFF-F401`

**What's wrong:** `contextlib.closing` imported but unused

**How to fix:** Auto-fix available: Remove unused import: `contextlib.closing` (applicability: safe)

**Action:** Plan to fix this issue in your next sprint or release.

---

### [MEDIUM] \`uuid\` imported but unused

- **File:** `runtime/bridge-daemon.py:29`
- **Scanner:** ruff
- **Rule:** `RUFF-F401`

**What's wrong:** `uuid` imported but unused

**How to fix:** Auto-fix available: Remove unused import: `uuid` (applicability: safe)

**Action:** Plan to fix this issue in your next sprint or release.

---

### [MEDIUM] \`sys\` imported but unused

- **File:** `runtime/bridge-daemon.py:24`
- **Scanner:** ruff
- **Rule:** `RUFF-F401`

**What's wrong:** `sys` imported but unused

**How to fix:** Auto-fix available: Remove unused import: `sys` (applicability: safe)

**Action:** Plan to fix this issue in your next sprint or release.

---

### [MEDIUM] \`fastapi.responses.PlainTextResponse\` imported but unused

- **File:** `app/main.py:36`
- **Scanner:** ruff
- **Rule:** `RUFF-F401`

**What's wrong:** `fastapi.responses.PlainTextResponse` imported but unused

**How to fix:** Auto-fix available: Remove unused import: `fastapi.responses.PlainTextResponse` (applicability: safe)

**Action:** Plan to fix this issue in your next sprint or release.

---

### [MEDIUM] Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

- **File:** `runtime/n8n-health-poller.py:209`
- **Scanner:** opengrep
- **Rule:** `python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected`
- **CWE:** [CWE-939: Improper Authorization in Handler for Custom URL Scheme](https://cwe.mitre.org/data/definitions/939.html)
- **OWASP:** A

**What's wrong:** Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

**Code:**
```python
requires login
```

**How to fix:** Review this finding and apply the appropriate fix based on the description: Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib call

**Action:** Plan to fix this issue in your next sprint or release.

---

### [MEDIUM] Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

- **File:** `runtime/n8n-health-poller.py:117`
- **Scanner:** opengrep
- **Rule:** `python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected`
- **CWE:** [CWE-939: Improper Authorization in Handler for Custom URL Scheme](https://cwe.mitre.org/data/definitions/939.html)
- **OWASP:** A

**What's wrong:** Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

**Code:**
```python
requires login
```

**How to fix:** Review this finding and apply the appropriate fix based on the description: Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib call

**Action:** Plan to fix this issue in your next sprint or release.

---

### [MEDIUM] Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

- **File:** `runtime/n8n-health-poller.py:101`
- **Scanner:** opengrep
- **Rule:** `python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected`
- **CWE:** [CWE-939: Improper Authorization in Handler for Custom URL Scheme](https://cwe.mitre.org/data/definitions/939.html)
- **OWASP:** A

**What's wrong:** Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

**Code:**
```python
requires login
```

**How to fix:** Review this finding and apply the appropriate fix based on the description: Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib call

**Action:** Plan to fix this issue in your next sprint or release.

---

### [MEDIUM] Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

- **File:** `runtime/hook-dispatch.py:98`
- **Scanner:** opengrep
- **Rule:** `python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected`
- **CWE:** [CWE-939: Improper Authorization in Handler for Custom URL Scheme](https://cwe.mitre.org/data/definitions/939.html)
- **OWASP:** A

**What's wrong:** Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

**Code:**
```python
requires login
```

**How to fix:** Review this finding and apply the appropriate fix based on the description: Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib call

**Action:** Plan to fix this issue in your next sprint or release.

---

### [MEDIUM] Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

- **File:** `runtime/hook-dispatch.py:74`
- **Scanner:** opengrep
- **Rule:** `python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected`
- **CWE:** [CWE-939: Improper Authorization in Handler for Custom URL Scheme](https://cwe.mitre.org/data/definitions/939.html)
- **OWASP:** A

**What's wrong:** Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

**Code:**
```python
requires login
```

**How to fix:** Review this finding and apply the appropriate fix based on the description: Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib call

**Action:** Plan to fix this issue in your next sprint or release.

---

### [MEDIUM] Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

- **File:** `runtime/bridge-daemon.py:691`
- **Scanner:** opengrep
- **Rule:** `python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected`
- **CWE:** [CWE-939: Improper Authorization in Handler for Custom URL Scheme](https://cwe.mitre.org/data/definitions/939.html)
- **OWASP:** A

**What's wrong:** Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

**Code:**
```python
requires login
```

**How to fix:** Review this finding and apply the appropriate fix based on the description: Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib call

**Action:** Plan to fix this issue in your next sprint or release.

---

### [MEDIUM] Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

- **File:** `runtime/bridge-daemon.py:547`
- **Scanner:** opengrep
- **Rule:** `python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected`
- **CWE:** [CWE-939: Improper Authorization in Handler for Custom URL Scheme](https://cwe.mitre.org/data/definitions/939.html)
- **OWASP:** A

**What's wrong:** Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

**Code:**
```python
requires login
```

**How to fix:** Review this finding and apply the appropriate fix based on the description: Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib call

**Action:** Plan to fix this issue in your next sprint or release.

---

### [MEDIUM] Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

- **File:** `runtime/bridge-daemon.py:361`
- **Scanner:** opengrep
- **Rule:** `python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected`
- **CWE:** [CWE-939: Improper Authorization in Handler for Custom URL Scheme](https://cwe.mitre.org/data/definitions/939.html)
- **OWASP:** A

**What's wrong:** Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

**Code:**
```python
requires login
```

**How to fix:** Review this finding and apply the appropriate fix based on the description: Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib call

**Action:** Plan to fix this issue in your next sprint or release.

---

### [MEDIUM] Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

- **File:** `runtime/bridge-daemon.py:134`
- **Scanner:** opengrep
- **Rule:** `python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected`
- **CWE:** [CWE-939: Improper Authorization in Handler for Custom URL Scheme](https://cwe.mitre.org/data/definitions/939.html)
- **OWASP:** A

**What's wrong:** Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib calls to ensure user data cannot control the URLs, or consider using the 'requests' library instead.

**Code:**
```python
requires login
```

**How to fix:** Review this finding and apply the appropriate fix based on the description: Detected a dynamic value being used with urllib. urllib supports 'file://' schemes, so a dynamic value controlled by a malicious actor may allow them to read arbitrary files. Audit uses of urllib call

**Action:** Plan to fix this issue in your next sprint or release.

---

## Low Findings (11)

- **SBOM-LICENSE-UNKNOWN**: Unknown License: uvicorn@0.34.0 (`/requirements.txt`)
- **SBOM-LICENSE-UNKNOWN**: Unknown License: pyyaml@6.0.2 (`/requirements.txt`)
- **SBOM-LICENSE-UNKNOWN**: Unknown License: pydantic@2.10.4 (`/requirements.txt`)
- **SBOM-LICENSE-UNKNOWN**: Unknown License: jinja2@3.1.6 (`/requirements.txt`)
- **SBOM-LICENSE-UNKNOWN**: Unknown License: httpx@0.28.1 (`/requirements.txt`)
- **SBOM-LICENSE-UNKNOWN**: Unknown License: fastapi@0.115.6 (`/requirements.txt`)
- **SBOM-LICENSE-UNKNOWN**: Unknown License: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 (`/.github/workflows/ci.yml`)
- **SBOM-LICENSE-UNKNOWN**: Unknown License: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 (`/.github/workflows/ci.yml`)
- **LICENSE-Apache-2.0**: License Compliance: Apache-2.0 in  (`LICENSE`)
- **LICENSE-BSD-3-Clause**: License Compliance: BSD-3-Clause in jinja2 (`requirements.txt`)
- **LICENSE-BSD-3-Clause**: License Compliance: BSD-3-Clause in httpx (`requirements.txt`)

## Skipped Scanners (1)

Scanners that did not run on this scan, with the reason why and how to enable them.

| Scanner | Reason | How to enable |
|---------|--------|---------------|
| `oxlint` | no_matching_files | No .js/.ts files found — Oxlint requires a JavaScript/TypeScript project |

## Recommendations

1. Update 3 vulnerable dependency/dependencies -- run `npm audit fix` or equivalent

---
*Generated by Code Hardener v0.1.0 | 2026-07-24T19:58:40.026Z*
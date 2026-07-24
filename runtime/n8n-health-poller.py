#!/usr/bin/env python3
"""n8n-health-poller — external health observer for registered n8n workflows.

Reads n8n's execution history via its REST API (which records every execution
with final status regardless of which branch the workflow took), aggregates
per-workflow health over a 24h window, and pushes each result to Event Router's
HMAC-authenticated POST /workflows/{name}/health endpoint.

This is the "observe from outside" architecture — no workflow graph is ever
modified.

Intended to be run on a schedule (cron, systemd timer, launchd, or any job
runner). Exit codes:
  0  every registry workflow polled AND pushed, OR n8n unreachable and skipped
  1  partial failure (n8n or event-router errors on >=1 workflow)
  2  configuration error (missing env file, secrets, or registry)

If your n8n instance is reached over a VPN, an SSH tunnel, or any other link
that can be down independently of n8n itself, a failed poll at the TCP layer is
indistinguishable from n8n being down. To avoid false "down" alerts, main() runs
a cheap reachability probe first; if n8n cannot be reached it exits 0 with a
"skipped" note instead of reporting a failure. Pass --no-skip-unreachable to
treat an unreachable n8n as a hard failure (exit 1) instead.

Configuration (all optional, environment variables):
  POLLER_ENV_FILE         path to a KEY=VALUE file holding the secrets below
  POLLER_REGISTRY         path to workflow-registry.yaml
  POLLER_N8N_BASE_URL     n8n base URL           (default http://localhost:5678)
  POLLER_EVENT_ROUTER_URL event-router base URL  (default http://127.0.0.1:8085)

Usage:
  n8n-health-poller.py [--dry-run] [--workflow KEY] [--no-skip-unreachable]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

_ENV_FILE_SETTING = os.environ.get("POLLER_ENV_FILE", "")
ENV_FILE = Path(_ENV_FILE_SETTING) if _ENV_FILE_SETTING else None
REGISTRY_PATH = Path(os.environ.get("POLLER_REGISTRY", "./config/workflow-registry.yaml"))
N8N_BASE_URL = os.environ.get("POLLER_N8N_BASE_URL", "http://localhost:5678")
EVENT_ROUTER_URL = os.environ.get("POLLER_EVENT_ROUTER_URL", "http://127.0.0.1:8085")
HTTP_TIMEOUT_S = 10
REACHABILITY_TIMEOUT_S = 4  # short probe so an unreachable-n8n run fails fast, not after 10s * N workflows
WINDOW_HOURS = 24
PAGE_LIMIT = 100
MAX_PAGES = 5

# n8n execution status -> event-router last_status enum (success|failure|running|unknown)
STATUS_MAP = {
    "success": "success",
    "error": "failure",
    "crashed": "failure",
    "canceled": "failure",
    "running": "running",
    "waiting": "running",
    "new": "running",
}


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE lines. Never shell-sources; ignores comments/blanks."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        out[key.strip()] = value
    return out


def load_config_env(env_file: Path | None) -> dict[str, str]:
    """Read secrets from the process environment, optionally topped up from a file.

    POLLER_ENV_FILE is unset by default: the normal path is to export
    N8N_API_KEY and WORKFLOW_HEALTH_HMAC_SECRET into the environment. Setting
    POLLER_ENV_FILE to a KEY=VALUE file is supported for schedulers that cannot
    easily inject environment variables. Values already present in the process
    environment win over the file.
    """
    values: dict[str, str] = {}
    if env_file is not None:
        values.update(parse_env_file(env_file))
    for key in ("N8N_API_KEY", "WORKFLOW_HEALTH_HMAC_SECRET"):
        from_env = os.environ.get(key)
        if from_env:
            values[key] = from_env
    return values


def load_registry(path: Path) -> dict[str, dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    workflows = data.get("workflows") or {}
    if not workflows:
        raise ValueError(f"no workflows found in registry {path}")
    return workflows


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def http_get_json(url: str, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_reachable(api_key: str) -> tuple[bool, str]:
    """Cheap probe: can we reach n8n's API at all?

    Distinguishes "n8n is down / the network path to it is dead" (TCP-level
    failure — URLError/OSError/timeout) from "n8n answered". A non-2xx HTTP reply
    still counts as reachable: the tunnel and process are alive even if the
    endpoint is unauthorized or not found, which is a real-workflow problem worth
    polling for, not a connectivity skip.
    """
    url = f"{N8N_BASE_URL}/api/v1/executions?{urllib.parse.urlencode({'limit': '1'})}"
    req = urllib.request.Request(url, headers={"X-N8N-API-KEY": api_key})
    try:
        with urllib.request.urlopen(req, timeout=REACHABILITY_TIMEOUT_S):
            return True, "reachable"
    except urllib.error.HTTPError as exc:
        return True, f"reachable (HTTP {exc.code})"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"unreachable: {exc}"


def fetch_executions(n8n_id: str, api_key: str, cutoff: datetime) -> tuple[list[dict], bool]:
    """Fetch executions for one workflow, newest-first, until older than cutoff.

    Returns (executions, truncated). `truncated` is True when MAX_PAGES was hit
    while still inside the window — 24h counts are then lower bounds.
    """
    executions: list[dict] = []
    cursor: str | None = None
    headers = {"X-N8N-API-KEY": api_key}
    for _ in range(MAX_PAGES):
        params = {"workflowId": n8n_id, "limit": str(PAGE_LIMIT)}
        if cursor:
            params["cursor"] = cursor
        url = f"{N8N_BASE_URL}/api/v1/executions?{urllib.parse.urlencode(params)}"
        payload = http_get_json(url, headers)
        page = payload.get("data") or []
        executions.extend(page)
        cursor = payload.get("nextCursor")
        if not cursor or not page:
            return executions, False
        oldest = parse_ts(page[-1].get("startedAt"))
        if oldest is not None and oldest < cutoff:
            return executions, False
    return executions, True


def aggregate(executions: list[dict], sla_minutes: float | None, now: datetime) -> dict:
    """Reduce a newest-first execution list to the event-router health payload."""
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    payload = {
        "last_status": "unknown",
        "last_run_timestamp": None,
        "success_count_24h": 0,
        "failure_count_24h": 0,
        "avg_latency_ms": 0.0,
        "sla_breaches_24h": 0,
    }
    if not executions:
        return payload

    latest = executions[0]
    payload["last_status"] = STATUS_MAP.get(latest.get("status", ""), "unknown")
    started = parse_ts(latest.get("startedAt"))
    payload["last_run_timestamp"] = started.isoformat() if started else None

    latencies: list[float] = []
    for ex in executions:
        start = parse_ts(ex.get("startedAt"))
        if start is None or start < cutoff:
            continue
        mapped = STATUS_MAP.get(ex.get("status", ""), "unknown")
        if mapped == "success":
            payload["success_count_24h"] += 1
        elif mapped == "failure":
            payload["failure_count_24h"] += 1
        stop = parse_ts(ex.get("stoppedAt"))
        if stop is not None:
            duration_ms = (stop - start).total_seconds() * 1000.0
            if duration_ms >= 0:
                latencies.append(duration_ms)
                if sla_minutes is not None and duration_ms > sla_minutes * 60_000:
                    payload["sla_breaches_24h"] += 1
    if latencies:
        payload["avg_latency_ms"] = round(sum(latencies) / len(latencies), 1)
    return payload


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def push_health(registry_key: str, payload: dict, secret: str) -> tuple[bool, str]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    url = f"{EVENT_ROUTER_URL}/workflows/{urllib.parse.quote(registry_key)}/health"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Workflow-Health-Signature": f"sha256={sign(secret, body)}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        return False, f"HTTP {exc.code}: {detail}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"network: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="poll and print payloads without pushing")
    parser.add_argument("--workflow", help="poll only this registry key")
    parser.add_argument(
        "--no-skip-unreachable",
        action="store_true",
        help="treat an unreachable n8n as a hard failure (exit 1) instead of skipping (exit 0)",
    )
    args = parser.parse_args(argv)

    try:
        env = load_config_env(ENV_FILE)
        api_key = env["N8N_API_KEY"]
        secret = env["WORKFLOW_HEALTH_HMAC_SECRET"]
        registry = load_registry(REGISTRY_PATH)
    except (OSError, KeyError, ValueError, yaml.YAMLError) as exc:
        print(f"config error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    reachable, detail = check_reachable(api_key)
    if not reachable:
        if args.no_skip_unreachable:
            print(f"n8n unreachable at {N8N_BASE_URL} — {detail}", file=sys.stderr)
            return 1
        print(f"skipped: n8n unreachable at {N8N_BASE_URL} — {detail}")
        return 0

    if args.workflow:
        if args.workflow not in registry:
            print(f"config error: '{args.workflow}' not in registry", file=sys.stderr)
            return 2
        registry = {args.workflow: registry[args.workflow]}

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    pushed = poll_failed = push_failed = 0

    for key, meta in registry.items():
        n8n_id = meta.get("id") or key
        try:
            executions, truncated = fetch_executions(n8n_id, api_key, cutoff)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            poll_failed += 1
            print(f"poll failed: {key} ({n8n_id}): {exc}", file=sys.stderr)
            continue
        payload = aggregate(executions, meta.get("sla_minutes"), now)
        if truncated:
            print(f"note: {key}: 24h window truncated at {MAX_PAGES * PAGE_LIMIT} executions — counts are lower bounds")
        if args.dry_run:
            print(f"{key}: {json.dumps(payload)}")
            pushed += 1
            continue
        ok, detail = push_health(key, payload, secret)
        if ok:
            pushed += 1
        else:
            push_failed += 1
            print(f"push failed: {key}: {detail}", file=sys.stderr)

    total = len(registry)
    print(f"polled={total} pushed={pushed} push_failed={push_failed} poll_failed={poll_failed}")
    return 0 if pushed == total else 1


if __name__ == "__main__":
    sys.exit(main())

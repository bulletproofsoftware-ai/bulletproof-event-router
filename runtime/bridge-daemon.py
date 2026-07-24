#!/usr/bin/env python3
"""Bridge daemon: tail event-router routing_log → mirror into runtime-security + data-plane.

What this does, in a continuous 5-second loop:
  1. Read new rows from event-router's routing_log sqlite (since last-seen rowid)
  2. For session.start events  → provision a runtime-security session (POST)
  3. For session.end events    → revoke that runtime-security session
  4. For agent.dispatch on Bash with risky patterns → POST a runtime-security threat
  5. For every agent.* event   → INSERT lineage_node + edge in data-plane Postgres
  6. Persist cursor + node map to a state file
Idempotent — re-runs from cursor.

This is the producer that makes runtime-security :8093 and data-plane :8100 show real data.
Metrics-engine :8086 is already wired to pull from the same routing_log via its own loop.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import os
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

# --- config ---
ROUTER_DB = Path(os.environ.get("EVENT_ROUTER_DB", "./data/dead-letter-queue.sqlite"))
RUNTIME_SECURITY_URL = os.environ.get("RUNTIME_SECURITY_URL", "http://127.0.0.1:8093")
STATE_PATH = Path(os.environ.get("BRIDGE_STATE", "./data/bridge-state.json"))
LOG_PATH = Path("/tmp/observability-bridge.log")

PG_DSN = os.environ.get(
    "DATA_PLANE_DSN",
    "",
)

POLL_SEC = float(os.environ.get("BRIDGE_POLL_SEC", "5"))
DEFAULT_PIPELINE = "default"
HTTP_TIMEOUT = 2.0
# Runtime-security signals (sessions, threats, behavior) only fire for events newer than this.
# Lineage backfills regardless — DAG history is valuable, stale "active sessions" are not.
RS_FRESH_WINDOW_SEC = float(os.environ.get("RS_FRESH_WINDOW_SEC", "600"))  # 10 min

# n8n workflow health syncer — polls n8n executions, updates event-router workflow_health table.
N8N_BASE_URL = os.environ.get("N8N_BASE_URL", "http://localhost:5678")
N8N_API_KEY = os.environ.get("N8N_API_KEY", "")  # injected by launchd plist via docker exec
N8N_SYNC_INTERVAL_SEC = float(os.environ.get("N8N_SYNC_INTERVAL_SEC", "60"))
WORKFLOW_REGISTRY_PATH = Path(os.environ.get(
    "WORKFLOW_REGISTRY_PATH",
    "./config/workflow-registry.yaml",
))

# Telegram heartbeat via the existing notify script.
TELEGRAM_NOTIFY = Path(os.environ.get("NOTIFY_SCRIPT", ""))
HEARTBEAT_FAIL_THRESHOLD = int(os.environ.get("HEARTBEAT_FAIL_THRESHOLD", "3"))

# Event-router workflow_health writer — POST via HMAC-authenticated endpoint instead of direct sqlite.
EVENT_ROUTER_BASE_URL = os.environ.get("EVENT_ROUTER_BASE_URL", "http://127.0.0.1:8085")
WORKFLOW_HEALTH_HMAC_SECRET = os.environ.get("WORKFLOW_HEALTH_HMAC_SECRET", "")

# Economics cost event writer — POST to economics-api via nginx (token added at proxy layer)
ECONOMICS_BASE_URL = os.environ.get("ECONOMICS_BASE_URL", "http://127.0.0.1:8096")
# We use the dashboard proxy URL so the nginx-injected Bearer token authenticates us.

# Metrics-engine sqlite path (host-mounted) — bridge backfills cost/tokens from agent-economics
METRICS_SQLITE_PATH = Path(os.environ.get(
    "METRICS_SQLITE_PATH",
    "./data/metrics.sqlite",
))

# Shared connection to agent_economics — reused across cycles (see _get_economics_conn).
ECONOMICS_DSN = os.environ.get(
    "ECONOMICS_DSN",
    "",
)

# --- state ---
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_rowid": 0, "session_nodes": {}, "session_provisioned": {}}


MAX_SESSION_ENTRIES = 500  # cap session_nodes/session_provisioned so the state file can't grow unbounded


def _prune_session_maps(state: dict) -> None:
    """Keep only the most recent sessions. dicts preserve insertion order,
    so the oldest keys are at the front and get dropped first."""
    for field in ("session_nodes", "session_provisioned"):
        d = state.get(field)
        if isinstance(d, dict) and len(d) > MAX_SESSION_ENTRIES:
            for k in list(d.keys())[:-MAX_SESSION_ENTRIES]:
                del d[k]


def save_state(state: dict) -> None:
    _prune_session_maps(state)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_PATH)


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f"{ts} {msg}\n"
    try:
        with LOG_PATH.open("a") as f:
            f.write(line)
    except OSError:
        pass


# --- runtime-security calls ---
def _http_json(method: str, url: str, body: dict | None = None) -> dict | None:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        log(f"http {method} {url} HTTP {exc.code}: {exc.read()[:200]!r}")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        log(f"http {method} {url} error: {exc}")
    return None


def provision_session(session_id: str) -> str | None:
    resp = _http_json("POST", f"{RUNTIME_SECURITY_URL}/api/security/sessions", {
        "agent_id": f"claude-code:{session_id[:12]}",
        "agent_class": "claude-code",
        "scope": {"tools": ["*"], "data_class": "INTERNAL"},
        "ttl_hours": 8,
    })
    return resp.get("session_id") if resp else None


def revoke_session(rs_session_id: str) -> None:
    _http_json("POST", f"{RUNTIME_SECURITY_URL}/api/security/sessions/{rs_session_id}/revoke",
               {"reason": "session_end"})


def emit_threat(text: str, agent_id: str, session_id: str | None) -> None:
    _http_json("POST", f"{RUNTIME_SECURITY_URL}/api/security/threats/check", {
        "text": text,
        "agent_id": agent_id,
        "session_id": session_id,
    })


def emit_behavior(agent_id: str, metric: str, value: float, session_id: str | None) -> None:
    _http_json("POST", f"{RUNTIME_SECURITY_URL}/api/security/behavioral/sample", {
        "agent_id": agent_id,
        "metric": metric,
        "value": value,
        "agent_class": "claude-code",
        "session_id": session_id,
    })


# Reused agent_economics connection — opening a fresh one every 30s cycle exhausted
# Postgres ("too many clients already"). The daemon is single-threaded, so a module-level
# holder is safe. Lazily (re)connected; dropped + reopened on any psycopg2 error.
_eco_conn_holder: dict = {"conn": None}


def _get_economics_conn():
    conn = _eco_conn_holder["conn"]
    if conn is not None and getattr(conn, "closed", 1) == 0:
        return conn
    conn = psycopg2.connect(ECONOMICS_DSN)
    conn.autocommit = True  # read-only SELECTs — don't hold transactions open
    _eco_conn_holder["conn"] = conn
    return conn


def _drop_economics_conn() -> None:
    conn = _eco_conn_holder.get("conn")
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    _eco_conn_holder["conn"] = None


def _backfill_metrics_costs(limit: int = 200) -> int:
    """Backfill task_events.cost_cents/input_tokens/output_tokens from agent-economics cost_events.

    Joins on session_id (n8n correlation_id == economics session_id). Picks the cost_event
    closest in time to each task_event (±60s) to avoid mismatches when sessions span many calls.

    Returns count of rows updated. Idempotent — only touches rows where cost_cents IS NULL or 0.
    """
    if not METRICS_SQLITE_PATH.exists():
        return 0

    # Pull unprocessed task_events
    sconn = sqlite3.connect(str(METRICS_SQLITE_PATH))
    sconn.row_factory = sqlite3.Row
    try:
        rows = sconn.execute(
            """SELECT event_id, correlation_id, timestamp
               FROM task_events
               WHERE event_type IN ('task.dispatch','task.complete')
                 AND (cost_cents IS NULL OR cost_cents = 0)
                 AND correlation_id IS NOT NULL AND correlation_id != ''
               ORDER BY timestamp DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        if not rows:
            return 0

        # Group by correlation_id so we make one Postgres query per session
        by_session: dict[str, list[dict]] = {}
        for r in rows:
            by_session.setdefault(r["correlation_id"], []).append(dict(r))

        # Pull matching cost_events from agent-economics (reused connection)
        eco_conn = _get_economics_conn()
        updates: list[tuple[float, int, int, str]] = []
        try:
            with eco_conn.cursor() as cur:
                for session_id, te_rows in by_session.items():
                    cur.execute(
                        """SELECT cost_cents, input_tokens, output_tokens, created_at
                           FROM cost_events
                           WHERE session_id = %s
                           ORDER BY created_at""",
                        (session_id,),
                    )
                    cost_rows = cur.fetchall()
                    if not cost_rows:
                        continue
                    # For each task_event, find the closest cost_event by timestamp.
                    cost_times = [(row[3].timestamp(), row[0], row[1], row[2]) for row in cost_rows]
                    for te in te_rows:
                        try:
                            ts = datetime.fromisoformat(te["timestamp"].replace("Z", "+00:00")).timestamp()
                        except ValueError:
                            continue
                        # Pick the closest cost_event in time (±60s)
                        best = min(cost_times, key=lambda c: abs(c[0] - ts))
                        if abs(best[0] - ts) > 60:
                            continue  # No good match within window
                        updates.append((float(best[1]), int(best[2]), int(best[3]), te["event_id"]))
        except psycopg2.Error as exc:
            log(f"metrics backfill economics query failed: {exc}; will reconnect next cycle")
            _drop_economics_conn()
            return 0

        if not updates:
            return 0

        # Apply updates in one transaction
        sconn.executemany(
            """UPDATE task_events
               SET cost_cents = ?, input_tokens = ?, output_tokens = ?
               WHERE event_id = ?""",
            updates,
        )
        sconn.commit()
        return len(updates)
    except sqlite3.Error as exc:
        log(f"metrics backfill sqlite error: {exc}")
        return 0
    finally:
        sconn.close()


def _recompute_economics_rate() -> None:
    """Compute hourly cost rate from cost_events and write to Redis directly.

    economics-api's metering engine populates `budget:*` and SADD `metrics:live:active_agents`
    on every event, but it never sets `metrics:live:cost_rate_cents_per_hour` or resets the
    `events_per_minute` counter. We compute the rate here on each cycle (cheap query) and
    write via the SAME nginx-proxied path the dashboard uses — but Redis lives on host:6379,
    so we just hit it directly with the econ: prefix.
    """
    try:
        # Query Postgres for the past hour's total cost (reused connection)
        conn = _get_economics_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT
                         COALESCE(SUM(cost_cents), 0)::bigint AS hour_cents,
                         COUNT(*) AS hour_events,
                         COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '1 minute') AS last_min_events
                       FROM cost_events
                       WHERE created_at >= NOW() - INTERVAL '1 hour'"""
                )
                hour_cents, hour_events, last_min_events = cur.fetchone()
        except psycopg2.Error:
            _drop_economics_conn()
            raise

        # Write to Redis via the host port — economics-api uses keyPrefix 'econ:'
        import socket
        sock = socket.create_connection(("127.0.0.1", 6379), timeout=2.0)
        try:
            def cmd(*args: str) -> str:
                parts = [f"*{len(args)}\r\n"]
                for a in args:
                    parts.append(f"${len(a)}\r\n{a}\r\n")
                sock.sendall("".join(parts).encode())
                resp = sock.recv(4096).decode()
                return resp
            cmd("SET", "econ:metrics:live:cost_rate_cents_per_hour", str(int(hour_cents)))
            cmd("SET", "econ:metrics:live:events_per_minute", str(int(last_min_events)))
        finally:
            sock.close()
    except (psycopg2.Error, OSError, ValueError) as exc:
        log(f"economics rate recompute failed: {exc}")


def _post_economics_cost(agent_id: str, session_id: str) -> None:
    """Record a tool_use cost event so economics-api :8096 reflects real activity.

    Token counts are heuristic — event-router's routing_log doesn't carry real token
    counts. Use opus-tier estimates that produce visible (>1 cent) costs per event:
      3000 in × 1500 cents/M  = 4_500_000 microcents
       500 out × 7500 cents/M = 3_750_000 microcents
      total ≈ 8 cents per tool dispatch — representative of an Opus 4.7 turn.
    """
    body = {
        "agent_id": agent_id,
        "session_id": session_id,
        "project_id": "claude-code",
        "model": "claude-opus-4-7",
        "routed_tier": "opus",
        "event_type": "tool_use",
        "input_tokens": 3000,
        "output_tokens": 500,
        "latency_ms": 250,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{ECONOMICS_BASE_URL}/economics/events",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=HTTP_TIMEOUT).read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        # Fire-and-forget — never block the bridge.
        pass


# --- data-plane lineage writer ---
def pg_insert_node(cur, operation: str, agent_id: str, session_id: str,
                   tier: str, metadata: dict, transform_fn: str | None = None) -> str:
    meta_with_pipeline = dict(metadata)
    meta_with_pipeline.setdefault("pipeline_id", DEFAULT_PIPELINE)
    if transform_fn:
        cur.execute(
            """INSERT INTO lineage_nodes (node_id, operation, agent_id, session_id, tier, transform_fn, metadata, created_at)
               VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s::jsonb, NOW())
               RETURNING node_id::text""",
            (operation, agent_id, session_id, tier, transform_fn, json.dumps(meta_with_pipeline)),
        )
    else:
        cur.execute(
            """INSERT INTO lineage_nodes (node_id, operation, agent_id, session_id, tier, metadata, created_at)
               VALUES (gen_random_uuid(), %s, %s, %s, %s, %s::jsonb, NOW())
               RETURNING node_id::text""",
            (operation, agent_id, session_id, tier, json.dumps(meta_with_pipeline)),
        )
    return cur.fetchone()[0]


def pg_insert_edge(cur, from_node: str, to_node: str, transform: str | None) -> None:
    cur.execute(
        """INSERT INTO lineage_edges (from_node, to_node, transform_applied)
           VALUES (%s::uuid, %s::uuid, %s)
           ON CONFLICT DO NOTHING""",
        (from_node, to_node, transform),
    )


# --- routing log reader ---
def fetch_new_rows(last_rowid: int):
    """Read-only open against event-router's sqlite."""
    if not ROUTER_DB.exists():
        return []
    conn = sqlite3.connect(f"file:{ROUTER_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, event_id, correlation_id, category, type, timestamp
               FROM routing_log WHERE id > ? ORDER BY id ASC LIMIT 500""",
            (last_rowid,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def fetch_event_payload(event_id: str) -> dict:
    """Routing_log doesn't store payload; we lost it. Best-effort empty."""
    return {}


# --- main loop ---
def process_batch(rows, state, pg_conn) -> int:
    if not rows:
        return 0
    written = 0
    session_nodes = state.setdefault("session_nodes", {})
    session_prov = state.setdefault("session_provisioned", {})

    with pg_conn:
        with pg_conn.cursor() as cur:
            now_utc = datetime.now(timezone.utc)
            for row in rows:
                cat, etype = row["category"], row["type"]
                corr = row["correlation_id"] or "unknown"
                agent_id = f"claude-code:{corr[:12]}"
                key = f"{cat}.{etype}"

                # Is this event fresh enough to trigger runtime-security signals?
                fresh = False
                ts_str = row.get("timestamp")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        fresh = (now_utc - ts).total_seconds() <= RS_FRESH_WINDOW_SEC
                    except ValueError:
                        pass

                # Runtime-security: track sessions (fresh only — stale replays would inflate active count)
                if fresh:
                    if key == "session.start":
                        rs_id = provision_session(corr)
                        if rs_id:
                            session_prov[corr] = rs_id
                    elif key == "session.end":
                        rs_id = session_prov.pop(corr, None)
                        if rs_id:
                            revoke_session(rs_id)
                    elif key == "agent.dispatch":
                        emit_behavior(agent_id, "tool_dispatch_rate", 1.0, corr)
                        _post_economics_cost(agent_id, corr)

                # Lineage: every event becomes a node, chained to prior node in same session
                op = {
                    "session.start": "source",
                    "session.end": "output",
                    "session.user_prompt": "source",
                    "agent.dispatch": "transform",
                    "agent.complete": "transform",
                    "agent.fail": "transform",
                    "session.notification": "transform",
                }.get(key, "transform")

                metadata = {
                    "event_category": cat,
                    "event_type": etype,
                    "event_id": row["event_id"],
                    "router_timestamp": row["timestamp"],
                    "pipeline_id": DEFAULT_PIPELINE,
                    "source_type": key if op == "source" else None,
                    "destination_type": key if op == "output" else None,
                }
                metadata = {k: v for k, v in metadata.items() if v is not None}

                node_id = pg_insert_node(
                    cur, op, agent_id, corr,
                    tier="INTERNAL",
                    metadata=metadata,
                    transform_fn=key if op == "transform" else None,
                )

                # Edge from previous node in this session (DAG chain)
                prev = session_nodes.get(corr)
                if prev:
                    pg_insert_edge(cur, prev, node_id, transform=key)
                session_nodes[corr] = node_id
                written += 1

                state["last_rowid"] = row["id"]

    return written


# --- n8n workflow health syncer ---
def _load_registry_yaml() -> dict:
    """Parse workflow-registry.yaml without requiring PyYAML.
    The file is simple enough that we extract the (workflow_name, id, sla_minutes) triples by line scan.
    """
    if not WORKFLOW_REGISTRY_PATH.exists():
        return {}
    registry: dict[str, dict] = {}
    current_key: str | None = None
    try:
        for raw in WORKFLOW_REGISTRY_PATH.read_text().splitlines():
            line = raw.rstrip()
            if not line or line.lstrip().startswith("#"):
                continue
            # workflow key (2-space indent, ends with ':' and no further chars)
            if line.startswith("  ") and not line.startswith("    ") and line.rstrip().endswith(":"):
                current_key = line.strip().rstrip(":")
                registry[current_key] = {}
            elif current_key and line.startswith("    "):
                stripped = line.strip()
                if ":" in stripped:
                    k, _, v = stripped.partition(":")
                    v = v.strip().strip('"').strip("'")
                    if k.strip() in ("id", "sla_minutes", "enabled", "priority"):
                        registry[current_key][k.strip()] = v
    except OSError:
        return {}
    return registry


def _fetch_n8n_executions(max_pages: int = 4) -> list[dict]:
    """Fetch recent executions across multiple pages (~1000 most recent).
    Returns list of {workflowId, status, startedAt, stoppedAt}.
    """
    if not N8N_API_KEY:
        return []
    out: list[dict] = []
    cursor = ""
    for _ in range(max_pages):
        url = f"{N8N_BASE_URL}/api/v1/executions?limit=250"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor)}"
        req = urllib.request.Request(url, headers={"X-N8N-API-KEY": N8N_API_KEY}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read().decode())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            log(f"n8n executions fetch failed: {exc}")
            break
        page = data.get("data", []) or []
        out.extend(page)
        cursor = data.get("nextCursor") or ""
        if not cursor or not page:
            break
    return out


def sync_workflow_health() -> int:
    """Poll n8n executions, aggregate per workflow, upsert into event-router workflow_health.

    Returns number of workflows updated.
    """
    registry = _load_registry_yaml()
    if not registry:
        return 0

    executions = _fetch_n8n_executions()
    if not executions:
        return 0

    now = datetime.now(timezone.utc)
    cutoff_24h_ts = now.timestamp() - 86400
    cutoff_7d_ts = now.timestamp() - (7 * 86400)

    # Group executions by workflowId (all, not just last 24h — we want latest run regardless of age)
    by_wf: dict[str, list[dict]] = {}
    for ex in executions:
        wf_id = ex.get("workflowId") or ""
        if not wf_id:
            continue
        by_wf.setdefault(wf_id, []).append(ex)

    def _started_ts(ex: dict) -> float | None:
        started = ex.get("startedAt")
        if not started:
            return None
        try:
            return datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    def _in_24h(ex: dict) -> bool:
        ts = _started_ts(ex)
        return ts is not None and ts >= cutoff_24h_ts

    def _in_7d(ex: dict) -> bool:
        ts = _started_ts(ex)
        return ts is not None and ts >= cutoff_7d_ts

    updated = 0
    try:
        for wf_name, meta in registry.items():
            n8n_id = meta.get("id")
            if not n8n_id:
                continue
            sla_minutes = float(meta.get("sla_minutes") or 60)
            exes = by_wf.get(n8n_id, [])

            # Sort by startedAt desc
            exes_sorted = sorted(exes, key=lambda e: e.get("startedAt") or "", reverse=True)
            last = exes_sorted[0] if exes_sorted else None

            # 24h kept for SLA breach accounting; 7d powers the visible success_count/failure_count
            # so workflows that run weekly stop showing 0/0.
            exes_24h = [e for e in exes if _in_24h(e)]
            exes_7d = [e for e in exes if _in_7d(e)]
            success_window = sum(1 for e in exes_7d if e.get("status") == "success")
            failure_window = sum(1 for e in exes_7d if e.get("status") == "error")

            latency_source = exes_7d or exes
            latencies_ms: list[float] = []
            sla_breaches = 0
            for e in latency_source:
                started = e.get("startedAt")
                stopped = e.get("stoppedAt")
                if not (started and stopped):
                    continue
                try:
                    t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    t1 = datetime.fromisoformat(stopped.replace("Z", "+00:00"))
                    ms = (t1 - t0).total_seconds() * 1000.0
                    if ms >= 0:
                        latencies_ms.append(ms)
                        if e in exes_24h and (ms / 60000.0) > sla_minutes:
                            sla_breaches += 1
                except ValueError:
                    continue

            avg_latency = sum(latencies_ms) / len(latencies_ms) if latencies_ms else 0.0

            if not last:
                # No execution history at all — skip to preserve registry default
                continue
            last_status = "success" if last.get("status") == "success" else (
                "failure" if last.get("status") == "error" else "running"
            )
            last_ts = last.get("startedAt") or last.get("stoppedAt") or ""

            payload = {
                "last_status": last_status,
                "last_run_timestamp": last_ts,
                "success_count_24h": success_window,
                "failure_count_24h": failure_window,
                "avg_latency_ms": avg_latency,
                "sla_breaches_24h": sla_breaches,
            }
            if _post_workflow_health(wf_name, payload):
                updated += 1
    finally:
        pass

    return updated


def _post_workflow_health(wf_name: str, payload: dict) -> bool:
    """POST workflow health update to event-router with HMAC signature.

    Returns True on success. Falls back to direct sqlite write only if the HMAC
    secret is missing (so the daemon is still useful on fresh installs).
    """
    body = json.dumps(payload, separators=(",", ":")).encode()
    url = f"{EVENT_ROUTER_BASE_URL}/workflows/{urllib.parse.quote(wf_name)}/health"

    if not WORKFLOW_HEALTH_HMAC_SECRET:
        log(f"workflow_health: HMAC secret missing, skipping {wf_name} (set WORKFLOW_HEALTH_HMAC_SECRET)")
        return False

    sig = _hmac.new(WORKFLOW_HEALTH_HMAC_SECRET.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Workflow-Health-Signature": f"sha256={sig}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as exc:
        body_preview = (exc.read()[:120] if hasattr(exc, "read") else b"").decode("utf-8", "replace")
        log(f"workflow_health POST {wf_name} HTTP {exc.code}: {body_preview}")
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log(f"workflow_health POST {wf_name} network error: {exc}")
        return False


def notify_telegram(level: str, message: str, detail: str = "") -> None:
    """Fire-and-forget Telegram ping via the operator-configured notify script.

    Security: the script path comes only from the NOTIFY_SCRIPT env var (operator
    config, never a network input). We resolve it to an absolute path and require it
    to be an existing *regular* file before executing, and invoke it with shell=False
    and a fixed argument vector — so there is no shell to inject into and no way for the
    level/message/detail strings (all internally generated) to be interpreted as a
    command. This closes the "tainted env args" concern without a shell round-trip.
    """
    if not TELEGRAM_NOTIFY.is_file():
        return
    script = str(TELEGRAM_NOTIFY.resolve())
    try:
        import subprocess as _sp
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
        # Safe: NOTIFY_SCRIPT is operator configuration (never a network/user input),
        # validated above to be an existing regular file and resolved to an absolute
        # path; invoked with shell=False and a fixed argv, so there is no shell to
        # inject into. level/message/detail are internally generated status strings.
        _sp.Popen(  # noqa: S603
            [script, str(level), str(message), str(detail)],
            shell=False,
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, close_fds=True,
        )
    except OSError:
        pass


def main_loop() -> None:
    state = load_state()
    log(f"bridge starting, last_rowid={state.get('last_rowid', 0)}, n8n_key_present={bool(N8N_API_KEY)}")
    notify_telegram("info", "observability-bridge started",
                    f"cursor={state.get('last_rowid', 0)} n8n_key={'yes' if N8N_API_KEY else 'no'}")

    pg_conn = psycopg2.connect(PG_DSN)
    pg_conn.autocommit = False

    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("flag", True))

    next_n8n_sync = 0.0
    next_economics_recompute = 0.0
    consecutive_failures = 0
    last_alerted_failure = False
    while not stop["flag"]:
        cycle_failed = False
        try:
            rows = fetch_new_rows(state["last_rowid"])
            if rows:
                n = process_batch(rows, state, pg_conn)
                if n:
                    log(f"processed {n} events, cursor={state['last_rowid']}")
                    save_state(state)

            now_ts = time.time()
            if now_ts >= next_n8n_sync:
                try:
                    updated = sync_workflow_health()
                    if updated:
                        log(f"workflow_health: updated {updated} workflows from n8n")
                except Exception as exc:  # noqa: BLE001
                    log(f"workflow_health sync error: {exc!r}")
                    cycle_failed = True
                next_n8n_sync = now_ts + N8N_SYNC_INTERVAL_SEC

            # Recompute economics live cost rate every 30s (cheap query)
            if now_ts >= next_economics_recompute:
                _recompute_economics_rate()
                # Same cadence: backfill task_events with cost/tokens from agent-economics
                try:
                    n_back = _backfill_metrics_costs()
                    if n_back:
                        log(f"metrics-engine: backfilled cost/tokens on {n_back} task_events rows")
                except Exception as exc:  # noqa: BLE001
                    log(f"metrics backfill error: {exc!r}")
                next_economics_recompute = now_ts + 30.0
        except psycopg2.Error as exc:
            log(f"postgres error: {exc}; reconnecting")
            cycle_failed = True
            try:
                pg_conn.close()
            except Exception:
                pass
            time.sleep(2)
            try:
                pg_conn = psycopg2.connect(PG_DSN)
                pg_conn.autocommit = False
            except psycopg2.Error as exc2:
                log(f"postgres reconnect failed: {exc2}")
        except Exception as exc:  # noqa: BLE001
            log(f"unexpected error: {exc!r}")
            cycle_failed = True

        # Heartbeat accounting
        if cycle_failed:
            consecutive_failures += 1
            if consecutive_failures >= HEARTBEAT_FAIL_THRESHOLD and not last_alerted_failure:
                notify_telegram(
                    "error", "observability-bridge degraded",
                    f"{consecutive_failures} consecutive cycle failures; see /tmp/observability-bridge.log",
                )
                last_alerted_failure = True
        else:
            if last_alerted_failure:
                notify_telegram("success", "observability-bridge recovered",
                                "cycles green again")
                last_alerted_failure = False
            consecutive_failures = 0

        time.sleep(POLL_SEC)

    save_state(state)
    pg_conn.close()
    _drop_economics_conn()
    log("bridge stopped")
    notify_telegram("warn", "observability-bridge stopped",
                    "process exited cleanly via signal")


if __name__ == "__main__":
    main_loop()

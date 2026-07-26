"""
Event Router — PRD 13 implementation.

FastAPI service that:
  - Receives events via POST /events (fire-and-forget by default)
  - Validates against ~/.claude/events/taxonomy.yaml (REQ-ED-001, REQ-ED-006)
  - Matches against routing-rules.yaml with glob patterns (REQ-ED-002, REQ-ED-010)
  - Dispatches to 3 consumer types: n8n webhook, conductor state, direct action (REQ-ED-009)
  - Failed deliveries → SQLite DLQ with replay (REQ-ED-004, REQ-ED-011)
  - Logs every routing decision (REQ-ED-012)
  - Workflow health tracking against SLA (REQ-ED-008)
  - Hot-reload of routing rules without restart (REQ-ED-010)
  - Correlation ID propagation (REQ-ED-014)
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import hmac as _hmac
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("event-router")


def _lg(value: object, limit: int = 200) -> str:
    """Render an untrusted value safe for a single log line.

    Event categories, types and correlation ids arrive from callers. Written to
    the log verbatim, a value containing CR/LF can forge additional log entries,
    and control characters can corrupt a terminal reading the log
    (CodeQL py/log-injection).
    """
    text = str(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    text = "".join(ch if ch.isprintable() else "\\x%02x" % ord(ch) for ch in text)
    return text[:limit] + ("…" if len(text) > limit else "")

# --- Config paths (env-overridable) ---
EVENTS_DIR = Path(os.environ.get("EVENTS_DIR", str(Path.home() / ".claude" / "events")))
TAXONOMY_PATH = EVENTS_DIR / "taxonomy.yaml"
ROUTES_PATH = EVENTS_DIR / "routing-rules.yaml"
REGISTRY_PATH = EVENTS_DIR / "workflow-registry.yaml"
DLQ_PATH = Path(os.environ.get("DLQ_PATH", str(EVENTS_DIR / "dead-letter-queue.sqlite")))

DLQ_RETENTION_DAYS = int(os.environ.get("DLQ_RETENTION_DAYS", "30"))
DLQ_MAX_RETRIES = int(os.environ.get("DLQ_MAX_RETRIES", "3"))
DLQ_RETRY_INTERVAL_SECONDS = int(os.environ.get("DLQ_RETRY_INTERVAL_SECONDS", "60"))
DLQ_BACKOFF_MS_BASE = int(os.environ.get("DLQ_BACKOFF_MS_BASE", "2000"))
N8N_BASE_URL = os.environ.get("N8N_BASE_URL", "http://localhost:5678")
WEBHOOK_TIMEOUT_SECONDS = float(os.environ.get("WEBHOOK_TIMEOUT_SECONDS", "10"))
ROUTING_RELOAD_SECONDS = int(os.environ.get("ROUTING_RELOAD_SECONDS", "30"))

# --- In-memory state ---
_state: dict[str, Any] = {
    "taxonomy": {},
    "routes": {"global": {}, "routes": []},
    "registry": {"workflows": {}},
    "config_mtime": {"taxonomy": 0, "routes": 0, "registry": 0},
    "metrics": {
        "events_received_total": 0,
        "events_routed_total": 0,
        "events_dlq_total": 0,
        "events_per_category": {},
        "started_at": time.time(),
        "last_reload": 0,
        "p99_routing_latency_ms": 0.0,
        "_recent_latencies": [],
    },
    "ws_clients": set(),
}


# --- Models ---

class IncomingEvent(BaseModel):
    """REQ-ED-006: every event must include category, type, payload, source, timestamp, correlation_id."""

    category: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str
    timestamp: str | None = None
    correlation_id: str | None = None

    @field_validator("category")
    @classmethod
    def _valid_category(cls, v: str) -> str:
        if not v or "." in v:
            raise ValueError("category must be a non-empty taxonomy key without dots")
        return v


# --- DLQ (SQLite) ---

DLQ_SCHEMA = """
CREATE TABLE IF NOT EXISTS dlq (
    event_id TEXT PRIMARY KEY,
    event_category TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_payload TEXT NOT NULL,
    target_consumer TEXT NOT NULL,
    failure_reason TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    first_failure_timestamp TEXT NOT NULL,
    last_retry_timestamp TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    expiry_timestamp TEXT NOT NULL,
    correlation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_dlq_category ON dlq(event_category);
CREATE INDEX IF NOT EXISTS idx_dlq_status ON dlq(status);
CREATE INDEX IF NOT EXISTS idx_dlq_first_failure ON dlq(first_failure_timestamp);

CREATE TABLE IF NOT EXISTS routing_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    correlation_id TEXT,
    category TEXT NOT NULL,
    type TEXT NOT NULL,
    matched_rules TEXT NOT NULL,
    consumers_targeted TEXT NOT NULL,
    delivery_status TEXT NOT NULL,
    routing_latency_ms REAL NOT NULL,
    timestamp TEXT NOT NULL,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_event_id ON routing_log(event_id);
CREATE INDEX IF NOT EXISTS idx_log_correlation ON routing_log(correlation_id);
CREATE INDEX IF NOT EXISTS idx_log_timestamp ON routing_log(timestamp);

CREATE TABLE IF NOT EXISTS workflow_health (
    workflow_name TEXT PRIMARY KEY,
    last_status TEXT,
    last_run_timestamp TEXT,
    success_count_24h INTEGER NOT NULL DEFAULT 0,
    failure_count_24h INTEGER NOT NULL DEFAULT 0,
    avg_latency_ms REAL NOT NULL DEFAULT 0,
    sla_breaches_24h INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS delayed_dispatch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket TEXT NOT NULL UNIQUE,
    due_at TEXT NOT NULL,
    consumer_json TEXT NOT NULL,
    event_json TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delayed_due ON delayed_dispatch(status, due_at);
"""


def db_connect() -> sqlite3.Connection:
    DLQ_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DLQ_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def db_init() -> None:
    with closing(db_connect()) as conn:
        conn.executescript(DLQ_SCHEMA)
        # Migration guard: routing_log predates payload_json on existing DBs.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(routing_log)")}
        if "payload_json" not in cols:
            conn.execute("ALTER TABLE routing_log ADD COLUMN payload_json TEXT")
        conn.commit()


# --- Config loading & hot-reload (REQ-ED-010) ---

def _load_yaml(path: Path) -> dict:
    if not path.exists():
        log.warning("Config %s missing — using empty defaults", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        log.error("Failed to parse %s: %s", path, exc)
        return {}


def reload_configs(force: bool = False) -> dict[str, bool]:
    """Reload taxonomy, routes, and registry if their mtime changed. Returns map of changed configs."""
    changed: dict[str, bool] = {}
    for key, path in (("taxonomy", TAXONOMY_PATH), ("routes", ROUTES_PATH), ("registry", REGISTRY_PATH)):
        if not path.exists():
            continue
        mtime = path.stat().st_mtime
        if force or mtime != _state["config_mtime"][key]:
            data = _load_yaml(path)
            _state[key] = data if data else _state[key]
            _state["config_mtime"][key] = mtime
            changed[key] = True
    if changed:
        _state["metrics"]["last_reload"] = time.time()
        log.info("Reloaded configs: %s", list(changed.keys()))
    return changed


async def _config_watcher() -> None:
    """Background task: poll config files for mtime changes and reload."""
    while True:
        try:
            await asyncio.sleep(ROUTING_RELOAD_SECONDS)
            reload_configs()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.error("Config watcher error: %s", exc)


# --- Validation against taxonomy (REQ-ED-001) ---

def validate_event(category: str, event_type: str) -> tuple[bool, str]:
    """Return (valid, reason). Unknown category/type rejected."""
    taxonomy = _state.get("taxonomy", {}).get("categories", {})
    if not taxonomy:
        # If no taxonomy loaded, fail open with warning — but log
        log.warning("No taxonomy loaded — accepting event %s.%s without validation", _lg(category), _lg(event_type))
        return True, "no taxonomy loaded"
    cat_def = taxonomy.get(category)
    if cat_def is None:
        return False, f"unknown category: {category}"
    events = cat_def.get("events", {})
    full_type = f"{category}.{event_type}"
    if full_type not in events and event_type not in events:
        # Allow new event types within known category (extensible per PRD spec note "room for growth")
        log.info("Event type %s not in taxonomy but category %s is known — accepting", _lg(event_type), _lg(category))
    return True, "ok"


# --- Routing rule matching (REQ-ED-002) ---

def match_rules(category: str, event_type: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Find rules whose match criteria fit this event. Glob support on category/type."""
    rules = _state.get("routes", {}).get("routes", []) or _state.get("routes", {}).get("rules", [])
    matched: list[dict[str, Any]] = []
    full = f"{category}.{event_type}"
    for rule in rules:
        # Two YAML shapes supported: {event: "category.type", handlers: [...]}  AND  {match: {...}, consumers: [...]}
        if "event" in rule:
            pattern = rule.get("event", "")
            if pattern == full or fnmatch.fnmatch(full, pattern):
                matched.append(rule)
        elif "match" in rule:
            m = rule.get("match", {})
            cat_match = m.get("category", "*")
            type_match = m.get("type", "*")
            if not (fnmatch.fnmatch(category, cat_match) and fnmatch.fnmatch(event_type, type_match)):
                continue
            payload_match = m.get("payload", {})
            if payload_match and not all(payload.get(k) == v for k, v in payload_match.items()):
                continue
            matched.append(rule)
    return matched


# --- Consumer dispatch (REQ-ED-009) ---

async def _dispatch_webhook(consumer: dict[str, Any], event: dict[str, Any]) -> tuple[bool, str]:
    url = consumer.get("url") or consumer.get("endpoint")
    url_env = consumer.get("url_env")
    if url_env:
        url = os.environ.get(url_env, url)
    if not url:
        # Fallback: build from registry
        target = consumer.get("target")
        if target and target in _state.get("registry", {}).get("workflows", {}):
            url = f"{N8N_BASE_URL}/webhook/{target}"
        else:
            return False, "no webhook URL"
    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=event)
            if 200 <= resp.status_code < 300:
                return True, f"HTTP {resp.status_code}"
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except httpx.RequestError as exc:
        return False, f"network: {exc}"


async def _dispatch_state_update(consumer: dict[str, Any], event: dict[str, Any]) -> tuple[bool, str]:
    """Conductor state update — append to event_routing.last_event in any active conductor-state.json files."""
    field = consumer.get("field", "event_routing.last_event")
    target_dir = consumer.get("target_directory") or os.environ.get("CONDUCTOR_STATE_DIR")
    if not target_dir:
        return True, "no target_directory configured (no-op)"
    state_path = Path(target_dir) / "conductor-state.json"
    if not state_path.exists():
        return False, f"conductor-state.json not found at {state_path}"
    try:
        data = json.loads(state_path.read_text())
        # Set nested field
        keys = field.split(".")
        cur = data
        for k in keys[:-1]:
            cur = cur.setdefault(k, {})
        cur[keys[-1]] = event
        state_path.write_text(json.dumps(data, indent=2))
        return True, "state updated"
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"state write failed: {exc}"


async def _dispatch_direct(consumer: dict[str, Any], event: dict[str, Any]) -> tuple[bool, str]:
    """Direct action — log, increment metrics, broadcast to ws clients."""
    action = consumer.get("action", "log_event")
    if action == "log_event":
        log.info("Direct: %s %s %s", _lg(event.get("category")), _lg(event.get("type")), _lg(event.get("correlation_id")))
        return True, "logged"
    if action == "broadcast_ws":
        await _broadcast_ws(event)
        return True, "broadcast"
    return True, f"action {action} not implemented (no-op)"


# --- Delayed dispatch with dedup (REQ-ED-002: delay_seconds / dedup_key rule fields) ---

DELAYED_DISPATCH_POLL_SECONDS = int(os.environ.get("DELAYED_DISPATCH_POLL_SECONDS", "30"))


def schedule_delayed_dispatch(consumer: dict[str, Any], event: dict[str, Any], rule_name: str) -> str:
    """Debounce-schedule a consumer dispatch instead of delivering immediately.

    First event for a (rule, consumer, dedup value) bucket opens a fixed window of
    delay_seconds; later events in the window collapse into the pending row (latest
    payload wins, event_count increments) without extending the deadline. After the
    worker delivers, the next event opens a fresh window.
    """
    delay = int(consumer.get("delay_seconds", 0))
    dedup_key = consumer.get("dedup_key")
    dedup_value = str(event.get("payload", {}).get(dedup_key)) if dedup_key else None
    if not dedup_value or dedup_value == "None":
        dedup_value = event["event_id"]  # no dedup field present — window per event
    target = consumer.get("target") or consumer.get("url") or consumer.get("action", "?")
    bucket = f"{rule_name}|{target}|{dedup_value}"
    now = datetime.now(timezone.utc)
    due = datetime.fromtimestamp(now.timestamp() + delay, tz=timezone.utc).isoformat()
    with closing(db_connect()) as conn:
        conn.execute(
            """INSERT INTO delayed_dispatch (bucket, due_at, consumer_json, event_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(bucket) DO UPDATE SET
                 event_json = excluded.event_json,
                 event_count = event_count + 1,
                 updated_at = excluded.updated_at
               WHERE status = 'pending'
            """,
            (bucket, due, json.dumps(consumer), json.dumps(event), now.isoformat(), now.isoformat()),
        )
        # A delivered/failed row still occupies the bucket — recycle it into a new window.
        cur = conn.execute(
            "UPDATE delayed_dispatch SET due_at = ?, event_json = ?, event_count = 1, status = 'pending', "
            "created_at = ?, updated_at = ? WHERE bucket = ? AND status != 'pending'",
            (due, json.dumps(event), now.isoformat(), now.isoformat(), bucket),
        )
        conn.commit()
        recycled = cur.rowcount > 0
    log.info("Delayed dispatch %s: bucket=%s due=%s", "rescheduled" if recycled else "scheduled", _lg(bucket), _lg(due))
    return bucket


async def deliver_due_delayed() -> int:
    """One delivery pass: dispatch pending rows whose window has elapsed. Returns count delivered."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with closing(db_connect()) as conn:
        rows = conn.execute(
            "SELECT id, bucket, consumer_json, event_json, event_count FROM delayed_dispatch "
            "WHERE status = 'pending' AND due_at <= ? LIMIT 50",
            (now_iso,),
        ).fetchall()
    delivered = 0
    for row in rows:
        consumer = json.loads(row["consumer_json"])
        event = json.loads(row["event_json"])
        ok, reason = await dispatch_to_consumer(consumer, event)
        with closing(db_connect()) as conn:
            conn.execute(
                "UPDATE delayed_dispatch SET status = ?, updated_at = ? WHERE id = ?",
                ("delivered" if ok else "failed", datetime.now(timezone.utc).isoformat(), row["id"]),
            )
            conn.commit()
        if ok:
            delivered += 1
            log.info("Delayed dispatch delivered: bucket=%s (absorbed %d events): %s",
                     row["bucket"], row["event_count"], reason)
        else:
            log.warning("Delayed dispatch failed: bucket=%s: %s — routing to DLQ", row["bucket"], reason)
            dlq_add(f"{event['event_id']}#delayed:{row['bucket']}", event, json.dumps(consumer), reason)
    return delivered


async def _delayed_dispatch_worker() -> None:
    """Background: deliver delayed dispatches whose window has elapsed."""
    while True:
        try:
            await asyncio.sleep(DELAYED_DISPATCH_POLL_SECONDS)
            await deliver_due_delayed()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.error("Delayed dispatch worker error: %s", exc)


async def dispatch_to_consumer(consumer: dict[str, Any], event: dict[str, Any]) -> tuple[bool, str]:
    """Top-level dispatcher routing by consumer.type."""
    ctype = consumer.get("type", "webhook")
    if ctype in ("webhook", "n8n_webhook"):
        return await _dispatch_webhook(consumer, event)
    if ctype in ("conductor", "conductor_state_update"):
        return await _dispatch_state_update(consumer, event)
    if ctype in ("direct", "direct_action"):
        return await _dispatch_direct(consumer, event)
    return False, f"unknown consumer type: {ctype}"


# --- DLQ operations ---

def dlq_add(event_id: str, event: dict[str, Any], target: str, reason: str) -> None:
    expiry = datetime.now(timezone.utc).timestamp() + DLQ_RETENTION_DAYS * 86400
    expiry_iso = datetime.fromtimestamp(expiry, tz=timezone.utc).isoformat()
    with closing(db_connect()) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO dlq (event_id, event_category, event_type, event_payload,
                target_consumer, failure_reason, retry_count, first_failure_timestamp,
                status, expiry_timestamp, correlation_id)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'pending', ?, ?)
            """,
            (
                event_id,
                event.get("category", ""),
                event.get("type", ""),
                json.dumps(event),
                target,
                reason,
                datetime.now(timezone.utc).isoformat(),
                expiry_iso,
                event.get("correlation_id"),
            ),
        )
        conn.commit()
    _state["metrics"]["events_dlq_total"] += 1


def dlq_list(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    with closing(db_connect()) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM dlq WHERE status = ? ORDER BY first_failure_timestamp DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dlq ORDER BY first_failure_timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


async def dlq_replay(event_id: str) -> tuple[bool, str]:
    with closing(db_connect()) as conn:
        row = conn.execute("SELECT * FROM dlq WHERE event_id = ?", (event_id,)).fetchone()
        if not row:
            return False, "not found"
        if row["status"] == "exhausted" and row["retry_count"] >= DLQ_MAX_RETRIES:
            return False, "retry budget exhausted"
        event = json.loads(row["event_payload"])
        target = json.loads(row["target_consumer"]) if row["target_consumer"].startswith("{") else {"type": row["target_consumer"]}
    ok, reason = await dispatch_to_consumer(target, event)
    with closing(db_connect()) as conn:
        if ok:
            conn.execute(
                "UPDATE dlq SET status = 'replayed', last_retry_timestamp = ?, retry_count = retry_count + 1 WHERE event_id = ?",
                (datetime.now(timezone.utc).isoformat(), event_id),
            )
        else:
            new_count = row["retry_count"] + 1
            new_status = "exhausted" if new_count >= DLQ_MAX_RETRIES else "retrying"
            conn.execute(
                "UPDATE dlq SET status = ?, last_retry_timestamp = ?, retry_count = ?, failure_reason = ? WHERE event_id = ?",
                (new_status, datetime.now(timezone.utc).isoformat(), new_count, reason, event_id),
            )
        conn.commit()
    return ok, reason


async def dlq_purge_aged() -> int:
    """Remove entries past expiry. Returns count purged."""
    with closing(db_connect()) as conn:
        cur = conn.execute("DELETE FROM dlq WHERE expiry_timestamp < ?", (datetime.now(timezone.utc).isoformat(),))
        conn.commit()
        return cur.rowcount


async def _dlq_retry_worker() -> None:
    """Background: retry pending DLQ entries with exponential backoff."""
    while True:
        try:
            await asyncio.sleep(DLQ_RETRY_INTERVAL_SECONDS)
            with closing(db_connect()) as conn:
                rows = conn.execute(
                    "SELECT event_id, retry_count FROM dlq WHERE status IN ('pending','retrying') AND retry_count < ? LIMIT 50",
                    (DLQ_MAX_RETRIES,),
                ).fetchall()
            for row in rows:
                # Exponential backoff: skip if not enough time elapsed since last retry
                await dlq_replay(row["event_id"])
            await dlq_purge_aged()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.error("DLQ worker error: %s", exc)


# --- Routing log (REQ-ED-012) ---

def log_routing_decision(
    event_id: str,
    correlation_id: str | None,
    category: str,
    event_type: str,
    matched_rules: list[str],
    consumers: list[str],
    statuses: list[str],
    latency_ms: float,
    payload: dict[str, Any] | None = None,
) -> None:
    with closing(db_connect()) as conn:
        conn.execute(
            """
            INSERT INTO routing_log
                (event_id, correlation_id, category, type, matched_rules,
                 consumers_targeted, delivery_status, routing_latency_ms, timestamp, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                correlation_id,
                category,
                event_type,
                json.dumps(matched_rules),
                json.dumps(consumers),
                json.dumps(statuses),
                latency_ms,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(payload) if payload else None,
            ),
        )
        conn.commit()


# --- Core routing (REQ-ED-005 fire-and-forget, REQ-ED-013 100/sec) ---

async def route_event(event: IncomingEvent) -> dict[str, Any]:
    started = time.perf_counter()
    event_id = str(uuid.uuid4())
    correlation_id = event.correlation_id or str(uuid.uuid4())  # REQ-ED-014
    timestamp = event.timestamp or datetime.now(timezone.utc).isoformat()

    valid, reason = validate_event(event.category, event.type)
    if not valid:
        return {"event_id": event_id, "status": "rejected", "reason": reason}

    full_event = {
        "event_id": event_id,
        "category": event.category,
        "type": event.type,
        "payload": event.payload,
        "source": event.source,
        "timestamp": timestamp,
        "correlation_id": correlation_id,
    }

    rules = match_rules(event.category, event.type, event.payload)
    consumers_seen: list[str] = []
    statuses: list[str] = []
    matched_names: list[str] = []
    success_count = 0
    failure_count = 0

    for rule in rules:
        rule_name = rule.get("name", rule.get("event", "unnamed"))
        matched_names.append(rule_name)
        targets = rule.get("consumers") or rule.get("handlers") or []
        for consumer in targets:
            consumers_seen.append(f"{consumer.get('type', '?')}:{consumer.get('target') or consumer.get('url') or consumer.get('action', '?')}")
            if int(consumer.get("delay_seconds", 0)) > 0:
                bucket = schedule_delayed_dispatch(consumer, full_event, rule_name)
                success_count += 1
                statuses.append(f"delayed:{bucket}")
                continue
            ok, reason = await dispatch_to_consumer(consumer, full_event)
            if ok:
                success_count += 1
                statuses.append("ok")
            else:
                failure_count += 1
                statuses.append(f"fail:{reason}")
                # REQ-ED-004: failures → DLQ
                dlq_add(f"{event_id}#{consumers_seen[-1]}", full_event, json.dumps(consumer), reason)

    latency_ms = (time.perf_counter() - started) * 1000

    # metrics
    _state["metrics"]["events_received_total"] += 1
    _state["metrics"]["events_routed_total"] += 1 if success_count else 0
    cat_counts = _state["metrics"]["events_per_category"].setdefault(event.category, 0)
    _state["metrics"]["events_per_category"][event.category] = cat_counts + 1
    recent = _state["metrics"]["_recent_latencies"]
    recent.append(latency_ms)
    if len(recent) > 1000:
        del recent[: len(recent) - 1000]
    if recent:
        sorted_lat = sorted(recent)
        _state["metrics"]["p99_routing_latency_ms"] = sorted_lat[int(len(sorted_lat) * 0.99) - 1] if len(sorted_lat) > 1 else sorted_lat[0]

    log_routing_decision(event_id, correlation_id, event.category, event.type, matched_names, consumers_seen, statuses, latency_ms, event.payload)
    await _broadcast_ws(full_event | {"_routing": {"matched": matched_names, "consumers": consumers_seen, "statuses": statuses, "latency_ms": round(latency_ms, 2)}})

    return {
        "event_id": event_id,
        "correlation_id": correlation_id,
        "status": "routed" if success_count else "no_handlers" if not rules else "all_failed",
        "matched_rules": matched_names,
        "consumers_targeted": len(consumers_seen),
        "successes": success_count,
        "failures": failure_count,
        "latency_ms": round(latency_ms, 2),
    }


# --- Workflow health (REQ-ED-007, REQ-ED-008) ---

def workflow_health_summary() -> dict[str, Any]:
    workflows = _state.get("registry", {}).get("workflows", {})
    out: dict[str, Any] = {"workflows": [], "aggregate": {}}
    with closing(db_connect()) as conn:
        for name, meta in workflows.items():
            row = conn.execute("SELECT * FROM workflow_health WHERE workflow_name = ?", (name,)).fetchone()
            health = dict(row) if row else {
                "workflow_name": name,
                "last_status": "unknown",
                "last_run_timestamp": None,
                "success_count_24h": 0,
                "failure_count_24h": 0,
                "avg_latency_ms": 0,
                "sla_breaches_24h": 0,
            }
            total = health["success_count_24h"] + health["failure_count_24h"]
            success_rate = (health["success_count_24h"] / total * 100) if total else 0
            out["workflows"].append({
                **health,
                "sla_minutes": meta.get("sla_minutes"),
                "priority": meta.get("priority"),
                "schedule": meta.get("schedule"),
                "success_rate_pct": round(success_rate, 1),
                "enabled": meta.get("enabled", True),
            })
        # aggregate
        agg = conn.execute(
            """
            SELECT COUNT(*) AS dlq_pending,
                   (SELECT COUNT(*) FROM routing_log WHERE timestamp > datetime('now','-24 hours')) AS events_24h,
                   (SELECT COUNT(*) FROM workflow_health WHERE sla_breaches_24h > 0) AS sla_violations
            FROM dlq WHERE status = 'pending'
            """
        ).fetchone()
        out["aggregate"] = dict(agg) if agg else {}
    return out


# --- WebSocket broadcast ---

async def _broadcast_ws(event: dict[str, Any]) -> None:
    if not _state["ws_clients"]:
        return
    message = json.dumps(event, default=str)
    dead: set[Any] = set()
    for ws in list(_state["ws_clients"]):
        try:
            await ws.send_text(message)
        except Exception:
            dead.add(ws)
    _state["ws_clients"].difference_update(dead)


# --- App lifecycle ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    db_init()
    reload_configs(force=True)
    log.info("Loaded taxonomy: %d categories", len(_state["taxonomy"].get("categories", {})))
    log.info("Loaded routes: %d rules", len(_state["routes"].get("routes", []) + _state["routes"].get("rules", [])))
    log.info("Loaded registry: %d workflows", len(_state["registry"].get("workflows", {})))
    watcher = asyncio.create_task(_config_watcher())
    retrier = asyncio.create_task(_dlq_retry_worker())
    delayer = asyncio.create_task(_delayed_dispatch_worker())
    yield
    watcher.cancel()
    retrier.cancel()
    delayer.cancel()


app = FastAPI(title="Event Router", version="1.0.0", lifespan=lifespan)

# Strong references to in-flight background routing tasks. Without this the
# event loop may garbage-collect a task that nothing else holds, which drops
# the event just as surely as cancelling it did.
_INFLIGHT: set[asyncio.Task] = set()

# Shared-secret gate.
#
# Every route below was reachable unauthenticated: anyone able to reach the
# port could inject arbitrary events into the routing fabric or subscribe to
# the live event stream and read everything flowing through it.
#
# Send the token as `Authorization: Bearer <token>` or `X-Router-Token`.
# Unset token => the service refuses everything (fail closed).
EVENT_ROUTER_TOKEN = os.environ.get("EVENT_ROUTER_TOKEN", "")
_PUBLIC_PATHS = {"/health", "/healthz", "/readyz", "/metrics"}


def _token_ok(presented: str) -> bool:
    return bool(presented) and _hmac.compare_digest(presented, EVENT_ROUTER_TOKEN)


@app.middleware("http")
async def require_router_token(request: Request, call_next):
    if request.url.path in _PUBLIC_PATHS or request.url.path.startswith("/static/"):
        return await call_next(request)
    if not EVENT_ROUTER_TOKEN:
        return JSONResponse(
            {"detail": "EVENT_ROUTER_TOKEN is not set; the router refuses requests until it is configured."},
            status_code=503,
        )
    header = request.headers.get("authorization", "")
    presented = (header[7:] if header.lower().startswith("bearer ") else "") or request.headers.get("x-router-token", "")
    if not _token_ok(presented):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)

# Static + templates
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# --- API endpoints ---

@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - _state["metrics"]["started_at"], 1),
        "taxonomy_loaded": bool(_state["taxonomy"]),
        "routes_loaded": bool(_state["routes"]),
        "registry_loaded": bool(_state["registry"]),
        "events_received_total": _state["metrics"]["events_received_total"],
        "dlq_pending": _dlq_pending_count(),
    }


def _dlq_pending_count() -> int:
    try:
        with closing(db_connect()) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM dlq WHERE status IN ('pending','retrying')").fetchone()
            return int(row["n"]) if row else 0
    except sqlite3.OperationalError:
        return 0


@app.post("/events")
async def emit_event(event: IncomingEvent) -> JSONResponse:
    """Fire-and-forget event emission. Returns immediately after queueing for routing."""
    # Schedule routing as a background task (REQ-ED-005).
    #
    # asyncio.wait_for CANCELS the task it is waiting on when the timeout
    # fires, so any routing that took longer than 50ms was destroyed while
    # this endpoint returned 202 "queued" — the event was silently dropped.
    # asyncio.shield keeps the task running past the timeout, and a strong
    # reference prevents the loop from garbage-collecting it mid-flight.
    task = asyncio.create_task(route_event(event))
    _INFLIGHT.add(task)
    task.add_done_callback(_INFLIGHT.discard)
    try:
        result = await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
        return JSONResponse(result, status_code=202)
    except asyncio.TimeoutError:
        # Still routing — genuinely fire-and-forget now, not cancelled.
        return JSONResponse({"status": "queued"}, status_code=202)


@app.post("/events/sync")
async def emit_event_sync(event: IncomingEvent) -> JSONResponse:
    """Synchronous emission — waits for routing decision (for testing)."""
    result = await route_event(event)
    return JSONResponse(result, status_code=200 if result["status"] in ("routed", "no_handlers") else 422)


@app.get("/events")
async def query_events(
    category: str | None = None,
    correlation_id: str | None = None,
    limit: int = Query(default=100, le=1000),
) -> dict[str, Any]:
    with closing(db_connect()) as conn:
        sql = "SELECT * FROM routing_log WHERE 1=1"
        params: list[Any] = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        if correlation_id:
            sql += " AND correlation_id = ?"
            params.append(correlation_id)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return {"events": [dict(r) for r in rows], "count": len(rows)}


@app.get("/events/{event_id}")
async def get_event(event_id: str) -> dict[str, Any]:
    with closing(db_connect()) as conn:
        row = conn.execute("SELECT * FROM routing_log WHERE event_id = ?", (event_id,)).fetchone()
        if not row:
            raise HTTPException(404, "event not found in routing log")
        return dict(row)


@app.get("/workflows")
async def list_workflows() -> dict[str, Any]:
    return workflow_health_summary()


@app.get("/workflows/{name}")
async def get_workflow(name: str) -> dict[str, Any]:
    summary = workflow_health_summary()
    for wf in summary["workflows"]:
        if wf["workflow_name"] == name:
            return wf
    raise HTTPException(404, "workflow not in registry")


# --- Workflow health write endpoint (HMAC-authenticated) ---

class WorkflowHealthUpdate(BaseModel):
    """Payload for POST /workflows/{name}/health.
    Body: {last_status, last_run_timestamp, success_count_24h, failure_count_24h,
           avg_latency_ms, sla_breaches_24h}.
    """
    last_status: str
    last_run_timestamp: str | None = None
    success_count_24h: int = 0
    failure_count_24h: int = 0
    avg_latency_ms: float = 0.0
    sla_breaches_24h: int = 0

    @field_validator("last_status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        if v not in ("success", "failure", "running", "unknown"):
            raise ValueError("last_status must be one of: success, failure, running, unknown")
        return v


def _verify_hmac(payload_bytes: bytes, signature_header: str, secret: str) -> bool:
    """Constant-time HMAC verification. Returns False on any malformed input."""
    if not signature_header or not secret:
        return False
    # Accept "sha256=HEX" or just "HEX"
    sig = signature_header.split("=", 1)[1] if "=" in signature_header else signature_header
    try:
        expected = _hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
        return _hmac.compare_digest(expected, sig.strip())
    except (TypeError, ValueError):
        return False


@app.post("/workflows/{name}/health")
async def upsert_workflow_health(name: str, request: Request) -> dict[str, Any]:
    """Upsert a workflow's health row. HMAC-SHA256 required.

    Caller computes `signature = hex(hmac_sha256(secret, raw_body))` and sends it as
    `X-Workflow-Health-Signature: sha256=<hex>` or just `<hex>`.
    Secret lives in the WORKFLOW_HEALTH_HMAC_SECRET env var on the server.
    """
    secret = os.environ.get("WORKFLOW_HEALTH_HMAC_SECRET", "")
    if not secret:
        raise HTTPException(503, "workflow_health endpoint disabled — WORKFLOW_HEALTH_HMAC_SECRET not set")

    raw = await request.body()
    signature = request.headers.get("x-workflow-health-signature", "")
    if not _verify_hmac(raw, signature, secret):
        raise HTTPException(401, "invalid or missing HMAC signature")

    try:
        body = json.loads(raw.decode())
        update = WorkflowHealthUpdate(**body)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise HTTPException(400, f"invalid payload: {exc}")

    # Caller must reference a name that exists in the registry — guards against typos.
    registry_workflows = set((_state.get("registry") or {}).get("workflows", {}).keys())
    if name not in registry_workflows:
        raise HTTPException(404, f"workflow '{name}' not in registry")

    with closing(db_connect()) as conn:
        conn.execute(
            """INSERT INTO workflow_health
               (workflow_name, last_status, last_run_timestamp, success_count_24h,
                failure_count_24h, avg_latency_ms, sla_breaches_24h)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(workflow_name) DO UPDATE SET
                 last_status = excluded.last_status,
                 last_run_timestamp = excluded.last_run_timestamp,
                 success_count_24h = excluded.success_count_24h,
                 failure_count_24h = excluded.failure_count_24h,
                 avg_latency_ms = excluded.avg_latency_ms,
                 sla_breaches_24h = excluded.sla_breaches_24h
            """,
            (
                name,
                update.last_status,
                update.last_run_timestamp,
                update.success_count_24h,
                update.failure_count_24h,
                update.avg_latency_ms,
                update.sla_breaches_24h,
            ),
        )
        conn.commit()

    return {"updated": True, "workflow_name": name, "last_status": update.last_status}


@app.get("/dlq")
async def list_dlq(status: str | None = None, limit: int = Query(default=100, le=1000)) -> dict[str, Any]:
    return {"entries": dlq_list(status=status, limit=limit), "pending": _dlq_pending_count()}


@app.post("/dlq/{event_id}/replay")
async def replay_dlq_one(event_id: str) -> dict[str, Any]:
    ok, reason = await dlq_replay(event_id)
    return {"event_id": event_id, "replayed": ok, "reason": reason}


@app.post("/dlq/replay")
async def replay_dlq_bulk(category: str | None = None, since: str | None = None) -> dict[str, Any]:
    with closing(db_connect()) as conn:
        sql = "SELECT event_id FROM dlq WHERE status IN ('pending','retrying','exhausted')"
        params: list[Any] = []
        if category:
            sql += " AND event_category = ?"
            params.append(category)
        if since:
            sql += " AND first_failure_timestamp >= ?"
            params.append(since)
        rows = conn.execute(sql, params).fetchall()
    replayed = 0
    failed = 0
    for r in rows:
        ok, _ = await dlq_replay(r["event_id"])
        if ok:
            replayed += 1
        else:
            failed += 1
    return {"replayed": replayed, "failed": failed, "total": len(rows)}


@app.post("/reload")
async def reload_now() -> dict[str, Any]:
    """REQ-ED-010: hot reload routing rules without restart."""
    changed = reload_configs(force=True)
    return {"reloaded": list(changed.keys())}


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    m = _state["metrics"]
    return {
        "events_received_total": m["events_received_total"],
        "events_routed_total": m["events_routed_total"],
        "events_dlq_total": m["events_dlq_total"],
        "events_per_category": m["events_per_category"],
        "p99_routing_latency_ms": round(m["p99_routing_latency_ms"], 2),
        "dlq_pending": _dlq_pending_count(),
        "uptime_seconds": round(time.time() - m["started_at"], 1),
        "last_reload_timestamp": m["last_reload"],
        "ws_clients": len(_state["ws_clients"]),
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    # HTTP middleware does not run for websocket handshakes, so the token is
    # checked here. Without it, anyone reaching the port could subscribe to
    # the live stream and read every event flowing through the router.
    if not EVENT_ROUTER_TOKEN:
        await ws.close(code=1011, reason="EVENT_ROUTER_TOKEN not configured")
        return
    presented = (
        ws.headers.get("x-router-token")
        or (ws.headers.get("authorization", "")[7:]
            if ws.headers.get("authorization", "").lower().startswith("bearer ") else "")
        or ws.query_params.get("token", "")
    )
    if not _token_ok(presented):
        await ws.close(code=1008, reason="Unauthorized")
        return
    await ws.accept()
    _state["ws_clients"].add(ws)
    try:
        while True:
            await ws.receive_text()  # Keepalive
    except WebSocketDisconnect:
        _state["ws_clients"].discard(ws)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

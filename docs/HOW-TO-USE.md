# How to use — bulletproof-event-router

This guide covers the day-to-day workflow: emitting events, understanding the
taxonomy, writing routing rules (including fan-out, delayed dispatch, and dedup),
working the dead-letter queue, and reading the dashboard. Everything here maps to real
endpoints in [`app/main.py`](../app/main.py) and the example configs in
[`config.example/`](../config.example/).

## 1. Emitting events

Every event needs four fields: `category`, `type`, `source`, and `payload`. The
`category` must be a taxonomy key with **no dots** (e.g. `session`, not `session.x`).
`timestamp` and `correlation_id` are optional — the router fills them in if omitted.

### Fire-and-forget (default)

```bash
curl -X POST http://localhost:8085/events \
  -H 'Content-Type: application/json' \
  -d '{"category":"session","type":"start","source":"my-app","payload":{"session_id":"abc"}}'
```

Returns `202` almost immediately. The router briefly (50 ms) waits to capture an
`event_id`; if routing takes longer it returns `{"status":"queued"}` and routing
finishes in the background.

### Synchronous (for testing / when you need the routing decision)

```bash
curl -X POST http://localhost:8085/events/sync \
  -H 'Content-Type: application/json' \
  -d '{"category":"agent","type":"complete","source":"conductor","payload":{"agent":"builder","duration_ms":1200}}'
```

Returns the full decision:

```json
{
  "event_id": "…",
  "correlation_id": "…",
  "status": "routed",
  "matched_rules": ["agent.complete"],
  "consumers_targeted": 3,
  "successes": 3,
  "failures": 0,
  "latency_ms": 4.2
}
```

`status` is `routed` (≥1 consumer succeeded), `no_handlers` (no rule matched),
`all_failed` (rules matched but every delivery failed), or `rejected` (unknown
category).

### From a shell / hook

```bash
runtime/emit-event.sh session start '{"session_id":"abc"}'
# emit-event.sh <category> <type> <payload-json> [correlation_id]
```

It POSTs in the background and never blocks — safe to call from a Claude Code hook.

## 2. The event taxonomy

`taxonomy.yaml` defines the categories the router accepts. The shipped example has 11
categories, each with typed events and payload schemas:

| Category | Example events | Emitted by (per the example) |
|----------|----------------|------------------------------|
| `session` | `start`, `end`, `compact` | Claude Code session hooks |
| `memory` | `store`, `recall`, `consolidate` | memory MCP tools / consolidation workflow |
| `agent` | `dispatch`, `complete`, `fail` | conductor orchestrator |
| `governance` | `violation`, `gate_blocked`, `audit_event` | governance hooks |
| `security` | `threat_detected`, `lockdown` | runtime-security hooks |
| `infra` | `container_restart`, `disk_warning`, `service_degraded` | health/system monitors |
| `schedule` | `daily`, `weekly`, `monthly` | n8n cron |
| `git` | `commit`, `push`, `pr_created` | git hooks / gh CLI |
| `external` | `webhook_received`, `agent_invoked` | webhook endpoint / agent gateway |
| `recovery` | `attempt`, `success`, `escalated` | recovery engine |
| `cost` | `recorded` | metering engine |

**Validation behavior** (see `validate_event`): an unknown *category* is **rejected**;
an unknown *type* within a known category is **accepted** (the taxonomy explicitly
allows "room for growth"). If no taxonomy is loaded at all, the router **fails open**
and accepts everything with a logged warning.

Event naming convention: `category.action[.detail]`, all lowercase, max 3 levels.

## 3. Writing routing rules

`routing-rules.yaml` maps events to consumers. The router supports **two rule shapes**
(see `match_rules`):

**Shape A — `event` + `handlers` (used in the example):**

```yaml
routes:
  - event: "session.start"        # exact or glob (fnmatch)
    handlers:
      - type: direct
        action: log_event
      - type: conductor
        action: update_session_tracking
        field: "event_routing.last_event"
```

**Shape B — `match` + `consumers`:**

```yaml
routes:
  - match:
      category: "agent"
      type: "*"                   # glob
      payload: { phase: "build" } # optional payload equality match
    consumers:
      - type: webhook
        target: agent-visual-formatter
```

A single event can match **multiple** rules and fan out to all their consumers.
Glob patterns (`fnmatch`) work on both the `event` string and the `category`/`type`
fields — e.g. `event: "schedule.*"` matches every `schedule.` event.

### Consumer types

| `type` | Aliases | What it does |
|--------|---------|--------------|
| `webhook` | `n8n_webhook` | HTTP POST the event. URL resolution order: `url` / `endpoint` → `url_env` (env var) → built from the registry as `${N8N_BASE_URL}/webhook/<target>`. |
| `conductor` | `conductor_state_update` | Writes the event into a nested `field` (dot-path, default `event_routing.last_event`) of a `conductor-state.json` located via `target_directory` or `$CONDUCTOR_STATE_DIR`. No-op if no directory is configured. |
| `direct` | `direct_action` | In-process action: `log_event` (log line) or `broadcast_ws` (push to WebSocket clients). Unknown actions are a logged no-op. |

### Delayed dispatch + dedup

A webhook (or any) consumer can debounce with `delay_seconds` and collapse duplicates
with `dedup_key` (a field name in the payload):

```yaml
- event: "session.end"
  handlers:
    - type: webhook
      target: "session-extraction"
      delay_seconds: 7200          # hold 2 hours
      dedup_key: session_id        # one pending delivery per session_id
```

Behavior (see `schedule_delayed_dispatch`): the first event for a
`(rule, consumer, dedup-value)` bucket opens a fixed window of `delay_seconds`. Later
events in the window **collapse into the same pending row** — latest payload wins,
`event_count` increments — **without extending the deadline**. After delivery, the
next event opens a fresh window. If `dedup_key` is absent, each event gets its own
window (keyed by `event_id`). If a delayed delivery fails, it goes to the DLQ.

### Hot reload

Edit any of the three YAML files and the router picks up the change within
`ROUTING_RELOAD_SECONDS` (default 30 s). To force an immediate reload:

```bash
curl -X POST http://localhost:8085/reload
# → {"reloaded":["routes"]}
```

## 4. Querying the routing log

Every routing decision is recorded. Query it:

```bash
# recent events (optionally filter by category or correlation_id)
curl "http://localhost:8085/events?category=agent&limit=20"

# one event by id
curl "http://localhost:8085/events/<event_id>"
```

Correlation IDs are propagated end-to-end, so you can trace a whole session:

```bash
curl "http://localhost:8085/events?correlation_id=<session-uuid>"
```

## 5. Working the dead-letter queue

Failed deliveries land in the DLQ (SQLite) and are retried automatically (bounded by
`DLQ_MAX_RETRIES`, default 3, on a `DLQ_RETRY_INTERVAL_SECONDS` cadence). You can also
inspect and replay manually:

```bash
# list DLQ entries (optionally by status: pending|retrying|exhausted|replayed)
curl "http://localhost:8085/dlq?status=pending"

# replay one entry
curl -X POST http://localhost:8085/dlq/<event_id>/replay

# bulk replay (optionally filter by category / since-timestamp)
curl -X POST "http://localhost:8085/dlq/replay?category=agent"
```

Entries past `DLQ_RETENTION_DAYS` (default 30) are purged automatically.

## 6. Workflow health

If you populate `workflow-registry.yaml` and feed health data (via the
`runtime/n8n-health-poller.py` or `bridge-daemon.py`), the router aggregates
per-workflow health against each workflow's SLA:

```bash
curl "http://localhost:8085/workflows"          # all workflows + aggregate
curl "http://localhost:8085/workflows/<name>"   # one workflow
```

Health data is **written** via the HMAC-authenticated `POST /workflows/{name}/health`
endpoint — see [ADMINISTRATOR.md](ADMINISTRATOR.md#workflow-health-write-endpoint).

## 7. The dashboard

Open `http://localhost:8085/` in a browser for a live view: metrics cards (events
received/routed, DLQ pending, P99 latency), a per-category chart, workflow health, and
recent routing decisions. It refreshes every 5 s and subscribes to the `/ws` WebSocket
for push updates.

## 8. Metrics & health endpoints

```bash
curl http://localhost:8085/health    # liveness + config-loaded flags + DLQ pending
curl http://localhost:8085/metrics   # counters, P99 latency, per-category, ws clients
```

## Endpoint reference

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/events` | Emit an event (fire-and-forget, `202`). |
| `POST` | `/events/sync` | Emit and wait for the routing decision. |
| `GET` | `/events` | Query the routing log (`category`, `correlation_id`, `limit`). |
| `GET` | `/events/{event_id}` | One routing-log entry. |
| `GET` | `/workflows` | Workflow health summary + aggregate. |
| `GET` | `/workflows/{name}` | One workflow's health. |
| `POST` | `/workflows/{name}/health` | Upsert health (HMAC-authenticated). |
| `GET` | `/dlq` | List DLQ entries. |
| `POST` | `/dlq/{event_id}/replay` | Replay one DLQ entry. |
| `POST` | `/dlq/replay` | Bulk replay. |
| `POST` | `/reload` | Force config hot-reload. |
| `GET` | `/metrics` | Runtime metrics. |
| `GET` | `/health` | Liveness. |
| `WS` | `/ws` | Live event stream. |
| `GET` | `/` | Dashboard. |

---

Apache-2.0 © 2026 bulletproofsoftware-ai. See [LICENSE](../LICENSE) and [NOTICE](../NOTICE).

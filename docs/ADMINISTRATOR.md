# Administrator guide — bulletproof-event-router

Operational reference for running, securing, and integrating the event router.

## Environment variables

### Core service (`app/main.py`)

| Var | Default | Purpose |
|-----|---------|---------|
| `EVENTS_DIR` | `~/.claude/events` | Directory holding `taxonomy.yaml`, `routing-rules.yaml`, `workflow-registry.yaml`. |
| `DLQ_PATH` | `$EVENTS_DIR/dead-letter-queue.sqlite` | SQLite file for the DLQ, routing log, workflow health, and delayed-dispatch tables. |
| `N8N_BASE_URL` | `http://localhost:5678` | Base URL used to build webhook targets from the registry (`${N8N_BASE_URL}/webhook/<target>`). |
| `WEBHOOK_TIMEOUT_SECONDS` | `10` | Per-webhook HTTP timeout. |
| `ROUTING_RELOAD_SECONDS` | `30` | Config mtime poll interval (hot reload). |
| `DLQ_RETENTION_DAYS` | `30` | Age after which DLQ entries are purged. |
| `DLQ_MAX_RETRIES` | `3` | Retry budget per DLQ entry before `exhausted`. |
| `DLQ_RETRY_INTERVAL_SECONDS` | `60` | DLQ retry-worker cadence. |
| `DLQ_BACKOFF_MS_BASE` | `2000` | Base for retry backoff. |
| `DELAYED_DISPATCH_POLL_SECONDS` | `30` | Delayed-dispatch worker cadence. |
| `CONDUCTOR_STATE_DIR` | (unset) | Fallback directory for `conductor` consumers that don't set `target_directory`. |
| `WORKFLOW_HEALTH_HMAC_SECRET` | (unset) | HMAC-SHA256 secret for `POST /workflows/{name}/health`. **Unset disables the endpoint** (returns 503). |

### Runtime bridge (`runtime/bridge-daemon.py`) — all optional

The bridge is off toward any target whose variable is blank. See
[`.env.example`](../.env.example) for the full annotated list. Highlights:

| Var | Effect if unset |
|-----|-----------------|
| `RUNTIME_SECURITY_URL` | No runtime-security session/threat mirror. |
| `DATA_PLANE_DSN` | No Postgres lineage mirror (the bridge's `psycopg2` connection is only opened when this is set). |
| `ECONOMICS_DSN` / `ECONOMICS_BASE_URL` | No agent-economics cost mirror. |
| `METRICS_SQLITE_PATH` | No metrics-engine backfill. |
| `N8N_API_KEY` | No n8n workflow-health sync. |
| `WORKFLOW_HEALTH_HMAC_SECRET` | Health POSTs are skipped (logged). |
| `NOTIFY_SCRIPT` | No Telegram heartbeat. Must resolve to an **executable file** (validated via `shutil.which`). |

## Security & trust model

**`POST /events` is unauthenticated by design.** The router is meant to run inside a
**localhost / private-mesh trust domain** where every producer is already trusted. The
example `routing-rules.yaml` documents this explicitly (CISO note F-8): if integrity
of emitted events is ever required, extend the existing HMAC pattern to `/events`.

Consequences to keep in mind:

- **Do not expose `:8085` to an untrusted network.** Anything that can reach the port
  can emit events and (via routing rules) trigger webhooks / state writes.
- **Payloads are business data.** The example marks `cost.recorded` as `direct`/
  `log_event` only (CISO note F-2) — a webhook handler would persist the full payload
  (cost, project_id, agent_id) into the DLQ on failure. Review data sensitivity before
  adding webhook handlers to sensitive categories.
- **The dashboard renders payloads.** Jinja2 auto-escapes HTML by default; the routing
  rules file carries a reminder to keep it that way.

### Workflow-health write endpoint

`POST /workflows/{name}/health` is the **one authenticated endpoint**. It requires:

1. `WORKFLOW_HEALTH_HMAC_SECRET` set on the server (else `503`).
2. Header `X-Workflow-Health-Signature: sha256=<hex>` (or bare `<hex>`), where
   `<hex> = HMAC_SHA256(secret, raw_request_body)`. Verified in **constant time**
   (`hmac.compare_digest`).
3. `name` must already exist in `workflow-registry.yaml` (guards against typos).

The `runtime/n8n-health-poller.py` and `runtime/bridge-daemon.py` both implement this
signing. Example from the poller:

```python
sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
headers = {"X-Workflow-Health-Signature": f"sha256={sig}"}
```

### Container hardening

The image runs as non-root `appuser` (uid 10001); writable paths (`/events`,
`/app/data`) are pre-created and `chown`ed to that user. A `/health` HEALTHCHECK ships
in the Dockerfile and compose file.

## The runtime layer — operating the producers

All `runtime/` scripts are optional. Overview in [OVERVIEW.md](OVERVIEW.md#the-runtime-layer);
operational notes below.

### `hook-dispatch.py`

Reads a Claude Code hook payload from stdin and POSTs a derived event. It maps hooks
to taxonomy events: `SessionStart → session.start`, `Stop`/`SessionEnd → session.end`,
`UserPromptSubmit → session.user_prompt`, `PreToolUse → agent.dispatch`,
`PostToolUse → agent.complete|fail`, `Notification → session.notification`. It also
optionally marks the session active in agent-economics (only if an
`ECONOMICS_TOKEN_PATH` token file exists). Fire-and-forget, always exits 0 — it can
never block Claude Code.

### `bridge-daemon.py`

A continuous 5-second loop that tails the router's `routing_log` (read-only) and
mirrors events downstream, plus syncs n8n workflow health back into the router. It is
**idempotent** (resumes from a cursor in `BRIDGE_STATE`) and **fails soft** — a dead
downstream is logged and skipped, never fatal. Requires `psycopg2` **only** when
`DATA_PLANE_DSN`/`ECONOMICS_DSN` are set. Launch it via `launch-bridge.sh`, which
injects the n8n API key and HMAC secret from the environment at start.

### `n8n-health-poller.py`

A standalone "observe from outside" poller: reads n8n execution history over its REST
API, aggregates 24h health per registry workflow, and pushes each result to the
router's HMAC endpoint. Exit codes: `0` = all polled+pushed (or n8n unreachable and
skipped), `1` = partial failure, `2` = config error. It probes reachability first and
**skips gracefully** (exit 0) when n8n is unreachable (e.g. VPN down), unless
`--no-skip-unreachable` is passed. Config comes from `POLLER_ENV_FILE` (an env file
with `N8N_API_KEY` and `WORKFLOW_HEALTH_HMAC_SECRET`) and `POLLER_REGISTRY`.

> Note: `bridge-daemon.py` also contains its own n8n health syncer. If you run the
> standalone poller, you generally do **not** also need the bridge's syncer — pick one
> writer for the workflow-health table to avoid redundant load.

## Operations

### Persistence & backup

Everything durable lives in the single SQLite file at `DLQ_PATH` (DLQ, routing log,
workflow health, delayed dispatch). Back it up by copying that file (WAL mode is on;
checkpoint or copy the `-wal`/`-shm` siblings too, or stop the service first). In
Docker, mount a volume at `/events`.

### Scaling notes

- The service runs with `--workers 1`. Metrics and the WebSocket client set are
  **in-process**, so multiple workers would each keep separate metrics/ws state. Keep
  a single worker unless you add shared state.
- Throughput target (per the source's REQ notes) is ~100 events/sec/category; routing
  is in-process and cheap. The bottleneck is downstream webhook latency, which is why
  slow deliveries can be pushed to delayed dispatch or absorbed by the DLQ.

### Log lines

The service logs every routing decision and config reload at `INFO`. Missing configs
log a `WARNING` (and the service runs with empty defaults — a missing taxonomy means
**no category validation**, which you usually do not want in production). The bridge
writes to `/tmp/observability-bridge.log`.

## Known gaps / things to check

- **A missing `taxonomy.yaml` fails open** (accepts all events). Ship a taxonomy in
  production so unknown categories are rejected.
- **`/events` has no auth** (see trust model). Only deploy behind a trusted boundary.
- The `dead_letter:` block in the example `routing-rules.yaml` (with
  `${CLAUDE_PLUGIN_ROOT}` paths) documents an earlier plugin-embedded DLQ design; the
  running service uses `DLQ_PATH` instead. It is descriptive config, not read by
  `app/main.py`.

---

Apache-2.0 © 2026 bulletproofsoftware-ai. See [LICENSE](../LICENSE) and [NOTICE](../NOTICE).

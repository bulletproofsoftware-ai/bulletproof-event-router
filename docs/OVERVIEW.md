# Overview — bulletproof-event-router

`bulletproof-event-router` is a small FastAPI service that gives a multi-agent
system a single place to **emit events** and have them **validated, routed, and
dispatched** to downstream consumers. It is the "event backbone" that lets
otherwise-isolated pieces (Claude Code hooks, n8n workflows, conductor state files,
security/lineage sidecars) react to each other without point-to-point wiring.

## The problem it solves

In a system built from many independent loops — hooks that fire on session events,
n8n workflows on schedules, agents dispatched by an orchestrator — every producer
would otherwise need to know every consumer's URL, retry policy, and health state.
The router inverts that: producers emit a typed event once, and the router fans it
out to whatever consumers a declarative rule file says should receive it.

## What it does

1. **Receive** — `POST /events` accepts an event (`category`, `type`, `source`,
   `payload`). By default it is fire-and-forget: the caller gets a `202` immediately
   and routing happens in the background.
2. **Validate** — the event's `category` is checked against a versioned taxonomy
   (`taxonomy.yaml`). Unknown categories are rejected; unknown *types* within a known
   category are accepted (the taxonomy explicitly leaves "room for growth").
3. **Route** — the event is matched against glob-pattern rules in
   `routing-rules.yaml`. A single event can match multiple rules and fan out to
   multiple consumers.
4. **Dispatch** — each matched consumer is delivered to by type:
   - `webhook` / `n8n_webhook` → HTTP POST to an n8n webhook (or any URL)
   - `conductor` / `conductor_state_update` → writes a nested field into a
     `conductor-state.json` file
   - `direct` / `direct_action` → in-process action (log, WebSocket broadcast)
5. **Recover** — a failed delivery lands in a SQLite **dead-letter queue** (DLQ)
   with automatic retry (bounded, with backoff) and manual/bulk **replay**.
6. **Observe** — every routing decision is logged to SQLite; workflow health is
   tracked against per-workflow SLAs; correlation IDs are propagated end-to-end;
   a small live dashboard is served at `/`.

## Architecture at a glance

```
 producers                    event-router (:8085)                 consumers
 ─────────                    ────────────────────                 ─────────
 Claude Code hooks ─┐         ┌───────────────────────┐        ┌─ n8n webhooks
   (hook-dispatch)  │  POST   │ validate (taxonomy)   │  POST  │
 emit-event.sh ─────┼───────► │ match  (routing-rules)├────────┼─ conductor-state.json
 any HTTP client ───┘ /events │ dispatch (3 types)    │        │
                              │ DLQ + retry (sqlite)  │  logs  └─ direct (log / ws)
                              │ routing_log (sqlite)  ├──────────► dashboard "/"
                              │ workflow_health       │           + WebSocket "/ws"
                              └──────────┬────────────┘
                                         │ routing_log (read-only)
                              ┌──────────▼────────────┐
                              │ runtime/ producers &  │  (all downstream mirrors
                              │ bridge-daemon (opt-in)│   OFF unless env-configured)
                              └───────────────────────┘
```

## Configuration model

The service reads three YAML files from `EVENTS_DIR` (default `~/.claude/events`;
working examples ship in [`config.example/`](../config.example/)):

| File | Purpose |
|------|---------|
| `taxonomy.yaml` | Event categories + per-event payload schemas. 11 categories ship in the example (`session`, `memory`, `agent`, `governance`, `security`, `infra`, `schedule`, `git`, `external`, `recovery`, `cost`). |
| `routing-rules.yaml` | Event → consumer routing with glob patterns, fan-out, delayed dispatch, and dedup. |
| `workflow-registry.yaml` | Catalog of known n8n workflows with SLA / schedule / priority metadata, used for health tracking. |

All three are **hot-reloaded** without a restart: a background watcher polls their
mtimes every `ROUTING_RELOAD_SECONDS` (default 30s), and `POST /reload` forces an
immediate reload.

## The `runtime/` layer

The router receives events — something has to *produce* them. `runtime/` bundles the
optional producers and mirrors that make the system turnkey:

| Script | Role |
|--------|------|
| `emit-event.sh` | Fire-and-forget CLI emitter. Never blocks the caller. |
| `hook-dispatch.py` | Reads a Claude Code hook payload from stdin and POSTs a derived event. Also (optionally) marks the session active in agent-economics. |
| `bridge-daemon.py` | Tails the router's `routing_log` and mirrors events into downstream targets (runtime-security, a data-plane Postgres, metrics, economics) **and** syncs n8n workflow health back into the router. Every downstream is opt-in via env var. |
| `launch-bridge.sh` | Wrapper that pulls the n8n API key + HMAC secret from the environment and execs the bridge. |
| `n8n-health-poller.py` | Standalone "observe from outside" health poller — reads n8n execution history and pushes per-workflow health to the router's HMAC-authenticated endpoint. |

**Nothing in `runtime/` does anything harmful out of the box.** Each mirror is
skipped unless its URL/DSN env var is set. No credentials are shipped; see
[`.env.example`](../.env.example).

## What this repo is *not*

- It is **not** a message broker with durable ordered topics — it is a lightweight
  HTTP router with a SQLite DLQ. There is no Kafka/RabbitMQ.
- `POST /events` is **unauthenticated** by design: it is meant to run inside a
  localhost trust domain. Only the workflow-health write endpoint is authenticated
  (HMAC-SHA256). See [ADMINISTRATOR.md](ADMINISTRATOR.md) for the trust model.
- The provider-mirror integrations in `bridge-daemon.py` reference specific sibling
  services (runtime-security, data-plane, agent-economics). Those services are **not**
  part of this repo; the bridge is a no-op toward any target you have not configured.

## Where to go next

- [INSTALL.md](INSTALL.md) — run it locally or in Docker.
- [HOW-TO-USE.md](HOW-TO-USE.md) — emit events, write routing rules, work the DLQ, read the taxonomy.
- [ADMINISTRATOR.md](ADMINISTRATOR.md) — environment variables, endpoints, security/trust model, operations.
- [SBOM.md](SBOM.md) — dependency inventory.
- [scan/scan-report.md](scan/scan-report.md) — security scan results.

---

Apache-2.0 © 2026 bulletproofsoftware-ai. See [LICENSE](../LICENSE) and [NOTICE](../NOTICE).

# Bulletproof Event Router: Architecture and Operational Briefing

The bulletproof-event-router is a lightweight FastAPI-based service designed to serve as the "event backbone" for multi-agent systems. It centralizes the validation, routing, and dispatching of events, allowing disparate components—such as Claude Code hooks, n8n workflows, and orchestrators—to interact without direct point-to-point integration.

## Executive Summary

The router provides a unified entry point for event emission, utilizing a declarative configuration model to manage system-wide reactions. It is characterized by its "fire-and-forget" default behavior, a SQLite-backed dead-letter queue (DLQ) for resilience, and a robust taxonomy for data integrity. Designed to operate within a trusted private network, the service prioritizes low-latency routing (targeting ~100 events/sec/category) while offering deep observability through end-to-end correlation IDs and a live dashboard.

## Core Event Taxonomy and Routing Model

The router’s behavior is governed by three primary YAML configuration files stored in the `EVENTS_DIR`: `taxonomy.yaml`, `routing-rules.yaml`, and `workflow-registry.yaml`.

### Event Taxonomy
The `taxonomy.yaml` file defines the valid categories for events. The system ships with 11 predefined categories:
*   **Session/Agent:** `session` (hooks), `agent` (conductor/orchestrator).
*   **Intelligence:** `memory` (MCP tools/consolidation).
*   **Governance/Security:** `governance` (audit/violations), `security` (threats/lockdowns).
*   **Infrastructure/External:** `infra` (system monitors), `git` (hooks), `external` (agent gateways).
*   **Operations:** `schedule` (cron), `recovery` (engine attempts), `cost` (metering).

**Validation Behavior:**
*   **Category Level:** The router rejects events with unknown categories. Categories must not contain dots.
*   **Type Level:** Unknown *types* within a known category are accepted to provide "room for growth."
*   **Fail-Open Condition:** If no taxonomy is loaded, the router logs a warning and accepts all events without validation.

### Routing and Dispatch
The `routing-rules.yaml` file maps events to consumers using glob patterns (fnmatch). The system supports event "fan-out," where a single event can match multiple rules and trigger multiple consumers.

| Rule Shape | Description |
| :--- | :--- |
| **Shape A** | Maps a specific event to a list of handlers. |
| **Shape B** | Uses a "match" block to map events to various consumers. |

The router supports "hot reloading," polling configuration files every 30 seconds (`ROUTING_RELOAD_SECONDS`) to update rules without a service restart.

## Consumer Types and Delivery Mechanics

The router dispatches events to three primary consumer types:

1.  **Webhook (`webhook`, `n8n_webhook`):** Performs an HTTP POST. URL resolution follows a specific hierarchy: explicit URL/endpoint > environment variable (`url_env`) > registry-built URL (using `${N8N_BASE_URL}/webhook/<target>`).
2.  **Conductor (`conductor`, `conductor_state_update`):** Writes the event to a nested field in a `conductor-state.json` file. This is used for updating orchestrator state.
3.  **Direct (`direct`, `direct_action`):** Executes in-process actions, specifically `log_event` (writing to logs) or `broadcast_ws` (pushing to WebSocket dashboard clients).

### Delayed Dispatch and Deduplication
Consumers can utilize `delay_seconds` to debounce events and `dedup_key` to collapse duplicates. When a `dedup_key` is provided, events within the delay window are collapsed into a single pending row where the latest payload wins and the event count increments.

## Dead-Letter Queue (DLQ) and Replay

Resilience is managed through a SQLite-backed DLQ located at `DLQ_PATH`. 

*   **Automatic Retry:** Failed deliveries land in the DLQ and are retried based on `DLQ_MAX_RETRIES` (default 3) and `DLQ_RETRY_INTERVAL_SECONDS` (default 60).
*   **Backoff:** Retries utilize a base backoff period (`DLQ_BACKOFF_MS_BASE`).
*   **Retention:** Entries are automatically purged after `DLQ_RETENTION_DAYS` (default 30).
*   **Manual Replay:** Administrators can trigger manual or bulk replays via POST requests to `/dlq/{event_id}/replay` or `/dlq/replay`.

## Workflow Health and SLA Monitoring

The router tracks the health of n8n workflows against defined Service Level Agreements (SLAs) using the `workflow-registry.yaml`. 

*   **Aggregation:** The system aggregates 24-hour health data per registry workflow.
*   **Authenticated Writes:** The `/workflows/{name}/health` endpoint is the only authenticated write point in the system, requiring an HMAC-SHA256 signature verified in constant time.
*   **Data Sources:** Health data is typically fed into the router via the `runtime/n8n-health-poller.py` or the `bridge-daemon.py`.

## The Runtime Producer Layer

The `runtime/` directory contains optional scripts that facilitate event production and downstream mirroring:

*   **`hook-dispatch.py`:** Maps Claude Code hooks (e.g., `SessionStart`, `PreToolUse`) to taxonomy events. It is designed to be fire-and-forget to ensure it never blocks the primary application.
*   **`bridge-daemon.py`:** A 5-second loop that tails the router's `routing_log` and mirrors events to downstream targets like Postgres (lineage), runtime-security (threat mirrors), or agent-economics (cost mirrors). It is idempotent and uses a cursor in `BRIDGE_STATE`.
*   **`n8n-health-poller.py`:** A standalone poller that reads n8n execution history and pushes results to the router. It is designed to skip gracefully if n8n is unreachable (e.g., VPN issues).

## Security and Trust Model

The router is built on a **localhost / private-mesh trust domain** assumption.

*   **Authentication:** `POST /events` is unauthenticated by design. Access must be restricted to trusted networks to prevent unauthorized event emission.
*   **Workflow Health Security:** Requires `WORKFLOW_HEALTH_HMAC_SECRET`. If unset, the health endpoint returns a 503.
*   **Container Hardening:** The Docker image runs as a non-root `appuser` (UID 10001).
*   **Data Sensitivity:** Since payloads are logged and stored in the DLQ, administrators are cautioned to review data sensitivity before adding webhook handlers to sensitive categories (e.g., `cost.recorded`).

## Important Quotes and Technical Annotations

> "The router is meant to run inside a localhost / private-mesh trust domain where every producer is already trusted." — *Administrator Guide (CISO Note F-8)*

> "A missing taxonomy.yaml fails open (accepts all events). Ship a taxonomy in production so unknown categories are rejected." — *Operational Gap Note*

> "The bottleneck is downstream webhook latency, which is why slow deliveries can be pushed to delayed dispatch or absorbed by the DLQ." — *Scaling Notes*

> "Jinja2 auto-escapes HTML by default; the routing rules file carries a reminder to keep it that way." — *Security/Trust Model*

## Actionable Insights for Administrators

*   **Deployment Strategy:** Do not expose port 8085 to untrusted networks. Ensure the service is deployed behind a secure boundary.
*   **Configuration Management:** Always provide a `taxonomy.yaml` in production to prevent the service from "failing open" and accepting unvalidated event categories.
*   **High Availability/Scaling:** Run the service with `--workers 1`. Because metrics and WebSocket states are in-process, multiple workers will cause fragmented state unless shared state management is added.
*   **Health Monitoring Conflict:** If using both `bridge-daemon.py` and `n8n-health-poller.py`, choose only one to write health data to avoid redundant load on the `workflow-health` table.
*   **Persistence:** Ensure the directory defined in `DLQ_PATH` is backed up regularly. In Docker environments, mount a persistent volume at `/events` to ensure the SQLite database (which contains the DLQ, routing logs, and health data) survives container restarts.
*   **Performance Tuning:** Adjust `WEBHOOK_TIMEOUT_SECONDS` (default 10) and `ROUTING_RELOAD_SECONDS` based on the specific latency of your downstream consumers and how frequently rules change.
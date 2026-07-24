# Install — bulletproof-event-router

The router is a single FastAPI service with six pinned Python dependencies. You can
run it directly with `uvicorn`, via Docker, or via Docker Compose (which also wires
in the optional bridge).

## Requirements

- **Python 3.12** (the Docker image is `python:3.12-slim`).
- The six runtime dependencies in [`requirements.txt`](../requirements.txt):
  `fastapi`, `uvicorn[standard]`, `httpx`, `pyyaml`, `jinja2`, `pydantic`.
- Optional, only for the `runtime/bridge-daemon.py` mirror: `psycopg2` (Postgres
  client) — **not** in `requirements.txt` because the core service does not need it.
- Optional: Docker / Docker Compose.

## 1. Get the configuration files in place

The service reads three YAML files from `EVENTS_DIR`. Working examples ship in
[`config.example/`](../config.example/). Copy them somewhere and point the service at
that directory:

```bash
mkdir -p ~/.config/event-router
cp config.example/*.yaml ~/.config/event-router/
export EVENTS_DIR=~/.config/event-router
```

If `EVENTS_DIR` is unset it defaults to `~/.claude/events`. If a config file is
missing the service logs a warning and runs with empty defaults (a missing taxonomy
means events are accepted without category validation — see ADMINISTRATOR.md).

## 2a. Run directly with uvicorn

```bash
pip install -r requirements.txt
export EVENTS_DIR=~/.config/event-router
uvicorn app.main:app --host 0.0.0.0 --port 8085
```

Confirm it is up:

```bash
curl -s http://localhost:8085/health | python3 -m json.tool
# → {"status":"ok", "taxonomy_loaded":true, "routes_loaded":true, ...}
```

## 2b. Run with Docker

The [`Dockerfile`](../Dockerfile) builds a minimal image that runs as a **non-root**
user (`appuser`, uid 10001) and includes a `/health` HEALTHCHECK.

```bash
docker build -t bulletproof-event-router .
docker run -p 8085:8085 \
  -e EVENTS_DIR=/config \
  -v "$PWD/config.example:/config:ro" \
  bulletproof-event-router
```

The DLQ SQLite file is written under `/events` (owned by `appuser`). Mount a volume
there if you want the DLQ to survive container restarts:

```bash
docker run -p 8085:8085 \
  -e EVENTS_DIR=/config \
  -v "$PWD/config.example:/config:ro" \
  -v event-router-data:/events \
  bulletproof-event-router
```

## 2c. Run with Docker Compose

[`docker-compose.yml`](../docker-compose.yml) builds the router, loads `.env`, mounts
`config.example/` read-only, and persists `./data`. Copy the env template first:

```bash
cp .env.example .env      # edit as needed; all downstream mirrors default OFF
docker compose up -d
docker compose logs -f event-router
```

The exposed host port is `${EVENT_ROUTER_PORT:-8085}`.

## 3. Emit a first event

```bash
curl -X POST http://localhost:8085/events \
  -H 'Content-Type: application/json' \
  -d '{"category":"session","type":"start","source":"install-test","payload":{"session_id":"abc"}}'
```

With the example configs loaded, `session.start` matches a rule that logs the event
and (optionally) updates a conductor-state file. A synchronous variant for testing
returns the full routing decision:

```bash
curl -X POST http://localhost:8085/events/sync \
  -H 'Content-Type: application/json' \
  -d '{"category":"session","type":"start","source":"install-test","payload":{"session_id":"abc"}}'
# → {"status":"routed","matched_rules":["session.start"],"consumers_targeted":2,...}
```

## 4. (Optional) Run the tests

The repo ships a pytest suite for the delayed-dispatch / dedup logic:

```bash
pip install pytest
pytest -q tests/
# → 9 passed
```

## 5. (Optional) Wire in the runtime producers

The [`runtime/`](../runtime/) scripts are opt-in. The simplest producer is the CLI
emitter, usable from any hook or script:

```bash
runtime/emit-event.sh session start '{"session_id":"abc"}'
```

To route Claude Code hooks through the router, pipe the hook payload into
`hook-dispatch.py`. The bridge daemon and n8n health poller are described in
[ADMINISTRATOR.md](ADMINISTRATOR.md) — they require additional env configuration and
(for the bridge) `psycopg2`.

## Uninstall

```bash
docker compose down                 # if using compose
docker rmi bulletproof-event-router # remove the image
rm -rf ~/.config/event-router       # remove copied configs
```

The DLQ SQLite file lives wherever `DLQ_PATH` / the mounted volume pointed; delete it
to clear queued/failed events.

---

Apache-2.0 © 2026 bulletproofsoftware-ai. See [LICENSE](../LICENSE) and [NOTICE](../NOTICE).

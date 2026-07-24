# bulletproof-event-router

**A central event dispatcher: receive events, match routing rules, fan out to n8n / state / actions.**

`bulletproof-event-router` is a FastAPI service that gives a multi-agent system a single
place to emit events and have them routed. It validates events against a taxonomy,
matches them against glob-pattern routing rules, and dispatches to three consumer types
— n8n webhooks, state updates, and direct actions — with a dead-letter queue and replay
for failed deliveries.

## What it does

- **Receive** events via `POST /events` (fire-and-forget by default).
- **Validate** against a versioned event taxonomy (`taxonomy.yaml`).
- **Route** with glob patterns (`routing-rules.yaml`), hot-reloaded without restart.
- **Dispatch** to n8n webhooks, conductor state, or direct actions.
- **Recover** — failed deliveries land in a SQLite dead-letter queue with replay.
- **Observe** — logs every routing decision, tracks workflow health against SLA,
  propagates correlation IDs, and serves a small dashboard.

## Configuration

The service reads three YAML files from `EVENTS_DIR`:

| File | Purpose |
|------|---------|
| `taxonomy.yaml` | Event categories + payload schemas |
| `routing-rules.yaml` | Event → consumer routing (glob patterns) |
| `workflow-registry.yaml` | Known n8n workflows + SLAs |

Working examples ship in [`config.example/`](config.example/). Copy them to your events
directory and point the service at it:

```bash
mkdir -p ~/.config/event-router
cp config.example/*.yaml ~/.config/event-router/
export EVENTS_DIR=~/.config/event-router
```

`EVENTS_DIR` defaults to `~/.claude/events` if unset. `DLQ_PATH` overrides the
dead-letter-queue location.

## Run it

```bash
pip install -r requirements.txt
export EVENTS_DIR=~/.config/event-router   # holding the 3 yaml files
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Or via Docker:

```bash
docker build -t bulletproof-event-router .
docker run -p 8080:8080 -e EVENTS_DIR=/config -v $PWD/config.example:/config bulletproof-event-router
```

Emit an event:

```bash
curl -X POST http://localhost:8080/events \
  -H 'Content-Type: application/json' \
  -d '{"event":"session.start","payload":{"session_id":"abc"}}'
```

## Environment

| Var | Default | Purpose |
|-----|---------|---------|
| `EVENTS_DIR` | `~/.claude/events` | Directory holding the 3 YAML configs |
| `DLQ_PATH` | `$EVENTS_DIR/dead-letter-queue.sqlite` | Dead-letter queue file |
| `N8N_BASE_URL` | `http://localhost:5678` | n8n base for webhook dispatch |

## License

Apache-2.0 © 2026 bulletproofsoftware-ai. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

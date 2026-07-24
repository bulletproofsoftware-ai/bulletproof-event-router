#!/usr/bin/env bash
# Wrapper that pulls N8N_API_KEY from the running n8n container env at start,
# then execs the bridge daemon. Keeps the secret out of scheduler config files.
#
# Downstream mirrors are opt-in: this wrapper deliberately does NOT set
# DATA_PLANE_DSN or N8N_BASE_URL. Export them yourself to enable those
# integrations; bridge-daemon.py defaults DATA_PLANE_DSN to empty (mirror
# disabled) and N8N_BASE_URL to http://localhost:5678.
set -u

VENV_PY="${VENV_PY:-python3}"
BRIDGE="${BRIDGE:-$(dirname "$0")/bridge-daemon.py}"

# Pull n8n API key from local container; tolerate failure (bridge runs without it, just no workflow sync).
N8N_API_KEY=""
if command -v docker >/dev/null 2>&1; then
    N8N_API_KEY=$(docker exec n8n env 2>/dev/null | awk -F= '/^N8N_API_KEY=/{print substr($0,index($0,"=")+1)}')
fi

export N8N_API_KEY

# Pull HMAC secret from the docker-compose .env where event-router reads it from
WORKFLOW_HEALTH_HMAC_SECRET=""
ENV_FILE="${BPM_ENV_FILE:-$(dirname "$0")/../.env}"
if [ -r "$ENV_FILE" ]; then
    WORKFLOW_HEALTH_HMAC_SECRET=$(awk -F= '/^WORKFLOW_HEALTH_HMAC_SECRET=/{print substr($0,index($0,"=")+1)}' "$ENV_FILE")
fi
export WORKFLOW_HEALTH_HMAC_SECRET
export EVENT_ROUTER_BASE_URL="${EVENT_ROUTER_BASE_URL:-http://127.0.0.1:8085}"

exec "$VENV_PY" "$BRIDGE"

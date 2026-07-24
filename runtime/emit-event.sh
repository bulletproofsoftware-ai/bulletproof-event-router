#!/usr/bin/env bash
# Fire-and-forget POST to event-router (:8085). Never blocks the hook.
# Usage: emit-event.sh <category> <type> <payload-json> [correlation_id]
#
# Reads JSON hook payload from stdin if no args; falls back to env vars.

set -u
ROUTER="${EVENT_ROUTER_URL:-http://127.0.0.1:8085/events}"
CATEGORY="${1:-}"
TYPE="${2:-}"
PAYLOAD="${3:-{}}"
CORR="${4:-${CLAUDE_CORRELATION_ID:-}}"
SOURCE="${EVENT_SOURCE:-claude-code}"
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

[ -z "$CATEGORY" ] || [ -z "$TYPE" ] && exit 0

if command -v jq >/dev/null 2>&1; then
  BODY=$(jq -cn \
    --arg category "$CATEGORY" \
    --arg type "$TYPE" \
    --argjson payload "$PAYLOAD" \
    --arg source "$SOURCE" \
    --arg timestamp "$TS" \
    --arg corr "$CORR" \
    '{category:$category, type:$type, payload:$payload, source:$source, timestamp:$timestamp}
     + (if $corr == "" then {} else {correlation_id:$corr} end)')
else
  # jq not installed — assemble minimal JSON. PAYLOAD must already be valid JSON.
  CORR_FIELD=""
  [ -n "$CORR" ] && CORR_FIELD=",\"correlation_id\":\"$CORR\""
  BODY="{\"category\":\"$CATEGORY\",\"type\":\"$TYPE\",\"payload\":$PAYLOAD,\"source\":\"$SOURCE\",\"timestamp\":\"$TS\"$CORR_FIELD}"
fi

curl -s -o /dev/null -m 2 -X POST "$ROUTER" \
  -H 'Content-Type: application/json' \
  -d "$BODY" 2>/dev/null &
disown 2>/dev/null
exit 0

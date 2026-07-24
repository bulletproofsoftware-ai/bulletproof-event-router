#!/usr/bin/env python3
"""Read a Claude Code hook payload from stdin, derive an event, POST to event-router.

Hook payload examples (Claude Code documents these in HOOK_EVENT_* envvars
and on stdin as JSON):
  - SessionStart: {session_id, cwd, hook_event_name, source}
  - UserPromptSubmit: {session_id, cwd, hook_event_name, prompt}
  - PreToolUse / PostToolUse: {session_id, cwd, hook_event_name, tool_name, tool_input, tool_response?}
  - Stop / SessionEnd: {session_id, cwd, hook_event_name, ...}

Fire-and-forget — short timeout, swallows all errors, exit 0 always.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

ROUTER_URL = os.environ.get("EVENT_ROUTER_URL", "http://127.0.0.1:8085/events")
SOURCE = "claude-code-hook"
TIMEOUT_SEC = 1.5

# REQ-057: optionally mark this session as an active agent in agent-economics so
# /economics/live's active_agents reflects real, live Claude Code sessions.
# Fire-and-forget, same as the event-router post above — never blocks the hook.
#
# DISABLED BY DEFAULT: both values are empty unless you set them. Set
# ECONOMICS_EVENTS_URL to your agent-economics events endpoint and
# ECONOMICS_TOKEN_PATH to a file containing the bearer token to enable it.
ECONOMICS_EVENTS_URL = os.environ.get("ECONOMICS_EVENTS_URL", "")
ECONOMICS_TOKEN_PATH = os.environ.get("ECONOMICS_TOKEN_PATH", "")
_economics_token_cache: dict = {}


def _economics_token() -> str | None:
    if "token" in _economics_token_cache:
        return _economics_token_cache["token"]
    if not ECONOMICS_TOKEN_PATH:
        _economics_token_cache["token"] = None
        return None
    try:
        with open(ECONOMICS_TOKEN_PATH, encoding="utf-8") as f:
            token = f.read().strip() or None
    except OSError:
        token = None
    _economics_token_cache["token"] = token
    return token


def _mark_active(session_id: str, duration_ms: int = 0) -> None:
    if not session_id or not ECONOMICS_EVENTS_URL:
        return
    token = _economics_token()
    if not token:
        return
    body = json.dumps({
        "agent_id": f"claude-code:{session_id[:12]}",
        "session_id": session_id,
        "project_id": "claude-code-session",
        "model": "claude-code-hook-presence",
        "routed_tier": "sonnet",
        "event_type": "tool_use",
        "input_tokens": 0,
        "output_tokens": 0,
        "latency_ms": duration_ms,
    }).encode()
    req = urllib.request.Request(
        ECONOMICS_EVENTS_URL,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT_SEC).read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        pass  # fire-and-forget


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _post(category: str, etype: str, payload: dict, correlation_id: str | None) -> None:
    body = {
        "category": category,
        "type": etype,
        "payload": payload,
        "source": SOURCE,
        "timestamp": _now(),
    }
    if correlation_id:
        body["correlation_id"] = correlation_id
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        ROUTER_URL, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT_SEC).read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        pass  # fire-and-forget


def _len_safe(v) -> int:
    try:
        return len(v)
    except Exception:
        return 0


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0

    hook = payload.get("hook_event_name") or os.environ.get("CLAUDE_HOOK_EVENT", "")
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or ""
    correlation_id = session_id or None

    # Route hook → (category, type) in our taxonomy.
    if hook == "SessionStart":
        _post(
            "session",
            "start",
            {
                "session_id": session_id,
                "working_directory": cwd,
                "source": payload.get("source") or "unknown",
            },
            correlation_id,
        )
    elif hook in ("Stop", "SessionEnd"):
        _post(
            "session",
            "end",
            {
                "session_id": session_id,
                "reason": payload.get("reason") or hook,
            },
            correlation_id,
        )
    elif hook == "UserPromptSubmit":
        prompt = payload.get("prompt") or ""
        _post(
            "session",
            "user_prompt",
            {
                "session_id": session_id,
                "prompt_length": _len_safe(prompt),
                "prompt_preview": prompt[:120],
            },
            correlation_id,
        )
    elif hook == "PreToolUse":
        tool = payload.get("tool_name") or ""
        tool_input = payload.get("tool_input") or {}
        # Treat each tool invocation as an agent.dispatch
        _post(
            "agent",
            "dispatch",
            {
                "agent": tool,
                "tool_name": tool,
                "session_id": session_id,
                "input_summary": _summarize_input(tool, tool_input),
                "dispatch_ts": _now(),
            },
            correlation_id,
        )
    elif hook == "PostToolUse":
        tool = payload.get("tool_name") or ""
        resp = payload.get("tool_response") or {}
        success = _infer_success(tool, resp)
        etype = "complete" if success else "fail"
        duration_ms = payload.get("duration_ms") or 0
        _post(
            "agent",
            etype,
            {
                "agent": tool,
                "tool_name": tool,
                "session_id": session_id,
                "duration_ms": duration_ms,
                "complete_ts": _now(),
                "exit_code": resp.get("exitCode") if isinstance(resp, dict) else None,
            },
            correlation_id,
        )
        _mark_active(session_id, duration_ms)
    elif hook == "Notification":
        _post(
            "session",
            "notification",
            {"session_id": session_id, "message": (payload.get("message") or "")[:200]},
            correlation_id,
        )

    return 0


def _summarize_input(tool: str, tool_input: dict) -> str:
    if not isinstance(tool_input, dict):
        return ""
    if tool == "Bash":
        return str(tool_input.get("command", ""))[:160]
    if tool in ("Read", "Edit", "Write"):
        return str(tool_input.get("file_path", ""))[:160]
    if tool == "Agent":
        return str(tool_input.get("description", ""))[:160]
    return ""


def _infer_success(tool: str, resp) -> bool:
    if not isinstance(resp, dict):
        return True
    if "error" in resp and resp.get("error"):
        return False
    if "exitCode" in resp and isinstance(resp["exitCode"], int):
        return resp["exitCode"] == 0
    if "isError" in resp:
        return not bool(resp["isError"])
    return True


if __name__ == "__main__":
    start = time.time()
    rc = main()
    # Always exit 0 — hook must never block Claude.
    sys.exit(0 if (time.time() - start) < 10 else 0)

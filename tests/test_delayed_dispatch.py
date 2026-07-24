"""Tests for delayed dispatch with dedup (delay_seconds / dedup_key rule fields)."""

import asyncio
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def app_main(tmp_path, monkeypatch):
    """Import app.main with an isolated SQLite db."""
    monkeypatch.setenv("DLQ_PATH", str(tmp_path / "test-dlq.sqlite"))
    monkeypatch.setenv("EVENTS_DIR", str(tmp_path))  # no configs — empty defaults
    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]
    from app import main
    main.db_init()
    return main


def make_event(session_id: str, note: str = "") -> dict:
    return {
        "event_id": f"evt-{session_id}-{note}",
        "category": "session",
        "type": "end",
        "payload": {"session_id": session_id, "note": note},
        "source": "test",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "correlation_id": "corr-1",
    }


CONSUMER = {"type": "webhook", "target": "session-extraction", "delay_seconds": 7200, "dedup_key": "session_id"}


def rows(main):
    with closing(main.db_connect()) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM delayed_dispatch").fetchall()]


class TestScheduleDedup:
    def test_same_session_collapses_to_one_row(self, app_main):
        app_main.schedule_delayed_dispatch(CONSUMER, make_event("s1", "first"), "session.end")
        app_main.schedule_delayed_dispatch(CONSUMER, make_event("s1", "second"), "session.end")
        r = rows(app_main)
        assert len(r) == 1
        assert r[0]["event_count"] == 2
        assert json.loads(r[0]["event_json"])["payload"]["note"] == "second"  # latest wins
        assert r[0]["status"] == "pending"

    def test_distinct_sessions_get_distinct_windows(self, app_main):
        app_main.schedule_delayed_dispatch(CONSUMER, make_event("s1"), "session.end")
        app_main.schedule_delayed_dispatch(CONSUMER, make_event("s2"), "session.end")
        assert len(rows(app_main)) == 2

    def test_window_deadline_not_extended_by_later_events(self, app_main):
        app_main.schedule_delayed_dispatch(CONSUMER, make_event("s1", "first"), "session.end")
        due_before = rows(app_main)[0]["due_at"]
        app_main.schedule_delayed_dispatch(CONSUMER, make_event("s1", "second"), "session.end")
        assert rows(app_main)[0]["due_at"] == due_before

    def test_missing_dedup_value_falls_back_to_event_id(self, app_main):
        ev1 = make_event("sX", "a"); ev1["payload"] = {}
        ev2 = make_event("sX", "b"); ev2["payload"] = {}
        app_main.schedule_delayed_dispatch(CONSUMER, ev1, "session.end")
        app_main.schedule_delayed_dispatch(CONSUMER, ev2, "session.end")
        assert len(rows(app_main)) == 2  # no dedup possible — each event its own window

    def test_delivered_bucket_recycles_into_new_window(self, app_main):
        app_main.schedule_delayed_dispatch(CONSUMER, make_event("s1", "first"), "session.end")
        with closing(app_main.db_connect()) as conn:
            conn.execute("UPDATE delayed_dispatch SET status = 'delivered'")
            conn.commit()
        app_main.schedule_delayed_dispatch(CONSUMER, make_event("s1", "after"), "session.end")
        r = rows(app_main)
        assert len(r) == 1
        assert r[0]["status"] == "pending"
        assert r[0]["event_count"] == 1  # fresh window


class TestDelivery:
    def _force_due(self, main):
        past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        with closing(main.db_connect()) as conn:
            conn.execute("UPDATE delayed_dispatch SET due_at = ?", (past,))
            conn.commit()

    def test_due_row_delivered_once(self, app_main, monkeypatch):
        calls = []
        async def fake_dispatch(consumer, event):
            calls.append(event["payload"])
            return True, "HTTP 200"
        monkeypatch.setattr(app_main, "dispatch_to_consumer", fake_dispatch)
        app_main.schedule_delayed_dispatch(CONSUMER, make_event("s1", "one"), "session.end")
        app_main.schedule_delayed_dispatch(CONSUMER, make_event("s1", "two"), "session.end")
        self._force_due(app_main)
        delivered = asyncio.run(app_main.deliver_due_delayed())
        assert delivered == 1
        assert len(calls) == 1
        assert calls[0]["note"] == "two"
        assert rows(app_main)[0]["status"] == "delivered"
        # second pass: nothing left
        assert asyncio.run(app_main.deliver_due_delayed()) == 0

    def test_not_due_row_untouched(self, app_main, monkeypatch):
        async def fake_dispatch(consumer, event):
            raise AssertionError("must not dispatch before due")
        monkeypatch.setattr(app_main, "dispatch_to_consumer", fake_dispatch)
        app_main.schedule_delayed_dispatch(CONSUMER, make_event("s1"), "session.end")
        assert asyncio.run(app_main.deliver_due_delayed()) == 0
        assert rows(app_main)[0]["status"] == "pending"

    def test_failed_delivery_goes_to_dlq(self, app_main, monkeypatch):
        async def fake_dispatch(consumer, event):
            return False, "network: refused"
        monkeypatch.setattr(app_main, "dispatch_to_consumer", fake_dispatch)
        app_main.schedule_delayed_dispatch(CONSUMER, make_event("s1"), "session.end")
        self._force_due(app_main)
        assert asyncio.run(app_main.deliver_due_delayed()) == 0
        assert rows(app_main)[0]["status"] == "failed"
        with closing(app_main.db_connect()) as conn:
            dlq = conn.execute("SELECT COUNT(*) AS n FROM dlq").fetchone()["n"]
        assert dlq == 1


class TestRouteEventIntegration:
    def test_delayed_consumer_not_dispatched_inline(self, app_main, monkeypatch):
        dispatched = []
        async def fake_dispatch(consumer, event):
            dispatched.append(consumer)
            return True, "ok"
        monkeypatch.setattr(app_main, "dispatch_to_consumer", fake_dispatch)
        app_main._state["routes"] = {"routes": [
            {"event": "session.end", "handlers": [CONSUMER, {"type": "direct", "action": "log_event"}]}
        ]}
        ev = app_main.IncomingEvent(category="session", type="end", source="test",
                                    payload={"session_id": "s9"})
        result = asyncio.run(app_main.route_event(ev))
        assert result["status"] == "routed"
        assert result["successes"] == 2
        assert len(dispatched) == 1  # only the direct handler ran inline
        assert dispatched[0]["type"] == "direct"
        r = rows(app_main)
        assert len(r) == 1 and r[0]["status"] == "pending"

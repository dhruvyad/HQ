from unittest.mock import patch
import os
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hq.adapters import Dispatcher
from hq.hooks import handle
from hq.presence import RENEW, ClaudePresence, codex_activity, codex_source
from hq.store import Store
from hq.stream import StateFeed


class PresenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/"test.sqlite3")
        self.store.register("claude-code:a", "Original", "claude-code", "a")
        self.store.register("codex:b", "Codex", "codex", "b")

    def activity(self, sid):
        return next(s["activity"]["state"] for s in self.store.snapshot()["sessions"] if s["id"] == sid)

    def collector(self, **kw):
        # A folder that doesn't exist: no status files, so the inventory is asked every sample.
        return ClaudePresence(self.store, folder=Path(self.temp.name) / "no-sessions", **kw)

    def test_connection_is_not_evidence_of_active_or_idle_and_leases_expire(self):
        self.store.heartbeat("claude-code:a", "channel")
        self.assertEqual(self.activity("claude-code:a"), "unknown")
        with patch("hq.store.time.time", return_value=100):
            self.store.observe_activity("claude-code:a", "active", "hooks", lease=3, priority=50)
            self.assertEqual(self.activity("claude-code:a"), "active")
            self.assertEqual(self.activity("codex:b"), "unknown")
        with patch("hq.store.time.time", return_value=104):
            self.assertEqual(self.activity("claude-code:a"), "unknown")

    def test_live_inventory_overrides_hooks_and_failure_does_not_fall_back_to_idle(self):
        self.store.observe_activity("claude-code:a", "idle", "hooks", priority=50)
        self.store.observe_activity("claude-code:a", "active", "claude-live")
        self.assertEqual(self.activity("claude-code:a"), "active")
        self.store.expire_activity("claude-live")
        self.assertEqual(self.activity("claude-code:a"), "unknown")
        self.store.observe_activity("claude-code:a", "idle", "claude-live")
        self.assertEqual(self.activity("claude-code:a"), "idle")
        self.store.expire_activity("claude-live", missing=set())
        self.assertEqual(self.activity("claude-code:a"), "offline")

    def test_claude_native_status_is_exact_session_live_metadata_without_claiming_delivery(self):
        collector = self.collector()
        records = [{"sessionId":"a", "pid":123, "name":"Do not overwrite title", "status":"busy"},
                   {"sessionId":"new", "pid":124, "name":"Live chat", "status":"idle"},
                   {"sessionId":"dead", "pid":125, "status":"busy"},
                   {"sessionId":"persisted-background", "kind":"background", "state":"blocked"}]
        def alive(pid, signal):
            if pid == 125:
                raise ProcessLookupError()
        with patch("hq.presence.subprocess.run", return_value=Mock(stdout=json.dumps(records))), patch("hq.presence.os.kill", side_effect=alive):
            collector.sample()
        self.assertEqual(self.activity("claude-code:a"), "active")
        self.assertEqual(self.activity("claude-code:new"), "idle")
        self.assertEqual(self.store.session("claude-code:a")["name"], "Original")
        self.assertEqual(self.store.session("claude-code:new")["connection"], "offline")
        self.assertEqual(self.store.session("claude-code:new")["route"], "inbox")
        for sid in ("claude-code:dead", "claude-code:persisted-background"):
            with self.assertRaises(ValueError):
                self.store.session(sid)
        with patch("hq.presence.subprocess.run", return_value=Mock(stdout='[{"bad":1}]')):
            with self.assertRaises(ValueError):
                collector.sample()
        self.assertEqual(self.activity("claude-code:a"), "active")

    def test_collector_timeout_invalidates_live_status(self):
        self.store.observe_activity("claude-code:a", "active", "claude-live")
        collector = self.collector()
        def stop(*args, **kwargs):
            collector.stop.set()
            raise subprocess.TimeoutExpired("claude", 2)
        with patch("hq.presence.subprocess.run", side_effect=stop):
            collector.run()
        self.assertEqual(self.activity("claude-code:a"), "unknown")

    def test_inaccessible_or_unattached_process_is_unknown_not_offline(self):
        collector = self.collector()
        self.store.observe_activity("claude-code:a", "idle", "claude-live")
        with patch("hq.presence.subprocess.run", return_value=Mock(stdout='[{"sessionId":"a","pid":123,"status":"idle"}]')), patch("hq.presence.os.kill", side_effect=PermissionError()):
            collector.sample()
        self.assertEqual(self.activity("claude-code:a"), "unknown")
        with patch("hq.presence.subprocess.run", return_value=Mock(stdout='[{"sessionId":"a","kind":"background","state":"blocked"}]')):
            collector.sample()
        self.assertEqual(self.activity("claude-code:a"), "unknown")
        with patch("hq.presence.subprocess.run", return_value=Mock(stdout='[{"sessionId":"a","status":{}}]')):
            with self.assertRaises(ValueError):
                collector.sample()

    def test_lifecycle_start_work_stop_end_and_stop_continuation(self):
        def event(name):
            handle("claude-code", {"session_id":"a", "hook_event_name":name}, self.store, lambda value: None)
        for name, expected in [("SessionStart","idle"),("UserPromptSubmit","active"),("PostToolUse","active"),("Stop","idle"),("SessionEnd","offline")]:
            event(name)
            self.assertEqual(self.activity("claude-code:a"), expected)
        item = self.store.add("claude-code:a", "Choose", kind="question")
        self.store.reply(item["id"], "Yes")
        event("Stop")
        self.assertEqual(self.activity("claude-code:a"), "active")

    def test_codex_events_are_scoped_to_the_owning_server_and_disconnect_invalidates(self):
        self.store.bind_codex("codex:b", "b", "/tmp/owner.sock")
        dispatcher = Dispatcher(self.store)
        with patch("hq.adapters.CodexClient") as constructor:
            dispatcher.client("/tmp/owner.sock")
            event = constructor.call_args.kwargs["on_event"]
            event({"method":"thread/status/changed", "params":{"threadId":"b", "status":{"type":"active"}}})
            self.assertEqual(self.activity("codex:b"), "active")
            event({"method":"thread/status/changed", "params":{"threadId":"b", "status":{"type":"active", "activeFlags":["waitingOnApproval"]}}})
            self.assertEqual(self.activity("codex:b"), "waiting")
            constructor.call_args.kwargs["on_disconnect"]()
            self.assertEqual(self.activity("codex:b"), "unknown")
            dispatcher.client("/tmp/other.sock")
            constructor.call_args.kwargs["on_event"]({"method":"thread/status/changed", "params":{"threadId":"b", "status":{"type":"idle"}}})
            self.assertEqual(self.activity("codex:b"), "unknown")
        self.assertEqual(codex_activity({"type":"idle"}), "idle")
        self.assertEqual(codex_activity({"type":"notLoaded"}), "offline")
        self.assertEqual(codex_activity({"type":"unknownFutureStatus"}), "unknown")

    def test_slow_inventory_cannot_overwrite_a_newer_runtime_event(self):
        source = codex_source("/tmp/owner.sock")
        self.store.observe_activity("codex:b", "active", source, observed=200)
        self.store.observe_activity("codex:b", "idle", source, observed=100)
        self.assertEqual(self.activity("codex:b"), "active")
        self.store.expire_activity(source, missing=set(), before=150)
        self.assertEqual(self.activity("codex:b"), "active")

    def test_feed_observes_other_process_writes_and_expires_without_new_writes(self):
        feed = StateFeed(self.store)
        self.addCleanup(feed.close)
        previous, _ = feed.wait(-1)
        other = Store(self.store.path)
        other.observe_activity("claude-code:a", "active", "claude-live", lease=0.6)
        version, payload = feed.wait(previous, timeout=2)
        self.assertGreater(version, previous)
        snapshot = json.loads(payload)
        self.assertEqual(snapshot["revision"], version)
        self.assertEqual(next(s["activity"]["state"] for s in snapshot["sessions"] if s["id"] == "claude-code:a"), "active")
        version2, payload = feed.wait(version, timeout=2)
        self.assertGreater(version2, version)
        self.assertEqual(next(s["activity"]["state"] for s in json.loads(payload)["sessions"] if s["id"] == "claude-code:a"), "unknown")
        # Renewing the same status must not force every browser to rebuild its forms.
        other.observe_activity("claude-code:a", "idle", "claude-live")
        version, _ = feed.wait(version2, timeout=2)
        other.observe_activity("claude-code:a", "idle", "claude-live")
        feed.publish()
        self.assertEqual(feed.version, version)

class QuietTests(unittest.TestCase):
    """HQ runs all day: nothing may cost CPU or wake the feed unless something changed."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "hq.sqlite3")
        self.folder = Path(self.temp.name) / "sessions"
        self.folder.mkdir()
        self.now = 1000.0

    def write(self, pid, sid, status):
        (self.folder / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid, "cwd": "/tmp", "name": sid, "status": status}))

    def states(self):
        return {s["id"]: s["activity"]["state"] for s in self.store.snapshot()["sessions"]}

    def test_status_files_are_read_and_claude_is_started_only_for_background_agents(self):
        me = os.getpid()
        self.write(me, "busy-one", "busy")
        self.write(999999, "gone", "busy")  # its process has exited
        (self.folder / "half.json").write_text('{"pid": 1, "sess')  # caught mid-write
        presence = ClaudePresence(self.store, folder=self.folder, clock=lambda: self.now)
        inventory = Mock(return_value=Mock(stdout="[]"))
        with patch("hq.presence.subprocess.run", inventory):
            for _ in range(5):
                presence.sample()
                self.now += 1
        self.assertEqual(self.states(), {"claude-code:busy-one": "active"})
        self.assertEqual(inventory.call_count, 1)  # once, then every BACKGROUND seconds, not every second
        self.now += 20
        with patch("hq.presence.subprocess.run", inventory):
            presence.sample()
        self.assertEqual(inventory.call_count, 2)
        self.write(me, "busy-one", "idle")
        with patch("hq.presence.subprocess.run", inventory):
            presence.sample()
        self.assertEqual(self.states(), {"claude-code:busy-one": "idle"})

    def test_a_name_you_gave_it_replaces_hqs_at_once_and_a_made_up_one_never(self):
        me = os.getpid()
        self.store.register("claude-code:s1", "Claude Code · noclick4", "claude-code", "s1", "/tmp")
        path = self.folder / f"{me}.json"
        row = {"pid": me, "sessionId": "s1", "cwd": "/tmp", "status": "busy", "name": "noclick4-59", "nameSource": "derived"}
        path.write_text(json.dumps(row))
        presence = ClaudePresence(self.store, folder=self.folder, clock=lambda: self.now)
        name = lambda: self.store.session("claude-code:s1")["name"]
        with patch("hq.presence.subprocess.run", return_value=Mock(stdout="[]")):
            presence.sample()
            self.assertEqual(name(), "Claude Code · noclick4")
            path.write_text(json.dumps({**row, "name": " main   coordinator ", "nameSource": "user"}))
            os.utime(path, (time.time() + 5, time.time() + 5))
            with patch.object(self.store, "register", wraps=self.store.register) as registered:
                for _ in range(3):
                    self.now += 1
                    presence.sample()
            self.assertEqual(name(), "main coordinator")
            self.assertEqual(registered.call_count, 1)  # once, not every second

    def test_running_a_shell_command_is_working(self):
        # Claude Code shows "shell" as working in its own list; it is not a lost signal.
        self.write(os.getpid(), "cmd", "shell")
        presence = ClaudePresence(self.store, folder=self.folder, clock=lambda: self.now)
        with patch("hq.presence.subprocess.run", return_value=Mock(stdout="[]")):
            presence.sample()
        self.assertEqual(self.states(), {"claude-code:cmd": "active"})

    def test_an_unchanged_state_is_written_only_to_renew_it(self):
        self.write(os.getpid(), "steady", "busy")
        presence = ClaudePresence(self.store, folder=self.folder, clock=lambda: self.now)
        with patch("hq.presence.subprocess.run", return_value=Mock(stdout="[]")), \
                patch.object(self.store, "observe_activity", wraps=self.store.observe_activity) as observed:
            for _ in range(RENEW):
                presence.sample()
                self.now += 1
            self.assertEqual(observed.call_count, 1)
            presence.sample()  # RENEW seconds on: renewed, well inside its lease
            self.assertEqual(observed.call_count, 2)

    def test_marking_a_missing_session_offline_happens_once(self):
        self.store.register("claude-code:a", "A", "claude-code")
        self.store.observe_activity("claude-code:a", "idle", "claude-live")
        self.store.expire_activity("claude-live", missing=set())
        with self.store.db() as db:
            before = db.execute("SELECT observed FROM activity").fetchone()[0]
        self.store.expire_activity("claude-live", missing=set())
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT observed FROM activity").fetchone()[0], before)  # not written again
        self.assertEqual(self.states()["claude-code:a"], "offline")

    def test_the_feed_stays_live_without_rebuilding_when_nothing_changes(self):
        feed = StateFeed(self.store)
        self.addCleanup(feed.close)
        time.sleep(0.6)
        with patch.object(feed, "publish", wraps=feed.publish) as rebuilt:
            time.sleep(1.5)
            self.assertEqual(rebuilt.call_count, 0)
        self.assertLess(time.monotonic() - feed.last_success, 1)  # clients still see a healthy feed
        Store(self.store.path).register("claude-code:new", "New", "claude-code")
        version, _ = feed.wait(feed.version, timeout=2)
        self.assertTrue(any(s["id"] == "claude-code:new" for s in json.loads(feed.payload)["sessions"]))


class ActivityTimesTests(unittest.TestCase):
    """When a state began, and when an agent last ran, survive the every-second observations."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "hq.sqlite3")
        self.store.register("claude-code:a", "Agent", "claude-code")

    def session(self):
        return next(s for s in self.store.snapshot()["sessions"] if s["id"] == "claude-code:a")

    def test_since_marks_the_change_not_the_latest_observation(self):
        self.store.observe_activity("claude-code:a", "active", "claude-live", observed=100)
        self.store.observe_activity("claude-code:a", "active", "claude-live", observed=105)
        self.store.observe_activity("claude-code:a", "active", "claude-live", observed=110)
        self.assertEqual(self.session()["activity"]["since"], 100)  # running since it started
        self.store.observe_activity("claude-code:a", "idle", "claude-live", observed=120)
        self.store.observe_activity("claude-code:a", "idle", "claude-live", observed=125)
        session = self.session()
        self.assertEqual(session["activity"]["since"], 120)  # idle since it finished
        self.assertEqual(session["last_active"], 110)  # last seen running

    def test_last_active_outlives_the_observation_and_spans_sources(self):
        self.store.observe_activity("claude-code:a", "active", "hooks", observed=50, priority=50)
        self.store.observe_activity("claude-code:a", "active", "claude-live", observed=90)
        self.store.expire_activity("claude-live", missing=set())
        self.assertEqual(self.session()["last_active"], 90)
        self.store.expire_activity("claude-live")
        self.assertEqual(self.session()["last_active"], 90)  # going quiet does not erase when it last ran

    def test_never_active_has_no_last_active(self):
        self.store.observe_activity("claude-code:a", "idle", "claude-live", observed=10)
        self.assertIsNone(self.session()["last_active"])

    def test_an_older_database_gains_the_columns(self):
        import sqlite3
        path = Path(self.temp.name) / "old.sqlite3"
        Store(path)
        with sqlite3.connect(path) as db:
            # The table as it was before these columns existed.
            db.execute("DROP TABLE activity")
            db.execute("""CREATE TABLE activity (session_id TEXT NOT NULL REFERENCES sessions(id), source TEXT NOT NULL,
                state TEXT NOT NULL, observed REAL NOT NULL, expires REAL NOT NULL,
                priority INTEGER NOT NULL, PRIMARY KEY(session_id,source))""")
        store = Store(path)
        store.register("codex:x", "X", "codex")
        store.observe_activity("codex:x", "active", "codex-runtime:default", observed=7)
        self.assertEqual(next(s for s in store.snapshot()["sessions"] if s["id"] == "codex:x")["last_active"], 7)

class BackgroundSessionTests(unittest.TestCase):
    def test_dispatched_background_sessions_report_state_not_status(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "hq.sqlite3")
            presence = ClaudePresence(store, folder=Path(temp) / "no-sessions")
            rows = [{"sessionId": "bg", "pid": os.getpid(), "kind": "background", "name": "Convert videos", "state": "blocked", "cwd": "/tmp"},
                    {"sessionId": "bg2", "pid": os.getpid(), "kind": "background", "name": "Write docs", "state": "running", "cwd": "/tmp"}]
            with patch("hq.presence.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(rows))):
                presence.sample()
            states = {s["id"]: s["activity"]["state"] for s in store.snapshot()["sessions"]}
            self.assertEqual(states["claude-code:bg"], "waiting")
            self.assertEqual(states["claude-code:bg2"], "active")


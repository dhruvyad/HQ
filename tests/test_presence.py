import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hq.adapters import Dispatcher
from hq.hooks import handle
from hq.presence import ClaudePresence, codex_activity, codex_source
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
        collector = ClaudePresence(self.store)
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
        collector = ClaudePresence(self.store)
        def stop(*args, **kwargs):
            collector.stop.set()
            raise subprocess.TimeoutExpired("claude", 2)
        with patch("hq.presence.subprocess.run", side_effect=stop):
            collector.run()
        self.assertEqual(self.activity("claude-code:a"), "unknown")

    def test_inaccessible_or_unattached_process_is_unknown_not_offline(self):
        collector = ClaudePresence(self.store)
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

import concurrent.futures
import json
import os
import sqlite3
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from hq.adapters import CodexClient
from hq.discovery import discover
from hq.server import Server
from hq.store import Store

ROOT = Path(__file__).resolve().parents[1]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.store.register("a", "Billing", "codex")
        self.store.register("b", "Product", "claude-code")

    def question(self, session="a"):
        return self.store.add(session, "Which plan?", kind="question", tags=["billing"], options=["Monthly", "Annual"])

    def test_persistence_and_filtering(self):
        item = self.question()
        self.store.add("b", "Polish homepage", tags=["product"])
        reopened = Store(self.store.path)
        self.assertEqual(reopened.snapshot(tag="billing")["items"][0]["id"], item["id"])
        self.assertEqual(len(reopened.snapshot(session_id="b")["items"]), 1)
        self.assertEqual(reopened.item(item["id"])["options"], ["Monthly", "Annual"])

    def test_many_items_in_one_conversation_stay_independent(self):
        decision = self.question()
        action = self.store.add("a", "Upload certificate", owner="human")
        waiting = self.store.add("a", "Vendor approval", kind="blocker", owner="external", waiting_on="Vendor")
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        later = self.store.add("a", "Review copy", owner="human", defer_until=tomorrow)
        agent = self.store.add("a", "Build the integration")
        self.assertEqual(len(self.store.snapshot(session_id="a")["items"]), 5)
        self.assertEqual(len(self.store.snapshot(session_id="a", bucket="needs")["items"]), 2)
        self.store.reply(decision["id"], "Annual")
        self.store.reply(action["id"], "Uploaded")
        self.assertEqual({r["item_id"] for r in self.store.inbox("a")}, {decision["id"], action["id"]})
        self.assertEqual(self.store.inbox("b"), [])
        for item, bucket in ((waiting, "waiting"), (later, "later"), (agent, "pending")):
            self.assertEqual(self.store.item(item["id"])["bucket"], bucket)
            self.assertIsNone(self.store.item(item["id"])["answer"])
        self.store.acknowledge(action["id"], "a")
        self.store.resolve(action["id"])
        self.assertEqual(self.store.item(decision["id"])["delivery"], "pending")

    def test_deferred_items_return_to_their_original_owner(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        item = self.store.add("a", "Vendor approval", owner="external", waiting_on="Vendor", defer_until=tomorrow)
        self.assertEqual(item["bucket"], "later")
        self.assertEqual(Store.bucket(item, tomorrow), "waiting")
        self.assertEqual(Store.bucket({**item, "owner": "human"}, tomorrow), "needs")
        restored = self.store.update(item["id"], "a", defer_until="")
        self.assertEqual(restored["waiting_on"], "Vendor")
        self.assertEqual(restored["bucket"], "waiting")
        updated = self.store.update(item["id"], "a", owner="human")
        self.assertEqual(updated["waiting_on"], "")
        self.assertEqual(updated["bucket"], "needs")
        self.assertEqual(len(self.store.snapshot()["items"]), 1)

    def test_invalid_and_cross_session_updates_preserve_item_and_answers(self):
        item = self.question()
        for patch_data in ({"owner": "external"}, {"defer_until": "2026-02-30"}, {"defer_until": "20261015"}, {"owner": "unknown"}, {"status": "done"}):
            with self.assertRaises(ValueError):
                self.store.update(item["id"], "a", **patch_data)
            self.assertEqual(self.store.item(item["id"]), item)
        with self.assertRaises(ValueError):
            self.store.update(item["id"], "b", owner="agent")
        self.store.reply(item["id"], "Annual")
        with self.assertRaises(ValueError):
            self.store.update(item["id"], "a", owner="external", waiting_on="Vendor")
        self.assertEqual(self.store.item(item["id"])["answer"], "Annual")

    def test_v1_migration_preserves_items_replies_and_defaults(self):
        question = self.question()
        task = self.store.add("a", "Agent task")
        self.store.reply(question["id"], "Annual")
        with self.store.db() as db:
            for column in ("owner", "waiting_on", "defer_until", "presentation"):
                db.execute(f"ALTER TABLE items DROP COLUMN {column}")
            db.execute("PRAGMA user_version=1")
        with concurrent.futures.ThreadPoolExecutor() as pool:
            migrated = list(pool.map(lambda _: Store(self.store.path), range(4)))
        for store in migrated:
            self.assertEqual(store.item(task["id"])["owner"], "agent")
            self.assertEqual(store.item(question["id"])["owner"], "human")
            self.assertEqual(store.item(question["id"])["answer"], "Annual")
            self.assertEqual(store.inbox("a")[0]["state"], "pending")
        with self.store.db() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_cli_reports_and_updates_one_of_many_items(self):
        def cli(*args):
            result = subprocess.run([sys.executable, str(ROOT / "bin/hq"), *args],
                env={**os.environ, "HQ_HOME": self.temp.name}, capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        cli("session", "add", "cli", "--name", "CLI conversation")
        waiting = cli("add", "Vendor approval", "--session", "cli", "--owner", "external", "--waiting-on", "Vendor")
        action = cli("add", "Upload file", "--session", "cli", "--owner", "human")
        updated = cli("update", waiting["id"], "--session", "cli", "--owner", "human")
        self.assertEqual(updated["bucket"], "needs")
        self.assertEqual({i["id"] for i in cli("list", "--session", "cli", "--bucket", "needs")}, {waiting["id"], action["id"]})

    def test_readable_report_persists_steps_links_choices_and_completion(self):
        item = self.store.add("a", "Choose the launch plan", kind="question", owner="human",
            unlocks="Finalizes launch scope", recommendation="Ship the **core flow** first",
            steps=["Review `scope`", "Choose a plan"], link={"label": "Open scope", "url": "https://example.com/scope"},
            paste=[{"label": "Message", "text": "Ship the core flow", "mono": False}], minutes=2, rank=20,
            source="agent1 · launch", options=[{"id": "core", "label": "Core flow", "note": "Ready", "recommended": True}, "Include importer"])
        self.store.update(item["id"], "a", steps=["Review the final scope"])
        reopened = Store(self.store.path)
        self.assertEqual(reopened.item(item["id"])["steps"], ["Review the final scope"])
        self.assertTrue(reopened.item(item["id"])["options"][0]["recommended"])
        self.assertEqual(reopened.item(item["id"])["paste"][0]["text"], "Ship the core flow")
        reopened.reply(item["id"], "Core flow")
        with self.assertRaises(ValueError):
            reopened.resolve(item["id"], "Shipped")
        reopened.acknowledge(item["id"], "a")
        closed = reopened.resolve(item["id"], "Shipped the core flow")
        self.assertEqual(closed["agent_note"], "Shipped the core flow")
        self.assertEqual(closed["answer"], "Core flow")

    def test_invalid_readable_fields_are_rejected_without_creating_items(self):
        for fields in ({"link": {"label": "Open", "url": "javascript:alert(1)"}},
                       {"link": {"label": "Open", "url": "https://user:secret@example.com"}},
                       {"steps": "long transcript"}, {"paste": [{"text": "Unlabelled"}]},
                       {"minutes": -1}, {"rank": True},
                       {"options": [{"label": "Go", "recommended": "yes"}]}):
            with self.assertRaises(ValueError):
                self.store.add("a", "Action", **fields)
        self.assertEqual(self.store.snapshot()["items"], [])

    def test_structured_cli_report_and_shared_instructions(self):
        fields = {"title": "Upload the certificate", "owner": "human", "steps": ["Open portal", "Upload file"],
                  "unlocks": "Starts vendor review", "minutes": 3}
        report_path = Path(self.temp.name) / "report.json"
        report_path.write_text(json.dumps(fields))
        env = {**os.environ, "HQ_HOME": self.temp.name}
        subprocess.run([sys.executable, str(ROOT / "bin/hq"), "session", "add", "cli", "--name", "CLI"], env=env, capture_output=True, check=True)
        result = subprocess.run([sys.executable, str(ROOT / "bin/hq"), "report", str(report_path), "--session", "cli"], env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["steps"], fields["steps"])
        guide = subprocess.run([sys.executable, str(ROOT / "bin/hq"), "instructions"], env=env, capture_output=True, text=True, check=True)
        self.assertIn("human action", guide.stdout)
        self.assertIn("never put key values", guide.stdout)

    def test_answer_returns_to_exact_waiting_session(self):
        item = self.question()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(self.store.wait, item["id"], "a", 3)
            self.store.reply(item["id"], "Annual — ₹12,000")
            self.assertEqual(future.result()["answer"], "Annual — ₹12,000")
        self.assertEqual(self.store.item(item["id"])["delivery"], "received")
        self.assertEqual(self.store.inbox("b"), [])
        with self.assertRaises(ValueError):
            self.store.wait(item["id"], "b", 1)

    def test_concurrent_answers_are_atomic(self):
        item = self.question()
        def answer(text):
            try:
                self.store.reply(item["id"], text)
                return True
            except ValueError:
                return False
        with concurrent.futures.ThreadPoolExecutor() as pool:
            accepted = list(pool.map(answer, ["Monthly", "Annual"]))
        self.assertEqual(sum(accepted), 1)
        self.assertIn(self.store.item(item["id"])["answer"], ["Monthly", "Annual"])

    def test_offline_reply_survives_until_acknowledgement(self):
        item = self.question()
        self.store.reply(item["id"], "Annual")
        reopened = Store(self.store.path)
        self.assertEqual(reopened.inbox("a")[0]["state"], "pending")
        with self.assertRaises(ValueError):
            reopened.acknowledge(item["id"], "b")
        with self.assertRaises(ValueError):
            reopened.resolve(item["id"])
        reopened.acknowledge(item["id"], "a")
        self.assertEqual(reopened.resolve(item["id"])["status"], "done")

    def test_late_transport_status_cannot_undo_acknowledgement(self):
        item = self.question()
        self.store.reply(item["id"], "Annual")
        reply_id = self.store.inbox("a")[0]["id"]
        self.store.acknowledge(item["id"], "a")
        self.store.delivery_result(reply_id, "sent")
        self.assertEqual(self.store.item(item["id"])["delivery"], "received")

    def test_timeout_and_cancellation_preserve_question(self):
        item = self.question()
        self.assertEqual(self.store.wait(item["id"], "a", .01)["status"], "timeout")
        event = threading.Event()
        event.set()
        self.assertEqual(self.store.wait(item["id"], "a", 1, event)["status"], "cancelled")
        self.assertEqual(self.store.item(item["id"])["status"], "open")

    def test_leases_do_not_claim_discovered_sessions_are_live(self):
        self.assertEqual(self.store.session("a")["connection"], "offline")
        self.store.heartbeat("a", lease=-1)
        self.assertEqual(self.store.session("a")["connection"], "offline")

    def test_native_outbox_claim_is_exclusive_and_crash_is_not_retried(self):
        self.store.bind_codex("a", "thread-a", "/tmp/test.sock")
        item = self.question()
        self.store.reply(item["id"], "Annual")
        with concurrent.futures.ThreadPoolExecutor() as pool:
            claims = list(pool.map(lambda _: self.store.claim_native(), range(4)))
        self.assertEqual(sum(c is not None for c in claims), 1)
        with self.store.db() as db:
            db.execute("UPDATE replies SET claim_until=0")
        self.store.recover_uncertain()
        self.assertEqual(self.store.item(item["id"])["delivery"], "failed")
        self.assertIsNone(self.store.claim_native())
        self.store.retry(item["id"])
        self.assertIsNotNone(self.store.claim_native())

    def test_waiting_tool_takes_precedence_over_native_turn(self):
        self.store.bind_codex("a", "thread-a")
        item = self.question()
        with self.store.db() as db:
            db.execute("INSERT INTO waiters VALUES (?,?,?)", (item["id"], "test", time.time() + 10))
        self.store.reply(item["id"], "Annual")
        self.assertIsNone(self.store.claim_native())

    def test_discovery_handles_long_names_without_changing_source(self):
        fake_home = Path(self.temp.name) / "home"
        source = fake_home / ".codex/state_5.sqlite"
        source.parent.mkdir(parents=True)
        with sqlite3.connect(source) as db:
            db.execute("CREATE TABLE threads (id,title,cwd,archived,source,updated_at)")
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?)", ("thread1", "Long title " * 100, "/project", 0, "cli", 1))
        before = source.read_bytes()
        with patch("hq.discovery.Path.home", return_value=fake_home):
            result = discover(self.store)
        self.assertEqual(result["discovered"], 1)
        self.assertLessEqual(len(self.store.session("codex:thread1")["name"]), 500)
        self.assertEqual(source.read_bytes(), before)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "http.sqlite3")
        self.store.register("a", "Agent")
        self.server = Server(0, self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        req = urllib.request.Request(self.server.origin + path, data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})})
        return urllib.request.urlopen(req)

    def test_roundtrip_and_html_assets(self):
        with self.request("/") as response:
            self.assertIn(self.server.token, response.read().decode())
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        with self.request("/app.js") as response:
            self.assertEqual(response.status, 200)
        headers = {"X-HQ-Token": self.server.token}
        with self.request("/api/items", {"session_id": "a", "title": "Question", "kind": "question"}, headers) as response:
            item = json.load(response)
        with self.request(f"/api/items/{item['id']}/reply", {"text": "Yes"}, headers) as response:
            self.assertEqual(json.load(response)["answer"], "Yes")

    def test_item_move_and_external_response_roundtrip(self):
        headers = {"X-HQ-Token": self.server.token}
        with self.request("/api/items", {"session_id": "a", "title": "Vendor", "kind": "blocker", "owner": "external", "waiting_on": "Support"}, headers) as response:
            item = json.load(response)
        other = self.store.add("a", "Unrelated", owner="human")
        with self.request(f"/api/items/{item['id']}/update", {"owner": "human"}, headers) as response:
            self.assertEqual(json.load(response)["bucket"], "needs")
        with self.request(f"/api/items/{item['id']}/reply", {"text": "Support approved"}, headers) as response:
            self.assertEqual(json.load(response)["bucket"], "waiting")
        self.assertEqual(self.store.item(other["id"]), other)

    def test_cross_origin_dns_rebinding_and_unauthorized_reads_blocked(self):
        for headers in ({}, {"Host": "attacker.example"}, {"Origin": "https://attacker.example", "X-HQ-Token": self.server.token}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/state", headers=headers)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/items", {"session_id": "a", "title": "Attack"})
        self.assertEqual(error.exception.code, 403)
        error.exception.close()


class CodexRoutingTests(unittest.TestCase):
    def client(self, thread):
        client = CodexClient.__new__(CodexClient)
        client.call = Mock(side_effect=[{"thread": thread}, {"turnId": "t"}])
        return client

    def test_active_turn_uses_expected_id(self):
        client = self.client({"status": {"type": "active"}, "turns": [{"id": "active-t", "status": "inProgress"}]})
        client.deliver("thread-b", "Go ahead")
        method, args = client.call.call_args.args
        self.assertEqual(method, "turn/steer")
        self.assertEqual(args["threadId"], "thread-b")
        self.assertEqual(args["expectedTurnId"], "active-t")

    def test_idle_thread_starts_turn_without_changing_permissions(self):
        client = self.client({"status": {"type": "idle"}})
        client.deliver("thread-b", "Go ahead")
        self.assertEqual(client.call.call_args.args, ("turn/start", {"threadId": "thread-b", "input": [{"type": "text", "text": "Go ahead"}]}))

    def test_unknown_active_id_stays_saved(self):
        client = self.client({"status": {"type": "active"}, "turns": []})
        with self.assertRaises(RuntimeError):
            client.deliver("thread-b", "Go ahead")
        self.assertEqual(client.call.call_count, 1)


class MCPTests(unittest.TestCase):
    channel = False
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "hq.sqlite3")
        self.store.register("a", "MCP agent")
        command = [sys.executable, str(ROOT / "bin/hq"), "mcp"]
        if self.channel:
            command += ["--channel", "--session-id", "a", "--provider", "claude-code"]
        self.process = subprocess.Popen(command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "HQ_HOME": self.temp.name})
        self.messages = queue.Queue()
        def reader():
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        threading.Thread(target=reader, daemon=True).start()

    def tearDown(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        for pipe in (self.process.stdout, self.process.stderr):
            pipe.close()
        self.temp.cleanup()

    def send(self, method, id=None, params=None):
        data = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if id is not None:
            data["id"] = id
        self.process.stdin.write(json.dumps(data) + "\n")
        self.process.stdin.flush()

    def test_full_mcp_human_response_and_concurrent_ping(self):
        self.send("initialize", 1, {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
        initialized = self.messages.get(timeout=3)
        self.assertEqual(initialized["id"], 1)
        self.assertIn("NoClick Queue writing format", initialized["result"]["instructions"])
        self.send("notifications/initialized")
        self.send("tools/list", 2)
        self.assertTrue(any(t["name"] == "hq_request_input" for t in self.messages.get(timeout=3)["result"]["tools"]))
        self.send("tools/call", 3, {"name": "hq_request_input", "arguments": {"session_id": "a", "title": "Ship?", "timeout": 5}})
        deadline = time.monotonic() + 3
        while not self.store.snapshot()["items"] and time.monotonic() < deadline:
            time.sleep(.02)
        item = self.store.snapshot()["items"][0]
        self.send("ping", 4)
        self.assertEqual(self.messages.get(timeout=3)["id"], 4)
        self.store.reply(item["id"], "Ship the smaller change")
        message = self.messages.get(timeout=3)
        self.assertEqual(message["id"], 3)
        self.assertEqual(json.loads(message["result"]["content"][0]["text"])["answer"], "Ship the smaller change")
        self.assertEqual(self.store.item(item["id"])["delivery"], "received")

    def test_mcp_report_update_and_filter_preserve_other_items(self):
        def call(request_id, name, arguments):
            self.send("tools/call", request_id, {"name": name, "arguments": arguments})
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                message = self.messages.get(timeout=3)
                if message.get("id") == request_id:
                    return message["result"]
            self.fail("No matching tool result")
        result = call(21, "hq_report", {"session_id": "a", "title": "Wait for approval", "owner": "external", "waiting_on": "Vendor", "unlocks": "Launch", "steps": ["Wait for the vendor"], "options": [{"label": "Continue", "recommended": True}]})
        self.assertFalse(result["isError"])
        item = json.loads(result["content"][0]["text"])
        self.assertEqual(item["unlocks"], "Launch")
        self.assertTrue(item["options"][0]["recommended"])
        other = self.store.add("a", "Other human action", owner="human")
        updated = call(22, "hq_update", {"session_id": "a", "item_id": item["id"], "owner": "human"})
        self.assertEqual(json.loads(updated["content"][0]["text"])["bucket"], "needs")
        listed = call(23, "hq_list", {"session_id": "a", "bucket": "needs"})
        self.assertEqual(len(json.loads(listed["content"][0]["text"])["items"]), 2)
        self.assertEqual(self.store.item(other["id"]), other)
        self.store.register("b", "Other session")
        rejected = call(24, "hq_update", {"session_id": "b", "item_id": item["id"], "owner": "agent"})
        self.assertTrue(rejected["isError"])

    def test_cancellation_keeps_question(self):
        self.send("tools/call", 1, {"name": "hq_request_input", "arguments": {"session_id": "a", "title": "Ship?", "timeout": 60}})
        time.sleep(.2)
        self.send("notifications/cancelled", params={"requestId": 1})
        result = self.messages.get(timeout=3)
        self.assertEqual(json.loads(result["result"]["content"][0]["text"])["status"], "cancelled")
        self.assertEqual(len(self.store.snapshot(status="open")["items"]), 1)


class ChannelTests(MCPTests):
    channel = True

    def test_channel_push_and_explicit_acknowledgement(self):
        self.send("initialize", 10)
        initialized = self.messages.get(timeout=3)
        self.assertIn("claude/channel", initialized["result"]["capabilities"]["experimental"])
        self.send("notifications/initialized")
        mine = self.store.add("a", "Which plan?", kind="question")
        self.store.register("b", "Other Claude conversation", "claude-code")
        other = self.store.add("b", "Other plan?", kind="question")
        self.store.reply(other["id"], "Private to b")
        self.store.reply(mine["id"], "Annual")
        notification = self.messages.get(timeout=3)
        self.assertEqual(notification["method"], "notifications/claude/channel")
        self.assertEqual(notification["params"]["meta"]["item_id"], mine["id"])
        self.assertNotIn("Private to b", notification["params"]["content"])
        self.send("tools/call", 11, {"name": "hq_acknowledge", "arguments": {"session_id": "a", "item_id": mine["id"]}})
        self.assertEqual(self.messages.get(timeout=3)["id"], 11)
        self.assertEqual(self.store.item(mine["id"])["delivery"], "received")
        self.assertEqual(self.store.item(other["id"])["delivery"], "pending")


if __name__ == "__main__":
    unittest.main()

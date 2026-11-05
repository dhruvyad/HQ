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

from hq.adapters import CodexClient, Dispatcher
from hq.discovery import discover
from hq.server import Server
from hq.store import Store

ROOT = Path(__file__).resolve().parents[1]
# The readable shape every agent item that needs the human must carry.
READABLE = {"minutes": 2, "matters": {"lead": "Unblocks the release", "points": ["Customers wait on it"]},
            "needs_you": {"lead": "Shipping is your call"}}


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
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 8)

    def test_cli_reports_and_updates_one_of_many_items(self):
        def cli(*args):
            result = subprocess.run([sys.executable, str(ROOT / "bin/hq"), *args],
                env={**os.environ, "HQ_HOME": self.temp.name}, capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        cli("session", "add", "cli", "--name", "CLI conversation")
        fields = Path(self.temp.name) / "fields.json"
        fields.write_text(json.dumps({**READABLE, "steps": ["Upload the file"]}))
        waiting = cli("add", "Vendor approval", "--session", "cli", "--owner", "external", "--waiting-on", "Vendor")
        action = cli("add", "Upload file", "--session", "cli", "--owner", "human", "--fields", str(fields))
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

    def test_agent_items_that_need_the_human_must_arrive_readable(self):
        with self.assertRaises(ValueError) as refused:
            self.store.add("a", "Choose the plan", kind="question", options=["Monthly", "Annual"], readable=True)
        for field in ("minutes", "matters", "needs_you"):
            self.assertIn(field, str(refused.exception))
        self.assertNotIn("steps", str(refused.exception))  # options are enough for a decision
        with self.assertRaises(ValueError) as refused:
            self.store.add("a", "Upload the file", owner="human", **READABLE, readable=True)
        self.assertIn("steps", str(refused.exception))
        self.assertEqual(self.store.snapshot()["items"], [])
        self.store.add("a", "Choose the plan", kind="question", options=["Monthly", "Annual"], **READABLE, readable=True)
        self.store.add("a", "Refactor the parser", owner="agent", readable=True)
        self.store.add("a", "Vendor approval", owner="external", waiting_on="Vendor", readable=True)
        self.store.add("a", "Call the bank", owner="human")  # the human's own items stay free-form
        self.assertEqual(len(self.store.snapshot()["items"]), 4)

    def test_steps_carry_links_copy_text_and_checks(self):
        steps = [{"text": "Open NoClick", "link": {"label": "noclick.com", "url": "https://noclick.com"}},
                 {"text": "Send this message", "copy": "build me an agent"},
                 {"text": "Check what it asks", "checks": ["A repository list", "A Linear team list"]}, "Reply here"]
        item = self.store.add("a", "Send one message", owner="human", steps=steps, **READABLE, readable=True)
        reopened = Store(self.store.path).item(item["id"])
        self.assertEqual(reopened["steps"], steps)
        self.assertEqual(reopened["needs_you"], {"lead": "Shipping is your call", "points": []})
        updated = self.store.update(item["id"], "a", matters={"lead": "Proves the lists work", "points": ["Live since 10-18"]})
        self.assertEqual(updated["matters"]["points"], ["Live since 10-18"])
        with self.store.db() as db:  # sessions on older HQ code read `steps` as plain text
            stored = json.loads(db.execute("SELECT presentation FROM items WHERE id=?", (item["id"],)).fetchone()[0])
        self.assertTrue(all(isinstance(step, str) for step in stored["steps"]))
        self.assertEqual(stored["steps"][1], "Send this message: build me an agent")
        self.assertEqual(stored["plan"], steps)
        plain = self.store.update(item["id"], "a", steps=["Just send the message"])  # an older session rewrites steps
        self.assertEqual(plain["steps"], ["Just send the message"])
        self.assertEqual(Store(self.store.path).item(item["id"])["steps"], ["Just send the message"])
        self.store.update(item["id"], "a", steps=steps)
        for bad in ({"steps": [{"text": "Open", "link": {"label": "x", "url": "javascript:alert(1)"}}]},
                    {"steps": [{"text": "Check", "checks": []}]}, {"steps": [{"link": {"label": "x", "url": "https://a.b"}}]},
                    {"steps": [{"text": "Run", "command": "rm -rf /"}]}, {"matters": {"points": ["No lead"]}},
                    {"needs_you": {"lead": "Why", "points": ["1", "2", "3", "4", "5", "6"]}}):
            with self.assertRaises(ValueError):
                self.store.add("a", "Action", owner="human", **{**READABLE, "steps": ["Do it"], **bad}, readable=True)
        self.assertEqual(len(self.store.snapshot()["items"]), 1)

    def test_readability_notes_flag_wording_without_blocking(self):
        long_step = "Open the billing page, find the restriction notice near the top, and work out whether a failed payment or the spending limit caused it"
        item = self.store.add("a", "Review the GitHub billing restriction that stopped the CI run on the HQ pull request",
            detail="x" * 700, owner="human", steps=[long_step, "b", "c", "d", "e", "f"], readable=True,
            **{**READABLE, "matters": {"lead": "Unblocks CI", "points": ["1", "2", "3", "4"]}})
        notes = Store.readability_notes(item)
        self.assertTrue(any(n.startswith("Title is") for n in notes))
        self.assertTrue(any(n.startswith("6 steps") for n in notes))
        self.assertTrue(any(n.startswith("Step 1 is") for n in notes))
        self.assertTrue(any(n.startswith("matters has 4 points") for n in notes))
        self.assertTrue(any(n.startswith("Context is 700") for n in notes))
        old = self.store.add("a", "Old paragraph item", detail="Everything in one paragraph", owner="human")
        self.assertTrue(any("must include" in n for n in Store.readability_notes(old)))
        clean = self.store.add("a", "Send one message", owner="human", steps=["Send it"], **READABLE, readable=True)
        self.assertEqual(Store.readability_notes(clean), [])

    def test_items_are_placed_in_projects_and_workstreams(self):
        from hq import workstreams
        self.store.register("nc", "Claude Code · noclick4", "claude-code", cwd="/Users/me/Documents/code/noclick4")
        self.store.register("cx", "Codex", "codex", cwd="/Users/me/.codex/worktrees/f48e/noclick/backend")
        tagged = self.store.add("nc", "Fix a Crisp reply", tags=["crisp", "billing"])  # most specific group wins
        named = self.store.add("cx", "Ship the SDK", workstream="platform sdk")
        new = self.store.add("nc", "Plan the offsite", workstream="Team offsite")
        moved = self.store.add("nc", "Restyle the inbox", tags=["design"], project="HQ")
        loose = self.store.add("a", "No repo, no tags")
        self.assertEqual((tagged["project"], tagged["project_name"], tagged["workstream"], tagged["workstream_name"]),
                         ("noclick", "NoClick", "support", "Support"))
        self.assertEqual((named["project"], named["workstream"], named["workstream_name"]), ("noclick", "platform", "Platform SDK"))
        self.assertEqual((new["workstream"], new["workstream_name"]), ("team-offsite", "Team offsite"))
        self.assertEqual((moved["project"], moved["workstream"]), ("hq", "design"))
        self.assertEqual((loose["project"], loose["workstream"]), ("other", "other"))
        listed = {i["id"]: i for i in self.store.snapshot()["items"]}
        self.assertEqual(listed[tagged["id"]]["workstream"], "support")
        index = workstreams.index(self.store.snapshot()["items"])
        self.assertIn("Team offsite", index["noclick"]["workstreams"])
        self.assertIn("Coordinator", index["noclick"]["workstreams"])

    def test_skip_closes_the_item_and_tells_the_agent_to_drop_it(self):
        item = self.question()
        question = self.store.ask_agent(item["id"], "Why annual?")
        self.store.update(item["id"], pinned=True)
        skipped = self.store.skip(item["id"], "Not a priority this month")
        # It leaves your list at once, but only closes once the agent has heard it.
        self.assertEqual((skipped["status"], skipped["bucket"], skipped["skipped"], skipped["pinned"]), ("answered", "pending", True, False))
        inbox = self.store.inbox("a")
        self.assertEqual(len(inbox), 1)
        self.assertTrue(inbox[0]["text"].startswith("Skipped: The user decided not to do this."))
        self.assertIn("Reason: Not a priority this month", inbox[0]["text"])
        self.assertEqual(self.store.comment(question["id"])["state"], "cancelled")
        waited = self.store.wait(item["id"], "a", 1)  # an agent blocked on the decision hears at once
        self.assertEqual(waited["status"], "received")
        self.assertTrue(waited["answer"].startswith("Skipped:"))
        self.assertEqual(self.store.item(item["id"])["status"], "done")
        self.assertEqual(self.store.inbox("a"), [])
        with self.assertRaises(ValueError):
            self.store.skip(item["id"])
        with self.assertRaises(ValueError):
            self.store.skip(self.question()["id"], "x" * 2001)

    def test_due_dates_and_pins_persist_and_validate(self):
        item = self.store.add("a", "Prepare the visit checklist", due="2026-10-21")
        self.assertEqual(item["due"], "2026-10-21")
        self.assertFalse(item["pinned"])
        pinned = self.store.update(item["id"], pinned=True, due="")
        self.assertTrue(pinned["pinned"])
        self.assertEqual(Store(self.store.path).item(item["id"])["due"], "")
        for bad in ({"due": "tomorrow"}, {"due": "2026-13-01"}, {"pinned": "yes"}, {"workstream": "x" * 61}):
            with self.assertRaises(ValueError):
                self.store.update(item["id"], **bad)
        self.assertTrue(self.store.item(item["id"])["pinned"])

    def test_structured_cli_report_and_shared_instructions(self):
        fields = {"title": "Upload the certificate", "owner": "human", "steps": ["Open portal", "Upload file"],
                  "unlocks": "Starts vendor review", **READABLE, "minutes": 3}
        report_path = Path(self.temp.name) / "report.json"
        report_path.write_text(json.dumps(fields))
        env = {**os.environ, "HQ_HOME": self.temp.name}
        subprocess.run([sys.executable, str(ROOT / "bin/hq"), "session", "add", "cli", "--name", "CLI"], env=env, capture_output=True, check=True)
        result = subprocess.run([sys.executable, str(ROOT / "bin/hq"), "report", str(report_path), "--session", "cli"], env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["steps"], fields["steps"])
        guide = subprocess.run([sys.executable, str(ROOT / "bin/hq"), "instructions"], env=env, capture_output=True, text=True, check=True)
        self.assertIn("needs_you", guide.stdout)
        self.assertIn("Projects and their workstreams", guide.stdout)
        self.assertIn("Coordinator", guide.stdout)
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

    def test_multiple_clarifications_persist_and_leave_original_decision_independent(self):
        item = self.question()
        other = self.store.add("a", "Separate action", owner="human")
        first = self.store.ask_agent(item["id"], "Why annual?")
        second = self.store.ask_agent(item["id"], "What is the cancellation policy?")
        reopened = Store(self.store.path)
        self.assertTrue(reopened.item(item["id"])["asking"])
        self.assertEqual(reopened.item(item["id"])["bucket"], "waiting")
        self.assertEqual(reopened.item(other["id"]), other)
        reopened.acknowledge_comment(first["id"], "a")
        self.assertEqual(len(reopened.comments("a", pending=True)), 2)
        with self.assertRaises(ValueError):
            reopened.resolve(item["id"])
        with self.assertRaises(ValueError):
            reopened.respond(first["id"], "b", "Wrong conversation")
        reopened.respond(first["id"], "a", "It saves 20%.")
        self.assertTrue(reopened.item(item["id"])["asking"])
        reopened.cancel_comment(second["id"])
        restored = reopened.item(item["id"])
        self.assertFalse(restored["asking"])
        self.assertEqual(restored["bucket"], "needs")
        self.assertEqual(restored["status"], "open")
        self.assertIsNone(restored["answer"])
        self.assertEqual(restored["options"], item["options"])
        self.assertEqual(len(restored["comments"]), 3)
        with self.assertRaises(ValueError):
            reopened.respond(second["id"], "a", "Too late")
        reopened.reply(item["id"], "Annual")
        reopened.acknowledge(item["id"], "a")
        reopened.resolve(item["id"])
        with self.assertRaises(ValueError):
            reopened.ask_agent(item["id"], "More?")

    def test_clarification_restores_deferred_external_owner_and_existing_answer(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        item = self.store.add("a", "Vendor review", owner="external", waiting_on="Vendor", defer_until=tomorrow)
        question = self.store.ask_agent(item["id"], "Which vendor?")
        self.assertEqual(self.store.item(item["id"])["bucket"], "waiting")
        self.store.respond(question["id"], "a", "Our hosting vendor.")
        self.assertEqual(self.store.item(item["id"])["bucket"], "later")
        self.assertEqual(self.store.item(item["id"])["waiting_on"], "Vendor")
        self.store.reply(item["id"], "Approved")
        question = self.store.ask_agent(item["id"], "What happens next?")
        self.store.respond(question["id"], "a", "I will ship it.")
        self.assertEqual(self.store.item(item["id"])["answer"], "Approved")
        self.assertEqual(self.store.item(item["id"])["delivery"], "pending")

    def test_concurrent_agent_replies_and_delivery_cannot_overwrite_history(self):
        item = self.question()
        question = self.store.ask_agent(item["id"], "Why?")
        def respond(text):
            try:
                self.store.respond(question["id"], "a", text)
                return True
            except ValueError:
                return False
        with concurrent.futures.ThreadPoolExecutor() as pool:
            self.assertEqual(sum(pool.map(respond, ["First", "Second"])), 1)
        self.store.comment_delivery(question["id"], "failed", "Late transport error")
        self.assertEqual(self.store.comment(question["id"])["state"], "received")
        self.assertEqual(len(self.store.comments("a")), 2)
        with self.assertRaises(ValueError):
            self.store.comments("b", item["id"])
        cancelled = self.store.ask_agent(item["id"], "Ignore this")
        self.store.cancel_comment(cancelled["id"])
        self.store.comment_delivery(cancelled["id"], "sent")
        self.assertEqual(self.store.comment(cancelled["id"])["state"], "cancelled")

    def test_clarification_claims_are_exclusive_recoverable_and_yield_to_waiter(self):
        self.store.bind_codex("a", "thread-a", "/tmp/test.sock")
        item = self.question()
        question = self.store.ask_agent(item["id"], "Why?")
        with self.store.db() as db:
            db.execute("INSERT INTO waiters VALUES (?,?,?)", (item["id"], "test", time.time()+10))
        self.assertIsNone(self.store.claim_comment())
        with self.store.db() as db:
            db.execute("DELETE FROM waiters")
        with concurrent.futures.ThreadPoolExecutor() as pool:
            claims = list(pool.map(lambda _: self.store.claim_comment(), range(4)))
        self.assertEqual(sum(c is not None for c in claims), 1)
        with self.store.db() as db:
            db.execute("UPDATE comments SET claim_until=0")
        self.store.recover_uncertain()
        self.assertEqual(self.store.comment(question["id"])["state"], "failed")
        self.assertIsNone(self.store.claim_comment())
        self.store.retry_comment(question["id"])
        self.assertEqual(self.store.claim_comment()["external_id"], "thread-a")

    def test_waiting_call_returns_clarification_then_resumes_original_decision(self):
        item = self.question()
        question = self.store.ask_agent(item["id"], "What does it cost?")
        result = self.store.wait(item["id"], "a", 1)
        self.assertEqual(result["status"], "clarification")
        self.assertEqual(result["questions"][0]["id"], question["id"])
        self.assertEqual(self.store.comments("a", pending=True)[0]["state"], "received")
        self.assertEqual(self.store.wait(item["id"], "a", .01)["status"], "timeout")
        self.store.respond(question["id"], "a", "100 per year.")
        self.store.reply(item["id"], "Annual")
        self.assertEqual(self.store.wait(item["id"], "a", 1)["answer"], "Annual")
        self.assertEqual(len(self.store.snapshot()["items"]), 1)

    def test_cli_question_and_agent_response_are_separate_from_decision(self):
        def cli(*args):
            result = subprocess.run([sys.executable, str(ROOT / "bin/hq"), *args],
                env={**os.environ, "HQ_HOME": self.temp.name}, capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        cli("session", "add", "cli", "--name", "CLI")
        fields = Path(self.temp.name) / "fields.json"
        fields.write_text(json.dumps({**READABLE, "steps": ["Pick the plan"]}))
        item = cli("add", "Choose plan", "--session", "cli", "--kind", "question", "--fields", str(fields))
        question = cli("question", item["id"], "Why this plan?")
        cli("comment-ack", question["id"], "--session", "cli")
        self.assertEqual(len(cli("comments", "--session", "cli", "--pending")), 1)
        response = cli("respond", question["id"], "It meets the requirements.", "--session", "cli")
        self.assertEqual(response["parent_id"], question["id"])
        self.assertEqual(cli("comments", "--session", "cli", "--pending"), [])
        self.assertEqual(cli("list", "--session", "cli")[0]["status"], "open")

    def test_native_clarification_routes_to_exact_thread_and_keeps_task_open(self):
        self.store.bind_codex("a", "thread-a", "/tmp/test.sock")
        item = self.question()
        question = self.store.ask_agent(item["id"], "Why annual?")
        other = self.store.ask_agent(self.question("b")["id"], "Private to b")
        dispatcher = Dispatcher(self.store)
        client = Mock()
        client.deliver.side_effect = lambda *_: dispatcher.stop.set()
        with patch("hq.adapters.CodexClient", return_value=client) as constructor:
            dispatcher.run()
        constructor.assert_called_once()
        self.assertEqual(constructor.call_args.args, ("/tmp/test.sock",))
        self.assertTrue(callable(constructor.call_args.kwargs["on_event"]))
        self.assertTrue(callable(constructor.call_args.kwargs["on_disconnect"]))
        thread, text = client.deliver.call_args.args
        self.assertEqual(thread, "thread-a")
        self.assertIn(question["id"], text)
        self.assertIn("hq_comment_reply", text)
        self.assertNotIn("Private to b", text)
        self.assertEqual(self.store.comment(question["id"])["state"], "sent")
        self.assertEqual(self.store.comment(other["id"])["state"], "pending")
        self.assertTrue(self.store.item(item["id"])["asking"])
        self.assertIsNone(self.store.item(item["id"])["answer"])


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

    def test_authenticated_stream_initial_state_live_change_and_reconnect(self):
        def read_event(response):
            lines = []
            while True:
                line = response.readline().decode().strip()
                if not line:
                    if lines:
                        return json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
                else:
                    lines.append(line)
        headers = {"X-HQ-Token": self.server.token}
        with self.request("/api/events", headers=headers) as response:
            self.assertTrue(response.headers["Content-Type"].startswith("text/event-stream"))
            first = read_event(response)
            self.assertEqual(first["sessions"][0]["activity"]["state"], "unknown")
            # An external CLI writes its own SQLite connection, with no HTTP mutation.
            other = Store(self.store.path)
            other.observe_activity("a", "active", "test-live")
            changed = read_event(response)
            self.assertGreater(changed["revision"], first["revision"])
            self.assertEqual(changed["sessions"][0]["activity"]["state"], "active")
        with self.request("/api/events", headers=headers) as response:
            latest = read_event(response)
            self.assertEqual(latest["sessions"][0]["activity"]["state"], "active")
            self.assertEqual(latest["instance"], first["instance"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/events")
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_local_fonts_are_served_as_bytes_and_paths_stay_allowlisted(self):
        for name in ("inter", "outfit", "instrument-serif"):
            with self.request(f"/{name}-latin.woff2") as response:
                self.assertEqual(response.headers["Content-Type"], "font/woff2")
                self.assertEqual(response.read(), (ROOT / "hq/static" / f"{name}-latin.woff2").read_bytes())
                self.assertIn("font-src 'self'", response.headers["Content-Security-Policy"])
        with self.request("/") as response:
            page = response.read().decode()
            self.assertIn('href="/inter-latin.woff2"', page)
            self.assertIn('href="/outfit-latin.woff2"', page)
            self.assertIn('href="/instrument-serif-latin.woff2"', page)
            self.assertIn('aria-labelledby="detail-title"', page)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/../store.py")
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_http_skip(self):
        headers = {"X-HQ-Token": self.server.token}
        with self.request("/api/items", {"session_id": "a", "title": "Upload the file", "owner": "human"}, headers) as response:
            item = json.load(response)
        with self.request(f"/api/items/{item['id']}/skip", {"reason": "Done another way"}, headers) as response:
            skipped = json.load(response)
        # Tracked until the agent hears it.
        self.assertEqual((skipped["status"], skipped["skipped"]), ("answered", True))
        self.assertIn("Done another way", self.store.inbox("a")[0]["text"])
        with self.assertRaises(urllib.error.HTTPError) as refused:
            self.request(f"/api/items/{item['id']}/skip", {}, headers)
        self.assertEqual(refused.exception.code, 400)
        refused.exception.close()

    def test_item_move_and_external_response_roundtrip(self):
        headers = {"X-HQ-Token": self.server.token}
        with self.request("/api/items", {"session_id": "a", "title": "Vendor", "kind": "blocker", "owner": "external", "waiting_on": "Support"}, headers) as response:
            item = json.load(response)
        other = self.store.add("a", "Unrelated", owner="human")
        with self.request(f"/api/items/{item['id']}/update", {"owner": "human"}, headers) as response:
            self.assertEqual(json.load(response)["bucket"], "needs")
        with self.request(f"/api/items/{item['id']}/reply", {"text": "Support approved"}, headers) as response:
            self.assertEqual(json.load(response)["bucket"], "pending")  # the agent's turn now
        self.assertEqual(self.store.item(other["id"]), other)

    def test_http_question_thread_response_and_cancellation(self):
        item = self.store.add("a", "Choose plan", kind="question")
        headers = {"X-HQ-Token": self.server.token}
        with self.request(f"/api/items/{item['id']}/ask", {"text": "Why?"}, headers) as response:
            question = json.load(response)
        self.store.respond(question["id"], "a", "It is cheaper.")
        with self.request("/api/state", headers=headers) as response:
            current = json.load(response)["items"][0]
        self.assertEqual(current["comments"][1]["text"], "It is cheaper.")
        self.assertFalse(current["asking"])
        self.assertEqual(current["status"], "open")
        question = self.store.ask_agent(item["id"], "Another question")
        with self.request(f"/api/comments/{question['id']}/cancel", {}, headers) as response:
            self.assertEqual(json.load(response)["state"], "cancelled")
        self.store.resolve(item["id"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(f"/api/items/{item['id']}/ask", {"text": "More?"}, headers)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

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


class FloatRouteTests(unittest.TestCase):
    """The floating panel's pages keep the dashboard's boundaries. Stub pages, so only the routes are tested."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        static = root / "static"
        static.mkdir()
        for name in ("float.html", "float-lab.html"):
            (static / name).write_text('<meta name="hq-token" content="__HQ_TOKEN__">' + name)
        for name in ("float.js", "float.css", "float-lab.js"):
            (static / name).write_text("/* __HQ_TOKEN__ stays literal outside pages */")
        patcher = patch("hq.server.STATIC", static)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = Store(root / "http.sqlite3")
        self.servers = []

    def serve(self, lab=False):
        with patch.dict(os.environ, {"HQ_FLOAT_LAB": "1"} if lab else {}):
            if not lab:
                os.environ.pop("HQ_FLOAT_LAB", None)
            server = Server(0, self.store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(stop)
        return server

    @staticmethod
    def get(server, path, headers=None):
        return urllib.request.urlopen(urllib.request.Request(server.origin + path, headers=headers or {}))

    def test_panel_page_has_the_token_and_the_same_policy_as_the_dashboard(self):
        server = self.serve()
        with self.get(server, "/float") as response:
            page = response.read().decode()
            self.assertIn(server.token, page)
            self.assertNotIn("__HQ_TOKEN__", page)
            self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")
            policy = response.headers["Content-Security-Policy"]
            self.assertIn("script-src 'self'", policy)
            self.assertIn("frame-ancestors 'none'", policy)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        for path, mime in (("/float.js", "text/javascript"), ("/float.css", "text/css")):
            with self.get(server, path) as response:
                body = response.read().decode()
                self.assertEqual(response.headers["Content-Type"], mime + "; charset=utf-8")
                self.assertEqual(response.headers["Content-Security-Policy"], policy)
                self.assertNotIn(server.token, body)  # The token is only ever injected into a page.

    def test_lab_pages_exist_only_on_the_labs_own_server(self):
        server = self.serve()
        for path in ("/float-lab", "/float-lab.js"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.get(server, path)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
        lab = self.serve(lab=True)
        with self.get(lab, "/float-lab") as response:
            self.assertIn(lab.token, response.read().decode())
            self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        with self.get(lab, "/float-lab.js") as response:
            self.assertEqual(response.status, 200)

    def test_new_routes_reject_foreign_hosts_and_origins(self):
        lab = self.serve(lab=True)
        for path in ("/float", "/float.js", "/float.css", "/float-lab", "/float-lab.js"):
            for headers in ({"Host": "attacker.example"}, {"Origin": "https://attacker.example"}):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.get(lab, path, headers)
                self.assertEqual(error.exception.code, 403)
                error.exception.close()

    def test_a_new_server_passes_the_route_check_the_panel_runs(self):
        from hq.floating import require_route
        require_route(self.serve().origin + "/float")


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
        self.assertIn("Why you, not the agent", initialized["result"]["instructions"])
        self.send("notifications/initialized")
        self.send("tools/list", 2)
        self.assertTrue(any(t["name"] == "hq_request_input" for t in self.messages.get(timeout=3)["result"]["tools"]))
        self.send("tools/call", 3, {"name": "hq_request_input", "arguments": {"session_id": "a", "title": "Ship?", "options": ["Ship", "Wait"], **READABLE, "timeout": 5}})
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
        refused = call(20, "hq_report", {"session_id": "a", "title": "Upload the certificate", "owner": "human", "detail": "One long paragraph"})
        self.assertTrue(refused["isError"])
        self.assertIn("needs_you", refused["content"][0]["text"])
        noted = call(19, "hq_report", {"session_id": "a", "title": "Upload the certificate to the vendor portal before the review window closes on Friday", "owner": "human", "steps": ["Upload it"], **READABLE})
        self.assertTrue(json.loads(noted["content"][0]["text"])["readability"][0].startswith("Title is"))
        self.store.resolve(json.loads(noted["content"][0]["text"])["id"])
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

    def test_mcp_clarification_interrupts_wait_and_agent_replies_in_item(self):
        item = self.store.add("a", "Choose plan", kind="question")
        self.send("tools/call", 31, {"name": "hq_wait", "arguments": {"session_id": "a", "item_id": item["id"], "timeout": 5}})
        deadline = time.monotonic()+3
        while not self.store.has_waiter(item["id"]) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(self.store.has_waiter(item["id"]))
        question = self.store.ask_agent(item["id"], "Why?")
        result = self.messages.get(timeout=3)
        self.assertEqual(result["id"], 31)
        received = json.loads(result["result"]["content"][0]["text"])
        self.assertEqual(received["status"], "clarification")
        self.send("tools/call", 32, {"name": "hq_comments", "arguments": {"session_id": "a", "pending": True}})
        pending = self.messages.get(timeout=3)
        self.assertEqual(json.loads(pending["result"]["content"][0]["text"])[0]["id"], question["id"])
        self.send("tools/call", 33, {"name": "hq_comment_reply", "arguments": {"session_id": "a", "comment_id": question["id"], "text": "It fits the scope."}})
        self.assertFalse(self.messages.get(timeout=3)["result"]["isError"])
        self.assertFalse(self.store.item(item["id"])["asking"])
        self.assertEqual(self.store.item(item["id"])["status"], "open")
        self.store.reply(item["id"], "Go ahead")
        self.send("tools/call", 34, {"name": "hq_wait", "arguments": {"session_id": "a", "item_id": item["id"], "timeout": 1}})
        result = self.messages.get(timeout=3)
        self.assertEqual(json.loads(result["result"]["content"][0]["text"])["answer"], "Go ahead")

    def test_cancellation_keeps_question(self):
        self.send("tools/call", 1, {"name": "hq_request_input", "arguments": {"session_id": "a", "title": "Ship?", "options": ["Ship", "Wait"], **READABLE, "timeout": 60}})
        time.sleep(.2)
        self.send("notifications/cancelled", params={"requestId": 1})
        result = self.messages.get(timeout=3)
        self.assertEqual(json.loads(result["result"]["content"][0]["text"])["status"], "cancelled")
        self.assertEqual(len(self.store.snapshot(status="open")["items"]), 1)


class ChannelTests(MCPTests):
    channel = True

    def test_channel_rejects_reports_for_another_conversation(self):
        self.store.register("other", "Other conversation", "claude-code")
        self.send("initialize", 1)
        self.messages.get(timeout=3)
        self.send("tools/call", 2, {"name": "hq_report", "arguments": {"session_id": "other", "title": "Wrong conversation"}})
        result = self.messages.get(timeout=3)["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(self.store.snapshot(session_id="other")["items"], [])

    def test_channel_clarification_push_requires_agent_reply(self):
        self.send("initialize", 40)
        self.messages.get(timeout=3)
        self.send("notifications/initialized")
        item = self.store.add("a", "Upload certificate", owner="human")
        self.store.register("b", "Other conversation")
        other = self.store.ask_agent(self.store.add("b", "Other task")["id"], "Private to b")
        question = self.store.ask_agent(item["id"], "Where should I upload it?")
        notification = self.messages.get(timeout=3)
        self.assertEqual(notification["params"]["meta"]["comment_id"], question["id"])
        self.assertEqual(notification["params"]["meta"]["session_id"], "a")
        self.assertNotIn("Private to b", notification["params"]["content"])
        self.send("tools/call", 41, {"name": "hq_comment_reply", "arguments": {"session_id": "a", "comment_id": question["id"], "text": "In the vendor portal."}})
        self.assertFalse(self.messages.get(timeout=3)["result"]["isError"])
        self.assertEqual(self.store.comment(question["id"])["state"], "received")
        self.assertFalse(self.store.item(item["id"])["asking"])
        self.assertEqual(self.store.comment(other["id"])["state"], "pending")

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

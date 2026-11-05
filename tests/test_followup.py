"""A decision closes like a handshake: your reply, then the agent hearing it and acting.

Nothing you decide counts as done until the agent confirms. If its agent stops first, the
next agent in the same project may adopt it, and an agent that heard but never closed is
reminded.
"""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hq.hooks import handle
from hq.store import Store, followup

HARBOR, HARBOR_TWO, LANTERN = "/Users/someone/code/harbor", "/Users/someone/code/harbor2", "/Users/someone/code/lantern"


class Handshake(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "hq.sqlite3")
        for name, cwd in (("a", HARBOR), ("b", HARBOR_TWO), ("c", LANTERN), ("d", HARBOR)):
            self.store.register(f"claude-code:{name}", f"Agent {name}", "claude-code", name, cwd)

    def decide(self, session="claude-code:a", answer="Annual"):
        item = self.store.add(session, "Pick the plan", kind="question", options=["Annual", "Monthly"])
        self.store.reply(item["id"], answer)
        return item

    def seen(self, item_id, at=None):
        snapshot = self.store.snapshot() if at is None else self.later(at, self.store.snapshot)
        return next(i for i in snapshot["items"] if i["id"] == item_id)

    @staticmethod
    def later(seconds, call, *args):
        with patch("hq.store.time.time", return_value=time.time() + seconds):
            return call(*args)

    def stop(self, session):
        self.store.observe_activity(session, "offline", "hooks", lease=3600, priority=50)


class ClosingTests(Handshake):
    def test_your_answer_stays_open_until_the_agent_hears_and_closes_it(self):
        item = self.decide()
        decided = self.seen(item["id"])
        self.assertEqual((decided["status"], decided["bucket"]), ("answered", "pending"))
        self.assertEqual(decided["followup"]["stage"], "delivering")
        self.store.acknowledge(item["id"], "claude-code:a")
        self.assertEqual(self.seen(item["id"])["followup"]["stage"], "acknowledged")
        self.assertEqual(self.store.item(item["id"])["status"], "answered")  # heard is not done
        self.store.resolve(item["id"], "Switched the plan to annual")
        done = self.seen(item["id"])
        self.assertEqual((done["status"], done["followup"]), ("done", None))

    def test_mark_done_tells_the_agent_and_closes_once_it_hears(self):
        item = self.store.add("claude-code:a", "Upload the signed file", owner="human")
        marked = self.store.mark_done(item["id"], "Uploaded to the shared drive")
        self.assertEqual((marked["status"], marked["bucket"]), ("answered", "pending"))
        text = self.store.inbox("claude-code:a")[0]["text"]
        self.assertTrue(text.startswith("Done: The user marked this done."))
        self.assertIn("Uploaded to the shared drive", text)
        self.store.acknowledge(item["id"], "claude-code:a")
        self.assertEqual(self.store.item(item["id"])["status"], "done")
        with self.assertRaises(ValueError):
            self.store.mark_done(item["id"])

    def test_a_skip_delivered_straight_into_the_conversation_closes_too(self):
        item = self.store.add("claude-code:a", "Rename the bucket", owner="human")
        self.store.skip(item["id"])
        reply = self.store.inbox("claude-code:a")[0]
        self.store.delivery_result(reply["id"], "received")
        self.assertEqual(self.store.item(item["id"])["status"], "done")

    def test_closing_it_yourself_withdraws_a_reply_the_agent_never_heard(self):
        item = self.decide()
        closed = self.store.close(item["id"], "Handled it myself")
        self.assertEqual((closed["status"], closed["agent_note"]), ("done", "Handled it myself"))
        self.assertEqual(self.store.inbox("claude-code:a"), [])
        self.assertEqual(self.store.claim_hook_messages("claude-code:a"), ([], []))
        with self.assertRaises(ValueError):
            self.store.acknowledge(item["id"], "claude-code:a")
        with self.assertRaises(ValueError):
            self.store.close(self.store.add("claude-code:a", "Still open")["id"])

    def test_skips_that_never_reached_their_agent_are_tracked_again_after_upgrade(self):
        item = self.store.add("claude-code:a", "Old skip", owner="human")
        self.store.skip(item["id"])
        with sqlite3.connect(self.store.path) as db:  # how a skip used to look
            db.execute("UPDATE items SET status='done' WHERE id=?", (item["id"],))
            db.execute("PRAGMA user_version=6")
        reopened = Store(self.store.path)
        self.assertEqual(reopened.item(item["id"])["status"], "answered")
        reopened.acknowledge(item["id"], "claude-code:a")
        self.assertEqual(reopened.item(item["id"])["status"], "done")


class StuckTests(Handshake):
    def test_what_counts_as_stuck_and_who_may_take_it_over(self):
        now, day = time.time(), 86400
        item = {"status": "answered", "project": "harbor", "delivery": "pending", "replied": now - 60, "heard": None}
        session = lambda state, since=now: {"activity": {"state": state, "since": since}}
        cases = [
            (item, session("offline", now - 400), "stopped", True),
            (item, session("offline", now - 100), None, False),  # a moment offline is not stopped
            ({**item, "replied": now - 2 * day}, session("unknown"), "unheard", True),  # no signal for a day
            ({**item, "replied": now - 2 * day}, session("idle"), "unheard", False),  # alive, just quiet
            ({**item, "delivery": "received", "heard": now - 2 * day}, session("idle"), "unclosed", False),
            ({**item, "delivery": "received", "heard": now - 2 * day}, session("active"), None, False),
            ({**item, "handoff": True}, session("idle"), None, True),
            ({**item, "project": "other"}, session("offline", now - 400), "stopped", False),
        ]
        for given, owner, stuck, adoptable in cases:
            result = followup(given, owner, now)
            self.assertEqual((result["stuck"], result["adoptable"]), (stuck, adoptable), (given, owner))
        self.assertIsNone(followup({**item, "status": "open"}, session("idle"), now))

    def test_the_next_agent_in_the_project_adopts_a_decision_its_agent_left(self):
        item = self.decide()
        self.stop("claude-code:a")
        self.assertEqual(self.store.adoptable("claude-code:b"), [])  # only just stopped
        offered = self.later(600, self.store.adoptable, "claude-code:b")
        self.assertEqual([i["id"] for i in offered], [item["id"]])
        self.assertEqual(self.later(600, self.store.adoptable, "claude-code:c"), [])  # another project
        self.assertEqual(self.later(600, self.store.adoptable, "claude-code:a"), [])  # its own
        taken = self.later(600, self.store.adopt, item["id"], "claude-code:b")
        self.assertEqual((taken["answer"], taken["item"]["session_id"], taken["item"]["delivery"]), ("Annual", "claude-code:b", "received"))
        self.assertEqual(taken["item"]["adopted_from"]["session_id"], "claude-code:a")
        with self.assertRaises(ValueError):  # exactly one conversation wins
            self.later(600, self.store.adopt, item["id"], "claude-code:d")
        self.store.resolve(item["id"], "Moved the account to annual billing")
        self.assertEqual(self.store.item(item["id"])["status"], "done")

    def test_you_can_hand_a_live_agents_decision_to_another(self):
        item = self.decide()
        self.store.observe_activity("claude-code:a", "idle", "claude-live", lease=60)
        self.assertEqual(self.store.adoptable("claude-code:b"), [])
        self.store.hand_off(item["id"])
        self.assertEqual([i["id"] for i in self.store.adoptable("claude-code:b")], [item["id"]])
        adopted = self.store.adopt(item["id"], "claude-code:b")["item"]
        self.assertFalse(adopted["handoff"])
        with self.assertRaises(ValueError):
            self.store.hand_off(self.store.add("claude-code:a", "Open")["id"])


class HookTests(Handshake):
    def run_hook(self, external, event, cwd, at=0):
        out = []
        payload = {"session_id": external, "hook_event_name": event, "cwd": cwd}
        self.later(at, handle, "claude-code", payload, self.store, out.append)
        return "\n".join(o.get("hookSpecificOutput", {}).get("additionalContext", "") for o in out)

    def test_a_new_agent_in_the_project_is_told_about_stuck_decisions(self):
        item = self.decide()
        self.stop("claude-code:a")
        start = self.run_hook("b", "SessionStart", HARBOR_TWO, at=600)
        self.assertIn(item["id"], start)
        self.assertIn("hq adopt", start)
        self.assertNotIn(item["id"], self.run_hook("b", "UserPromptSubmit", HARBOR_TWO, at=600))
        self.assertNotIn(item["id"], self.run_hook("c", "SessionStart", LANTERN, at=600))

    def test_an_agent_that_heard_but_never_closed_is_reminded_now_and_then(self):
        item = self.decide("claude-code:b")
        self.store.acknowledge(item["id"], "claude-code:b")
        self.assertNotIn("HQ reminder", self.run_hook("b", "UserPromptSubmit", HARBOR_TWO))  # just heard
        reminded = self.run_hook("b", "UserPromptSubmit", HARBOR_TWO, at=7200)
        self.assertIn("HQ reminder", reminded)
        self.assertIn(f"hq done {item['id']}", reminded)
        self.assertNotIn("HQ reminder", self.run_hook("b", "UserPromptSubmit", HARBOR_TWO, at=7300))
        self.assertIn("HQ reminder", self.run_hook("b", "SessionStart", HARBOR_TWO, at=7200 + 6 * 3600 + 1))


if __name__ == "__main__":
    unittest.main()

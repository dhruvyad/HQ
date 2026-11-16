"""A relay holds remote agents' conversations on an always-on server and syncs them with your Mac.
Every test runs a real relay on a loopback port and a Mac inbox, each in its own temporary
folder: never the real inbox, and never a network beyond this machine."""
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

from hq import relay, remote
from hq.store import Store

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {"owner": "human", "minutes": 1, "options": ["A", "B"], "matters": {"lead": "x"}, "needs_you": {"lead": "y"}}


class Relayed(unittest.TestCase):
    def setUp(self):
        here, there = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.addCleanup(here.cleanup)
        self.addCleanup(there.cleanup)
        self.mac = Store(Path(here.name) / "hq.sqlite3")
        self.mac.register("claude-code:local", "A local agent", "claude-code", cwd="/Users/me/code/noclick")
        self.local = self.mac.add("claude-code:local", "Local agent's private question", kind="question")
        self.relay_store = Store(Path(there.name) / "hq.sqlite3")
        self.hub = relay.Relay(self.relay_store)
        self.endpoint = remote.Endpoint(self.relay_store, port=0, log=lambda line: None, relay=self.hub)
        threading.Thread(target=self.endpoint.serve_forever, daemon=True).start()
        self.addCleanup(self.endpoint.server_close)
        self.addCleanup(self.endpoint.shutdown)
        self.addCleanup(self.endpoint.closing.set)
        self.port = self.endpoint.server_address[1]
        made = relay.connect(self.mac, f"http://127.0.0.1:{self.port}")
        self.hub.allow(made["hash"], "test-mac")
        self.sync = relay.RelaySync(self.mac)
        self.token = self.mac.add_token("alpha", "noclick")
        self.sync.push_once()

    # -- An agent on the relay, over HTTP ----------------------------------------------------------

    def post(self, body, token=None, path="/mcp", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        sent = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})}
        if token:
            sent["Authorization"] = f"Bearer {token}"
        conn.request("POST", path, json.dumps(body), sent)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data

    def call(self, name, arguments, token=None):
        response, data = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}}, token or self.token["token"])
        self.assertEqual(response.status, 200, data)
        if response.getheader("Content-Type") == "text/event-stream":
            data = [line[6:] for line in data.decode().splitlines() if line.startswith("data: ")][-1]
        result = json.loads(data)["result"]
        return json.loads(result["content"][0]["text"]) if not result["isError"] else result["content"][0]["text"]

    def agent_asks(self, sid="noclick-agent:1", title="Pick a name"):
        self.call("hq_register_session", {"session_id": sid, "name": "NoClick agent 1", "external_id": "1"})
        return self.call("hq_report", {"session_id": sid, "title": title, "kind": "question", **FIELDS})

    def item(self, store, id):
        return store.item(id)

    def outbox(self):
        with self.mac.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM relay_outbox ORDER BY seq")]

    def synced(self):
        self.sync.push_once(force=True)
        while self.sync.pull_once(wait=0):
            pass


class RoundTripTests(Relayed):
    def test_an_agent_asks_on_the_relay_you_answer_on_your_mac_and_the_loop_closes(self):
        asked = self.agent_asks()
        self.synced()
        mirrored = self.mac.item(asked["id"])
        self.assertEqual((mirrored["title"], mirrored["status"], mirrored["project"]), ("Pick a name", "open", "noclick"))
        session = self.mac.session("noclick-agent:1")
        self.assertEqual((session["provider"], session["mirror"], session["remote_token"]), ("remote", 1, self.token["id"]))
        # The agent waits on the relay while you answer on your Mac.
        heard = {}
        waiting = threading.Thread(target=lambda: heard.update(self.call(
            "hq_wait", {"session_id": "noclick-agent:1", "item_id": asked["id"], "timeout": 15})))
        waiting.start()
        self.mac.reply(asked["id"], "Use B")
        self.assertEqual([o["op"] for o in self.outbox()], ["reply"])
        self.sync.push_once()
        waiting.join(10)
        self.assertEqual((heard["status"], heard["answer"]), ("received", "Use B"))
        self.synced()
        self.assertEqual(self.mac.item(asked["id"])["delivery"], "received")
        self.call("hq_resolve", {"session_id": "noclick-agent:1", "item_id": asked["id"], "note": "Named it B"})
        self.synced()
        closed = self.mac.item(asked["id"])
        self.assertEqual((closed["status"], closed["agent_note"]), ("done", "Named it B"))
        self.assertEqual(self.outbox()[0]["state"], "sent")

    def test_while_the_mac_sleeps_agents_keep_reporting_and_it_catches_up_on_waking(self):
        for n in range(7):
            self.agent_asks(title=f"Question {n}")
        later = relay.RelaySync(self.mac)  # a fresh start, as after a restart
        original = relay.PULL_LIMIT
        try:
            relay.PULL_LIMIT = 3
            self.assertTrue(later.pull_once(wait=0))
        finally:
            relay.PULL_LIMIT = original
        while later.pull_once(wait=0):
            pass
        titles = sorted(i["title"] for i in self.mac.snapshot(session_id="noclick-agent:1")["items"])
        self.assertEqual(titles, [f"Question {n}" for n in range(7)])
        # Nothing new: a pull that waits returns empty once its wait is up.
        started = time.monotonic()
        self.assertFalse(later.pull_once(wait=1))
        self.assertGreaterEqual(time.monotonic() - started, 0.9)

    def test_a_pull_wakes_as_soon_as_an_agent_reports(self):
        self.synced()
        threading.Timer(0.5, self.agent_asks).start()
        started = time.monotonic()
        while not self.mac.snapshot(session_id="noclick-agent:1")["items"] and time.monotonic() - started < 10:
            self.sync.pull_once(wait=10)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(self.mac.snapshot(session_id="noclick-agent:1")["items"]), 1)

    def test_the_worker_carries_an_answer_out_while_a_pull_waits(self):
        asked = self.agent_asks()
        self.synced()
        worker = relay.RelaySync(self.mac)
        threading.Thread(target=worker.run, daemon=True).start()
        self.addCleanup(worker.stop.set)
        time.sleep(0.5)  # its pull is now waiting on the relay
        self.mac.reply(asked["id"], "Use A")
        heard = self.call("hq_wait", {"session_id": "noclick-agent:1", "item_id": asked["id"], "timeout": 10})
        self.assertEqual(heard["answer"], "Use A")
        deadline = time.monotonic() + 10
        while self.mac.item(asked["id"])["delivery"] != "received" and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.mac.item(asked["id"])["delivery"], "received")


    def test_stopping_the_worker_ends_its_long_poll_at_once(self):
        worker = relay.RelaySync(self.mac)
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        time.sleep(1)  # its pull now waits on the relay for up to 25 seconds
        started = time.monotonic()
        worker.stop.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 2)


class ActionTests(Relayed):
    def test_every_kind_of_answer_reaches_the_agent(self):
        ids = [self.agent_asks(title=t)["id"] for t in ("Skip me", "I did it", "Pin me", "Snooze me")]
        self.synced()
        self.mac.skip(ids[0], "Not needed")
        self.mac.mark_done(ids[1], "Done by hand")
        self.mac.update(ids[2], pinned=True)
        self.mac.update(ids[3], defer_until="2099-01-01")
        self.synced()
        there = {i: self.relay_store.item(i) for i in ids}
        self.assertTrue(there[ids[0]]["answer"].startswith("Skipped:") and there[ids[0]]["answer"].endswith("Not needed"))
        self.assertTrue(there[ids[1]]["answer"].startswith("Done:"))
        self.assertTrue(there[ids[2]]["pinned"])
        self.assertEqual(there[ids[3]]["defer_until"], "2099-01-01")
        inbox = self.call("hq_inbox", {"session_id": "noclick-agent:1"})
        self.assertEqual({r["item_id"] for r in inbox}, set(ids[:2]))
        for item_id in ids[:2]:
            self.call("hq_acknowledge", {"session_id": "noclick-agent:1", "item_id": item_id})
        self.synced()
        # A skip or a done closes once its agent has heard it, on both sides.
        self.assertEqual([self.mac.item(i)["status"] for i in ids], ["done", "done", "open", "open"])
        self.assertEqual(self.mac.item(ids[3])["bucket"], "later")

    def test_you_ask_the_agent_and_its_answer_comes_back(self):
        asked = self.agent_asks()
        self.synced()
        question = self.mac.ask_agent(asked["id"], "Which name is shorter?")
        self.synced()
        pending = self.call("hq_comments", {"session_id": "noclick-agent:1", "pending": True})
        self.assertEqual([(c["id"], c["text"]) for c in pending], [(question["id"], "Which name is shorter?")])
        self.call("hq_comment_reply", {"session_id": "noclick-agent:1", "comment_id": question["id"], "text": "B is"})
        self.synced()
        thread = self.mac.item(asked["id"])["comments"]
        self.assertEqual([(c["author"], c["text"]) for c in thread], [("human", "Which name is shorter?"), ("agent", "B is")])
        self.assertFalse(self.mac.item(asked["id"])["asking"])

    def test_a_question_you_cancel_is_cancelled_there_too(self):
        asked = self.agent_asks()
        self.synced()
        question = self.mac.ask_agent(asked["id"], "Never mind?")
        self.mac.cancel_comment(question["id"])
        self.synced()
        self.assertEqual(self.relay_store.comment(question["id"])["state"], "cancelled")
        self.assertEqual(self.call("hq_comments", {"session_id": "noclick-agent:1", "pending": True}), [])

    def test_closing_a_decided_item_yourself_withdraws_an_unheard_answer(self):
        asked = self.agent_asks()
        self.synced()
        self.mac.reply(asked["id"], "Use B")
        self.mac.close(asked["id"], "Handled it")
        self.synced()
        there = self.relay_store.item(asked["id"])
        self.assertEqual((there["status"], there["delivery"]), ("done", "withdrawn"))
        self.assertEqual(self.call("hq_inbox", {"session_id": "noclick-agent:1"}), [])

    def test_an_action_on_its_way_is_never_undone_by_a_pull(self):
        asked = self.agent_asks()
        self.synced()
        self.mac.reply(asked["id"], "Use B")
        self.sync.pull_once(wait=0)  # before the answer went out
        self.assertEqual(self.mac.item(asked["id"])["status"], "answered")
        self.assertEqual(self.mac.item(asked["id"])["answer"], "Use B")
        self.synced()
        self.assertEqual(self.relay_store.item(asked["id"])["answer"], "Use B")

    def test_an_action_the_relay_refuses_is_reported_and_the_relay_wins(self):
        asked = self.agent_asks()
        self.synced()
        self.call("hq_resolve", {"session_id": "noclick-agent:1", "item_id": asked["id"], "note": "Went with A"})
        self.mac.reply(asked["id"], "Use B")  # this Mac had not heard yet
        self.synced()
        self.assertEqual(self.mac.item(asked["id"])["status"], "done")
        self.assertIsNone(self.mac.item(asked["id"])["answer"])
        refused = relay.status(self.mac)["refused"]
        self.assertEqual([(r["op"], r["title"]) for r in refused], [("reply", "Pick a name")])
        self.assertIn("Only open items", refused[0]["error"])

    def test_an_action_sent_twice_is_applied_once(self):
        asked = self.agent_asks()
        self.synced()
        question = self.mac.ask_agent(asked["id"], "Sure?")
        self.mac.reply(asked["id"], "Use B")
        self.sync.push_once()
        with self.mac.db() as db:  # as if the Mac stopped before it wrote down that they were sent
            db.execute("UPDATE relay_outbox SET state='pending',sent=NULL")
        self.sync.push_once()
        self.assertEqual([o["state"] for o in self.outbox()], ["sent", "sent"])
        with self.relay_store.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM comments WHERE id=?", (question["id"],)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM replies").fetchone()[0], 1)


class BoundaryTests(Relayed):
    def test_this_macs_own_conversations_never_reach_the_relay(self):
        self.mac.update(self.local["id"], pinned=True)
        self.mac.ask_agent(self.local["id"], "A question of mine")
        self.mac.reply(self.local["id"], "My answer with a pasted secret")
        self.assertEqual(self.outbox(), [])
        self.synced()
        with self.relay_store.db() as db:
            for table in ("sessions", "items", "replies", "comments"):
                text = json.dumps([dict(r) for r in db.execute(f"SELECT * FROM {table}")])
                self.assertNotIn("pasted secret", text)
                self.assertNotIn("claude-code:local", text)

    def test_only_this_macs_key_syncs_and_browsers_never_do(self):
        for headers in ({}, {"Authorization": "Bearer hqs_wrong"}, {"Authorization": f"Bearer {self.token['token']}"}):
            response, _ = self.post({"ops": []}, path="/sync/push", headers=headers)
            self.assertEqual(response.status, 401)
        key = relay.load_config(self.mac)["key"]
        response, _ = self.post({"ops": []}, path="/sync/push", headers={"Authorization": f"Bearer {key}", "Origin": "https://evil.example"})
        self.assertEqual(response.status, 403)
        # The sync key is not an agent's token either.
        response, _ = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=key)
        self.assertEqual(response.status, 401)
        # The relay keeps hashes only.
        with self.relay_store.db() as db:
            stored = json.dumps([dict(r) for r in db.execute("SELECT * FROM relay_keys")] +
                                [dict(r) for r in db.execute("SELECT * FROM remote_tokens")])
        self.assertNotIn(key, stored)
        self.assertNotIn(self.token["token"], stored)

    def test_your_mac_decides_which_agents_exist_and_revoking_stops_one(self):
        self.agent_asks()
        self.mac.revoke_token("alpha")
        self.sync.push_once()
        response, _ = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=self.token["token"])
        self.assertEqual(response.status, 401)
        beta = self.mac.add_token("beta")
        self.sync.push_once()
        self.assertEqual(self.call("hq_register_session", {"session_id": "noclick-agent:2", "name": "Two"}, beta["token"])["id"],
                         "noclick-agent:2")
        with self.relay_store.db() as db:
            self.assertEqual(sorted(r["name"] for r in db.execute("SELECT name FROM remote_tokens")), ["alpha", "beta"])
        # Last use comes back to your Mac.
        self.synced()
        self.assertIsNotNone([t for t in self.mac.tokens() if t["name"] == "beta"][0]["last_used"])

    def test_on_your_mac_only_the_agent_itself_acts_for_its_conversation(self):
        asked = self.agent_asks()
        self.synced()
        for attempt in (lambda: self.mac.add("noclick-agent:1", "Sneaky"),
                        lambda: self.mac.register("noclick-agent:1", "Hijack", "claude-code"),
                        lambda: self.mac.resolve(asked["id"]),
                        lambda: self.mac.update(asked["id"], "noclick-agent:1", title="Renamed"),
                        lambda: self.mac.hand_off(asked["id"])):
            with self.assertRaises(ValueError):
                attempt()
        self.mac.reply(asked["id"], "Use B")
        for attempt in (lambda: self.mac.acknowledge(asked["id"], "noclick-agent:1"),
                        lambda: self.mac.wait(asked["id"], "noclick-agent:1", 1)):
            with self.assertRaises(ValueError):
                attempt()
        self.assertEqual(self.mac.adoptable("claude-code:local"), [])

    def test_files_cant_be_sent_to_an_agent_elsewhere(self):
        from hq import attachments
        asked = self.agent_asks()
        self.synced()
        meta = attachments.save(self.mac.path.parent, b"hello", "text/plain", "note.txt")
        with self.assertRaisesRegex(ValueError, "Files can't reach"):
            self.mac.reply(asked["id"], "See the file", [meta["id"]])
        self.assertEqual(self.mac.item(asked["id"])["status"], "open")
        self.assertEqual(self.outbox(), [])

    def test_a_relay_started_afresh_is_read_again_from_the_start(self):
        self.agent_asks()
        self.synced()
        with self.relay_store.db() as db:
            db.execute("UPDATE meta SET value='another' WHERE key='relay_instance'")
        self.hub.instance = "another"
        self.assertTrue(self.sync.pull_once(wait=0))
        self.assertEqual(self.sync.meta("relay_cursor"), "0")
        self.synced()
        self.assertEqual(self.sync.meta("relay_instance"), "another")


class ReachTests(unittest.TestCase):
    def test_an_address_that_never_answers_costs_a_moment_not_a_timeout(self):
        # Like a network whose IPv6 is broken: the first address swallows the connection.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        dead = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port))  # TEST-NET-1: never routed
        live = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))
        with unittest.mock.patch("socket.getaddrinfo", return_value=[dead, dead, live]):
            started = time.monotonic()
            sock = relay.happy_connect("relay.invalid", port, timeout=10)
        self.addCleanup(sock.close)
        self.assertEqual(sock.getpeername()[0], "127.0.0.1")
        self.assertLess(time.monotonic() - started, 3)

    def test_nothing_answering_fails_within_the_timeout(self):
        dead = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 9))
        with unittest.mock.patch("socket.getaddrinfo", return_value=[dead]):
            started = time.monotonic()
            with self.assertRaises(OSError):
                relay.happy_connect("relay.invalid", 9, timeout=1)
        self.assertLess(time.monotonic() - started, 3)


class CLITests(unittest.TestCase):
    def run_hq(self, home, *args):
        result = subprocess.run([sys.executable, "-m", "hq", *args], cwd=ROOT, capture_output=True, text=True, timeout=60,
                                env={**os.environ, "HQ_HOME": home})
        return result

    def test_connecting_keeps_the_key_on_the_mac_and_prints_only_its_hash(self):
        with tempfile.TemporaryDirectory() as home:
            result = self.run_hq(home, "relay", "connect", "https://relay.invalid")
            self.assertEqual(result.returncode, 0, result.stderr)
            shown = json.loads(result.stdout)
            config = Path(home) / "relay.json"
            key = json.loads(config.read_text())["key"]
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(key, result.stdout)
            self.assertIn(relay.token_hash(key), shown["run_on_relay"])
            self.assertFalse(shown["reachable"])
            status = json.loads(self.run_hq(home, "relay", "status").stdout)
            self.assertEqual((status["connected"], status["url"]), (True, "https://relay.invalid"))
            # A Mac that syncs with a relay isn't one; agents added here print the relay's address.
            self.assertNotEqual(self.run_hq(home, "relay", "allow", "a" * 64).returncode, 0)
            added = json.loads(self.run_hq(home, "remote", "add", "alpha").stdout)
            self.assertEqual(added["mcp_server"]["url"], "https://relay.invalid/mcp")
            self.assertNotEqual(self.run_hq(home, "relay", "connect", "http://relay.invalid").returncode, 0)

    def test_a_relay_takes_its_agents_from_your_mac(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(self.run_hq(home, "relay", "allow", "a" * 64, "--name", "mac").returncode, 0)
            self.assertIn("mac", self.run_hq(home, "relay", "keys").stdout)
            refused = self.run_hq(home, "remote", "add", "alpha")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("on your Mac", refused.stdout + refused.stderr)
            self.assertNotEqual(self.run_hq(home, "relay", "connect", "https://other.invalid").returncode, 0)


if __name__ == "__main__":
    unittest.main()

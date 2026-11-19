"""Agents running elsewhere reach HQ's tools over Streamable HTTP, each with its own token. Every
test runs its own endpoint on a loopback port against a temporary inbox: never the real one."""
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from hq import remote
from hq.store import Store

ROOT = Path(__file__).resolve().parents[1]


class Endpoint(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "hq.sqlite3")
        self.store.register("claude-code:local", "A local agent", "claude-code", cwd="/Users/me/code/noclick")
        self.local = self.store.add("claude-code:local", "Local agent's private question", kind="question")
        self.store.reply(self.local["id"], "My answer with a pasted secret")
        self.alpha, self.beta = self.store.add_token("alpha", "noclick"), self.store.add_token("beta")
        self.logs = []
        self.endpoint = remote.Endpoint(self.store, port=0, log=self.logs.append)
        threading.Thread(target=self.endpoint.serve_forever, daemon=True).start()
        self.addCleanup(self.endpoint.server_close)
        self.addCleanup(self.endpoint.shutdown)
        self.port = self.endpoint.server_address[1]

    def open(self, method, body=None, token=None, headers=None, path="/mcp"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})}
        if token:
            sent["Authorization"] = f"Bearer {token['token']}"
        conn.request(method, path, body if isinstance(body, (bytes, str)) or body is None else json.dumps(body), sent)
        return conn, conn.getresponse()

    def post(self, body, token=None, headers=None, path="/mcp"):
        conn, response = self.open("POST", body, token, headers, path)
        data = response.read()
        conn.close()
        return response, data

    @staticmethod
    def events(data):
        return [json.loads(line[6:]) for line in data.decode().splitlines() if line.startswith("data: ")]

    def call(self, token, name, arguments, id=1, params=None):
        response, data = self.post({"jsonrpc": "2.0", "id": id, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments, **(params or {})}}, token)
        self.assertEqual(response.status, 200, data)
        message = self.events(data)[-1] if response.getheader("Content-Type") == "text/event-stream" else json.loads(data)
        return message["result"]

    def ok(self, token, name, arguments, **kw):
        result = self.call(token, name, arguments, **kw)
        self.assertFalse(result["isError"], result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])

    def refused(self, token, name, arguments):
        result = self.call(token, name, arguments)
        self.assertTrue(result["isError"], result)
        return result["content"][0]["text"]

    def register(self, token, sid, **fields):
        return self.ok(token, "hq_register_session", {"session_id": sid, "name": f"Agent {sid}", **fields})

    def ask(self, token, sid, title="Pick a name"):
        return self.ok(token, "hq_report", {"session_id": sid, "title": title, "kind": "question", "owner": "human", "minutes": 1,
                                            "options": ["A", "B"], "matters": {"lead": "x"}, "needs_you": {"lead": "y"}})


class ConnectTests(Endpoint):
    def test_an_agent_connects_and_sees_every_tool_but_taking_over_others(self):
        response, data = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "agent", "version": "1"}}}, self.alpha)
        init = json.loads(data)["result"]
        self.assertEqual((response.status, init["protocolVersion"], init["serverInfo"]["name"]), (200, "2025-06-18", "hq"))
        self.assertIn("remote agent alpha", init["instructions"])
        _, data = self.post({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}, self.alpha)
        self.assertEqual(json.loads(data)["result"]["protocolVersion"], remote.PROTOCOLS[0])
        response, data = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.alpha)
        self.assertEqual((response.status, data), (202, b""))
        _, data = self.post({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, self.alpha)
        names = {t["name"] for t in json.loads(data)["result"]["tools"]}
        self.assertIn("hq_request_input", names)
        self.assertNotIn("hq_adopt", names)
        _, data = self.post([{"jsonrpc": "2.0", "id": 4, "method": "ping"}, {"jsonrpc": "2.0", "id": 5, "method": "nope"}], self.alpha)
        self.assertEqual([m.get("result", m.get("error", {}).get("code")) for m in json.loads(data)], [{}, -32601])

    def test_without_a_working_token_or_from_a_browser_nothing_gets_in(self):
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        response, _ = self.post(ping)
        self.assertEqual((response.status, response.getheader("WWW-Authenticate")), (401, 'Bearer realm="hq"'))
        self.assertEqual(self.post(ping, {"token": "hqr_" + "x" * 43})[0].status, 401)
        self.assertEqual(self.post(ping, {"token": self.alpha["token"]}, {"Origin": "https://evil.example"})[0].status, 403)
        self.assertEqual(self.post(ping, self.alpha, path="/api/state")[0].status, 404)
        self.assertEqual(self.post(ping, self.alpha, {"MCP-Protocol-Version": "1999-01-01"})[0].status, 400)
        self.assertEqual(self.post("x", self.alpha, {"Content-Length": str(remote.BODY_LIMIT + 1)})[0].status, 413)
        self.assertEqual(self.post("{not json", self.alpha)[0].status, 400)
        conn, response = self.open("GET", token=self.alpha)
        self.assertEqual((response.status, response.getheader("Allow")), (405, "POST"))
        conn.close()
        conn, response = self.open("GET", path="/health")
        self.assertEqual((response.status, json.loads(response.read())), (200, {"ok": True}))
        conn.close()
        self.store.revoke_token("alpha")
        self.assertEqual(self.post(ping, self.alpha)[0].status, 401)
        self.assertEqual(self.post(ping, self.beta)[0].status, 200)

    def test_it_never_listens_beyond_this_machine(self):
        with self.assertRaisesRegex(ValueError, "this machine only"):
            remote.Endpoint(self.store, host="0.0.0.0", port=0)

    def test_tokens_are_kept_as_hashes_and_their_last_use_is_written_down(self):
        self.assertIsNone(self.store.tokens()[0]["last_used"])
        self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, self.alpha)
        self.assertIsNotNone(self.store.tokens()[0]["last_used"])
        self.assertNotIn("hash", self.store.tokens()[0])
        stored = b"".join(p.read_bytes() for p in self.store.path.parent.glob("hq.sqlite3*"))
        for token in (self.alpha["token"], self.beta["token"]):
            self.assertNotIn(token.encode(), stored)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.store.add_token("alpha")

    def test_too_many_requests_are_told_when_to_come_back(self):
        with patch.object(remote, "BURST", 3), patch.object(remote, "PER_SECOND", 0.01):
            self.endpoint.buckets.clear()
            statuses = [self.post({"jsonrpc": "2.0", "id": n, "method": "ping"}, self.alpha)[0] for n in range(4)]
        self.assertEqual([r.status for r in statuses], [200, 200, 200, 429])
        self.assertGreaterEqual(int(statuses[-1].getheader("Retry-After")), 1)
        self.assertEqual(self.post({"jsonrpc": "2.0", "id": 9, "method": "ping"}, self.beta)[0].status, 200)  # its own allowance


class ScopeTests(Endpoint):
    def test_an_agent_sees_and_acts_on_only_the_conversations_it_registered(self):
        mine = self.register(self.alpha, "noclick-agent:a1", cwd="/workspace/noclick", external_id="agent-a1")
        self.assertEqual((mine["provider"], mine["remote_token"]), ("remote", self.alpha["id"]))
        item = self.ask(self.alpha, "noclick-agent:a1")
        self.register(self.beta, "noclick-agent:b1")
        self.assertIn("another agent", self.refused(self.beta, "hq_register_session", {"session_id": "noclick-agent:a1", "name": "Mine"}))
        self.assertIn("another agent", self.refused(self.beta, "hq_register_session", {"session_id": "claude-code:local", "name": "Mine"}))
        self.assertIn("this Mac's agents", self.refused(self.beta, "hq_register_session", {"session_id": "codex:new", "name": "Mine"}))
        for name, args in [("hq_inbox", {"session_id": "noclick-agent:a1"}), ("hq_inbox", {"session_id": "claude-code:local"}),
                           ("hq_wait", {"session_id": "noclick-agent:b1", "item_id": item["id"], "timeout": 1}),
                           ("hq_update", {"session_id": "noclick-agent:b1", "item_id": item["id"], "title": "Taken"}),
                           ("hq_acknowledge", {"session_id": "noclick-agent:b1", "item_id": self.local["id"]}),
                           ("hq_resolve", {"session_id": "noclick-agent:b1", "item_id": item["id"]}),
                           ("hq_comments", {"session_id": "noclick-agent:b1", "item_id": item["id"]}),
                           ("hq_adopt", {"session_id": "noclick-agent:b1"}),
                           ("hq_list", {"session_id": "claude-code:local"})]:
            self.refused(self.beta, name, args)
        listed = self.ok(self.beta, "hq_list", {})
        self.assertEqual(([s["id"] for s in listed["sessions"]], listed["items"]), (["noclick-agent:b1"], []))
        listed = self.ok(self.alpha, "hq_list", {})
        self.assertEqual([i["id"] for i in listed["items"]], [item["id"]])
        self.assertNotIn("pasted secret", json.dumps(listed))
        self.assertEqual(self.store.item(self.local["id"])["delivery"], "pending")  # never marked heard
        with self.assertRaisesRegex(ValueError, "another agent"):
            self.store.register("noclick-agent:a1", "Taken locally", "claude-code")
        # The guide lists workstreams of its own project, not the rest of your HQ.
        self.assertEqual(set(self.ok(self.alpha, "hq_reporting_guide", {})["projects"]), {"noclick"})
        self.assertEqual(self.ok(self.beta, "hq_reporting_guide", {})["projects"], {})

    def test_an_agent_elsewhere_cannot_have_hq_read_files_on_this_mac(self):
        shot = self.store.path.parent / "private.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        self.register(self.alpha, "noclick-agent:a1")
        item = self.ask(self.alpha, "noclick-agent:a1")
        for name, args in (("hq_report", {"session_id": "noclick-agent:a1", "title": "Look", "media": [str(shot)]}),
                           ("hq_update", {"session_id": "noclick-agent:a1", "item_id": item["id"], "media": [str(shot)]})):
            self.assertIn("agents on this Mac", self.refused(self.alpha, name, args))
        self.assertEqual(self.store.item(item["id"])["media"], [])
        self.assertFalse((self.store.path.parent / "attachments").exists() and any((self.store.path.parent / "attachments").iterdir()))

    def test_an_answer_reaches_the_agent_that_asked_and_the_loop_closes(self):
        self.register(self.alpha, "noclick-agent:a1")
        item = self.ask(self.alpha, "noclick-agent:a1")
        self.store.reply(item["id"], "Call it Atlas")
        inbox = self.ok(self.alpha, "hq_inbox", {"session_id": "noclick-agent:a1"})
        self.assertEqual([r["text"] for r in inbox], ["Call it Atlas"])
        self.ok(self.alpha, "hq_acknowledge", {"session_id": "noclick-agent:a1", "item_id": item["id"]})
        self.assertEqual(self.store.item(item["id"])["delivery"], "received")
        self.ok(self.alpha, "hq_resolve", {"session_id": "noclick-agent:a1", "item_id": item["id"], "note": "Named it Atlas"})
        self.assertEqual(self.store.item(item["id"])["status"], "done")


class WaitTests(Endpoint):
    def waiting(self, token, arguments, id=1, params=None):
        """Starts a waiting call; returns its connection and response for reading as it streams."""
        return self.open("POST", {"jsonrpc": "2.0", "id": id, "method": "tools/call",
                                  "params": {"name": "hq_request_input" if "title" in arguments else "hq_wait",
                                             "arguments": arguments, **(params or {})}}, token)

    def test_a_wait_streams_keepalives_then_the_answer(self):
        self.register(self.alpha, "noclick-agent:a1")
        with patch.object(remote, "KEEPALIVE", 0.05):
            conn, response = self.waiting(self.alpha, {"session_id": "noclick-agent:a1", "title": "Ship it today?", "options": ["Yes", "No"],
                                                       "minutes": 1, "matters": {"lead": "x"}, "needs_you": {"lead": "y"}, "timeout": 10}, params={"_meta": {"progressToken": "p1"}})
            self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
            for _ in range(200):
                asked = [i for i in self.store.snapshot()["items"] if i["title"] == "Ship it today?"]
                if asked:
                    break
                time.sleep(0.02)
            time.sleep(0.2)
            self.store.reply(asked[0]["id"], "Yes")
            data = response.read()
            conn.close()
        self.assertTrue(data.startswith(b": waiting for an answer in HQ\n\n"), data[:60])
        self.assertIn(b": still waiting for an answer in HQ", data)
        messages = self.events(data)
        self.assertTrue(any(m.get("method") == "notifications/progress" and m["params"]["progressToken"] == "p1" for m in messages))
        answer = json.loads(messages[-1]["result"]["content"][0]["text"])
        self.assertEqual((answer["status"], answer["answer"]), ("received", "Yes"))
        self.assertEqual(self.store.item(asked[0]["id"])["delivery"], "received")

    def test_a_cancelled_or_abandoned_wait_stops_waiting(self):
        self.register(self.alpha, "noclick-agent:a1")
        item = self.ask(self.alpha, "noclick-agent:a1")
        with patch.object(remote, "KEEPALIVE", 0.05):
            conn, response = self.waiting(self.alpha, {"session_id": "noclick-agent:a1", "item_id": item["id"], "timeout": 30}, id=7)
            response.fp.readline()
            self.post({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}}, self.alpha)
            answer = json.loads(self.events(response.read())[-1]["result"]["content"][0]["text"])
            conn.close()
            self.assertEqual(answer["status"], "cancelled")
            conn, response = self.waiting(self.alpha, {"session_id": "noclick-agent:a1", "item_id": item["id"], "timeout": 30}, id=8)
            response.fp.readline()
            response.close()  # the agent goes away mid-wait
            conn.close()
            for _ in range(200):
                if not self.endpoint.busy.get(self.alpha["id"]):
                    break
                time.sleep(0.02)
        self.assertEqual(self.endpoint.busy.get(self.alpha["id"]), 0)
        self.assertEqual(self.endpoint.cancels, {})

    def test_waits_are_capped_and_an_agent_can_hold_only_a_few_open(self):
        self.register(self.alpha, "noclick-agent:a1")
        item = self.ask(self.alpha, "noclick-agent:a1")
        self.assertIn("between 1 and 600", self.refused(self.alpha, "hq_wait", {"session_id": "noclick-agent:a1", "item_id": item["id"],
                                                                                "timeout": 601}))
        with patch.object(remote, "CONCURRENT", 1), patch.object(remote, "KEEPALIVE", 0.05):
            conn, response = self.waiting(self.alpha, {"session_id": "noclick-agent:a1", "item_id": item["id"], "timeout": 2})
            response.fp.readline()
            text = self.refused(self.alpha, "hq_wait", {"session_id": "noclick-agent:a1", "item_id": item["id"], "timeout": 1})
            self.assertIn("1 waits open", text)
            response.read()
            conn.close()

    def test_revoking_a_token_ends_its_waits(self):
        self.register(self.alpha, "noclick-agent:a1")
        item = self.ask(self.alpha, "noclick-agent:a1")
        with patch.object(remote, "KEEPALIVE", 0.05):
            conn, response = self.waiting(self.alpha, {"session_id": "noclick-agent:a1", "item_id": item["id"], "timeout": 30})
            response.fp.readline()
            self.store.revoke_token("alpha")
            answer = json.loads(self.events(response.read())[-1]["result"]["content"][0]["text"])
            conn.close()
        self.assertEqual(answer["status"], "cancelled")


class CLITests(unittest.TestCase):
    def test_adding_an_agent_shows_its_token_once_with_the_config_to_paste(self):
        with tempfile.TemporaryDirectory() as home:
            run = lambda *args: subprocess.run([sys.executable, str(ROOT / "bin/hq"), "remote", *args], capture_output=True, text=True,
                                               env={**os.environ, "HQ_HOME": home}, timeout=30)
            made = json.loads(run("add", "noclick1", "--project", "noclick", "--url", "https://hq.example.com/").stdout)
            self.assertTrue(made["token"].startswith("hqr_"))
            self.assertEqual(made["secret_value"], f"Bearer {made['token']}")
            self.assertEqual(made["mcp_server"], {"url": "https://hq.example.com/mcp", "headers": {"Authorization": {"secret": "HQ_TOKEN"}}})
            listed = run("list").stdout
            self.assertNotIn(made["token"], listed)
            self.assertEqual([a["name"] for a in json.loads(listed)["agents"]], ["noclick1"])
            self.assertIsNotNone(json.loads(run("revoke", "noclick1").stdout)["revoked"])
            self.assertIn("No remote agent named", run("revoke", "noclick1").stderr + run("revoke", "noclick1").stdout)


if __name__ == "__main__":
    unittest.main()

import concurrent.futures
import io
import json
import os
import socket
import struct
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest.mock import Mock, patch

from hq.adapters import sync_codex
from hq.cli import parser
from hq.connections import connect_chatgpt, install_hooks, launch
from hq.hooks import handle, run
from hq.store import Store
from hq.websocket import MAX_FRAME, UnixWebSocket


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.sid = "claude-code:one"
        self.store.register(self.sid, "Keep this conversation title", "claude-code")
        self.store.register("claude-code:two", "Other conversation", "claude-code")

    def event(self, event="PostToolUse", **extra):
        return {"session_id": "one", "cwd": self.temp.name, "hook_event_name": event, **extra}

    def activity(self, sid=None):
        return next(s for s in self.store.snapshot()["sessions"] if s["id"] == (sid or self.sid))["activity"]["state"]

    def test_running_subagents_keep_a_finished_turn_active(self):
        # The live inventory says the turn is idle; the turn's own Stop says subagents are working.
        self.store.observe_activity(self.sid, "idle", "claude-live")
        tasks = [{"id": "a1", "type": "subagent", "status": "running", "agent_type": "general-purpose"},
                 {"id": "s1", "type": "shell", "status": "running", "command": "npm run dev"}]
        handle("claude-code", self.event("Stop", background_tasks=tasks), self.store, [].append)
        self.assertEqual(self.activity(), "active")
        # Two subagents still count as one conversation at work.
        self.assertEqual(sum(s["activity"]["state"] == "active" for s in self.store.snapshot()["sessions"]), 1)
        handle("claude-code", self.event("Stop", background_tasks=[]), self.store, [].append)
        self.assertEqual(self.activity(), "idle")

    def test_a_background_shell_alone_does_not(self):
        self.store.observe_activity(self.sid, "idle", "claude-live")
        handle("claude-code", self.event("Stop", background_tasks=[{"id": "s1", "type": "shell", "status": "running"},
                                                                    {"id": "m1", "type": "monitor", "status": "running"}]),
               self.store, [].append)
        self.assertEqual(self.activity(), "idle")

    def test_a_subagent_at_work_keeps_its_conversation_active_and_takes_no_messages(self):
        self.store.observe_activity(self.sid, "idle", "claude-live")
        item = self.question()
        self.store.reply(item["id"], "Annual")
        values = []
        handle("claude-code", self.event("PostToolUse", agent_id="agent-1", agent_type="Explore"), self.store, values.append)
        self.assertEqual(self.activity(), "active")
        self.assertEqual(values, [])  # the parent's reply waits for the parent
        self.assertEqual(self.store.item(item["id"])["delivery"], "pending")
        handle("claude-code", self.event("SubagentStop", agent_id="agent-1", background_tasks=[]), self.store, [].append)
        self.assertEqual(self.activity(), "idle")

    def test_a_subagent_finishing_while_others_run_keeps_it_active(self):
        self.store.observe_activity(self.sid, "idle", "claude-live")
        others = [{"id": "a2", "type": "subagent", "status": "running"}]
        handle("claude-code", self.event("SubagentStop", agent_id="agent-1", background_tasks=others), self.store, [].append)
        self.assertEqual(self.activity(), "active")

    def test_offline_and_waiting_still_win_over_background_work(self):
        tasks = [{"id": "a1", "type": "workflow", "status": "running"}]
        handle("claude-code", self.event("Stop", background_tasks=tasks), self.store, [].append)
        self.store.observe_activity(self.sid, "waiting", "claude-live")
        self.assertEqual(self.activity(), "waiting")
        self.store.observe_activity(self.sid, "offline", "claude-live")
        self.assertEqual(self.activity(), "offline")
        handle("claude-code", self.event("SessionEnd"), self.store, [].append)
        self.store.observe_activity(self.sid, "idle", "claude-live")
        self.assertEqual(self.activity(), "idle")

    def test_an_unknown_conversation_is_not_invented_from_a_subagent(self):
        handle("claude-code", {"session_id": "never-seen", "hook_event_name": "PostToolUse", "agent_id": "x"}, self.store, [].append)
        self.assertNotIn("claude-code:never-seen", [s["id"] for s in self.store.snapshot()["sessions"]])

    def question(self, sid=None):
        return self.store.add(sid or self.sid, "Choose a plan", kind="question")

    def test_registration_context_and_existing_title(self):
        values = []
        handle("claude-code", self.event("SessionStart"), self.store, values.append)
        self.assertIn(self.sid, values[0]["hookSpecificOutput"]["additionalContext"])
        # Every conversation is told, in plain words, to ask the user's decisions in HQ.
        self.assertIn("ask it in HQ", values[0]["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.store.session(self.sid)["name"], "Keep this conversation title")
        handle("codex", self.event("UserPromptSubmit"), self.store, values.append)
        self.assertEqual(self.store.session("codex:one")["provider"], "codex")

    def test_independent_responses_and_clarifications_require_receipt(self):
        item, other = self.question(), self.question("claude-code:two")
        self.store.reply(item["id"], "Annual")
        self.store.reply(other["id"], "Private to two")
        task = self.store.add(self.sid, "Build connector")
        comment = self.store.ask_agent(task["id"], "When can I use it?")
        values = []
        handle("claude-code", self.event(), self.store, values.append)
        text = json.dumps(values)
        self.assertIn("Annual", text)
        self.assertIn(comment["id"], text)
        self.assertNotIn("Private to two", text)
        self.assertEqual(self.store.item(item["id"])["delivery"], "sent")
        self.assertEqual(self.store.comment(comment["id"])["state"], "sent")
        self.assertTrue(self.store.item(task["id"])["asking"])
        with self.assertRaises(ValueError):
            self.store.resolve(item["id"])
        self.store.acknowledge(item["id"], self.sid)
        self.store.respond(comment["id"], self.sid, "After the live check")
        self.assertEqual(self.store.comments(self.sid, pending=True), [])

    def test_concurrent_hooks_emit_once_and_stop_does_not_loop(self):
        item = self.question()
        self.store.reply(item["id"], "Annual")
        def invoke(_):
            values = []
            handle("claude-code", self.event("Stop", stop_hook_active=True), self.store, values.append)
            return values
        with concurrent.futures.ThreadPoolExecutor() as pool:
            values = list(pool.map(invoke, range(4)))
        self.assertEqual(sum(bool(v) for v in values), 1)
        self.assertEqual(next(v for v in values if v)[0]["decision"], "block")
        self.assertEqual(invoke(1), [])

    def test_waiter_channel_and_native_routes_take_precedence(self):
        item = self.question()
        self.store.reply(item["id"], "Annual")
        with self.store.db() as db:
            db.execute("INSERT INTO waiters VALUES (?,?,?)", (item["id"], "waiter", time.time()+30))
        self.assertEqual(self.store.claim_hook_messages(self.sid), ([], []))
        with self.store.db() as db:
            db.execute("DELETE FROM waiters")
        self.store.heartbeat(self.sid, "channel")
        values = []
        handle("claude-code", self.event(), self.store, values.append)
        self.assertEqual(values, [])
        self.assertEqual(self.store.session(self.sid)["connection"], "channel")
        codex = "codex:native"
        self.store.register(codex, "Native", "codex")
        self.store.bind_codex(codex, "native")
        native_item = self.question(codex)
        self.store.reply(native_item["id"], "Annual")
        self.assertEqual(self.store.claim_hook_messages(codex), ([], []))

    def test_output_failure_stays_saved_and_requires_explicit_retry(self):
        item = self.question()
        self.store.reply(item["id"], "Annual")
        with self.assertRaises(BrokenPipeError):
            handle("claude-code", self.event(), self.store, Mock(side_effect=BrokenPipeError()))
        self.assertEqual(self.store.item(item["id"])["delivery"], "failed")
        values = []
        handle("claude-code", self.event(), self.store, values.append)
        self.assertEqual(values, [])
        self.store.retry(item["id"])
        handle("claude-code", self.event(), self.store, values.append)
        self.assertEqual(len(values), 1)

    def test_subagents_cannot_consume_parent_messages_and_end_clears_lease(self):
        item = self.question()
        self.store.reply(item["id"], "Annual")
        values = []
        handle("claude-code", self.event(agent_id="child"), self.store, values.append)
        self.assertEqual(values, [])
        self.assertEqual(self.store.item(item["id"])["delivery"], "pending")
        self.store.heartbeat(self.sid, "hooks")
        handle("claude-code", self.event("SessionEnd"), self.store, values.append)
        self.assertEqual(self.store.session(self.sid)["connection"], "offline")

    def test_invalid_hook_fails_open_without_claiming_responses(self):
        item = self.question()
        self.store.reply(item["id"], "Annual")
        with patch("sys.stdin", io.StringIO("{}")), patch("sys.stderr", io.StringIO()) as error, patch("hq.hooks.Store", return_value=self.store):
            run("claude-code")
        self.assertIn("HQ hook unavailable", error.getvalue())
        self.assertEqual(self.store.item(item["id"])["delivery"], "pending")


class SetupTests(unittest.TestCase):
    def test_chatgpt_tunnel_uses_key_reference_and_requires_observed_health(self):
        key = "secret-must-not-be-stored-or-printed"
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"HQ_HOME": folder, "HQ_TEST_KEY": key}), \
                patch("hq.connections.shutil.which", return_value="tunnel-client"), \
                patch("hq.connections.subprocess.run") as run:
            run.side_effect = [Mock(stdout='{"aliases":[]}'), Mock(stdout='{}'),
                               Mock(stdout='{"process_running":true,"healthy":true,"ready":false}')]
            result = connect_chatgpt("tunnel_test", "env:HQ_TEST_KEY")
            self.assertFalse(result["connected"])
            connect = run.call_args_list[1].args[0]
            self.assertIn("env:HQ_TEST_KEY", connect)
            self.assertIn("--mcp-command", connect)
            self.assertNotIn(key, str(connect) + json.dumps(result))
            self.assertEqual(run.call_args.args[0][-3:], ["status", "hq", "--json"])
            run.side_effect = [Mock(stdout='{"aliases":[]}'), Mock(stdout='{}'),
                               Mock(stdout='{"process_running":true,"healthy":true,"ready":true}')]
            self.assertTrue(connect_chatgpt("tunnel_test", "env:HQ_TEST_KEY")["connected"])
            run.reset_mock()
            run.side_effect = [Mock(stdout='{"aliases":[{"alias":"hq","tunnel_id":"tunnel_other"}]}')]
            with self.assertRaises(ValueError):
                connect_chatgpt("tunnel_test", "env:HQ_TEST_KEY")
            self.assertEqual(run.call_count, 1)
            run.reset_mock()
            with self.assertRaises(ValueError):
                connect_chatgpt("tunnel_test", key)
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
                connect_chatgpt("tunnel_test")
            run.assert_not_called()

    def test_hook_install_preserves_other_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder, patch("hq.connections.Path.home", return_value=Path(folder)):
            path = Path(folder) / ".claude/settings.json"
            path.parent.mkdir()
            before = {"permissions": {"allow": ["Read"]}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing-hook"}]}]}}
            path.write_text(json.dumps(before))
            result = install_hooks("claude-code")
            first = path.read_bytes()
            self.assertEqual(result["hooks"], "configured")
            self.assertEqual(install_hooks("claude-code")["hooks"], "already configured")
            self.assertEqual(path.read_bytes(), first)
            after = json.loads(first)
            self.assertEqual(after["permissions"], before["permissions"])
            self.assertEqual(after["hooks"]["Stop"][0], before["hooks"]["Stop"][0])
            self.assertEqual(len(list(path.parent.glob("settings.json.hq-backup-*"))), 1)
            codex = install_hooks("codex")
            self.assertIn("/hooks", codex["next_step"])

    def test_codex_sync_binds_runtime_inventory_not_shared_history(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "test.sqlite3")
            store.register("codex:historical", "Old", "codex", "historical")
            client = Mock()
            client.call.side_effect = [{"data": ["live", "temporary", "child"], "nextCursor": "page2"},
                {"thread": {"name": "Live", "cwd": folder}}, {"thread": {"ephemeral": True}},
                {"thread": {"canAcceptDirectInput": False}}, {"data": []}]
            result = sync_codex(store, client, "/tmp/owning.sock")
            self.assertEqual(result["session_ids"], ["codex:live"])
            self.assertEqual(store.session("codex:live")["endpoint"], "/tmp/owning.sock")
            self.assertEqual(store.session("codex:live")["connection"], "codex")
            self.assertEqual(store.session("codex:historical")["route"], "inbox")
            self.assertNotIn("thread/list", [call.args[0] for call in client.call.call_args_list])
            store.watch_codex("/tmp/owning.sock")
            store.watch_codex("/tmp/owning.sock")
            self.assertEqual(Store(store.path).codex_endpoints(), ["/tmp/owning.sock"])

    def test_claude_launcher_uses_exact_resume_id_and_keeps_confirmation(self):
        sid = "00000000-0000-4000-8000-000000000001"
        def launched(args):
            config = json.loads(Path(args[args.index("--mcp-config")+1]).read_text())
            channel = config["mcpServers"]["hq-channel"]
            self.assertEqual(channel["args"][-1], "claude-code:" + sid)
            self.assertIn("--dangerously-load-development-channels", args)
            self.assertNotIn("--dangerously-skip-permissions", args)
            self.assertEqual(args[1:3], ["--resume", sid])
            return 0
        with patch("hq.connections.subprocess.call", side_effect=launched):
            self.assertEqual(launch("claude-code", ["--", "Continue"], resume=sid), 0)
        for args in (["--continue"], ["--session-id=other"], ["--print"]):
            with self.assertRaises(ValueError):
                launch("claude-code", args)

    def test_launch_parser_accepts_setup_options_and_forwarded_arguments(self):
        args = parser().parse_args(["launch", "claude-code", "--resume", "uuid", "--", "--effort", "high", "Continue"])
        self.assertEqual(args.resume, "uuid")
        self.assertEqual(args.arguments, ["--effort", "high", "Continue"])


class WebSocketTests(unittest.TestCase):
    def test_standard_upgrade_masking_fragmentation_ping_and_large_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(Path(folder)/"control.sock"))
            listener.listen()
            errors = []
            messages = []
            def server():
                try:
                    with listener.accept()[0] as peer:
                        peer.settimeout(3)
                        request = b""
                        while not request.endswith(b"\r\n\r\n"):
                            request += peer.recv(1)
                        self.assertIn(b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", request)
                        # Published RFC 6455 handshake vector, not generated by the client code.
                        peer.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: keep-alive, Upgrade\r\nSec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n")
                        start, end = b'{"id":7,', b'"result":{}}'
                        peer.sendall(bytes([1,len(start)])+start+b'\x89\x01?'+bytes([0x80,len(end)])+end)
                        def exact(count):
                            value=b""
                            while len(value)<count:
                                part=peer.recv(count-len(value))
                                if not part: raise EOFError()
                                value+=part
                            return value
                        for expected_opcode in (10,1):
                            head=exact(2)
                            self.assertEqual(head[0],0x80|expected_opcode)
                            self.assertTrue(head[1]&0x80)
                            size=head[1]&0x7f
                            if size==126: size=struct.unpack('!H',exact(2))[0]
                            if size==127: size=struct.unpack('!Q',exact(8))[0]
                            mask=exact(4);data=exact(size)
                            messages.append(bytes(b^mask[i%4] for i,b in enumerate(data)))
                except Exception as error:
                    errors.append(error)
            worker=threading.Thread(target=server,daemon=True)
            worker.start()
            try:
                with patch('hq.websocket.os.urandom',return_value=b'the sample nonce'):
                    client=UnixWebSocket(Path(folder)/"control.sock")
                try:
                    self.assertEqual(client.receive(),{"id":7,"result":{}})
                    client.send({"text":"x"*70000})
                finally:
                    client.close()
                worker.join(timeout=4)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors,[])
                self.assertEqual(messages[0],b'?')
                self.assertEqual(json.loads(messages[1]),{"text":"x"*70000})
            finally:
                listener.close()

    def test_invalid_server_frames_are_rejected_before_large_allocations(self):
        for frame in (b'\xc1\x00',b'\x81\x80',b'\x09\x00',b'\x81\x7f'+struct.pack('!Q',MAX_FRAME+1)):
            left,right=socket.socketpair()
            client=UnixWebSocket.__new__(UnixWebSocket)
            client.socket=left;client.buffer=b'';client.lock=threading.Lock()
            try:
                right.sendall(frame)
                with self.assertRaises(RuntimeError): client.receive()
            finally:
                client.close();right.close()


if __name__ == "__main__":
    unittest.main()

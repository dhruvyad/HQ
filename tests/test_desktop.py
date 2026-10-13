import io
import json
import queue
import threading
import unittest

from hq.desktop import DesktopSubscriptions, bridge


class DesktopSubscriptionTests(unittest.TestCase):
    def test_hq_subscription_keeps_exact_chat_streaming_and_archive_releases_it(self):
        state = DesktopSubscriptions()
        request, receipt = state.subscribe("one", pin=True)
        state.incoming({"id": request["id"], "result": {"thread": {"id": "one"}}})
        self.assertTrue(receipt.wait(0))
        self.assertTrue(receipt.accepted)
        unsubscribe = {"id": "unsub", "method": "thread/unsubscribe", "params": {"threadId": "one"}}
        messages = state.outgoing(unsubscribe)
        self.assertEqual(messages[0], unsubscribe)
        self.assertEqual(messages[1]["params"], {"threadId": "one"})
        self.assertEqual(state.outgoing({**unsubscribe, "params": {"threadId": "other"}}),
                         [{**unsubscribe, "params": {"threadId": "other"}}])
        state.outgoing({"method": "thread/archive", "params": {"threadId": "one"}})
        self.assertNotIn("one", state.pinned)

    def test_bootstrap_probe_cannot_accept_hq_delivery_and_close_releases_waiters(self):
        state = DesktopSubscriptions()
        for name, ready in [("codex_desktop", False), ("Codex Desktop", True)]:
            state.outgoing({"id": name, "method": "initialize", "params": {"clientInfo": {"name": name}}})
            state.incoming({"id": name, "result": {}})
            self.assertEqual(state.ready.is_set(), ready)
        _, receipt = state.subscribe("one")
        state.close()
        self.assertTrue(receipt.wait(0))
        self.assertFalse(receipt.accepted)
        self.assertFalse(state.ready.is_set())

    def test_bridge_preserves_approval_exchange_and_closes_on_stdin_eof(self):
        approved = threading.Event()

        class Transport:
            def __init__(self):
                self.queue = queue.Queue()
                self.sent = []
                self.closed = False

            def send(self, message):
                self.sent.append(message)
                if message.get("method") == "thread/resume":
                    self.queue.put({"id": message["id"], "result": {"thread": {"id": "one"}}})
                elif message.get("method") == "thread/items/list":
                    self.queue.put({"id": "approval", "method": "item/commandExecution/requestApproval",
                                    "params": {"threadId": "one"}})
                elif message.get("id") == "approval":
                    self.queue.put({"id": "view", "result": {"items": []}})

            def receive(self):
                value = self.queue.get(timeout=2)
                if value is None:
                    raise EOFError()
                return value

            def close(self):
                self.closed = True
                self.queue.put(None)

        class Writer(io.StringIO):
            def flush(self):
                if "requestApproval" in self.getvalue():
                    approved.set()

        def requests():
            yield json.dumps({"id": "view", "method": "thread/items/list", "params": {"threadId": "one"}})
            self.assertTrue(approved.wait(2))
            yield json.dumps({"id": "approval", "result": {"decision": "decline"}})

        transport, output = Transport(), Writer()
        bridge("unused", requests(), output, lambda _: transport)
        values = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([v["id"] for v in values], ["approval", "view"])
        self.assertEqual(transport.sent[-1], {"id": "approval", "result": {"decision": "decline"}})
        self.assertTrue(transport.closed)

    def test_metadata_discovery_does_not_resume_other_conversations(self):
        state = DesktopSubscriptions()
        for method in ["thread/list", "thread/read", "thread/loaded/list"]:
            message = {"id": 1, "method": method, "params": {"threadId": "other"}}
            self.assertEqual(state.outgoing(message), [message])

    def test_view_subscribes_exact_thread_without_permission_overrides(self):
        state = DesktopSubscriptions()
        message = {"id": "view", "method": "thread/items/list", "params": {"threadId": "one"}}
        subscribe, forwarded = state.outgoing(message)
        self.assertEqual(subscribe["params"], {"threadId": "one"})
        self.assertEqual(subscribe["method"], "thread/resume")
        self.assertEqual(forwarded, message)
        self.assertEqual(state.outgoing(message), [message])
        self.assertIsNone(state.incoming({"id": subscribe["id"], "result": {"thread": {"id": "one"}}}))
        self.assertEqual(state.outgoing(message), [message])
        other = {**message, "params": {"threadId": "two"}}
        self.assertEqual(state.outgoing(other)[0]["params"], {"threadId": "two"})

    def test_failed_subscription_can_retry_and_never_claims_success(self):
        state = DesktopSubscriptions()
        message = {"id": 4, "method": "thread/turns/list", "params": {"threadId": "one"}}
        subscribe = state.outgoing(message)[0]
        self.assertIsNone(state.incoming({"id": subscribe["id"], "error": {"message": "gone"}}))
        self.assertNotIn("one", state.subscribed)
        self.assertEqual(len(state.outgoing(message)), 2)

    def test_subscription_requires_receipt_for_the_exact_thread(self):
        state = DesktopSubscriptions()
        view = {"id": 1, "method": "thread/items/list", "params": {"threadId": "one"}}
        for result in [{}, {"thread": {"id": "two"}}]:
            subscribe = state.outgoing(view)[0]
            state.incoming({"id": subscribe["id"], "result": result})
            self.assertNotIn("one", state.subscribed)
            self.assertNotIn("two", state.subscribed)

    def test_native_resume_response_events_and_approvals_pass_through(self):
        state = DesktopSubscriptions()
        state.outgoing({"id": 7, "method": "thread/resume", "params": {"threadId": "one"}})
        response = {"id": 7, "result": {"thread": {"id": "one"}}}
        self.assertEqual(state.incoming(response), response)
        view = {"id": 8, "method": "thread/items/list", "params": {"threadId": "one"}}
        self.assertEqual(state.outgoing(view), [view])
        for event in [{"method": "thread/status/changed", "params": {"threadId": "one"}},
                      {"id": "approval", "method": "item/commandExecution/requestApproval", "params": {"threadId": "one"}}]:
            self.assertEqual(state.incoming(event), event)
        answer = {"id": "approval", "result": {"decision": "decline"}}
        self.assertEqual(state.outgoing(answer), [answer])
        state.outgoing({"method": "thread/unsubscribe", "params": {"threadId": "one"}})
        self.assertEqual(len(state.outgoing(view)), 2)

"""Experimental desktop stdio bridge to one owning Codex runtime.

Opening a history view uses read APIs without subscribing to turn events. Resume
the exact viewed thread on the desktop connection so replies also reach that
client. This never starts a turn or overrides its permissions or configuration.
"""
import json
import sys
import threading
import uuid

from .websocket import UnixWebSocket


class DesktopSubscriptions:
    def __init__(self):
        self.prefix = "__hq_view_" + uuid.uuid4().hex + "_"
        self.pending = {}
        self.subscribed = set()
        self.requests = {}
        self.lock = threading.RLock()
        self.counter = 0
        self.initializes = set()
        self.ready = threading.Event()
        self.receipts = {}
        self.pinned = set()

    def subscribe(self, tid, pin=False):
        with self.lock:
            if pin:
                self.pinned.add(tid)
            if tid in self.subscribed:
                receipt = threading.Event()
                receipt.accepted = True
                receipt.set()
                return None, receipt
            for request_id, pending_tid in self.pending.items():
                if pending_tid == tid:
                    return None, self.receipts[request_id]
            self.counter += 1
            request_id = self.prefix + str(self.counter)
            receipt = threading.Event()
            receipt.accepted = False
            self.pending[request_id], self.receipts[request_id] = tid, receipt
            return {"id": request_id, "method": "thread/resume", "params": {"threadId": tid}}, receipt

    def close(self):
        self.ready.clear()
        with self.lock:
            for receipt in self.receipts.values():
                receipt.set()

    def outgoing(self, message):
        """Keep normal requests intact; subscribe only to requested timelines."""
        method, params = message.get("method"), message.get("params") or {}
        tid = params.get("threadId")
        messages = []
        with self.lock:
            if method == "initialize" and "id" in message:
                # The desktop bootstrap probe also uses this executable. Only
                # the actual UI connection may own HQ delivery subscriptions.
                if (params.get("clientInfo") or {}).get("name") == "Codex Desktop":
                    self.initializes.add(message["id"])
            if method in {"thread/start", "thread/resume"} and "id" in message:
                self.requests[message["id"]] = method
            if method == "thread/archive":
                self.pinned.discard(tid)
            if method in {"thread/unsubscribe", "thread/archive"}:
                self.subscribed.discard(tid)
            if (method in {"thread/items/list", "thread/turns/list"}
                    and isinstance(tid, str) and tid and tid not in self.subscribed
                    and tid not in self.pending.values()):
                request, _ = self.subscribe(tid)
                messages.append(request)
            messages.append(message)
            if method == "thread/unsubscribe" and tid in self.pinned:
                # Respect the UI's unsubscribe request, then retain the exact
                # chat requested by HQ so native replies/approvals still stream.
                request, _ = self.subscribe(tid)
                if request:
                    messages.append(request)
        return messages

    def incoming(self, message):
        """Hide our acknowledgements; forward desktop responses and all events."""
        with self.lock:
            request_id = message.get("id")
            if request_id in self.initializes and ("result" in message or "error" in message):
                self.initializes.discard(request_id)
                if "result" in message:
                    self.ready.set()
            if request_id in self.pending and ("result" in message or "error" in message):
                tid = self.pending.pop(request_id)
                result = message.get("result") or {}
                if (result.get("thread") or {}).get("id") == tid:
                    self.subscribed.add(tid)
                receipt = self.receipts.pop(request_id)
                receipt.accepted = tid in self.subscribed
                if not receipt.accepted:
                    self.pinned.discard(tid)
                receipt.set()
                return None
            if request_id in self.requests and ("result" in message or "error" in message):
                self.requests.pop(request_id)
                tid = (message.get("result", {}).get("thread") or {}).get("id")
                if tid:
                    self.subscribed.add(tid)
            params = message.get("params") or {}
            if message.get("method") in {"thread/closed", "thread/archived"}:
                self.subscribed.discard(params.get("threadId"))
                self.pinned.discard(params.get("threadId"))
        return message


def bridge(endpoint, reader=None, writer=None, transport_factory=UnixWebSocket, control_path=None):
    reader, writer = reader or sys.stdin, writer or sys.stdout
    transport = transport_factory(endpoint)
    subscriptions = DesktopSubscriptions()
    errors = []
    control = None
    if control_path:
        from .desktop_control import SubscriptionControl
        def subscribe(tid):
            if not subscriptions.ready.wait(10):
                raise RuntimeError("The Codex desktop connection is not ready")
            request, receipt = subscriptions.subscribe(tid, pin=True)
            if request:
                transport.send(request)
            if not receipt.wait(10) or not receipt.accepted:
                raise RuntimeError("Codex desktop did not subscribe to the exact chat")
        control = SubscriptionControl(control_path, subscribe)

    def send_requests():
        try:
            for line in reader:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("Expected a Codex protocol object")
                for message in subscriptions.outgoing(value):
                    transport.send(message)
        except (ValueError, RuntimeError, OSError) as error:
            errors.append(error)
        finally:
            transport.close()

    threading.Thread(target=send_requests, daemon=True).start()
    try:
        while True:
            value = subscriptions.incoming(transport.receive())
            if value is not None:
                writer.write(json.dumps(value) + "\n")
                writer.flush()
    except (EOFError, OSError):
        if errors:
            raise errors[0]
    finally:
        subscriptions.close()
        transport.close()
        if control:
            control.close()

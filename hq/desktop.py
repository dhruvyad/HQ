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

    def outgoing(self, message):
        """Keep normal requests intact; subscribe only to requested timelines."""
        method, params = message.get("method"), message.get("params") or {}
        tid = params.get("threadId")
        messages = []
        with self.lock:
            if method in {"thread/start", "thread/resume"} and "id" in message:
                self.requests[message["id"]] = method
            if method in {"thread/unsubscribe", "thread/archive"}:
                self.subscribed.discard(tid)
            if (method in {"thread/items/list", "thread/turns/list"}
                    and isinstance(tid, str) and tid and tid not in self.subscribed
                    and tid not in self.pending.values()):
                self.counter += 1
                request_id = self.prefix + str(self.counter)
                self.pending[request_id] = tid
                messages.append({"id": request_id, "method": "thread/resume",
                                 "params": {"threadId": tid}})
            messages.append(message)
        return messages

    def incoming(self, message):
        """Hide our acknowledgements; forward desktop responses and all events."""
        with self.lock:
            request_id = message.get("id")
            if request_id in self.pending and ("result" in message or "error" in message):
                tid = self.pending.pop(request_id)
                result = message.get("result") or {}
                if (result.get("thread") or {}).get("id") == tid:
                    self.subscribed.add(tid)
                return None
            if request_id in self.requests and ("result" in message or "error" in message):
                self.requests.pop(request_id)
                tid = (message.get("result", {}).get("thread") or {}).get("id")
                if tid:
                    self.subscribed.add(tid)
            params = message.get("params") or {}
            if message.get("method") in {"thread/closed", "thread/archived"}:
                self.subscribed.discard(params.get("threadId"))
        return message


def bridge(endpoint, reader=None, writer=None, transport_factory=UnixWebSocket):
    reader, writer = reader or sys.stdin, writer or sys.stdout
    transport = transport_factory(endpoint)
    subscriptions = DesktopSubscriptions()
    errors = []

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
        transport.close()

"""Supported Codex protocol; no private IPC or session database mutations."""
import os
from pathlib import Path
import queue
import threading
import time

from .websocket import UnixWebSocket


def sync_codex(store, client, endpoint=None):
    """Only an owning server's runtime inventory establishes a binding."""
    ids, cursor, warnings = [], None, []
    while True:
        result = client.call("thread/loaded/list", {"cursor": cursor} if cursor else {})
        for thread_id in result["data"]:
            try:
                thread = client.call("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
                if thread.get("ephemeral") or thread.get("canAcceptDirectInput") is False:
                    continue
                sid = "codex:" + thread_id
                name = (thread.get("name") or thread.get("preview") or thread_id).strip()[:500]
                store.register(sid, name or thread_id, "codex", thread_id, thread.get("cwd"))
                store.bind_codex(sid, thread_id, endpoint)
                store.heartbeat(sid, "codex")
                ids.append(sid)
            except (RuntimeError, ValueError) as error:
                warnings.append(f"A thread could not be bound: {error}")
        cursor = result.get("nextCursor")
        if not cursor:
            break
    return {"connected": len(ids), "session_ids": ids, "warnings": warnings}


class CodexClient:
    def __init__(self, socket=None):
        path = socket or Path(os.environ.get("CODEX_HOME", Path.home()/".codex")) / "app-server-control/app-server-control.sock"
        self.transport = UnixWebSocket(path)
        self.responses = queue.Queue()
        self.counter = 0
        threading.Thread(target=self.read, daemon=True).start()
        try:
            self.call("initialize", {"clientInfo": {"name": "hq", "title": "HQ", "version": "0.1.0"}})
            self.send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def read(self):
        try:
            while True:
                value = self.transport.receive()
                if "id" in value and ("result" in value or "error" in value):
                    self.responses.put(value)
        except (ValueError, OSError, EOFError, RuntimeError):
            pass
        self.responses.put({"error": {"message": "Codex control connection closed. Run hq connect codex with a running app-server socket."}})

    def send(self, value):
        self.transport.send(value)

    def call(self, method, params=None):
        self.counter += 1
        self.send({"id": self.counter, "method": method, "params": params or {}})
        while True:
            try:
                response = self.responses.get(timeout=15)
            except queue.Empty:
                raise RuntimeError(f"Codex did not acknowledge {method}; check the conversation before retrying") from None
            if response.get("id", self.counter) != self.counter:
                continue
            if "error" in response:
                raise RuntimeError(response["error"].get("message", "Codex request failed"))
            return response["result"]

    def deliver(self, thread_id, text):
        thread = self.call("thread/read", {"threadId": thread_id, "includeTurns": True})["thread"]
        if thread.get("status", {}).get("type") == "active":
            turns = [t for t in thread.get("turns", []) if t.get("status") == "inProgress"]
            if not turns:
                raise RuntimeError("Active turn id unavailable; response remains saved")
            return self.call("turn/steer", {"threadId": thread_id, "expectedTurnId": turns[-1]["id"],
                                             "input": [{"type": "text", "text": text}]})
        if thread.get("status", {}).get("type") == "notLoaded":
            # Keep the socket connection alive after subscribing, preserving turn execution.
            self.call("thread/resume", {"threadId": thread_id})
        return self.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}]})

    def close(self):
        self.transport.close()


class Dispatcher:
    def __init__(self, store):
        self.store = store
        self.clients = {}
        self.stop = threading.Event()
        self.next_sync = 0

    def refresh(self):
        if time.monotonic() < self.next_sync:
            return
        self.next_sync = time.monotonic() + 10
        for endpoint in self.store.codex_endpoints():
            try:
                if endpoint not in self.clients:
                    self.clients[endpoint] = CodexClient(endpoint)
                sync_codex(self.store, self.clients[endpoint], endpoint)
            except (RuntimeError, ValueError, OSError):
                client = self.clients.pop(endpoint, None)
                if client:
                    client.close()

    def run(self):
        while not self.stop.is_set():
            self.refresh()
            self.store.recover_uncertain()
            reply = self.store.claim_native()
            if not reply:
                question = self.store.claim_comment()
                if question:
                    endpoint = question["endpoint"]
                    try:
                        if endpoint not in self.clients:
                            self.clients[endpoint] = CodexClient(endpoint)
                        self.clients[endpoint].deliver(question["external_id"],
                            f"Human clarification from HQ\nItem: {question['title']}\nQuestion: {question['text']}\n"
                            f"Reply using hq_comment_reply with session_id {question['session_id']} and comment_id {question['id']}. "
                            "This clarification does not resolve the original item.")
                        self.store.comment_delivery(question["id"], "sent")
                    except Exception as error:
                        client = self.clients.pop(endpoint, None)
                        if client:
                            client.close()
                        self.store.comment_delivery(question["id"], "failed", str(error))
                else:
                    self.stop.wait(0.5)
                continue
            endpoint = reply["endpoint"]
            try:
                if endpoint not in self.clients:
                    self.clients[endpoint] = CodexClient(endpoint)
                self.clients[endpoint].deliver(reply["external_id"],
                    f"Human response from HQ\nItem: {reply['item_id']}\nQuestion: {reply['title']}\nAnswer: {reply['text']}\n"
                    f"After reading, acknowledge with hq_acknowledge using session_id {reply['session_id']} "
                    f"and item_id {reply['item_id']}, or hq ack {reply['item_id']} --session {reply['session_id']}. "
                    "Acknowledgement does not mean the work is complete.")
                self.store.delivery_result(reply["id"], "sent")
            except Exception as error:
                client = self.clients.pop(endpoint, None)
                if client:
                    client.close()
                self.store.delivery_result(reply["id"], "failed", str(error))
        for client in self.clients.values():
            client.close()

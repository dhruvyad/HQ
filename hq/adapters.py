"""Supported Codex protocol; no private IPC or session database mutations."""
import json
import queue
import subprocess
import threading


class CodexClient:
    def __init__(self, socket=None, executable="codex"):
        args = [executable, "app-server", "proxy"]
        if socket:
            args += ["--sock", socket]
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1)
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
        for line in self.process.stdout:
            try:
                value = json.loads(line)
                if "id" in value and ("result" in value or "error" in value):
                    self.responses.put(value)
            except ValueError:
                continue
        self.responses.put({"error": {"message": "Codex control connection closed. Run hq connect codex with a running app-server socket."}})

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

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
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe:
                pipe.close()


class Dispatcher:
    def __init__(self, store):
        self.store = store
        self.clients = {}
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            self.store.recover_uncertain()
            reply = self.store.claim_native()
            if not reply:
                self.stop.wait(0.5)
                continue
            endpoint = reply["endpoint"]
            try:
                if endpoint not in self.clients:
                    self.clients[endpoint] = CodexClient(endpoint)
                self.clients[endpoint].deliver(reply["external_id"],
                    f"Human response from HQ\nQuestion: {reply['title']}\nAnswer: {reply['text']}")
                self.store.delivery_result(reply["id"], "received")
            except Exception as error:
                client = self.clients.pop(endpoint, None)
                if client:
                    client.close()
                self.store.delivery_result(reply["id"], "failed", str(error))
        for client in self.clients.values():
            client.close()

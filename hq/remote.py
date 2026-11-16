"""HQ's tools for agents running elsewhere: MCP over Streamable HTTP, behind a per-agent token.

It listens on loopback only. HTTPS and a public hostname come from what sits in front of it (a
Cloudflare Tunnel or a reverse proxy), so a token never crosses a network in the clear, and the
dashboard is never reachable through it: this is a separate listener that serves /mcp and nothing
else. Each token sees and acts on only the conversations it registered itself.

It keeps no state between requests, so it can restart under a connected agent. A call that waits
for your answer streams it back as Server-Sent Events, with a keepalive line every few seconds so
the agent's host sees it is alive, and ends within ten minutes.

On a relay (relay.py) it also serves /sync to your Mac, behind the Mac's own key.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .mcp import AGENT_INSTRUCTIONS, Tools
from . import __version__ as VERSION

PATH = "/mcp"
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
BODY_LIMIT = 1 << 20
KEEPALIVE = 15          # seconds between keepalive lines on a waiting call
BURST, PER_SECOND = 120, 2.0   # each token's requests: a burst, then two a second
CONCURRENT = 8          # waiting calls one token may have open at once
TOUCH_EVERY = 60        # how often a token's last use is written down
BLOCKING = {"hq_wait", "hq_request_input"}


def loopback(host):
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Endpoint(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, store, host="127.0.0.1", port=4311, clock=time.monotonic, log=None, relay=None):
        if not loopback(host):
            raise ValueError("HQ's remote endpoint listens on this machine only; put HTTPS in front of it")
        super().__init__((host, port), Handler)
        self.store, self.clock, self.relay = store, clock, relay
        self.closing = threading.Event()
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self.lock = threading.Lock()
        self.buckets, self.busy, self.cancels, self.touched = {}, {}, {}, {}

    def admit(self, token_id):
        """Seconds to wait before this token may ask again, or 0."""
        with self.lock:
            now = self.clock()
            level, last = self.buckets.get(token_id, (BURST, now))
            level = min(BURST, level + (now - last) * PER_SECOND)
            if level < 1:
                self.buckets[token_id] = (level, now)
                return max(1, int((1 - level) / PER_SECOND + 0.999))
            self.buckets[token_id] = (level - 1, now)
            return 0

    def touch(self, token_id):
        with self.lock:
            due = self.clock() - self.touched.get(token_id, -TOUCH_EVERY) >= TOUCH_EVERY
            if due:
                self.touched[token_id] = self.clock()
        if due:
            self.store.token_used(token_id)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "hq"
    sys_version = ""

    def log_message(self, format, *args):
        pass  # requests are counted against their token, not written out

    # -- Answers --------------------------------------------------------------------

    def send(self, status, body=None, headers=()):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def refuse(self, status, message, headers=()):
        self.close_connection = True  # a body it never read must not be taken for the next request
        self.send(status, {"jsonrpc": "2.0", "id": None, "error": {"code": -32000 if status != 400 else -32600, "message": message}}, headers)

    # -- Who is asking ----------------------------------------------------------------

    def admitted(self):
        """The token behind this request, or None once a refusal has been sent."""
        if self.path.split("?", 1)[0] != PATH:
            self.refuse(404, "HQ serves agents at /mcp")
            return None
        if self.headers.get("Origin"):
            # Agents don't send one; a browser page does, and it never gets in.
            self.refuse(403, "Browsers can't use HQ's agent endpoint")
            return None
        scheme, _, secret = (self.headers.get("Authorization") or "").partition(" ")
        token = self.server.store.token_for(secret.strip()) if scheme.lower() == "bearer" else None
        if not token:
            self.refuse(401, "Send a working HQ token: Authorization: Bearer hqr_…",
                        [("WWW-Authenticate", 'Bearer realm="hq"')])
            return None
        wait = self.server.admit(token["id"])
        if wait:
            self.refuse(429, "Too many requests from this agent; slow down", [("Retry-After", str(wait))])
            return None
        version = self.headers.get("MCP-Protocol-Version")
        if version and version not in PROTOCOLS:
            self.refuse(400, f"Unsupported MCP-Protocol-Version {version}")
            return None
        self.server.touch(token["id"])
        return token

    # -- Your Mac, on a relay ---------------------------------------------------------------

    def syncing(self):
        """Whether this is your Mac's sync, allowed in; False once a refusal has been sent."""
        if self.headers.get("Origin"):
            self.refuse(403, "Browsers can't use HQ's relay")
            return False
        scheme, _, secret = (self.headers.get("Authorization") or "").partition(" ")
        if scheme.lower() != "bearer" or not self.server.relay.allowed(secret.strip()):
            self.refuse(401, "Send this relay's sync key: run `hq relay connect` on your Mac, then `hq relay allow` here",
                        [("WWW-Authenticate", 'Bearer realm="hq-relay"')])
            return False
        return True

    def sync(self, method):
        if not self.syncing():
            return
        path, query = urlsplit(self.path).path, parse_qs(urlsplit(self.path).query)
        try:
            if method == "GET" and path == "/sync/pull":
                number = lambda key: float((query.get(key) or ["0"])[0])
                return self.send(200, self.server.relay.pull(int(number("since")), number("wait"), stop=self.server.closing))
            if method == "POST" and path == "/sync/push":
                length = int(self.headers.get("Content-Length", ""))
                if not 0 < length <= BODY_LIMIT:
                    return self.refuse(413, "Push at most 1 MB at a time")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or not isinstance(body.get("ops", []), list):
                    return self.send(400, {"error": "Send {ops, tokens}"})
                return self.send(200, self.server.relay.push(body.get("ops", []), body.get("tokens")))
        except ValueError as error:
            self.close_connection = True
            return self.send(400, {"error": str(error)})
        self.refuse(404, "Not a relay path")

    # -- Methods ----------------------------------------------------------------------

    def do_GET(self):
        if self.path == "/health":
            return self.send(200, {"ok": True})
        if self.server.relay and self.path.startswith("/sync/"):
            return self.sync("GET")
        if self.admitted():
            # No stream of its own: an answer comes back on the call that waits for it.
            self.refuse(405, "HQ answers on each call's own stream", [("Allow", "POST")])

    def do_DELETE(self):
        if self.admitted():
            self.refuse(405, "HQ keeps no session to end", [("Allow", "POST")])

    def do_POST(self):
        if self.server.relay and self.path.startswith("/sync/"):
            return self.sync("POST")
        token = self.admitted()
        if not token:
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.refuse(411, "Send a Content-Length")
        if not 0 < length <= BODY_LIMIT:
            return self.refuse(413, "Requests are at most 1 MB")
        try:
            message = json.loads(self.rfile.read(length))
        except ValueError:
            return self.send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        batch = isinstance(message, list)
        messages = message if batch else [message]
        if not messages or not all(isinstance(m, dict) for m in messages):
            return self.send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}})
        tools = Tools(self.server.store, token=token)
        requests = [m for m in messages if "method" in m and "id" in m]
        for note in (m for m in messages if "method" in m and "id" not in m):
            self.notified(token, note)
        if not requests:
            return self.send(202)
        streams = "text/event-stream" in (self.headers.get("Accept") or "")
        if not batch and streams and requests[0].get("method") == "tools/call" \
                and (requests[0].get("params") or {}).get("name") in BLOCKING:
            return self.stream(token, tools, requests[0])
        answers = [self.answer(token, tools, r, threading.Event()) for r in requests]
        self.send(200, answers if batch else answers[0])

    def notified(self, token, note):
        if note.get("method") == "notifications/cancelled":
            request = (note.get("params") or {}).get("requestId")
            with self.server.lock:
                event = self.server.cancels.get((token["id"], json.dumps(request)))
            if event:
                event.set()

    def answer(self, token, tools, request, cancel):
        id, method, params = request.get("id"), request.get("method"), request.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {"jsonrpc": "2.0", "id": id, "result": {
                "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                "capabilities": {"tools": {}}, "serverInfo": {"name": "hq", "version": VERSION},
                "instructions": AGENT_INSTRUCTIONS + f"\nYou reach HQ as the remote agent {token['name']}: you see only the "
                                "conversations you register, and each wait ends within ten minutes, so wait again if no answer has come."}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": id, "result": {"tools": tools.listed()}}
        if method != "tools/call":
            return {"jsonrpc": "2.0", "id": id, "error": {"code": -32601, "message": "Method not found"}}
        name, arguments = params.get("name"), params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return {"jsonrpc": "2.0", "id": id, "error": {"code": -32602, "message": "arguments must be an object"}}
        try:
            text, failed = json.dumps(tools.call(name, arguments, cancel)), False
        except (ValueError, TypeError, KeyError) as error:
            text, failed = str(error) or "HQ couldn't do that", True
        except Exception as error:  # a fault in HQ, said plainly without its internals
            self.server.log(f"remote: {token['name']} {name} failed: {type(error).__name__}")
            text, failed = "HQ couldn't do that just now; try again", True
        return {"jsonrpc": "2.0", "id": id, "result": {"content": [{"type": "text", "text": text}], "isError": failed}}

    def stream(self, token, tools, request):
        """A waiting call, answered as Server-Sent Events: keepalives while it waits, then the answer."""
        key = (token["id"], json.dumps(request.get("id")))
        with self.server.lock:
            if self.server.busy.get(token["id"], 0) >= CONCURRENT:
                busy = True
            else:
                busy = False
                self.server.busy[token["id"]] = self.server.busy.get(token["id"], 0) + 1
                cancel = self.server.cancels[key] = threading.Event()
        if busy:
            return self.send(200, {"jsonrpc": "2.0", "id": request.get("id"), "result": {"isError": True, "content": [
                {"type": "text", "text": f"This agent already has {CONCURRENT} waits open; let one finish first"}]}})
        progress = ((request.get("params") or {}).get("_meta") or {}).get("progressToken")
        done, holder = threading.Event(), {}

        def work():
            try:
                holder["answer"] = self.answer(token, tools, request, cancel)
            finally:
                done.set()

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            threading.Thread(target=work, daemon=True).start()
            beat = 0
            while not done.wait(KEEPALIVE):
                beat += 1
                line = ": still waiting for an answer in HQ\n\n"
                if progress is not None:
                    note = {"jsonrpc": "2.0", "method": "notifications/progress",
                            "params": {"progressToken": progress, "progress": beat, "message": "Still waiting for an answer in HQ"}}
                    line += f"data: {json.dumps(note)}\n\n"
                try:
                    self.wfile.write(line.encode())
                    self.wfile.flush()
                except OSError:
                    cancel.set()  # the agent went away: stop waiting for it
                    done.wait()
                    return
                if not self.server.store.token_for_id(token["id"]):
                    cancel.set()  # revoked while it waited
            try:
                self.wfile.write(f"data: {json.dumps(holder['answer'])}\n\n".encode())
                self.wfile.flush()
            except OSError:
                pass
        finally:
            with self.server.lock:
                self.server.busy[token["id"]] -= 1
                self.server.cancels.pop(key, None)


def serve(store, host="127.0.0.1", port=4311, relay=None):
    endpoint = Endpoint(store, host, port, relay=relay)
    where = f"http://{host}:{endpoint.server_address[1]}"
    if relay:
        print(f"HQ relay at {where}: agents at {PATH}, your Mac at /sync. Put HTTPS in front of it; add agents on "
              "your Mac with `hq remote add`.", flush=True)
    else:
        print(f"HQ for remote agents at {where}{PATH}. Put HTTPS in front of it; "
              "hand each agent its own token from `hq remote add`.", flush=True)
    if threading.current_thread() is threading.main_thread():
        import signal
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=endpoint.shutdown, daemon=True).start())
    try:
        endpoint.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        endpoint.closing.set()
        endpoint.server_close()

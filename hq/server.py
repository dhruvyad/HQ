import fcntl
import json
import os
import secrets
import signal
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .adapters import Dispatcher
from .store import Store, data_dir

STATIC = Path(__file__).parent / "static"


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, store=None):
        self.store = store or Store()
        self.token = secrets.token_urlsafe(32)
        self.instance = str(uuid.uuid4())
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def output(self, code, body, mime="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self, token=False):
        port = self.server.server_port
        host = self.headers.get("Host", "")
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self.output(403, {"error": "HQ accepts localhost requests only"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {self.server.origin, f"http://localhost:{port}"}:
            self.output(403, {"error": "Origin rejected"})
            return False
        if token and not secrets.compare_digest(self.headers.get("X-HQ-Token", ""), self.server.token):
            self.output(403, {"error": "Open the HQ dashboard to access this endpoint"})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        path = urlsplit(self.path).path
        if path == "/api/health":
            return self.output(200, {"app": "hq", "version": __version__, "instance": self.server.instance,
                                     "pid": os.getpid()})
        if path == "/api/state":
            if self.allowed(token=True):
                self.output(200, self.server.store.snapshot())
            return
        assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                  "/style.css": ("style.css", "text/css"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
        if path not in assets:
            return self.output(404, {"error": "Not found"})
        name, mime = assets[path]
        body = (STATIC / name).read_text()
        if name == "index.html":
            body = body.replace("__HQ_TOKEN__", self.server.token)
        self.output(200, body, mime)

    def do_POST(self):
        if not self.allowed(token=True):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                return self.output(413, {"error": "Request too large or empty"})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.output(415, {"error": "Use application/json"})
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Request must be a JSON object")
            parts = urlsplit(self.path).path.strip("/").split("/")
            store = self.server.store
            if parts == ["api", "items"]:
                result = store.add(**body)
            elif len(parts) == 4 and parts[:2] == ["api", "items"]:
                if parts[3] == "reply":
                    result = store.reply(parts[2], body.get("text"))
                elif parts[3] == "resolve":
                    result = store.resolve(parts[2], body.get("note", ""))
                elif parts[3] == "update":
                    result = store.update(parts[2], **body)
                elif parts[3] == "retry":
                    result = store.retry(parts[2])
                else:
                    return self.output(404, {"error": "Not found"})
            elif parts == ["api", "sessions"]:
                result = store.register(**body)
            else:
                return self.output(404, {"error": "Not found"})
            self.output(200, result)
        except (ValueError, TypeError, KeyError) as error:
            self.output(400, {"error": str(error)})


def serve(port=4310):
    home = data_dir()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (home / "server.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("HQ is already running; use hq to open it") from None
        server = Server(port)
        state = home / "server.json"
        state.write_text(json.dumps({"pid": os.getpid(), "port": server.server_port, "instance": server.instance}))
        os.chmod(state, 0o600)
        dispatcher = Dispatcher(server.store)
        thread = threading.Thread(target=dispatcher.run, daemon=True)
        thread.start()
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
        print(f"HQ is running at {server.origin}", flush=True)
        try:
            server.serve_forever()
        finally:
            dispatcher.stop.set()
            thread.join(timeout=5)
            server.server_close()
            state.unlink(missing_ok=True)

import fcntl
import json
import os
import secrets
import signal
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from . import attachments as files
from .accounts import Accounts, AccountsWorker
from .adapters import Dispatcher
from .store import Store, data_dir
from .presence import ClaudePresence
from .stream import StateFeed
from .usage import SampleUsage, Usage, UsageWorker

STATIC = Path(__file__).parent / "static"


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, store=None, accounts=None):
        self.store = store or Store()
        self.token = secrets.token_urlsafe(32)
        self.instance = str(uuid.uuid4())
        # The design lab's pages exist only on the throwaway HQ that `hq float lab` starts.
        self.lab = os.environ.get("HQ_FLOAT_LAB") == "1"
        self.usage = SampleUsage() if self.lab else Usage()
        # The lab never reads a real login. Constructing Accounts reads nothing; its worker does.
        self.accounts = None if self.lab else (accounts or Accounts(self.store))
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.feed = StateFeed(self.store, self.usage, self.accounts)

    def server_close(self):
        self.feed.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def output(self, code, body, mime="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", mime + "; charset=utf-8" if mime.startswith("text/") or mime == "application/json" else mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; font-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
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
                self.output(200, self.server.feed.current())
            return
        if path == "/api/events":
            if self.allowed(token=True):
                self.events()
            return
        if path.startswith("/api/attachments/"):
            if self.allowed(token=True):
                self.attachment(path.rsplit("/", 1)[1])
            return
        if path == "/api/accounts" or path.startswith("/api/accounts/jobs/"):
            if self.allowed(token=True):
                self.accounts_get(path)
            return
        assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                  "/style.css": ("style.css", "text/css"), "/favicon.svg": ("favicon.svg", "image/svg+xml"),
                  "/float": ("float.html", "text/html"), "/float.js": ("float.js", "text/javascript"),
                  "/float.css": ("float.css", "text/css"),
                  "/inter-latin.woff2": ("inter-latin.woff2", "font/woff2"),
                  "/outfit-latin.woff2": ("outfit-latin.woff2", "font/woff2"),
                  "/instrument-serif-latin.woff2": ("instrument-serif-latin.woff2", "font/woff2")}
        if self.server.lab:
            assets.update({"/float-lab": ("float-lab.html", "text/html"), "/float-lab.js": ("float-lab.js", "text/javascript")})
        if path not in assets:
            return self.output(404, {"error": "Not found"})
        name, mime = assets[path]
        body = (STATIC / name).read_bytes()
        if mime == "text/html":
            body = body.replace(b"__HQ_TOKEN__", self.server.token.encode())
        self.output(200, body, mime)

    def accounts_get(self, path):
        accounts = self.server.accounts
        if accounts is None:
            return self.output(404, {"error": "Accounts aren't available here"})
        try:
            if path == "/api/accounts":
                return self.output(200, accounts.list(self.server.usage.current()))
            return self.output(200, accounts.job(path.rsplit("/", 1)[1]))
        except ValueError as error:
            return self.output(400, {"error": str(error)})

    def accounts_post(self, parts, body):
        accounts = self.server.accounts
        if accounts is None:
            raise ValueError("Accounts aren't available here")
        if len(parts) == 4 and parts[3] == "add":
            return accounts.add(parts[2])
        if len(parts) == 5 and parts[2] == "jobs" and parts[4] == "cancel":
            return accounts.cancel(parts[3])
        if len(parts) == 5 and parts[4] == "use":
            return accounts.use(parts[2], parts[3])
        if len(parts) == 5 and parts[4] == "remove":
            return accounts.remove(parts[2], parts[3])
        return None

    def upload(self):
        """A pasted or dropped file: raw bytes, its type, and its name in X-HQ-Filename."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= files.LIMIT:
                return self.output(413, {"error": "Files can be at most 25 MB"})
            from urllib.parse import unquote
            meta = files.save(self.server.store.path.parent, self.rfile.read(length),
                              self.headers.get("Content-Type", ""), unquote(self.headers.get("X-HQ-Filename", "")))
            return self.output(200, meta)
        except ValueError as error:
            return self.output(400, {"error": str(error)})

    def attachment(self, id):
        """Serve a file back to the page, as an inert download it can only show as an image."""
        try:
            meta = files.find(self.server.store.path.parent, id)
            body = Path(meta["path"]).read_bytes()
        except (ValueError, OSError):
            return self.output(404, {"error": "Not found"})
        mime = meta["type"] if meta["type"].startswith("image/") or meta["type"] == "application/pdf" else "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime == "text/plain" else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.end_headers()
        self.wfile.write(body)

    def events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.connection.settimeout(5)
        previous = -1
        try:
            while not self.server.feed.stop.is_set():
                version, payload = self.server.feed.wait(previous)
                healthy = time.monotonic() - self.server.feed.last_success < 3
                message = (f"event: state\ndata: {payload}\n\n" if healthy and version != previous
                           else 'event: ping\ndata: {"healthy":' + str(healthy).lower() + '}\n\n')
                self.wfile.write(message.encode())
                self.wfile.flush()
                previous = version if healthy else -1
        except (OSError, TimeoutError):
            pass
        self.close_connection = True

    def do_POST(self):
        if not self.allowed(token=True):
            return
        if urlsplit(self.path).path == "/api/attachments":
            return self.upload()
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
                    result = store.reply(parts[2], body.get("text"), body.get("attachments"))
                elif parts[3] == "ask":
                    result = store.ask_agent(parts[2], body.get("text"), body.get("attachments"))
                elif parts[3] in {"resolve", "done"}:
                    # From the dashboard this is you, not the agent. An open item is marked done
                    # and the agent told; it closes once the agent has heard. A decided one you
                    # close yourself.
                    if store.item(parts[2])["status"] == "answered":
                        result = store.close(parts[2], body.get("note", ""))
                    else:
                        result = store.mark_done(parts[2], body.get("note", ""))
                elif parts[3] == "close":
                    # You take a decided item back and close it yourself.
                    result = store.close(parts[2], body.get("note", ""))
                elif parts[3] == "handoff":
                    result = store.hand_off(parts[2])
                elif parts[3] == "update":
                    result = store.update(parts[2], **body)
                elif parts[3] == "retry":
                    result = store.retry(parts[2])
                elif parts[3] == "skip":
                    result = store.skip(parts[2], body.get("reason", ""))
                else:
                    return self.output(404, {"error": "Not found"})
            elif len(parts) == 4 and parts[:2] == ["api", "comments"]:
                if parts[3] == "cancel":
                    result = store.cancel_comment(parts[2])
                elif parts[3] == "retry":
                    result = store.retry_comment(parts[2])
                else:
                    return self.output(404, {"error": "Not found"})
            elif parts == ["api", "sessions"]:
                result = store.register(**body)
            elif parts[:2] == ["api", "accounts"]:
                result = self.accounts_post(parts, body)
                if result is None:
                    return self.output(404, {"error": "Not found"})
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
        try:
            server.store.sweep_files()
        except OSError:
            pass  # Leftover uploads are tidied next start.
        # The lab's throwaway HQ holds only fixtures: nothing may discover a real
        # conversation into it, or deliver from it.
        workers = []
        if not server.lab:
            usage = UsageWorker(server.usage, cache=home / "claude-usage.json", activity=server.store.agent_activity)
            # A switch, by HQ or by /login, makes the last account's numbers wrong for the new one.
            server.accounts.on_switch = usage.forget
            workers = [(Dispatcher(server.store), 5), (ClaudePresence(server.store), 3), (usage, 3),
                       (AccountsWorker(server.accounts, server.usage), 3)]
        threads = [threading.Thread(target=worker.run, daemon=True) for worker, _ in workers]
        for thread in threads:
            thread.start()
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
        print(f"HQ is running at {server.origin}", flush=True)
        try:
            server.serve_forever()
        finally:
            for worker, _ in workers:
                worker.stop.set()
            for thread, (_, patience) in zip(threads, workers):
                thread.join(timeout=patience)
            server.server_close()
            state.unlink(missing_ok=True)

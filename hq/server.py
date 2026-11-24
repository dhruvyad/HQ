import fcntl
import html
import json
import os
import secrets
import signal
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import __version__
from . import attachments as files
from .accounts import Accounts, AccountsWorker
from .adapters import Dispatcher
from .store import Store, data_dir
from .presence import ClaudePresence
from .relay import RelaySync
from .links import REFERENCE
from .stream import StateFeed
from .usage import SampleUsage, Usage, UsageWorker
from .version import build

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
        # Pills listening right now: an item link opens there when there is one.
        self.panels, self.panels_lock = 0, threading.Lock()
        # The build it started from, so hq doctor can tell a server left running on old code.
        self.commit = ""

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
            return self.output(200, {"app": "hq", "version": __version__, "commit": self.server.commit,
                                     "instance": self.server.instance, "pid": os.getpid()})
        if path == "/api/state":
            if self.allowed(token=True):
                self.output(200, self.server.feed.current())
            return
        if path == "/api/events":
            if self.allowed(token=True):
                self.events()
            return
        if path.startswith("/i/"):
            return self.open_link(path[3:])
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
                  "/float.css": ("float.css", "text/css"), "/open.js": ("open.js", "text/javascript"),
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
        feed, previous = self.server.feed, -1
        # The pill says it is the pill, so item links clicked in an agent's chat open there.
        panel = parse_qs(urlsplit(self.path).query).get("surface") == ["float"]
        opened = feed.opens_after(0)[1] if panel else None
        if panel:
            with self.server.panels_lock:
                self.server.panels += 1
        try:
            while not feed.stop.is_set():
                version, payload = feed.wait(previous, opened)
                message = ""
                if panel:
                    items, opened = feed.opens_after(opened)
                    message = "".join(f"event: open\ndata: {json.dumps(item)}\n\n" for item in items)
                healthy = time.monotonic() - feed.last_success < 3
                if healthy and version != previous:
                    message += f"event: state\ndata: {payload}\n\n"
                elif not message:
                    message = 'event: ping\ndata: {"healthy":' + str(healthy).lower() + '}\n\n'
                self.wfile.write(message.encode())
                self.wfile.flush()
                previous = version if healthy else -1
        except (OSError, TimeoutError):
            pass
        finally:
            if panel:
                with self.server.panels_lock:
                    self.server.panels -= 1
        self.close_connection = True

    def clicked(self):
        """Someone opened this link: from another app, the address bar, or their own click on a
        page. Not a page loading it on its own, which must never pop the pill open."""
        site = self.headers.get("Sec-Fetch-Site")
        if site in {None, "none", "same-origin"}:
            return True
        return self.headers.get("Sec-Fetch-Mode") == "navigate" and self.headers.get("Sec-Fetch-User") == "?1"

    def open_link(self, ref):
        """An item link from an agent's chat: the item opens in the pill and this tab closes
        itself, or, with no pill running, the dashboard opens on it."""
        ref = unquote(ref).strip("/").lower()
        ids = self.server.store.find_items(ref) if REFERENCE.fullmatch(ref) else []
        if not ids:
            return self.link_page(404, "HQ has no item " + ref[:40],
                                  "It may have been deleted, or the link was cut short.", [("Open the dashboard", "/")])
        if len(ids) > 1:
            store = self.server.store
            return self.link_page(300, "This link fits more than one item", "Pick the one you meant.",
                                  [(store.item(id)["title"], "/i/" + id) for id in ids[:10]])
        dashboard = "/#/?item=" + quote(ids[0], safe="")
        if self.server.panels and self.clicked():
            self.server.feed.open_item(ids[0])
            return self.link_page(200, "Opened in HQ", "It's in the pill. You can close this tab.",
                                  [("Open it in the dashboard instead", dashboard)], close=True)
        self.send_response(303)
        self.send_header("Location", dashboard)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def link_page(self, code, title, text, links, close=False):
        anchors = "".join(f'<a href="{html.escape(href)}">{html.escape(label)}</a>' for label, href in links)
        self.output(code, f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark"><title>{html.escape(title)} · HQ</title>
<link rel="icon" href="/favicon.svg"><link rel="stylesheet" href="/style.css"><script src="/open.js" defer></script></head>
<body class="link-page"{' data-close' if close else ''}><main><p class="wordmark">HQ</p><h1>{html.escape(title)}</h1>
<p>{html.escape(text)}</p><nav>{anchors}</nav></main></body></html>""", "text/html")

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
            if "media" in body and parts[:2] == ["api", "items"]:
                raise ValueError("Media comes from the agent: it names its files with hq_report or hq_update")
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
            elif parts == ["api", "sessions", "archive"]:
                if not isinstance(body.get("archived"), bool):
                    raise ValueError("archived must be true or false")
                result = store.archive(body.get("ids"), body["archived"])
            elif parts[:2] == ["api", "accounts"]:
                result = self.accounts_post(parts, body)
                if result is None:
                    return self.output(404, {"error": "Not found"})
            else:
                return self.output(404, {"error": "Not found"})
            self.output(200, result)
        except (ValueError, TypeError, KeyError) as error:
            self.output(400, {"error": str(error)})


class FileSweeper:
    """Deletes attachments nothing needs any more (see Store.sweep_files) every few hours, so a
    server left running for weeks keeps the 30-day promise too."""
    EVERY = 6 * 3600

    def __init__(self, store):
        self.store, self.stop = store, threading.Event()

    def run(self):
        while not self.stop.wait(self.EVERY):
            try:
                self.store.sweep_files()
            except (OSError, sqlite3.Error):
                pass  # tried again next time


def serve(port=4310):
    home = data_dir()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (home / "server.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("HQ is already running; use hq to open it") from None
        server = Server(port)
        server.commit = build().get("commit") or ""
        state = home / "server.json"
        state.write_text(json.dumps({"pid": os.getpid(), "port": server.server_port, "instance": server.instance}))
        os.chmod(state, 0o600)
        try:
            server.store.sweep_files()
        except OSError:
            pass  # Leftover uploads are tidied next start.
        if server.accounts:
            server.accounts.sweep_sign_ins()  # a sign-in left in Terminal that landed after HQ gave up
        # The lab's throwaway HQ holds only fixtures: nothing may discover a real
        # conversation into it, or deliver from it.
        workers = []
        if not server.lab:
            usage = UsageWorker(server.usage, cache=home / "claude-usage.json", activity=server.store.agent_activity)
            # A switch, by HQ or by /login, makes the last account's numbers wrong for the new one.
            server.accounts.on_switch = usage.forget
            workers = [(Dispatcher(server.store), 5), (ClaudePresence(server.store), 3), (usage, 3),
                       (AccountsWorker(server.accounts, server.usage), 3), (FileSweeper(server.store), 3),
                       # Idle until `hq relay connect`: then remote agents' conversations sync both ways.
                       (RelaySync(server.store, log=lambda line: print(line, flush=True)), 3)]
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

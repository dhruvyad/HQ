"""A relay keeps remote agents' conversations on an always-on server, so they can report and get
your answers while this Mac sleeps. The relay is an ordinary HQ inbox that holds only those
conversations, serves them to their agents (see remote.py), and syncs them with your Mac.

Your Mac only ever connects out. It pushes what you did to a relay's item (an answer, a skip, a
question, a pin) and pulls what the agents did. Each side owns its half: the relay is the truth
for a remote conversation, and your Mac keeps a mirror of it that your dashboard and pill show.
What you do to a mirrored item is applied here at once and queued in the same transaction, so it
reaches the relay exactly once and in order, however long the Mac was asleep.

The relay holds no secret: it keeps a hash of your Mac's sync key and of each agent's token, and
your Mac decides which agents exist. Nothing of this Mac's own agents ever leaves it.
"""
import hashlib
import http.client
import json
import os
import queue
import secrets
import socket
import ssl
import threading
import time
import urllib.parse
import uuid

from .store import token_hash

TABLES = ("sessions", "items", "replies", "comments")
# Only a change worth showing: an agent's every call refreshes its lease, and a wait does so
# several times a second, which would otherwise wake your Mac's sync just as often.
SESSION_CHANGE = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in ("name", "external_id", "cwd", "connection", "remote_token"))
OPS = {"reply", "skip", "done", "close", "update", "ask", "cancel"}
PULL_LIMIT = 500
PULL_WAIT = 25          # a pull waits this long for news before it returns empty
PUSH_BATCH = 8          # actions per push; each carries at most one 20,000-character answer
SYNC_BODY = 1 << 20
CONFIG = "relay.json"
STAGGER = 0.25          # how long one address gets before the next is tried alongside it
CONNECT = 10            # the longest any one address may take to connect
USER_AGENT = "hq-relay-sync"  # Cloudflare's browser check turns away Python's default name


# -- Reaching the relay ------------------------------------------------------------------------

def happy_connect(host, port, timeout=CONNECT, stagger=STAGGER):
    """Connect the way browsers do: each address in turn, IPv6 and IPv4 interleaved, the next
    starting a moment after the last without waiting for it to fail, and the first to answer
    wins. On a network whose IPv6 is broken that costs a quarter second, not a timeout for every
    IPv6 address before IPv4 is tried at all."""
    found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    sixes, fours = [a for a in found if a[0] == socket.AF_INET6], [a for a in found if a[0] != socket.AF_INET6]
    order = [a for pair in zip(sixes, fours) for a in pair] + sixes[len(fours):] + fours[len(sixes):]
    results, won, attempts = queue.Queue(), threading.Event(), []

    def attempt(address):
        family, kind, protocol, _, where = address
        sock = socket.socket(family, kind, protocol)
        sock.settimeout(timeout)
        try:
            sock.connect(where)
        except OSError as error:
            sock.close()
            results.put((None, error))
            return
        results.put((sock, None))

    failures, error, deadline = 0, None, time.monotonic() + timeout
    try:
        while failures < len(order):
            if len(attempts) < len(order):
                attempts.append(threading.Thread(target=attempt, args=(order[len(attempts)],), daemon=True))
                attempts[-1].start()
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"No address of {host} answered within {timeout}s")
            try:
                sock, error = results.get(timeout=min(stagger, left) if len(attempts) < len(order) else left)
            except queue.Empty:
                continue
            if sock:
                won.set()
                return sock
            failures += 1
        raise error or OSError(f"{host} has no address")
    finally:
        def tidy():  # whatever connects after the winner is closed, not left open
            for thread in attempts:
                thread.join(timeout + 1)
            while not results.empty():
                late, _ = results.get_nowait()
                if late:
                    late.close()
        threading.Thread(target=tidy, daemon=True).start()


class Plain(http.client.HTTPConnection):
    def connect(self):
        self.sock = happy_connect(self.host, self.port, min(self.timeout or CONNECT, CONNECT))
        self.sock.settimeout(self.timeout)


class Secure(http.client.HTTPSConnection):
    def connect(self):
        sock = happy_connect(self.host, self.port, min(self.timeout or CONNECT, CONNECT))
        sock.settimeout(self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def fetch(url, method="GET", body=None, headers=None, timeout=30, links=None):
    """One request to the relay: its status and parsed JSON answer. `links` holds it while it is
    open, so whoever stops the sync can cut a long poll short."""
    parsed = urllib.parse.urlsplit(url)
    link = (Secure(parsed.hostname, parsed.port or 443, timeout=timeout, context=ssl.create_default_context())
            if parsed.scheme == "https" else Plain(parsed.hostname, parsed.port or 80, timeout=timeout))
    data = None if body is None else json.dumps(body).encode()
    if links is not None:
        links.add(link)
    try:
        link.request(method, (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else ""), body=data, headers={
            "User-Agent": USER_AGENT, **({"Content-Type": "application/json"} if data is not None else {}), **(headers or {})})
        response = link.getresponse()
        answer = response.read()
    finally:
        if links is not None:
            links.discard(link)
        link.close()
    try:
        return response.status, json.loads(answer) if answer else None
    except ValueError:
        return response.status, None


def reachable(url):
    try:
        return fetch(check_url(url) + "/health", timeout=15) == (200, {"ok": True})
    except (OSError, ValueError, http.client.HTTPException):
        return False


def sync_key():
    return "hqs_" + secrets.token_urlsafe(32)


# -- The relay ---------------------------------------------------------------------------------

class Relay:
    """The relay's side: a change log of its conversations, the Macs allowed to sync, and the
    actions they send, each applied once."""

    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for statement in (
                    "CREATE TABLE IF NOT EXISTS relay_changes (tbl TEXT NOT NULL, row_id TEXT NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY(tbl,row_id))",
                    "CREATE INDEX IF NOT EXISTS relay_changes_seq ON relay_changes(seq)",
                    "CREATE TABLE IF NOT EXISTS relay_keys (hash TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, added REAL NOT NULL, last_used REAL)",
                    "CREATE TABLE IF NOT EXISTS relay_applied (id TEXT PRIMARY KEY, result TEXT NOT NULL, at REAL NOT NULL)"):
                db.execute(statement)
            for table in TABLES:
                change = f"INSERT OR REPLACE INTO relay_changes VALUES ('{table}', NEW.id, (SELECT COALESCE(MAX(seq),0)+1 FROM relay_changes))"
                db.execute(f"CREATE TRIGGER IF NOT EXISTS relay_{table}_added AFTER INSERT ON {table} BEGIN {change}; END")
                when = f" WHEN {SESSION_CHANGE}" if table == "sessions" else ""
                db.execute(f"CREATE TRIGGER IF NOT EXISTS relay_{table}_changed AFTER UPDATE ON {table}{when} BEGIN {change}; END")
                # Rows from before the log existed are news too.
                last = db.execute("SELECT COALESCE(MAX(seq),0) FROM relay_changes").fetchone()[0]
                for n, row in enumerate(db.execute(f"""SELECT id FROM {table} WHERE id NOT IN
                        (SELECT row_id FROM relay_changes WHERE tbl='{table}') ORDER BY rowid""").fetchall(), 1):
                    db.execute("INSERT INTO relay_changes VALUES (?,?,?)", (table, row["id"], last + n))
            found = db.execute("SELECT value FROM meta WHERE key='relay_instance'").fetchone()
            self.instance = found["value"] if found else uuid.uuid4().hex
            db.execute("INSERT OR IGNORE INTO meta VALUES ('relay_instance',?)", (self.instance,))

    # -- Macs allowed to sync --------------------------------------------------------------------

    def allow(self, digest, name):
        digest = (digest or "").strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Paste the 64-character hash that `hq relay connect` printed on your Mac")
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError("Name the Mac, such as dhruvs-macbook")
        with self.store.db() as db:
            db.execute("INSERT INTO relay_keys (hash,name,added) VALUES (?,?,?) ON CONFLICT(hash) DO UPDATE SET name=excluded.name",
                       (digest, name.strip(), self.clock()))
        return {"allowed": name.strip()}

    def keys(self):
        with self.store.db() as db:
            return [dict(r) for r in db.execute("SELECT name,added,last_used FROM relay_keys ORDER BY added")]

    def forget(self, name):
        with self.store.db() as db:
            if not db.execute("DELETE FROM relay_keys WHERE name=?", (name,)).rowcount:
                raise ValueError(f"No Mac named {name} syncs with this relay")
        return {"forgotten": name}

    def allowed(self, secret):
        if not isinstance(secret, str) or not secret.startswith("hqs_") or len(secret) > 200:
            return False
        digest = token_hash(secret)
        with self.store.db() as db:
            row = db.execute("SELECT last_used FROM relay_keys WHERE hash=?", (digest,)).fetchone()
            if row and (row["last_used"] or 0) < self.clock() - 60:
                db.execute("UPDATE relay_keys SET last_used=? WHERE hash=?", (self.clock(), digest))
        return row is not None

    # -- What changed ------------------------------------------------------------------------------

    def latest(self):
        with self.store.db() as db:
            return db.execute("SELECT COALESCE(MAX(seq),0) FROM relay_changes").fetchone()[0]

    def pull(self, since=0, wait=0, limit=None, stop=None):
        """Everything that changed after `since`, each row as it is now, with the rows it hangs
        from, so a mirror can always insert it. Waits up to `wait` seconds for something to change."""
        since, limit = max(0, int(since)), limit or PULL_LIMIT
        deadline = time.monotonic() + max(0, min(float(wait), PULL_WAIT))
        while self.latest() <= since and time.monotonic() < deadline and not (stop and stop.is_set()):
            time.sleep(0.25)
        with self.store.db() as db:
            changes = db.execute("SELECT tbl,row_id,seq FROM relay_changes WHERE seq>? ORDER BY seq LIMIT ?",
                                 (since, limit)).fetchall()
            wanted = {table: [] for table in TABLES}
            for change in changes:
                wanted[change["tbl"]].append(change["row_id"])

            def rows(table, ids):
                found = []
                for start in range(0, len(ids), 400):
                    chunk = ids[start:start + 400]
                    found += [dict(r) for r in db.execute(
                        f"SELECT * FROM {table} WHERE id IN ({','.join('?' * len(chunk))})", chunk)]
                return found

            changed = rows("replies", wanted["replies"]) + rows("comments", wanted["comments"])
            item_ids = sorted(set(wanted["items"]) | {r["item_id"] for r in changed})
            items = rows("items", item_ids)

            def under(table):
                # An item comes with all of its replies and comments, so a mirror can match them exactly.
                found = []
                for start in range(0, len(item_ids), 400):
                    chunk = item_ids[start:start + 400]
                    found += [dict(r) for r in db.execute(
                        f"SELECT * FROM {table} WHERE item_id IN ({','.join('?' * len(chunk))}) ORDER BY created,rowid", chunk)]
                return found

            replies, comments = under("replies"), under("comments")
            sessions = rows("sessions", sorted(set(wanted["sessions"]) | {i["session_id"] for i in items}))
            tokens = {r["id"]: r["last_used"] for r in db.execute("SELECT id,last_used FROM remote_tokens")}
        return {"instance": self.instance, "seq": changes[-1]["seq"] if changes else since,
                "more": len(changes) == limit, "sessions": sessions, "items": items,
                "replies": replies, "comments": comments, "tokens": tokens}

    # -- What you did, from your Mac ---------------------------------------------------------------

    def push(self, ops=(), tokens=None):
        """Your Mac's agents' tokens, and what you did, in order. Each action is applied once."""
        if tokens is not None:
            self.tokens(tokens)
        results = []
        for op in ops:
            result = self.apply(op)
            results.append(result)
            if result.get("retry"):
                break  # keep the order: the rest waits for this one
        return {"instance": self.instance, "results": results}

    def tokens(self, tokens):
        """Make the relay's agents exactly your Mac's: it holds only their hashes."""
        if not isinstance(tokens, list):
            raise ValueError("tokens must be a list")
        kept = []
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for token in tokens:
                if not isinstance(token, dict) or not all(isinstance(token.get(k), str) and token[k] for k in ("id", "name", "hash")):
                    raise ValueError("each token needs an id, a name and a hash")
                kept.append(token["id"])
                # A name your Mac gave a new token: the relay's old row of that name goes first.
                db.execute("DELETE FROM remote_tokens WHERE name=? AND id!=?", (token["name"], token["id"]))
                db.execute("""INSERT INTO remote_tokens (id,name,hash,project,created,revoked) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,hash=excluded.hash,project=excluded.project,
                    revoked=excluded.revoked""",
                           (token["id"], token["name"], token["hash"], token.get("project") or "",
                            token.get("created") or self.clock(), token.get("revoked")))
            if kept:
                db.execute(f"DELETE FROM remote_tokens WHERE id NOT IN ({','.join('?' * len(kept))})", kept)
            else:
                db.execute("DELETE FROM remote_tokens")

    def apply(self, op):
        if not isinstance(op, dict) or not isinstance(op.get("id"), str) or op.get("op") not in OPS \
                or not isinstance(op.get("item_id"), str) or not isinstance(op.get("args", {}), dict):
            return {"id": op.get("id") if isinstance(op, dict) else None, "ok": False, "error": "Not an action HQ knows"}
        with self.store.db() as db:
            done = db.execute("SELECT result FROM relay_applied WHERE id=?", (op["id"],)).fetchone()
        if done:
            result = json.loads(done["result"])
        else:
            try:
                self.perform(op["item_id"], op["op"], op.get("args") or {})
                result = {"id": op["id"], "ok": True}
            except (ValueError, TypeError, KeyError) as error:
                result = {"id": op["id"], "ok": False, "error": str(error) or "HQ couldn't do that"}
            except Exception as error:  # a fault on the relay: not an answer, so it is tried again
                return {"id": op["id"], "ok": False, "retry": True, "error": type(error).__name__}
            with self.store.db() as db:
                db.execute("INSERT OR IGNORE INTO relay_applied VALUES (?,?,?)", (op["id"], json.dumps(result), self.clock()))
        self.touch(op["item_id"])
        return result

    def touch(self, item_id):
        """Send the item whole on the next pull, whether or not the action changed it, so the
        mirror that held it back while the action was on its way catches up either way."""
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = [("items", item_id)]
            rows += [("replies", r["id"]) for r in db.execute("SELECT id FROM replies WHERE item_id=?", (item_id,))]
            rows += [("comments", r["id"]) for r in db.execute("SELECT id FROM comments WHERE item_id=?", (item_id,))]
            if not db.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
                return
            last = db.execute("SELECT COALESCE(MAX(seq),0) FROM relay_changes").fetchone()[0]
            for n, (table, row_id) in enumerate(rows, 1):
                db.execute("INSERT INTO relay_changes VALUES (?,?,?) ON CONFLICT(tbl,row_id) DO UPDATE SET seq=excluded.seq",
                           (table, row_id, last + n))

    def perform(self, item_id, kind, args):
        store = self.store
        with store.db() as db:
            row = db.execute("""SELECT i.status,s.provider FROM items i JOIN sessions s ON s.id=i.session_id
                WHERE i.id=?""", (item_id,)).fetchone()
            if not row or row["provider"] != "remote":
                raise ValueError("Item not found")
            exists = lambda table, id: db.execute(f"SELECT 1 FROM {table} WHERE id=?", (id,)).fetchone() is not None
            reply_there = kind in {"reply", "skip", "done"} and exists("replies", args.get("reply_id"))
            asked_there = kind == "ask" and exists("comments", args.get("comment_id"))
            cancelled = kind == "cancel" and db.execute(
                "SELECT 1 FROM comments WHERE id=? AND item_id=? AND state='cancelled'", (args.get("comment_id"), item_id)).fetchone()
        # Something that already happened (an action applied just before the relay stopped) is done.
        if reply_there or asked_there or cancelled or (kind == "close" and row["status"] == "done"):
            return
        if kind == "reply":
            store.reply(item_id, args["text"], reply_id=args["reply_id"])
        elif kind == "skip":
            store.skip(item_id, args.get("reason", ""), reply_id=args["reply_id"])
        elif kind == "done":
            store.mark_done(item_id, args.get("note", ""), reply_id=args["reply_id"])
        elif kind == "close":
            store.close(item_id, args.get("note", ""))
        elif kind == "update":
            patch = args.get("patch")
            if not isinstance(patch, dict):
                raise ValueError("update needs a patch")
            store.update(item_id, **patch)
        elif kind == "ask":
            store.ask_agent(item_id, args["text"], comment_id=args["comment_id"])
        elif kind == "cancel":
            comment = store.comment(args["comment_id"])
            if comment["item_id"] != item_id:
                raise ValueError("Comment not found")
            store.cancel_comment(args["comment_id"])


# -- Your Mac ------------------------------------------------------------------------------------

def config_path(store):
    return store.path.parent / CONFIG


def load_config(store):
    try:
        config = json.loads(config_path(store).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(config, dict) or not isinstance(config.get("url"), str) or not isinstance(config.get("key"), str):
        return None
    return config


def check_url(url):
    parsed = urllib.parse.urlsplit(url or "")
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("Give the relay's HTTPS address, such as https://hq-relay.example.com")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Give the relay's address alone, with no credentials or query")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def connect(store, url):
    """Point this Mac at a relay. The key stays here; the relay is given only its hash."""
    url, path = check_url(url), config_path(store)
    config = load_config(store) or {}
    key = config.get("key") or sync_key()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"url": url, "key": key}))
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    if config.get("url") and config["url"] != url:
        with store.db() as db:
            db.execute("DELETE FROM meta WHERE key IN ('relay_cursor','relay_upstream')")
    name = socket.gethostname().split(".")[0] or "mac"
    return {"url": url, "hash": token_hash(key), "allow": f"hq relay allow {token_hash(key)} --name {name}"}


def disconnect(store):
    config_path(store).unlink(missing_ok=True)
    with store.db() as db:
        db.execute("DELETE FROM meta WHERE key LIKE 'relay_%'")
    return {"connected": False, "kept": "Its conversations stay here as they were; they no longer update."}


def status(store):
    config = load_config(store)
    with store.db() as db:
        meta = {r["key"]: r["value"] for r in db.execute("SELECT key,value FROM meta WHERE key LIKE 'relay_%'")}
        waiting = db.execute("SELECT COUNT(*) FROM relay_outbox WHERE state='pending'").fetchone()[0]
        refused = [dict(r) for r in db.execute("""SELECT o.op,o.item_id,o.error,o.created,i.title FROM relay_outbox o
            LEFT JOIN items i ON i.id=o.item_id WHERE o.state='refused' ORDER BY o.seq DESC LIMIT 10""")]
        mirrored = db.execute("SELECT COUNT(*) FROM sessions WHERE mirror=1").fetchone()[0]
    pulled = float(meta.get("relay_pulled") or 0) or None
    return {"connected": bool(config), "url": config and config["url"], "last_sync": pulled,
            "error": meta.get("relay_error") or None, "waiting_to_send": waiting, "refused": refused,
            "conversations": mirrored}


class RelaySync:
    """Your Mac's side: push what you did to the relay's items, pull what its agents did."""

    def __init__(self, store, log=None):
        self.store, self.stop = store, threading.Event()
        self.links = set()  # requests open right now, cut short when HQ stops
        self.log = log or (lambda line: None)
        self.signature, self.pushed, self.last_error = None, 0, None
        self.attempts = {}

    # -- Talking to the relay ----------------------------------------------------------------------

    def request(self, config, method, path, body=None, timeout=30):
        try:
            status, answer = fetch(config["url"] + path, method, body,
                                   {"Authorization": f"Bearer {config['key']}"}, timeout, self.links)
        except (OSError, http.client.HTTPException) as error:
            raise ConnectionError(f"Can't reach the relay: {error}") from None
        if status == 401:
            raise ConnectionError("The relay doesn't know this Mac yet: run the `hq relay allow` line that "
                                  "`hq relay connect` printed, on the relay")
        if status != 200 or not isinstance(answer, dict):
            raise ConnectionError(f"The relay answered {status}")
        return answer

    def note(self, key, value):
        with self.store.db() as db:
            if value is None:
                db.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def meta(self, key):
        with self.store.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def upstream(self):
        """Which relay this Mac last read. Macs once kept it under relay_instance, the key that marks
        a store as a relay itself, so a Mac that had synced looked like one; move it on first use."""
        found = self.meta("relay_upstream")
        if found is None and self.meta("relay_instance") is not None:
            with self.store.db() as db:
                db.execute("UPDATE meta SET key='relay_upstream' WHERE key='relay_instance'")
            found = self.meta("relay_upstream")
        return found

    # -- Push ---------------------------------------------------------------------------------------

    def tokens(self):
        with self.store.db() as db:
            return [dict(r) for r in db.execute("SELECT id,name,hash,project,created,revoked FROM remote_tokens ORDER BY created")]

    def push_once(self, config=None, force=False):
        """Send what is waiting. Returns how many actions the relay took or refused."""
        config = config or load_config(self.store)
        if not config:
            return 0
        with self.store.db() as db:
            ops = [dict(r) for r in db.execute(
                "SELECT id,item_id,op,args FROM relay_outbox WHERE state='pending' ORDER BY seq LIMIT ?", (PUSH_BATCH,))]
        tokens = self.tokens()
        signature = hashlib.sha256(json.dumps(tokens, sort_keys=True).encode()).hexdigest()
        if not ops and signature == self.signature and not force and time.monotonic() - self.pushed < 60:
            return 0
        answer = self.request(config, "POST", "/sync/push", {
            "ops": [{**op, "args": json.loads(op["args"])} for op in ops], "tokens": tokens})
        self.signature, self.pushed = signature, time.monotonic()
        settled = 0
        with self.store.db() as db:
            for result in answer.get("results") or []:
                if result.get("retry"):
                    # A fault on the relay: tried again, and given up after a few, so one bad
                    # action never holds up everything after it.
                    tries = self.attempts[result.get("id")] = self.attempts.get(result.get("id"), 0) + 1
                    if tries >= 5:
                        db.execute("UPDATE relay_outbox SET state='refused',sent=?,error=? WHERE id=? AND state='pending'",
                                   (time.time(), f"The relay kept failing on it ({result.get('error')})", result.get("id")))
                    break
                state = "sent" if result.get("ok") else "refused"
                db.execute("UPDATE relay_outbox SET state=?,sent=?,error=? WHERE id=? AND state='pending'",
                           (state, time.time(), result.get("error"), result.get("id")))
                settled += 1
                if state == "refused":
                    self.log(f"relay: the relay refused {result.get('id')}: {result.get('error')}")
        return settled

    # -- Pull ---------------------------------------------------------------------------------------

    def pull_once(self, config=None, wait=PULL_WAIT):
        """Bring the mirror up to date. Returns whether to pull again at once."""
        config = config or load_config(self.store)
        if not config:
            return False
        cursor = int(self.meta("relay_cursor") or 0)
        answer = self.request(config, "GET", f"/sync/pull?since={cursor}&wait={wait}", timeout=wait + 20)
        instance = answer.get("instance")
        if instance != self.upstream():
            if cursor:  # a new relay, or one started afresh: read it all again
                self.note("relay_cursor", 0)
                self.note("relay_upstream", instance)
                return True
            self.note("relay_upstream", instance)
        if not self.mirror(answer):
            self.stop.wait(1)  # an action of yours is on its way; read these again once it has landed
            return False
        return bool(answer.get("more"))

    def mirror(self, answer):
        """Apply a pull in one transaction, with the cursor, so a crash never skips a change.
        An item with an action of yours not yet on the relay is held back, and the cursor stays
        put, so the next pull reads it again once the relay has the action. Returns whether
        everything was applied."""
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            held = {r["item_id"] for r in db.execute("SELECT item_id FROM relay_outbox WHERE state='pending'")}
            skipped = False
            sessions = set()
            for row in answer.get("sessions") or []:
                local = db.execute("SELECT mirror FROM sessions WHERE id=?", (row["id"],)).fetchone()
                if local and not local["mirror"]:
                    self.log(f"relay: skipped {row['id']}, which is one of this Mac's own conversations")
                    continue
                db.execute("""INSERT INTO sessions (id,name,provider,external_id,cwd,route,endpoint,created,seen,
                    connection,lease_until,remote_token,mirror) VALUES (?,?,'remote',?,?,'inbox',NULL,?,?,?,?,?,1)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,provider='remote',external_id=excluded.external_id,
                    cwd=excluded.cwd,seen=excluded.seen,connection=excluded.connection,lease_until=excluded.lease_until,
                    remote_token=excluded.remote_token,mirror=1""",
                           (row["id"], row["name"], row.get("external_id"), row.get("cwd"), row["created"], row["seen"],
                            row.get("connection") or "offline", row.get("lease_until") or 0, row.get("remote_token")))
            sessions = {r["id"] for r in db.execute("SELECT id FROM sessions WHERE mirror=1")}
            items = set()
            for row in answer.get("items") or []:
                if row["id"] in held:
                    skipped = True
                if row["session_id"] not in sessions or row["id"] in held:
                    continue
                local = db.execute("SELECT session_id FROM items WHERE id=?", (row["id"],)).fetchone()
                if local and local["session_id"] not in sessions:
                    continue
                items.add(row["id"])
                db.execute("""INSERT INTO items (id,session_id,title,detail,kind,status,tags,options,created,updated,
                    owner,waiting_on,defer_until,presentation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET session_id=excluded.session_id,title=excluded.title,detail=excluded.detail,
                    kind=excluded.kind,status=excluded.status,tags=excluded.tags,options=excluded.options,
                    created=excluded.created,updated=excluded.updated,owner=excluded.owner,waiting_on=excluded.waiting_on,
                    defer_until=excluded.defer_until,presentation=excluded.presentation""",
                           tuple(row[k] for k in ("id", "session_id", "title", "detail", "kind", "status", "tags", "options",
                                                  "created", "updated", "owner", "waiting_on", "defer_until", "presentation")))
            skipped = skipped or any(r["item_id"] in held for r in (answer.get("replies") or []) + (answer.get("comments") or []))
            # What the relay doesn't have for an item it sent whole, such as an answer it refused, goes.
            for item_id in items:
                keep = {"replies": {r["id"] for r in answer.get("replies") or [] if r["item_id"] == item_id},
                        "comments": {c["id"] for c in answer.get("comments") or [] if c["item_id"] == item_id}}
                for table, order in (("replies", ""), ("comments", " ORDER BY parent_id IS NULL")):
                    for row in db.execute(f"SELECT id FROM {table} WHERE item_id=?{order}", (item_id,)).fetchall():
                        if row["id"] not in keep[table]:
                            db.execute(f"DELETE FROM {table} WHERE id=?", (row["id"],))
            mirrored = lambda item_id: item_id not in held and db.execute(
                "SELECT 1 FROM items i JOIN sessions s ON s.id=i.session_id WHERE i.id=? AND s.mirror=1", (item_id,)).fetchone()
            for row in answer.get("replies") or []:
                if not mirrored(row["item_id"]):
                    continue
                db.execute("DELETE FROM replies WHERE item_id=? AND id!=?", (row["item_id"], row["id"]))
                db.execute("""INSERT INTO replies (id,item_id,text,state,error,created,delivered,claim_until,attachments)
                    VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET text=excluded.text,state=excluded.state,
                    error=excluded.error,created=excluded.created,delivered=excluded.delivered,claim_until=excluded.claim_until""",
                           (row["id"], row["item_id"], row["text"], row["state"], row.get("error"), row["created"],
                            row.get("delivered"), row.get("claim_until") or 0, row.get("attachments") or "[]"))
            # A question before the answer that points at it.
            for row in sorted(answer.get("comments") or [], key=lambda c: (c.get("parent_id") is not None, c["created"])):
                if not mirrored(row["item_id"]):
                    continue
                if row.get("parent_id") and not db.execute("SELECT 1 FROM comments WHERE id=?", (row["parent_id"],)).fetchone():
                    continue
                db.execute("""INSERT INTO comments (id,item_id,parent_id,author,text,state,error,created,delivered,claim_until,attachments)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET text=excluded.text,state=excluded.state,
                    error=excluded.error,delivered=excluded.delivered,claim_until=excluded.claim_until""",
                           (row["id"], row["item_id"], row.get("parent_id"), row["author"], row["text"], row["state"],
                            row.get("error"), row["created"], row.get("delivered"), row.get("claim_until") or 0,
                            row.get("attachments") or "[]"))
            for token_id, used in (answer.get("tokens") or {}).items():
                if used:
                    db.execute("UPDATE remote_tokens SET last_used=MAX(COALESCE(last_used,0),?) WHERE id=?", (used, token_id))
            news = [("relay_pulled", time.time())] + ([] if skipped else [("relay_cursor", answer.get("seq") or 0)])
            for key, value in news:
                db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            db.execute("DELETE FROM meta WHERE key='relay_error'")
        return not skipped

    # -- The worker --------------------------------------------------------------------------------

    def failed(self, error, delay):
        if self.stop.is_set():
            return delay  # cut short because HQ is stopping, not a fault
        if str(error) != self.last_error:
            self.last_error = str(error)
            self.note("relay_error", str(error))
            self.log(f"relay: {error}")
        self.stop.wait(delay)
        return min(delay * 2, 60)

    def pushing(self):
        delay = 2
        while not self.stop.is_set():
            try:
                config = load_config(self.store)
                if not config:
                    self.stop.wait(5)
                    continue
                self.stop.wait(0.2 if self.push_once(config) == PUSH_BATCH else 1)
                delay = 2
            except Exception as error:  # the relay or this Mac's network; tried again shortly
                delay = self.failed(error, delay)

    def hang_up(self):
        """When HQ stops, end the long poll now rather than when the relay answers it, so HQ exits
        at once and can start again straight away."""
        self.stop.wait()
        for link in list(self.links):
            try:
                if link.sock:
                    link.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def run(self):
        threading.Thread(target=self.pushing, daemon=True).start()
        threading.Thread(target=self.hang_up, daemon=True).start()
        delay = 2
        while not self.stop.is_set():
            try:
                config = load_config(self.store)
                if not config:
                    self.stop.wait(5)
                    continue
                self.pull_once(config)
                self.last_error, delay = None, 2
            except Exception as error:  # the relay or this Mac's network; tried again shortly
                delay = self.failed(error, delay)

"""One local database shared by the CLI, dashboard, and MCP processes."""
import json
from datetime import date
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

PROVIDERS = {"codex", "claude-code", "claude-desktop", "chatgpt", "other"}
ROUTES = {"inbox", "codex"}


def data_dir():
    return Path(os.environ.get("HQ_HOME", Path.home() / ".local/share/hq")).expanduser().resolve()


def required(value, name, limit=10000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonempty text, at most {limit} characters")
    return value.strip()


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_dir() / "hq.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL,
                    external_id TEXT, cwd TEXT, route TEXT NOT NULL DEFAULT 'inbox',
                    endpoint TEXT, created REAL NOT NULL, seen REAL NOT NULL,
                    connection TEXT NOT NULL DEFAULT 'offline', lease_until REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                    tags TEXT NOT NULL DEFAULT '[]', options TEXT NOT NULL DEFAULT '[]',
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS replies (
                    id TEXT PRIMARY KEY, item_id TEXT NOT NULL UNIQUE REFERENCES items(id),
                    text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    error TEXT, created REAL NOT NULL, delivered REAL, claim_until REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS waiters (
                    item_id TEXT PRIMARY KEY REFERENCES items(id), owner TEXT, lease_until REAL
                );
                CREATE TABLE IF NOT EXISTS comments (
                    id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES items(id),
                    parent_id TEXT UNIQUE REFERENCES comments(id), author TEXT NOT NULL,
                    text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    error TEXT, created REAL NOT NULL, delivered REAL, claim_until REAL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS comments_item ON comments(item_id, created);
                CREATE INDEX IF NOT EXISTS items_session ON items(session_id, status);
                CREATE TABLE IF NOT EXISTS codex_connections (
                    endpoint TEXT PRIMARY KEY, updated REAL NOT NULL
                );
            """)
            # Serialize migrations across dashboard, CLI, and MCP processes.
            db.execute("BEGIN IMMEDIATE")
            columns = {r["name"] for r in db.execute("PRAGMA table_info(items)")}
            if "owner" not in columns:
                db.execute("ALTER TABLE items ADD COLUMN owner TEXT NOT NULL DEFAULT 'human'")
                db.execute("UPDATE items SET owner='agent' WHERE kind='todo'")
            if "waiting_on" not in columns:
                db.execute("ALTER TABLE items ADD COLUMN waiting_on TEXT NOT NULL DEFAULT ''")
            if "defer_until" not in columns:
                db.execute("ALTER TABLE items ADD COLUMN defer_until TEXT NOT NULL DEFAULT ''")
            if "presentation" not in columns:
                db.execute("ALTER TABLE items ADD COLUMN presentation TEXT NOT NULL DEFAULT '{}'")
            db.execute("PRAGMA user_version=4")
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, id, name, provider="other", external_id=None, cwd=None,
                 route="inbox", endpoint=None):
        id, name = required(id, "session id", 200), required(name, "name", 500)
        if provider not in PROVIDERS or route not in ROUTES:
            raise ValueError("Unknown provider or response route")
        if route == "codex" and (provider != "codex" or not external_id):
            raise ValueError("Codex routing requires a Codex thread id")
        now = time.time()
        with self.db() as db:
            db.execute("""INSERT INTO sessions
                (id,name,provider,external_id,cwd,route,endpoint,created,seen)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, provider=excluded.provider,
                external_id=COALESCE(excluded.external_id,sessions.external_id),
                cwd=COALESCE(excluded.cwd,sessions.cwd), seen=excluded.seen""",
                       (id, name, provider, external_id, cwd, route, endpoint, now, now))
        return self.session(id)

    def bind_codex(self, id, external_id, endpoint=None):
        self.session(id)
        with self.db() as db:
            db.execute("UPDATE sessions SET route='codex',external_id=?,endpoint=? WHERE id=? AND provider='codex'",
                       (required(external_id, "thread id", 200), endpoint, id))
        return self.session(id)

    def session(self, id):
        with self.db() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (id,)).fetchone()
        if not row:
            raise ValueError(f"Session {id} does not exist; register it first")
        result = dict(row)
        if result["lease_until"] < time.time():
            result["connection"] = "offline"
        return result

    def heartbeat(self, id, connection="tools", lease=30):
        self.session(id)
        with self.db() as db:
            now = time.time()
            # Tool calls must not hide an established push connection.
            db.execute("""UPDATE sessions SET seen=?,
                connection=CASE WHEN connection IN ('channel','codex') AND lease_until>? AND ? IN ('tools','hooks','waiting')
                    THEN connection ELSE ? END,
                lease_until=MAX(lease_until,?) WHERE id=?""",
                       (now, now, connection, connection, now + lease, id))

    def disconnect(self, id, connection):
        with self.db() as db:
            db.execute("UPDATE sessions SET connection='offline',lease_until=0 WHERE id=? AND connection=?", (id, connection))

    def watch_codex(self, endpoint=None):
        with self.db() as db:
            db.execute("INSERT INTO codex_connections VALUES (?,?) ON CONFLICT(endpoint) DO UPDATE SET updated=excluded.updated",
                       (endpoint or "", time.time()))

    def codex_endpoints(self):
        with self.db() as db:
            return [r["endpoint"] or None for r in db.execute("SELECT endpoint FROM codex_connections")]

    def claim_hook_messages(self, session_id, limit=4):
        """Queue context once; only an agent acknowledgement establishes receipt."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session:
                raise ValueError("Register the session first")
            if session["route"] == "codex" or (session["connection"] == "channel" and session["lease_until"] > time.time()):
                return [], []
            replies = db.execute("""SELECT r.*,i.title FROM replies r JOIN items i ON i.id=r.item_id
                WHERE i.session_id=? AND r.state='pending'
                AND NOT EXISTS (SELECT 1 FROM waiters w WHERE w.item_id=i.id AND w.lease_until>?)
                ORDER BY r.created LIMIT ?""", (session_id, time.time(), limit)).fetchall()
            questions = db.execute("""SELECT c.*,i.title FROM comments c JOIN items i ON i.id=c.item_id
                WHERE i.session_id=? AND c.author='human' AND c.state='pending'
                AND NOT EXISTS (SELECT 1 FROM comments a WHERE a.parent_id=c.id)
                AND NOT EXISTS (SELECT 1 FROM waiters w WHERE w.item_id=i.id AND w.lease_until>?)
                ORDER BY c.created,c.rowid LIMIT ?""", (session_id, time.time(), limit-len(replies))).fetchall()
            for row in replies:
                db.execute("UPDATE replies SET state='sending',claim_until=? WHERE id=?", (time.time()+60, row["id"]))
            for row in questions:
                db.execute("UPDATE comments SET state='sending',claim_until=? WHERE id=?", (time.time()+60, row["id"]))
        return [dict(r) for r in replies], [dict(q) for q in questions]

    def add(self, session_id, title, detail="", kind="todo", tags=None, options=None,
            owner=None, waiting_on="", defer_until="", *, unlocks="", recommendation="",
            steps=None, link=None, paste=None, minutes=None, rank=9999, source=""):
        self.session(session_id)
        title = required(title, "title", 500)
        if kind not in {"todo", "blocker", "question"}:
            raise ValueError("kind must be todo, blocker, or question")
        if not isinstance(detail, str) or len(detail) > 20000:
            raise ValueError("detail must be text, at most 20000 characters")
        tags, options = tags or [], options or []
        if not isinstance(tags, list) or not isinstance(options, list):
            raise ValueError("tags and options must be lists")
        if len(tags) > 20 or len(options) > 10:
            raise ValueError("Use at most 20 tags and 10 options")
        tags = sorted(set(required(t, "tag", 60) for t in tags))
        options = [self.option(o) for o in options]
        presentation = self.presentation(unlocks=unlocks, recommendation=recommendation, steps=[] if steps is None else steps,
                                         link=link, paste=[] if paste is None else paste, minutes=minutes, rank=rank, source=source)
        owner, waiting_on, defer_until = self.attention(("agent" if kind == "todo" else "human") if owner is None else owner, waiting_on, defer_until)
        id, now = str(uuid.uuid4()), time.time()
        with self.db() as db:
            db.execute("INSERT INTO items (id,session_id,title,detail,kind,tags,options,created,updated,owner,waiting_on,defer_until,presentation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (id, session_id, title, detail, kind, json.dumps(tags), json.dumps(options), now, now, owner, waiting_on, defer_until, json.dumps(presentation)))
        return self.item(id)

    @staticmethod
    def option(value):
        if isinstance(value, str):
            return required(value, "option", 500)
        if not isinstance(value, dict) or set(value) - {"id", "label", "note", "recommended"}:
            raise ValueError("options must be text or objects with label, note, recommended, and optional id")
        result = {"label": required(value.get("label"), "option label", 500)}
        if "id" in value:
            result["id"] = required(value["id"], "option id", 100)
        note = value.get("note", "")
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError("option note must be text, at most 2000 characters")
        if not isinstance(value.get("recommended", False), bool):
            raise ValueError("option recommended must be true or false")
        return {**result, "note": note, "recommended": value.get("recommended", False)}

    @staticmethod
    def presentation(**fields):
        result = {"unlocks": "", "recommendation": "", "steps": [], "link": None,
                  "paste": [], "minutes": None, "rank": 9999, "source": "", "agent_note": "", **fields}
        for key in ("unlocks", "recommendation", "source", "agent_note"):
            if not isinstance(result[key], str) or len(result[key]) > 4000:
                raise ValueError(f"{key} must be text, at most 4000 characters")
        if not isinstance(result["steps"], list) or len(result["steps"]) > 20:
            raise ValueError("steps must be a list of at most 20 short instructions")
        result["steps"] = [required(step, "step", 2000) for step in result["steps"]]
        link = result["link"]
        if link is not None:
            if not isinstance(link, dict) or set(link) != {"label", "url"}:
                raise ValueError("link must contain label and url")
            url = required(link["url"], "link url", 4000)
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
            except ValueError:
                valid = False
            if not valid or any(c.isspace() or ord(c) < 32 for c in url):
                raise ValueError("link must be an HTTPS URL without credentials")
            result["link"] = {"label": required(link["label"], "link label", 200), "url": url}
        if not isinstance(result["paste"], list) or len(result["paste"]) > 10:
            raise ValueError("paste must be a list of at most 10 copy blocks")
        blocks = []
        for block in result["paste"]:
            if not isinstance(block, dict) or set(block) - {"label", "text", "mono"}:
                raise ValueError("copy blocks must contain label and text, with optional mono")
            if not isinstance(block.get("mono", False), bool):
                raise ValueError("copy block mono must be true or false")
            blocks.append({"label": required(block.get("label"), "copy label", 200),
                           "text": required(block.get("text"), "copy text", 20000), "mono": block.get("mono", False)})
        result["paste"] = blocks
        for key in ("minutes", "rank"):
            value = result[key]
            if value is None and key == "minutes":
                continue
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100000:
                raise ValueError(f"{key} must be a nonnegative integer at most 100000")
        return result

    @staticmethod
    def attention(owner, waiting_on, defer_until):
        if owner not in {"human", "agent", "external"}:
            raise ValueError("owner must be human, agent, or external")
        if not isinstance(waiting_on, str) or len(waiting_on) > 200:
            raise ValueError("waiting_on must be text, at most 200 characters")
        waiting_on = waiting_on.strip()
        if owner == "external" and not waiting_on:
            raise ValueError("Name who or what this item is waiting on")
        if not isinstance(defer_until, str):
            raise ValueError("defer_until must be a date in YYYY-MM-DD format or empty")
        if defer_until:
            try:
                if date.fromisoformat(defer_until).isoformat() != defer_until:
                    raise ValueError()
            except ValueError:
                raise ValueError("defer_until must be a date in YYYY-MM-DD format or empty") from None
        return owner, waiting_on if owner == "external" else "", defer_until

    def update(self, item_id, session_id=None, **patch):
        allowed = {"owner", "waiting_on", "defer_until", "title", "detail", "unlocks", "recommendation", "steps", "link", "paste", "minutes", "rank", "source"}
        if not patch or set(patch) - allowed:
            raise ValueError("Update title, detail, owner, waiting_on, or defer_until")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise ValueError("Item not found")
            if session_id is not None and row["session_id"] != session_id:
                raise ValueError("Item belongs to a different session")
            if row["status"] != "open":
                raise ValueError("Only open items can be updated; saved replies are preserved")
            merged = {**dict(row), **patch}
            owner, waiting_on, defer_until = self.attention(merged["owner"], merged["waiting_on"], merged["defer_until"])
            title = required(merged["title"], "title", 500)
            detail = merged["detail"]
            if not isinstance(detail, str) or len(detail) > 20000:
                raise ValueError("detail must be text, at most 20000 characters")
            presentation = self.presentation(**{**json.loads(row["presentation"]), **{k: v for k, v in patch.items() if k not in {"owner", "waiting_on", "defer_until", "title", "detail"}}})
            db.execute("UPDATE items SET title=?,detail=?,owner=?,waiting_on=?,defer_until=?,presentation=?,updated=? WHERE id=?",
                       (title, detail, owner, waiting_on, defer_until, json.dumps(presentation), time.time(), item_id))
        return self.item(item_id)

    @staticmethod
    def bucket(item, today=None):
        if item.get("asking"):
            return "waiting"
        if item["status"] == "done":
            return "done"
        if item["status"] == "answered":
            return "waiting"
        if item["defer_until"] and item["defer_until"] > (today or date.today().isoformat()):
            return "later"
        return {"human": "needs", "agent": "pending", "external": "waiting"}[item["owner"]]

    @staticmethod
    def decode(row):
        data = dict(row)
        for key in ("tags", "options"):
            data[key] = json.loads(data[key])
        data.update(Store.presentation(**json.loads(data.pop("presentation"))))
        data["bucket"] = Store.bucket(data)
        return data

    def item(self, id):
        with self.db() as db:
            row = db.execute("""SELECT i.*,r.text AS answer,r.state AS delivery,r.error AS delivery_error
                FROM items i LEFT JOIN replies r ON i.id=r.item_id WHERE i.id=?""", (id,)).fetchone()
        if not row:
            raise ValueError("Item not found")
        result = self.decode(row)
        self.attach_comments([result])
        return result

    def snapshot(self, session_id=None, tag=None, status=None, bucket=None):
        with self.db() as db:
            sessions = [dict(r) for r in db.execute("SELECT * FROM sessions ORDER BY seen DESC")]
            items = [self.decode(r) for r in db.execute("""SELECT i.*,s.name AS session_name,s.provider,
                r.text AS answer,r.state AS delivery,r.error AS delivery_error
                FROM items i JOIN sessions s ON s.id=i.session_id
                LEFT JOIN replies r ON i.id=r.item_id ORDER BY i.created DESC""")]
        self.attach_comments(items)
        for s in sessions:
            if s["lease_until"] < time.time():
                s["connection"] = "offline"
        items = [i for i in items if (not session_id or i["session_id"] == session_id)
                 and (not tag or tag in i["tags"]) and (not status or i["status"] == status)
                 and (not bucket or i["bucket"] == bucket)]
        return {"sessions": sessions, "items": items}

    def reply(self, id, text):
        text, now = required(text, "answer", 20000), time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,kind FROM items WHERE id=?", (id,)).fetchone()
            if not row or row["status"] != "open":
                raise ValueError("Only open items can receive a response")
            db.execute("INSERT INTO replies (id,item_id,text,created) VALUES (?,?,?,?)",
                       (str(uuid.uuid4()), id, text, now))
            db.execute("UPDATE items SET status='answered',updated=? WHERE id=?", (now, id))
        return self.item(id)

    def resolve(self, id, note=""):
        if not isinstance(note, str) or len(note) > 4000:
            raise ValueError("completion note must be text, at most 4000 characters")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM items WHERE id=?", (id,)).fetchone():
                raise ValueError("Item not found")
            pending = db.execute("SELECT 1 FROM replies WHERE item_id=? AND state IN ('pending','sending','sent','failed')", (id,)).fetchone()
            if pending:
                raise ValueError("The response must be received before this item can be completed")
            if db.execute("SELECT 1 FROM comments c WHERE c.item_id=? AND c.author='human' AND c.state!='cancelled' AND NOT EXISTS (SELECT 1 FROM comments a WHERE a.parent_id=c.id)", (id,)).fetchone():
                raise ValueError("Answer or cancel the outstanding clarification before completing this item")
            presentation = json.loads(db.execute("SELECT presentation FROM items WHERE id=?", (id,)).fetchone()[0])
            if note:
                presentation["agent_note"] = required(note, "completion note", 4000)
            db.execute("UPDATE items SET status='done',presentation=?,updated=? WHERE id=?", (json.dumps(presentation), time.time(), id))
        return self.item(id)

    def attach_comments(self, items):
        threads = {}
        with self.db() as db:
            for row in db.execute("""SELECT c.*,
                EXISTS (SELECT 1 FROM comments a WHERE a.parent_id=c.id) AS answered
                FROM comments c ORDER BY c.created,c.rowid"""):
                comment = dict(row)
                comment["answered"] = bool(comment["answered"])
                threads.setdefault(comment["item_id"], []).append(comment)
        for item in items:
            item["comments"] = threads.get(item["id"], [])
            item["asking"] = any(c["author"] == "human" and not c["answered"] and c["state"] != "cancelled" for c in item["comments"])
            item["bucket"] = self.bucket(item)

    def ask_agent(self, item_id, text):
        text, now, comment_id = required(text, "question", 20000), time.time(), str(uuid.uuid4())
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
            if not item or item["status"] == "done":
                raise ValueError("Ask for clarification on an unfinished item")
            db.execute("INSERT INTO comments (id,item_id,author,text,created) VALUES (?,?,'human',?,?)", (comment_id, item_id, text, now))
            db.execute("UPDATE items SET updated=? WHERE id=?", (now, item_id))
        return self.comment(comment_id)

    def comment(self, comment_id):
        with self.db() as db:
            row = db.execute("""SELECT c.*,i.session_id,i.title,
                EXISTS (SELECT 1 FROM comments a WHERE a.parent_id=c.id) AS answered
                FROM comments c JOIN items i ON i.id=c.item_id WHERE c.id=?""", (comment_id,)).fetchone()
        if not row:
            raise ValueError("Comment not found")
        result = dict(row)
        result["answered"] = bool(result["answered"])
        return result

    def comments(self, session_id, item_id=None, pending=False):
        self.session(session_id)
        if item_id and self.item(item_id)["session_id"] != session_id:
            raise ValueError("Item belongs to a different session")
        with self.db() as db:
            rows = db.execute("""SELECT c.*,i.session_id,i.title,
                EXISTS (SELECT 1 FROM comments a WHERE a.parent_id=c.id) AS answered
                FROM comments c JOIN items i ON i.id=c.item_id WHERE i.session_id=?
                AND (? IS NULL OR c.item_id=?) ORDER BY c.created,c.rowid""", (session_id, item_id, item_id))
            result = [{**dict(row), "answered": bool(row["answered"])} for row in rows]
        return [c for c in result if not pending or (c["author"] == "human" and not c["answered"] and c["state"] != "cancelled")]

    def respond(self, comment_id, session_id, text):
        text, now, id = required(text, "agent response", 20000), time.time(), str(uuid.uuid4())
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            question = db.execute("SELECT c.*,i.session_id FROM comments c JOIN items i ON i.id=c.item_id WHERE c.id=?", (comment_id,)).fetchone()
            if not question or question["session_id"] != session_id or question["author"] != "human":
                raise ValueError("No human question in this session")
            if question["state"] == "cancelled":
                raise ValueError("This question was cancelled")
            if db.execute("SELECT 1 FROM comments WHERE parent_id=?", (comment_id,)).fetchone():
                raise ValueError("This question already has a response")
            db.execute("INSERT INTO comments (id,item_id,parent_id,author,text,state,created) VALUES (?,?,?,'agent',?,'posted',?)", (id, question["item_id"], comment_id, text, now))
            db.execute("UPDATE comments SET state='received',error=NULL,delivered=?,claim_until=0 WHERE id=?", (now, comment_id))
            db.execute("UPDATE items SET updated=? WHERE id=?", (now, question["item_id"]))
        return self.comment(id)

    def acknowledge_comment(self, comment_id, session_id):
        with self.db() as db:
            cursor = db.execute("""UPDATE comments SET state='received',error=NULL,delivered=?,claim_until=0
                WHERE id=? AND author='human' AND state!='cancelled'
                AND item_id IN (SELECT id FROM items WHERE session_id=?)""", (time.time(), comment_id, session_id))
            if not cursor.rowcount:
                raise ValueError("No active human question in this session")
        return self.comment(comment_id)

    def cancel_comment(self, comment_id):
        with self.db() as db:
            cursor = db.execute("""UPDATE comments SET state='cancelled',error=NULL,claim_until=0
                WHERE id=? AND author='human' AND NOT EXISTS (SELECT 1 FROM comments a WHERE a.parent_id=comments.id)""", (comment_id,))
            if not cursor.rowcount:
                raise ValueError("Only unanswered human questions can be cancelled")
        return self.comment(comment_id)

    def take_questions(self, item_id, session_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT c.* FROM comments c JOIN items i ON i.id=c.item_id
                WHERE c.item_id=? AND i.session_id=? AND c.author='human'
                AND c.state IN ('pending','sent','failed')
                AND NOT EXISTS (SELECT 1 FROM comments a WHERE a.parent_id=c.id)
                ORDER BY c.created,c.rowid""", (item_id, session_id)).fetchall()
            for row in rows:
                db.execute("UPDATE comments SET state='received',error=NULL,delivered=?,claim_until=0 WHERE id=?", (time.time(), row["id"]))
        return [{**dict(row), "state": "received"} for row in rows]

    def claim_comment(self, session_id=None):
        # A waiting tool gets first choice; native/channel delivery claims atomically.
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT c.*,i.session_id,i.title,s.external_id,s.endpoint
                FROM comments c JOIN items i ON i.id=c.item_id JOIN sessions s ON s.id=i.session_id
                WHERE c.author='human' AND c.state='pending'
                AND ((? IS NULL AND s.route='codex') OR s.id=?)
                AND NOT EXISTS (SELECT 1 FROM comments a WHERE a.parent_id=c.id)
                AND NOT EXISTS (SELECT 1 FROM waiters w WHERE w.item_id=i.id AND w.lease_until>?)
                ORDER BY c.created,c.rowid LIMIT 1""", (session_id, session_id, time.time())).fetchone()
            if row:
                db.execute("UPDATE comments SET state='sending',claim_until=? WHERE id=?", (time.time()+60, row["id"]))
                return dict(row)

    def comment_delivery(self, comment_id, state, error=None):
        with self.db() as db:
            db.execute("UPDATE comments SET state=?,error=?,delivered=?,claim_until=0 WHERE id=? AND state NOT IN ('received','cancelled')",
                       (state, error, time.time() if state == "received" else None, comment_id))

    def retry_comment(self, comment_id):
        with self.db() as db:
            cursor = db.execute("""UPDATE comments SET state='pending',error=NULL WHERE id=? AND author='human' AND state='failed'
                AND NOT EXISTS (SELECT 1 FROM comments a WHERE a.parent_id=comments.id)""", (comment_id,))
            if not cursor.rowcount:
                raise ValueError("Only failed unanswered questions can be retried")
        return self.comment(comment_id)

    def inbox(self, session_id):
        self.session(session_id)
        with self.db() as db:
            return [dict(r) for r in db.execute("""SELECT r.*,i.title FROM replies r
                JOIN items i ON i.id=r.item_id WHERE i.session_id=?
                AND r.state IN ('pending','sent','failed') ORDER BY r.created""", (session_id,))]

    def has_waiter(self, item_id):
        with self.db() as db:
            return db.execute("SELECT 1 FROM waiters WHERE item_id=? AND lease_until>?", (item_id, time.time())).fetchone() is not None

    def acknowledge(self, item_id, session_id):
        with self.db() as db:
            cursor = db.execute("""UPDATE replies SET state='received',delivered=?,error=NULL
                WHERE item_id=? AND item_id IN (SELECT id FROM items WHERE session_id=?)""",
                                (time.time(), item_id, session_id))
            if not cursor.rowcount:
                raise ValueError("No response for this item in the specified session")
        return self.item(item_id)

    def wait(self, item_id, session_id, timeout=3600, cancel=None):
        if self.item(item_id)["session_id"] != session_id:
            raise ValueError("This item belongs to a different session")
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 86400:
            raise ValueError("timeout must be between 1 and 86400 seconds")
        deadline, owner = time.monotonic() + timeout, str(uuid.uuid4())
        try:
            while time.monotonic() < deadline:
                if cancel and cancel.is_set():
                    return {"item_id": item_id, "status": "cancelled"}
                self.heartbeat(session_id, "waiting")
                with self.db() as db:
                    db.execute("INSERT INTO waiters VALUES (?,?,?) ON CONFLICT(item_id) DO UPDATE SET owner=excluded.owner,lease_until=excluded.lease_until",
                               (item_id, owner, time.time() + 5))
                item = self.item(item_id)
                if item["answer"] is not None:
                    self.acknowledge(item_id, session_id)
                    return {"item_id": item_id, "status": "received", "answer": item["answer"]}
                questions = self.take_questions(item_id, session_id)
                if questions:
                    return {"item_id": item_id, "status": "clarification", "questions": questions,
                            "message": "Reply to each comment with hq_comment_reply, then use hq_wait for the original decision. The question remains open."}
                if item["status"] == "done":
                    return {"item_id": item_id, "status": "closed"}
                if cancel:
                    cancel.wait(0.3)
                else:
                    time.sleep(0.3)
            return {"item_id": item_id, "status": "timeout", "message": "Question remains in HQ; await or read the response later."}
        finally:
            with self.db() as db:
                db.execute("DELETE FROM waiters WHERE item_id=? AND owner=?", (item_id, owner))

    def claim_native(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT r.*,i.session_id,i.title,s.external_id,s.endpoint
                FROM replies r JOIN items i ON i.id=r.item_id JOIN sessions s ON s.id=i.session_id
                WHERE s.route='codex' AND r.state='pending'
                AND NOT EXISTS (SELECT 1 FROM waiters w WHERE w.item_id=i.id AND w.lease_until>strftime('%s','now'))
                ORDER BY r.created LIMIT 1""").fetchone()
            if row:
                db.execute("UPDATE replies SET state='sending',claim_until=? WHERE id=?", (time.time() + 60, row["id"]))
                return dict(row)

    def delivery_result(self, reply_id, state, error=None):
        with self.db() as db:
            db.execute("UPDATE replies SET state=?,error=?,delivered=?,claim_until=0 WHERE id=? AND (state!='received' OR ?='received')",
                       (state, error, time.time() if state == "received" else None, reply_id, state))

    def recover_uncertain(self):
        # Never resend a turn after a crash without knowing whether it was accepted.
        with self.db() as db:
            db.execute("""UPDATE replies SET state='failed',error='Delivery interrupted; check the conversation before retrying.'
                WHERE state='sending' AND claim_until < ?""", (time.time(),))

            db.execute("""UPDATE comments SET state='failed',error='Delivery interrupted; check the conversation before retrying.'
                WHERE state='sending' AND claim_until < ?""", (time.time(),))

    def retry(self, item_id):
        with self.db() as db:
            cursor = db.execute("UPDATE replies SET state='pending',error=NULL WHERE item_id=? AND state='failed'", (item_id,))
            if not cursor.rowcount:
                raise ValueError("Only failed deliveries can be retried")
        return self.item(item_id)

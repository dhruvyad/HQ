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

from . import attachments as files
from .workstreams import OTHER, place, project_of

PROVIDERS = {"codex", "claude-code", "claude-desktop", "chatgpt", "other"}
ROUTES = {"inbox", "codex"}
# A decision closes in two steps, like a handshake. Your reply, answer or skip or done, is
# only the first: it stays tracked until the agent confirms it heard, and for an answer,
# until it says what it did. An agent that stops first leaves its decision stuck, and the
# next agent in the same project may adopt it.
SETTLES = {"skip", "done"}   # your closes that end once the agent has heard them
STOPPED_AFTER = 300          # offline this long: the agent stopped before acting
OFFLINE_FOR = 3600           # a session gone from a live inventory stays offline this long unless seen again
STALE_AFTER = 86400          # a day unheard, or heard but never closed
REMIND_EVERY = 6 * 3600      # how often an agent is reminded of what it heard but never closed
KEEP_FILES = 30 * 86400      # files on an item are deleted this long after it is done
SCHEMA = 10                  # PRAGMA user_version once every migration below has run


def data_dir():
    return Path(os.environ.get("HQ_HOME", Path.home() / ".local/share/hq")).expanduser().resolve()


def required(value, name, limit=10000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonempty text, at most {limit} characters")
    return value.strip()


def followup(item, session, now):
    """Where a decided item stands, between your reply and the agent closing it.

    stage: delivering until the agent confirms it heard, then acknowledged.
    stuck: stopped (its agent went offline first), unheard (a day with no acknowledgement)
    or unclosed (heard a day ago, never closed). adoptable: another agent in the same
    project may take it over, because its own agent is gone or you handed it off.
    """
    if item["status"] != "answered":
        return None
    activity = (session or {}).get("activity") or {}
    state, since = activity.get("state", "unknown"), activity.get("since") or now
    heard = item.get("delivery") == "received"
    stuck = None
    if state == "offline" and now - since >= STOPPED_AFTER:
        stuck = "stopped"
    elif not heard and now - (item.get("replied") or now) >= STALE_AFTER:
        stuck = "unheard"
    elif heard and state != "active" and now - (item.get("heard") or now) >= STALE_AFTER:
        stuck = "unclosed"
    # An agent with no live signal at all that has not heard in a day counts as gone too.
    gone = stuck == "stopped" or (stuck == "unheard" and state == "unknown")
    return {"stage": "acknowledged" if heard else "delivering", "stuck": stuck,
            "adoptable": item["project"] != OTHER and (gone or bool(item.get("handoff"))),
            "replied": item.get("replied"), "heard": item.get("heard")}


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
                CREATE TABLE IF NOT EXISTS activity (
                    session_id TEXT NOT NULL REFERENCES sessions(id), source TEXT NOT NULL,
                    state TEXT NOT NULL, observed REAL NOT NULL, expires REAL NOT NULL,
                    priority INTEGER NOT NULL, since REAL, last_active REAL, PRIMARY KEY(session_id,source)
                );
                -- Claude and Codex accounts you added. Never a secret: logins live only in the Keychain.
                CREATE TABLE IF NOT EXISTS accounts (
                    tool TEXT NOT NULL, key TEXT NOT NULL, identity TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
                    plan TEXT NOT NULL DEFAULT '', org TEXT NOT NULL DEFAULT '', added REAL NOT NULL, last_used REAL,
                    expires REAL, refresh_expires REAL, stale INTEGER NOT NULL DEFAULT 0,
                    usage TEXT, usage_as_of REAL, PRIMARY KEY(tool, key)
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
            # When a state began, and when an agent was last seen running: observations
            # repeat every second, so neither can be read from the latest one alone.
            observed = {r["name"] for r in db.execute("PRAGMA table_info(activity)")}
            if "since" not in observed:
                db.execute("ALTER TABLE activity ADD COLUMN since REAL")
            if "last_active" not in observed:
                db.execute("ALTER TABLE activity ADD COLUMN last_active REAL")
            # Before the handshake a skip closed at once, heard or not: track the unheard ones.
            if db.execute("PRAGMA user_version").fetchone()[0] < 7:
                for row in db.execute("""SELECT i.id,i.presentation FROM items i JOIN replies r ON r.item_id=i.id
                        WHERE i.status='done' AND r.state!='received'""").fetchall():
                    presentation = json.loads(row["presentation"])
                    if presentation.get("skipped"):
                        presentation["settle"] = "skip"
                        db.execute("UPDATE items SET status='answered',presentation=? WHERE id=?", (json.dumps(presentation), row["id"]))
            # Files pasted into a reply or a question, as their metadata; the text carries their paths.
            for table in ("replies", "comments"):
                if "attachments" not in {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
            # When you put an agent away; it comes back by itself once it runs or reports again.
            if "archived" not in {r["name"] for r in db.execute("PRAGMA table_info(sessions)")}:
                db.execute("ALTER TABLE sessions ADD COLUMN archived REAL NOT NULL DEFAULT 0")
            # An inbox that ran an early build of accounts has the table without these.
            kept = {r["name"] for r in db.execute("PRAGMA table_info(accounts)")}
            for name, declared in (("last_used", "REAL"), ("expires", "REAL"), ("refresh_expires", "REAL"),
                                   ("stale", "INTEGER NOT NULL DEFAULT 0"), ("usage", "TEXT"), ("usage_as_of", "REAL")):
                if name not in kept:
                    db.execute(f"ALTER TABLE accounts ADD COLUMN {name} {declared}")
            db.execute(f"PRAGMA user_version={SCHEMA}")
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

    def unbind_codex(self, endpoint):
        """Nothing listens at this runtime any more (it quit, or ChatGPT was opened without it), so its
        conversations take replies through their hooks again. A runtime that lists them later binds them anew."""
        with self.db() as db:
            db.execute("UPDATE sessions SET route='inbox',endpoint=NULL WHERE route='codex' AND COALESCE(endpoint,'')=?",
                       (endpoint or "",))

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

    def archive(self, ids, archived=True):
        """Put agents away in the dashboard and the pill, or bring them back. Only a view: nothing
        is deleted, and an agent comes back by itself once it runs or has something open."""
        if not isinstance(ids, list):
            raise ValueError("ids must be a list of session ids")
        ids = [required(id, "session id", 200) for id in ids]
        if not 0 < len(ids) <= 500:
            raise ValueError("Archive between 1 and 500 agents at once")
        with self.db() as db:
            db.executemany("UPDATE sessions SET archived=? WHERE id=?", [(time.time() if archived else 0, id) for id in ids])
        return {"archived": bool(archived), "sessions": ids}

    def watch_codex(self, endpoint=None):
        with self.db() as db:
            db.execute("INSERT INTO codex_connections VALUES (?,?) ON CONFLICT(endpoint) DO UPDATE SET updated=excluded.updated",
                       (endpoint or "", time.time()))

    def codex_endpoints(self):
        with self.db() as db:
            return [r["endpoint"] or None for r in db.execute("SELECT endpoint FROM codex_connections")]

    def observe_activity(self, session_id, state, source, lease=6, priority=100, observed=None):
        if state not in {"active", "idle", "waiting", "offline", "unknown"}:
            raise ValueError("Unknown activity state")
        if not 0 < lease <= 3600:
            raise ValueError("Activity lease must be between 0 and 3600 seconds")
        source = required(source, "activity source", 1000)
        now = time.time()
        at = now if observed is None else observed
        with self.db() as db:
            db.execute("""INSERT INTO activity (session_id,source,state,observed,expires,priority,since,last_active)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id,source) DO UPDATE SET
                since=CASE WHEN activity.state=excluded.state AND activity.since IS NOT NULL THEN activity.since ELSE excluded.observed END,
                last_active=CASE WHEN excluded.state='active' THEN excluded.observed ELSE activity.last_active END,
                state=excluded.state,observed=excluded.observed,expires=excluded.expires,priority=excluded.priority
                WHERE excluded.observed>=activity.observed""",
                       (session_id, source, state, at, now + lease, priority, at, at if state == "active" else None))

    def expire_activity(self, source, missing=None, before=None):
        """Without an inventory, what a source said is unknown; with one, a session missing from it is
        offline. Observers call this every second, so a row already marked, with time left, is not
        written again: each write wakes the feed, which rebuilds the whole inbox."""
        now = time.time()
        with self.db() as db:
            if missing is None:
                db.execute("UPDATE activity SET since=CASE WHEN state='unknown' THEN since ELSE ? END,"
                           "state='unknown',observed=?,expires=? WHERE source=? AND NOT (state='unknown' AND expires>?)",
                           (now, now, now+6, source, now+2))
            else:
                for row in db.execute("SELECT session_id,observed,state,expires FROM activity WHERE source=?", (source,)).fetchall():
                    if row["session_id"] in missing or (before is not None and row["observed"] > before):
                        continue
                    if row["state"] == "offline" and row["expires"] > now + 60:
                        continue
                    # A successful live inventory confirms this runtime no longer owns it.
                    db.execute("UPDATE activity SET since=CASE WHEN state='offline' THEN since ELSE ? END,"
                               "state='offline',observed=?,expires=? WHERE source=? AND session_id=?",
                               (now, now, now+OFFLINE_FOR, source, row["session_id"]))

    def attach_activity(self, sessions):
        now, observations = time.time(), {}
        with self.db() as db:
            rows = db.execute("SELECT * FROM activity WHERE expires>? ORDER BY priority DESC,observed DESC", (now,))
            background = {}
            for row in rows:
                observations.setdefault(row["session_id"], dict(row))
                if row["source"] == "background" and row["state"] == "active":
                    background[row["session_id"]] = dict(row)
            # Expired observations still say when an agent last ran, from any source.
            last_active = {r["session_id"]: r["last"] for r in db.execute(
                "SELECT session_id,MAX(last_active) AS last FROM activity GROUP BY session_id")}
            # An agent whose last word was that it ended stays ended until it is seen again, rather
            # than turning into no signal once that word expires. Background work only adds to a state.
            ended = {r["session_id"]: dict(r) for r in db.execute("""SELECT a.* FROM activity a JOIN
                (SELECT session_id,MAX(observed) AS observed FROM activity WHERE source!='background' GROUP BY session_id) m
                ON m.session_id=a.session_id AND m.observed=a.observed WHERE a.state='offline' AND a.source!='background'""")}
        for session in sessions:
            observation = observations.get(session["id"]) or ended.get(session["id"])
            # A conversation whose subagents are still running is at work, even when its own
            # turn has ended. Offline or waiting on you still says more than that.
            if session["id"] in background and (not observation or observation["state"] in {"idle", "unknown"}):
                observation = background[session["id"]]
            session["activity"] = ({k: observation[k] for k in ("state", "source", "observed", "expires", "since")}
                                   if observation else {"state": "unknown", "source": None, "since": None})
            session["last_active"] = last_active.get(session["id"])

    def agent_activity(self, now=None):
        """For each kind of agent: whether one is running now, and when one last ran."""
        now = now or time.time()
        with self.db() as db:
            rows = db.execute("""SELECT s.provider,MAX(a.last_active) AS last,
                MAX(CASE WHEN a.state='active' AND a.expires>? THEN 1 ELSE 0 END) AS running
                FROM activity a JOIN sessions s ON s.id=a.session_id GROUP BY s.provider""", (now,))
            return {r["provider"]: {"last": r["last"], "running": bool(r["running"])} for r in rows}

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
            steps=None, link=None, paste=None, minutes=None, rank=9999, source="",
            matters=None, needs_you=None, project="", workstream="", due="", readable=False):
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
                                         link=link, paste=[] if paste is None else paste, minutes=minutes, rank=rank, source=source,
                                         matters=matters, needs_you=needs_you, project=project, workstream=workstream, due=due)
        owner, waiting_on, defer_until = self.attention(("agent" if kind == "todo" else "human") if owner is None else owner, waiting_on, defer_until)
        if readable:
            self.require_readable(owner, options, presentation)
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
        result = {"unlocks": "", "recommendation": "", "steps": [], "link": None, "paste": [], "minutes": None,
                  "rank": 9999, "source": "", "agent_note": "", "matters": None, "needs_you": None,
                  "project": "", "workstream": "", "due": "", "pinned": False, "skipped": False, **fields}
        for key in ("project", "workstream"):
            if not isinstance(result[key], str) or len(result[key]) > 60:
                raise ValueError(f"{key} must be text, at most 60 characters")
            result[key] = result[key].strip()
        if result["due"]:
            try:
                if not isinstance(result["due"], str) or date.fromisoformat(result["due"]).isoformat() != result["due"]:
                    raise ValueError()
            except ValueError:
                raise ValueError("due must be a date in YYYY-MM-DD format or empty") from None
        if not isinstance(result["pinned"], bool):
            raise ValueError("pinned must be true or false")
        for key in ("unlocks", "recommendation", "source", "agent_note"):
            if not isinstance(result[key], str) or len(result[key]) > 4000:
                raise ValueError(f"{key} must be text, at most 4000 characters")
        if not isinstance(result["steps"], list) or len(result["steps"]) > 20:
            raise ValueError("steps must be a list of at most 20 short instructions")
        steps = [Store.step(step) for step in result["steps"]]
        plan = result.pop("plan", None)
        # Rich steps are stored apart, with plain text in `steps`, because sessions still running older HQ
        # code read `steps` as text. A plan survives only while that text still matches it.
        if any(isinstance(step, dict) for step in steps):
            result["plan"], steps = steps, [Store.plain_step(step) for step in steps]
        elif plan:
            plan = [Store.step(step) for step in plan]
            if [Store.plain_step(step) for step in plan] == steps:
                result["plan"] = plan
        result["steps"] = steps
        if result["link"] is not None:
            result["link"] = Store.link(result["link"])
        result["matters"] = Store.reason(result["matters"], "matters")
        result["needs_you"] = Store.reason(result["needs_you"], "needs_you")
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
    def link(link):
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
        return {"label": required(link["label"], "link label", 200), "url": url}

    @staticmethod
    def step(value):
        # One action, carrying what it needs: a link to open, text to copy, or things to check.
        if isinstance(value, str):
            return required(value, "step", 2000)
        if not isinstance(value, dict) or set(value) - {"text", "link", "copy", "checks"}:
            raise ValueError("a step must be text or an object with text and optional link, copy, and checks")
        result = {"text": required(value.get("text"), "step text", 500)}
        if value.get("link") is not None:
            result["link"] = Store.link(value["link"])
        if value.get("copy") is not None:
            result["copy"] = required(value["copy"], "step copy text", 20000)
        if value.get("checks") is not None:
            checks = value["checks"]
            if not isinstance(checks, list) or not 0 < len(checks) <= 10:
                raise ValueError("step checks must be a list of 1 to 10 things to check")
            result["checks"] = [required(check, "check", 500) for check in checks]
        return result

    @staticmethod
    def plain_step(step):
        if isinstance(step, str):
            return step
        extras = [step["link"]["url"]] if "link" in step else []
        extras += [step["copy"]] if "copy" in step else []
        extras += ["; ".join(step.get("checks", []))] if "checks" in step else []
        text = step["text"] + (": " + " · ".join(extras) if extras else "")
        return text if len(text) <= 2000 else text[:1999] + "…"

    @staticmethod
    def reason(value, name):
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) - {"lead", "points"}:
            raise ValueError(f"{name} must be an object with a lead line and optional points")
        points = value.get("points", [])
        if not isinstance(points, list) or len(points) > 5:
            raise ValueError(f"{name} points must be a list of at most 5 short lines")
        return {"lead": required(value.get("lead"), f"{name} lead", 300), "points": [required(p, f"{name} point", 500) for p in points]}

    @staticmethod
    def require_readable(owner, options, presentation):
        # An agent's item that needs the human has to arrive readable: the dashboard has nothing to show without these.
        if owner != "human":
            return
        missing = []
        if presentation["minutes"] is None:
            missing.append("minutes (the human's own time)")
        if not presentation["steps"] and not options:
            missing.append("steps (one action each, with its link, copy text or checks) or options (for a decision)")
        if not presentation["matters"]:
            missing.append("matters {lead, points}: what this unblocks and why it matters")
        if not presentation["needs_you"]:
            missing.append("needs_you {lead, points}: why the agent can't do it")
        if missing:
            raise ValueError("An item that needs the human must include " + "; ".join(missing)
                             + ". Read hq_reporting_guide (or `hq instructions`) for the worked example.")

    @staticmethod
    def readability_notes(item):
        """Wording checks returned to the reporting agent. They never block a report."""
        notes = []
        if len(item["title"]) > 70:
            notes.append(f"Title is {len(item['title'])} characters; aim for under 70 and start with the action.")
        steps = item["steps"]
        if len(steps) > 5:
            notes.append(f"{len(steps)} steps; use at most 5 and fold trivial ones into the step before.")
        for n, step in enumerate(steps, 1):
            text = step if isinstance(step, str) else step["text"]
            if len(text) > 120:
                notes.append(f"Step {n} is {len(text)} characters; make it one action and move detail into copy or checks.")
        for key, label in (("matters", "matters"), ("needs_you", "needs_you")):
            reason = item[key]
            if reason and len(reason["points"]) > 3:
                notes.append(f"{label} has {len(reason['points'])} points; keep the 3 that matter most.")
            if reason and any(len(p) > 160 for p in reason["points"]):
                notes.append(f"{label} has a point over 160 characters; one fact per line.")
        if len(item["detail"]) > 600:
            notes.append(f"Context is {len(item['detail'])} characters; keep it under 600 and put actions in steps, the why in matters and needs_you.")
        if item["owner"] == "human" and item["status"] == "open":
            try:
                Store.require_readable("human", item["options"], item)
            except ValueError as error:
                notes.append(str(error))
        return notes

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
        allowed = {"owner", "waiting_on", "defer_until", "title", "detail", "unlocks", "recommendation", "steps", "link", "paste",
                   "minutes", "rank", "source", "matters", "needs_you", "project", "workstream", "due", "pinned"}
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
            return "pending"
        if item["defer_until"] and item["defer_until"] > (today or date.today().isoformat()):
            return "later"
        return {"human": "needs", "agent": "pending", "external": "waiting"}[item["owner"]]

    @staticmethod
    def decode(row):
        data = dict(row)
        for key in ("tags", "options"):
            data[key] = json.loads(data[key])
        presentation = Store.presentation(**json.loads(data.pop("presentation")))
        plan = presentation.pop("plan", None)
        data.update(presentation, steps=plan or presentation["steps"])
        data.update(place(data, data.pop("session_cwd", None)))
        data["bucket"] = Store.bucket(data)
        data["answer_files"] = json.loads(data.get("answer_files") or "[]")
        return data

    def item(self, id):
        with self.db() as db:
            row = db.execute("""SELECT i.*,s.cwd AS session_cwd,r.text AS answer,r.state AS delivery,r.error AS delivery_error,r.attachments AS answer_files
                FROM items i JOIN sessions s ON s.id=i.session_id LEFT JOIN replies r ON i.id=r.item_id WHERE i.id=?""", (id,)).fetchone()
        if not row:
            raise ValueError("Item not found")
        result = self.decode(row)
        self.attach_comments([result])
        return result

    def snapshot(self, session_id=None, tag=None, status=None, bucket=None):
        with self.db() as db:
            sessions = [dict(r) for r in db.execute("SELECT * FROM sessions ORDER BY seen DESC")]
            items = [self.decode(r) for r in db.execute("""SELECT i.*,s.name AS session_name,s.provider,s.cwd AS session_cwd,
                r.text AS answer,r.state AS delivery,r.error AS delivery_error,r.created AS replied,r.delivered AS heard,r.attachments AS answer_files
                FROM items i JOIN sessions s ON s.id=i.session_id
                LEFT JOIN replies r ON i.id=r.item_id ORDER BY i.created DESC""")]
        self.attach_comments(items)
        for s in sessions:
            if s["lease_until"] < time.time():
                s["connection"] = "offline"
        self.attach_activity(sessions)
        by_id, now = {s["id"]: s for s in sessions}, time.time()
        for item in items:
            item["followup"] = followup(item, by_id.get(item["session_id"]), now)
        items = [i for i in items if (not session_id or i["session_id"] == session_id)
                 and (not tag or tag in i["tags"]) and (not status or i["status"] == status)
                 and (not bucket or i["bucket"] == bucket)]
        return {"sessions": sessions, "items": items}

    def compose(self, text, attachments, name):
        """Your words plus where each attached file is, which is what the agent reads."""
        found = files.resolve(self.path.parent, attachments)
        if not found:
            return required(text, name, 20000), []
        if text is not None and (not isinstance(text, str) or len(text) > 20000):
            raise ValueError(f"{name} must be text, at most 20000 characters")
        return "\n\n".join(part for part in ((text or "").strip(), files.describe(found)) if part), found

    def reply(self, id, text, attachments=None):
        (text, found), now = self.compose(text, attachments, "answer"), time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,kind FROM items WHERE id=?", (id,)).fetchone()
            if not row or row["status"] != "open":
                raise ValueError("Only open items can receive a response")
            db.execute("INSERT INTO replies (id,item_id,text,created,attachments) VALUES (?,?,?,?,?)",
                       (str(uuid.uuid4()), id, text, now, json.dumps(found)))
            db.execute("UPDATE items SET status='answered',updated=? WHERE id=?", (now, id))
        return self.item(id)

    def skip(self, id, reason=""):
        """The human decides not to do this: close it now and tell the agent to drop it."""
        if not isinstance(reason, str) or len(reason) > 2000:
            raise ValueError("reason must be text, at most 2000 characters")
        text = "Skipped: The user decided not to do this. Stop any work that depends on it and don't report it again."
        if reason.strip():
            text += f" Reason: {reason.strip()}"
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,presentation FROM items WHERE id=?", (id,)).fetchone()
            if not row or row["status"] != "open":
                raise ValueError("Only open items can be skipped")
            presentation = {**json.loads(row["presentation"]), "skipped": True, "settle": "skip", "pinned": False}
            # The reply reaches the agent through its usual inbox, hook or live route.
            db.execute("INSERT INTO replies (id,item_id,text,created) VALUES (?,?,?,?)", (str(uuid.uuid4()), id, text, now))
            db.execute("""UPDATE comments SET state='cancelled' WHERE item_id=? AND author='human' AND state!='cancelled'
                AND NOT EXISTS (SELECT 1 FROM comments a WHERE a.parent_id=comments.id)""", (id,))
            db.execute("UPDATE items SET status='answered',presentation=?,updated=? WHERE id=?", (json.dumps(presentation), now, id))
        return self.item(id)

    def mark_done(self, id, note=""):
        """You finished it yourself: tell the agent, and close once it has heard."""
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError("note must be text, at most 2000 characters")
        text = "Done: The user marked this done. Don't do it again, and carry on with anything that was waiting on it."
        if note.strip():
            text += f" Note: {note.strip()}"
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,presentation FROM items WHERE id=?", (id,)).fetchone()
            if not row or row["status"] != "open":
                raise ValueError("Only open items can be marked done")
            presentation = {**json.loads(row["presentation"]), "settle": "done", "pinned": False}
            db.execute("INSERT INTO replies (id,item_id,text,created) VALUES (?,?,?,?)", (str(uuid.uuid4()), id, text, now))
            db.execute("UPDATE items SET status='answered',presentation=?,updated=? WHERE id=?", (json.dumps(presentation), now, id))
        return self.item(id)

    def close(self, id, note=""):
        """You take a decided item back and close it yourself. A reply the agent has not
        heard is withdrawn, so it never acts on a decision you have already handled."""
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError("note must be text, at most 2000 characters")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,presentation FROM items WHERE id=?", (id,)).fetchone()
            if not row or row["status"] != "answered":
                raise ValueError("Only decided items can be closed this way")
            db.execute("UPDATE replies SET state='withdrawn',error=NULL,claim_until=0 WHERE item_id=? AND state!='received'", (id,))
            presentation = {**json.loads(row["presentation"]), "closed_by": "you", "handoff": False}
            if note.strip():
                presentation["agent_note"] = note.strip()
            db.execute("UPDATE items SET status='done',presentation=?,updated=? WHERE id=?", (json.dumps(presentation), time.time(), id))
        return self.item(id)

    def hand_off(self, id):
        """Offer a decided item to the next agent in its project, even while its own agent lives."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,presentation FROM items WHERE id=?", (id,)).fetchone()
            if not row or row["status"] != "answered":
                raise ValueError("Only decided items can be handed to another agent")
            presentation = {**json.loads(row["presentation"]), "handoff": True}
            db.execute("UPDATE items SET presentation=?,updated=? WHERE id=?", (json.dumps(presentation), time.time(), id))
        return self.item(id)

    @staticmethod
    def settle(db, item_id):
        """A skip or a done of yours closes once its agent has heard it."""
        row = db.execute("SELECT status,presentation FROM items WHERE id=?", (item_id,)).fetchone()
        if row and row["status"] == "answered" and json.loads(row["presentation"]).get("settle") in SETTLES:
            db.execute("UPDATE items SET status='done',updated=? WHERE id=?", (time.time(), item_id))

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
                comment["attachments"] = json.loads(comment.get("attachments") or "[]")
                threads.setdefault(comment["item_id"], []).append(comment)
        for item in items:
            item["comments"] = threads.get(item["id"], [])
            item["asking"] = any(c["author"] == "human" and not c["answered"] and c["state"] != "cancelled" for c in item["comments"])
            item["bucket"] = self.bucket(item)

    def ask_agent(self, item_id, text, attachments=None):
        (text, found), now, comment_id = self.compose(text, attachments, "question"), time.time(), str(uuid.uuid4())
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
            if not item or item["status"] == "done":
                raise ValueError("Ask for clarification on an unfinished item")
            db.execute("INSERT INTO comments (id,item_id,author,text,created,attachments) VALUES (?,?,'human',?,?,?)",
                       (comment_id, item_id, text, now, json.dumps(found)))
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
        result["attachments"] = json.loads(result.get("attachments") or "[]")
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
            result = [{**dict(row), "answered": bool(row["answered"]), "attachments": json.loads(row["attachments"] or "[]")} for row in rows]
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

    def adoptable(self, session_id):
        """Decisions in this conversation's project whose own agent is gone, or that you handed
        off, oldest first. Only another conversation in the same project may take them over."""
        session = self.session(session_id)
        project = project_of({}, session.get("cwd"))
        if project == OTHER:
            return []
        items = self.snapshot(status="answered")["items"]
        found = [i for i in items if i["session_id"] != session_id and i["project"] == project
                 and i.get("followup") and i["followup"]["adoptable"]]
        return sorted(found, key=lambda i: i["followup"]["replied"] or 0)

    def adopt(self, item_id, session_id):
        """Take over a stuck decision. Exactly one conversation wins; it now has the answer,
        so it must act on it and close it with hq_resolve."""
        offered = {i["id"]: i for i in self.adoptable(session_id)}
        if item_id not in offered:
            raise ValueError("That item is not up for adoption in this conversation's project")
        before = offered[item_id]
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT session_id,status,presentation FROM items WHERE id=?", (item_id,)).fetchone()
            # Compare and swap: another conversation may have adopted it a moment ago.
            if not row or row["status"] != "answered" or row["session_id"] != before["session_id"]:
                raise ValueError("Another conversation has already taken this item")
            presentation = {**json.loads(row["presentation"]), "handoff": False,
                            "adopted_from": {"session_id": before["session_id"], "name": before.get("session_name", ""), "at": time.time()}}
            db.execute("UPDATE items SET session_id=?,presentation=?,updated=? WHERE id=?",
                       (session_id, json.dumps(presentation), time.time(), item_id))
            # The adopter reads the answer right here, so this is its acknowledgement.
            db.execute("UPDATE replies SET state='received',delivered=?,error=NULL,claim_until=0 WHERE item_id=? AND state!='withdrawn'",
                       (time.time(), item_id))
            self.settle(db, item_id)
        item = self.item(item_id)
        return {"item": item, "answer": item["answer"],
                "next_step": "Act on this answer now, then close the item with hq_resolve and a one-line note of what you did."
                if item["status"] == "answered" else "This was a skip or a done; nothing more to do."}

    def unclosed(self, session_id, now=None):
        """This conversation's decisions it heard but never closed, due a reminder; marks them reminded."""
        now = now or time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT i.id,i.title,i.presentation,r.text,r.delivered FROM items i JOIN replies r ON r.item_id=i.id
                WHERE i.session_id=? AND i.status='answered' AND r.state='received' AND r.delivered<?
                ORDER BY r.delivered""", (session_id, now - 3600)).fetchall()
            due = []
            for row in rows:
                presentation = json.loads(row["presentation"])
                if now - presentation.get("reminded", 0) < REMIND_EVERY:
                    continue
                presentation["reminded"] = now
                db.execute("UPDATE items SET presentation=? WHERE id=?", (json.dumps(presentation), row["id"]))
                due.append({"item_id": row["id"], "title": row["title"], "answer": row["text"], "heard": row["delivered"]})
        return due

    def sweep_files(self, now=None):
        """Delete each file once nothing needs it: a day after upload when no reply or question used
        it, and KEEP_FILES after its item was done, long after the agent read it."""
        now = now or time.time()
        with self.db() as db:
            used = {a["id"] for table in ("replies", "comments")
                    for row in db.execute(f"""SELECT t.attachments FROM {table} t JOIN items i ON i.id=t.item_id
                        WHERE t.attachments!='[]' AND (i.status!='done' OR i.updated>?)""", (now - KEEP_FILES,))
                    for a in json.loads(row[0])}
        return files.sweep(self.path.parent, used, now=now)

    def has_waiter(self, item_id):
        with self.db() as db:
            return db.execute("SELECT 1 FROM waiters WHERE item_id=? AND lease_until>?", (item_id, time.time())).fetchone() is not None

    def acknowledge(self, item_id, session_id):
        with self.db() as db:
            cursor = db.execute("""UPDATE replies SET state='received',delivered=COALESCE(delivered,?),error=NULL
                WHERE item_id=? AND state!='withdrawn' AND item_id IN (SELECT id FROM items WHERE session_id=?)""",
                                (time.time(), item_id, session_id))
            if not cursor.rowcount:
                raise ValueError("No response for this item in the specified session")
            self.settle(db, item_id)
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
            db.execute("UPDATE replies SET state=?,error=?,delivered=?,claim_until=0 WHERE id=? AND state!='withdrawn' AND (state!='received' OR ?='received')",
                       (state, error, time.time() if state == "received" else None, reply_id, state))
            if state == "received":
                row = db.execute("SELECT item_id FROM replies WHERE id=?", (reply_id,)).fetchone()
                if row:
                    self.settle(db, row["item_id"])

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

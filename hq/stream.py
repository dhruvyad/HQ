"""One database observer broadcasts complete snapshots to all local clients."""
import json
import math
import sqlite3
import threading
import time
import uuid


def signature(snapshot):
    sessions = [{k: s.get(k) for k in ("id", "name", "provider", "route", "connection", "endpoint")}
                | {"activity": {k: s["activity"].get(k) for k in ("state", "source")}}
                for s in snapshot["sessions"]]
    # The numbers, not when they were read: a newer as_of alone is not news.
    usage = {name: reading and reading["windows"] for name, reading in (snapshot.get("usage") or {}).items()}
    return json.dumps({"items": snapshot["items"], "sessions": sorted(sessions, key=lambda s: s["id"]), "usage": usage,
                       "accounts": snapshot.get("accounts")}, sort_keys=True)


# The whole inbox is rebuilt when the database changes, at most every SPACING seconds (an agent's
# every tool call is a write, and one rebuild covers a burst), and otherwise only when something
# changes with time alone. Each rebuild is tens of milliseconds of CPU, so it is never on a timer.
SPACING = 0.5
REFRESH = 10


class StateFeed:
    def __init__(self, store, usage=None, accounts=None):
        self.store, self.stop = store, threading.Event()
        self.usage, self.accounts = usage, accounts
        self.condition = threading.Condition()
        self.publish_lock = threading.Lock()
        self.instance = str(uuid.uuid4())
        self.last_success = 0
        self.version, self.payload, self.last_signature = 0, "", None
        # Item links clicked in an agent's chat, for the pill to open: (number, event), newest last.
        self.opened, self.opens = 0, []
        self.publish()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def publish(self):
        with self.publish_lock:
            snapshot = self.store.snapshot()
            # Memory only: the worker does the reading, because this runs constantly.
            snapshot["usage"] = self.usage.current() if self.usage else {"claude": None, "codex": None}
            # Which account each tool is signed in as, from memory: never a login read here.
            snapshot["accounts"] = self.accounts.summary() if self.accounts else None
            self.last_success = time.monotonic()
            current = signature(snapshot)
            with self.condition:
                changed = current != self.last_signature
                if changed:
                    self.last_signature = current
                    self.version += 1
                snapshot.update(instance=self.instance, revision=self.version)
                self.payload = json.dumps(snapshot, separators=(",", ":"))
                if changed:
                    self.condition.notify_all()

    def current(self):
        if self.usage:
            self.usage.watched()
        self.publish()
        with self.condition:
            return self.payload

    def run(self):
        db = sqlite3.connect(self.store.path.resolve().as_uri()+"?mode=ro", uri=True)
        version, due, last = None, 0, -math.inf
        try:
            while not self.stop.is_set():
                try:
                    current = db.execute("PRAGMA data_version").fetchone()[0]
                    now = time.monotonic()
                    if (current != version and now - last >= SPACING) or now >= due:
                        self.publish()
                        version, last = current, now
                        due = now + self.quiet_for(db)
                    # The database answers: clients stay live without a rebuild every few seconds.
                    self.last_success = now
                except (sqlite3.Error, OSError):
                    # Clients notice missing keepalives instead of retaining a false live state.
                    pass
                self.stop.wait(0.25)
        finally:
            db.close()

    @staticmethod
    def quiet_for(db):
        """How long the snapshot can stand with no write: until the next activity lease runs out,
        and at most REFRESH, for what changes with time alone (a decision going quiet)."""
        wall = time.time()
        soonest = db.execute("SELECT MIN(expires) FROM activity WHERE expires>?", (wall,)).fetchone()[0]
        return REFRESH if soonest is None else max(0.05, min(REFRESH, soonest - wall + 0.05))

    def wait(self, previous, opened=None, timeout=2):
        """The next snapshot, or sooner, with opened, the next item link to open."""
        if self.usage:
            self.usage.watched()
        with self.condition:
            self.condition.wait_for(lambda: self.version != previous or self.stop.is_set()
                                    or (opened is not None and self.opened != opened), timeout)
            return self.version, self.payload

    def open_item(self, item_id):
        """Ask every open pill to show this item."""
        with self.condition:
            self.opened += 1
            self.opens = [*self.opens[-7:], (self.opened, {"id": item_id})]
            self.condition.notify_all()

    def opens_after(self, seen):
        """The item links asked for since seen, and the number to wait from next."""
        with self.condition:
            return [item for number, item in self.opens if number > seen], self.opened

    def close(self):
        self.stop.set()
        with self.condition:
            self.condition.notify_all()
        self.thread.join(timeout=3)

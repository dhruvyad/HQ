"""Live metadata only; a missing or broken observer must never imply idle."""
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time


def codex_source(endpoint=None):
    return "codex-runtime:" + str(endpoint or "default")


def codex_activity(status):
    if not isinstance(status, dict):
        return "unknown"
    if status.get("type") == "active":
        flags = status.get("activeFlags", [])
        if not isinstance(flags, list):
            return "unknown"
        return "waiting" if any(flag in {"waitingOnApproval", "waitingOnUserInput"}
                                for flag in flags if isinstance(flag, str)) else "active"
    return {"idle": "idle", "notLoaded": "offline"}.get(status.get("type"), "unknown")


# Each open Claude Code conversation keeps a small status file, <config>/sessions/<pid>.json,
# saying whether it is busy or idle. Reading those costs next to nothing, so they are looked at
# every second. `claude agents` starts all of Claude Code (about 0.15 s of CPU) and is only needed
# for dispatched background agents, which have no such file, so it runs every BACKGROUND seconds.
BACKGROUND = 20
LEASE = 60      # how long a reading stands unrenewed; the server going away lets it lapse
RENEW = 25      # an unchanged reading is written again this often, well inside its lease


def valid(row):
    return (isinstance(row, dict) and isinstance(row.get("sessionId"), str) and 0 < len(row["sessionId"]) <= 150
            and not any(row.get(key) is not None and not isinstance(row[key], str) for key in ("name", "status", "state", "cwd")))


class ClaudePresence:
    def __init__(self, store, folder=None, background=BACKGROUND, clock=time.monotonic):
        self.store = store
        self.stop = threading.Event()
        self.command = shutil.which("claude") or str(os.path.expanduser("~/.local/bin/claude"))
        config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
        self.folder = Path(folder) if folder else Path(config) / "sessions"
        self.background, self.clock = background, clock
        self.asked, self.agents = -math.inf, []
        self.files = {}    # path -> ((mtime, size), record), so an unchanged file is not read again
        self.written = {}  # session -> (state, when written), so an unchanged state is not written again

    def session_files(self):
        """The status files of open conversations; None when this Claude Code keeps none."""
        if not self.folder.is_dir():
            return None
        records, seen = [], set()
        for path in self.folder.glob("*.json"):
            seen.add(path)
            try:
                info = path.stat()
                stamp = (info.st_mtime_ns, info.st_size)
                cached = self.files.get(path)
                if cached and cached[0] == stamp:
                    record = cached[1]
                else:
                    record = json.loads(path.read_text())
                    self.files[path] = (stamp, record)
            except (OSError, ValueError):
                continue  # mid-write or just removed: next second
            if valid(record) and isinstance(record.get("pid"), int):
                records.append(record)
        for path in set(self.files) - seen:
            del self.files[path]
        return records

    def background_agents(self):
        result = subprocess.run([self.command, "agents", "--json"], capture_output=True,
                                text=True, timeout=2, check=True)
        records = json.loads(result.stdout)
        if not isinstance(records, list):
            raise ValueError("Claude did not provide a live session inventory")
        # Validate the entire inventory before expiring absent sessions.
        if not all(valid(row) for row in records):
            raise ValueError("Invalid Claude session metadata")
        return records

    def observe(self, sid, state):
        now = self.clock()
        last = self.written.get(sid)
        if last and last[0] == state and now - last[1] < RENEW:
            return
        self.store.observe_activity(sid, state, "claude-live", lease=LEASE)
        self.written[sid] = (state, now)

    def sample(self):
        files = self.session_files()
        if files is None or self.clock() - self.asked >= self.background:
            try:
                self.agents = self.background_agents()
            except (OSError, ValueError, subprocess.SubprocessError):
                if files is None:
                    raise
                self.agents = []  # background agents unknown until the next ask; conversations still read
            self.asked = self.clock()
        # A conversation's own file says more than the inventory, so it wins.
        records = list(files or [])
        mine = {row["sessionId"] for row in records}
        records += [row for row in self.agents if row["sessionId"] not in mine]
        present = set()
        for row in records:
            sid = "claude-code:" + row["sessionId"]
            pid = row.get("pid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                # Background entries can persist after their worker exits.
                self.unverified(sid, present)
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                self.unverified(sid, present)
                continue
            # A name you gave it (/rename, --name; Claude Code marks it as yours) replaces HQ's at once,
            # without waiting for your next message. One it made up from the folder never does.
            yours = row.get("name") if row.get("nameSource") == "user" and isinstance(row.get("name"), str) else ""
            yours = " ".join(yours.split())[:80]
            try:
                known = self.store.session(sid)
            except ValueError:
                known = self.store.register(sid, yours or (row.get("name") or "Claude Code conversation")[:500],
                                            "claude-code", row["sessionId"], row.get("cwd"))
            if yours and known["name"] != yours:
                self.store.register(sid, yours, "claude-code", row["sessionId"], row.get("cwd"))
            # Dispatched background sessions report "state" where interactive ones say "status".
            status = row.get("status") or row.get("state")
            state = {"busy": "active", "running": "active", "idle": "idle", "blocked": "waiting"}.get(status, "unknown")
            self.observe(sid, state)
            present.add(sid)
        for sid in set(self.written) - present:
            del self.written[sid]
        self.store.expire_activity("claude-live", missing=present)

    def unverified(self, sid, present):
        try:
            self.store.session(sid)
        except ValueError:
            return
        self.observe(sid, "unknown")
        present.add(sid)

    def run(self):
        while not self.stop.is_set():
            try:
                self.sample()
            except (OSError, ValueError, subprocess.SubprocessError, sqlite3.Error):
                self.written.clear()
                self.asked = -math.inf  # ask again next time rather than trust an old inventory
                try:
                    self.store.expire_activity("claude-live")
                except sqlite3.Error:
                    # Existing observations still expire; retry after the database recovers.
                    pass
            self.stop.wait(1)

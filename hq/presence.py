"""Live metadata only; a missing or broken observer must never imply idle."""
import json
import os
import shutil
import sqlite3
import subprocess
import threading


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


class ClaudePresence:
    def __init__(self, store):
        self.store = store
        self.stop = threading.Event()
        self.command = shutil.which("claude") or str(os.path.expanduser("~/.local/bin/claude"))

    def sample(self):
        result = subprocess.run([self.command, "agents", "--json"], capture_output=True,
                                text=True, timeout=2, check=True)
        records = json.loads(result.stdout)
        if not isinstance(records, list):
            raise ValueError("Claude did not provide a live session inventory")
        present = set()
        # Validate the entire inventory before expiring absent sessions.
        for row in records:
            if (not isinstance(row, dict) or not isinstance(row.get("sessionId"), str)
                    or not 0 < len(row["sessionId"]) <= 150
                    or any(row.get(key) is not None and not isinstance(row[key], str) for key in ("name", "status", "state", "cwd"))):
                raise ValueError("Invalid Claude session metadata")
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
            try:
                self.store.session(sid)
            except ValueError:
                name = (row.get("name") or "Claude Code conversation")[:500]
                self.store.register(sid, name, "claude-code", row["sessionId"], row.get("cwd"))
            # Dispatched background sessions report "state" where interactive ones say "status".
            status = row.get("status") or row.get("state")
            state = {"busy": "active", "running": "active", "idle": "idle", "blocked": "waiting"}.get(status, "unknown")
            self.store.observe_activity(sid, state, "claude-live", lease=4)
            present.add(sid)
        self.store.expire_activity("claude-live", missing=present)

    def unverified(self, sid, present):
        try:
            self.store.session(sid)
        except ValueError:
            return
        self.store.observe_activity(sid, "unknown", "claude-live", lease=4)
        present.add(sid)

    def run(self):
        while not self.stop.is_set():
            try:
                self.sample()
            except (OSError, ValueError, subprocess.SubprocessError, sqlite3.Error):
                try:
                    self.store.expire_activity("claude-live")
                except sqlite3.Error:
                    # Existing observations still expire; retry after the database recovers.
                    pass
            self.stop.wait(1)

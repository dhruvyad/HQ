"""Subscription usage left, from the only sources the tools themselves support.

Codex answers `account/rateLimits/read` over its app-server protocol. Claude Code
hands its rate limits to a status line command and nowhere else, so `hq statusline`
records them. HQ never reads a login, a transcript or an account API. When a tool
has not said, the answer is None, never an estimate.
"""
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .adapters import CodexClient
from .store import data_dir

CODEX_POLL = 60
CODEX_RETRY = 600  # A failed read is usually a missing login, which a minute will not fix.
CODEX_TIMEOUT = 20
TICK = 5
# Past this, nobody is using the numbers, so nothing starts a Codex server for them.
WATCHED_FOR = 120
# A reading this old no longer says much: other devices can spend the same limits.
MAX_AGE = 24 * 3600
READ_ERRORS = (OSError, RuntimeError, ValueError, EOFError, subprocess.SubprocessError)


def left_from_used(used):
    """Whole percent left, rounded down so it never overstates what remains."""
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or used < 0:
        return None
    return max(0, math.floor(100 - used))


def epoch(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return int(value)


def window(name, used, resets_at):
    left = left_from_used(used)
    return None if left is None else {"name": name, "left": left, "resets_at": epoch(resets_at)}


def reading(windows, as_of=None):
    windows = [item for item in windows if item]
    return {"windows": windows, "as_of": int(time.time() if as_of is None else as_of)} if windows else None


def codex_window_name(minutes):
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
        return "current"
    if minutes == 10080:
        return "weekly"
    if minutes == 1440:
        return "daily"
    return f"{minutes // 60}-hour" if minutes % 60 == 0 else f"{minutes}-minute"


def parse_codex(result):
    """Only the windows leave here: not the plan, the credit balance or the reset credits."""
    if not isinstance(result, dict):
        return None
    buckets = result.get("rateLimitsByLimitId")
    snapshot = buckets.get("codex") if isinstance(buckets, dict) else None
    if not isinstance(snapshot, dict):
        snapshot = result.get("rateLimits")
    if not isinstance(snapshot, dict):
        return None
    slots = [snapshot.get(slot) for slot in ("primary", "secondary")]
    return reading([window(codex_window_name(slot.get("windowDurationMins")), slot.get("usedPercent"), slot.get("resetsAt"))
                    for slot in slots if isinstance(slot, dict)])


def parse_claude(payload):
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return None
    slots = [("5-hour", limits.get("five_hour")), ("weekly", limits.get("seven_day"))]
    return reading([window(name, slot.get("used_percentage"), slot.get("resets_at"))
                    for name, slot in slots if isinstance(slot, dict)])


def valid_reading(value):
    """A reading file is ours, but a damaged one must show as unavailable, not as a number."""
    if not isinstance(value, dict) or not isinstance(value.get("windows"), list) or not value["windows"]:
        return None
    if epoch(value.get("as_of")) is None:
        return None
    for item in value["windows"]:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"] or len(item["name"]) > 40
                or isinstance(item.get("left"), bool) or not isinstance(item.get("left"), int) or not 0 <= item["left"] <= 100
                or (item.get("resets_at") is not None and epoch(item["resets_at"]) is None)):
            return None
    return {"windows": [{"name": i["name"], "left": i["left"], "resets_at": i.get("resets_at")} for i in value["windows"]],
            "as_of": epoch(value["as_of"])}


def fresh(value, now=None):
    """Drop what is no longer true. Claude Code drops a window at its reset too, and a
    window that has reset says nothing about what is left in the new one."""
    if not value:
        return None
    now = time.time() if now is None else now
    if not 0 <= now - value["as_of"] + 60 <= MAX_AGE + 60:
        return None
    windows = [item for item in value["windows"] if item["resets_at"] is None or item["resets_at"] > now]
    return {"windows": windows, "as_of": value["as_of"]} if windows else None


def claude_path():
    return data_dir() / "claude-usage.json"


def read_claude():
    try:
        return valid_reading(json.loads(claude_path().read_text()))
    except (OSError, ValueError):
        return None


def capture_statusline(text):
    """Keep Claude Code's rate limits and nothing else from its status line payload."""
    try:
        value = parse_claude(json.loads(text))
    except ValueError:
        return False
    # Absent says nothing about earlier numbers: the field arrives only after a
    # session's first response, so it must not erase what another session reported.
    if not value:
        return False
    path = claude_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        try:
            os.chmod(file.name, 0o600)
            json.dump(value, file)
            file.close()
            os.replace(file.name, path)
        finally:
            Path(file.name).unlink(missing_ok=True)
    return True


def statusline(stdin=None):
    """What Claude Code runs. It prints nothing, and a failure here must never reach the terminal."""
    try:
        capture_statusline((stdin or sys.stdin).read(1_000_000))
    except (OSError, ValueError):
        pass


def codex_binary():
    found = shutil.which("codex") or str(Path.home() / ".local/bin/codex")
    return found if os.access(found, os.X_OK) else None


def read_over_socket():
    """A running app-server answers without starting anything."""
    client = CodexClient(None)
    try:
        return client.call("account/rateLimits/read")
    finally:
        client.close()


def read_over_stdio(binary, timeout=CODEX_TIMEOUT):
    """Start a server just for this one read. Codex reads its own login; HQ never sees it."""
    process = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    # A hung server must not hang the worker: killing it ends the reads below.
    timer = threading.Timer(timeout, process.kill)
    timer.daemon = True
    timer.start()

    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def answer(request):
        for line in process.stdout:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("id") == request:
                if "error" in value or "result" not in value:
                    raise RuntimeError("Codex refused the request")
                return value["result"]
        raise EOFError("Codex closed before answering")

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "hq", "title": "HQ", "version": "0.1.0"}}})
        answer(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        return answer(2)
    finally:
        timer.cancel()
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except OSError:
                pass
        process.terminate()
        try:
            process.wait(3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def read_codex():
    try:
        return parse_codex(read_over_socket())
    except READ_ERRORS:
        pass
    binary = codex_binary()
    if not binary:
        return None
    try:
        return parse_codex(read_over_stdio(binary))
    except READ_ERRORS:
        return None


class Usage:
    """The latest readings. current() only copies memory, so publish() can call it constantly."""

    def __init__(self):
        self.lock = threading.Lock()
        self.readings = {"claude": None, "codex": None}
        self.last_watched = -math.inf

    def record(self, name, value):
        with self.lock:
            self.readings[name] = value

    def current(self):
        with self.lock:
            return {name: fresh(value) for name, value in self.readings.items()}

    def watched(self):
        self.last_watched = time.monotonic()

    def is_watched(self):
        return time.monotonic() - self.last_watched < WATCHED_FOR


class SampleUsage:
    """The lab's fixed numbers, so its circles render and nothing real is ever read."""

    def __init__(self):
        as_of = int(time.time())
        self.value = {"claude": {"windows": [{"name": "5-hour", "left": 62, "resets_at": None},
                                             {"name": "weekly", "left": 90, "resets_at": None}], "as_of": as_of},
                      "codex": {"windows": [{"name": "5-hour", "left": 18, "resets_at": None},
                                            {"name": "weekly", "left": 70, "resets_at": None}], "as_of": as_of}}

    def current(self):
        return self.value

    def watched(self):
        pass


class UsageWorker:
    def __init__(self, usage, claude=read_claude, codex=read_codex, clock=time.monotonic):
        self.usage, self.claude, self.codex, self.clock = usage, claude, codex, clock
        self.stop = threading.Event()
        self.next_codex = -math.inf

    def step(self):
        self.usage.record("claude", self.claude())
        if not self.usage.is_watched() or self.clock() < self.next_codex:
            return
        value = self.codex()
        # A failed read keeps the last number: its as_of says how old it is, and fresh()
        # retires it. Offline for a minute should not blank the circle.
        if value:
            self.usage.record("codex", value)
        self.next_codex = self.clock() + (CODEX_POLL if value else CODEX_RETRY)

    def run(self):
        delay = 1  # Not during startup, and never for a server already on its way out.
        while not self.stop.wait(delay):
            try:
                self.step()
            except READ_ERRORS:
                pass
            delay = TICK

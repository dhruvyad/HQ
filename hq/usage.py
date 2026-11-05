"""Subscription usage left, from what the tools themselves report.

Codex answers `account/rateLimits/read` over its app-server protocol. Claude Code
documents its rate limits only for a status line command, which `hq statusline`
records. Because a status line hides most of Claude Code's footer hints, the user
can instead opt in to `hq usage login`: HQ then reads Claude Code's saved login
from the Keychain and asks the same unofficial usage endpoint that community tools
use. That token stays in memory, goes only to Anthropic, and is never refreshed
here, since rotating it behind Claude Code's back could sign Claude Code out.
HQ never reads a transcript. When a tool has not said, the answer is None, never
an estimate.
"""
import json
import math
import random
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from datetime import datetime
import urllib.error
import urllib.request

from .adapters import CodexClient
from .store import data_dir

# Usage moves only while you use an agent, so ask often while one is at work and rarely
# otherwise, at random within each range so nothing ever asks in lockstep. Claude's usage
# endpoint rate-limits hard: after a refusal, wait as long as it says, or back off from
# 10 minutes up to an hour.
CLAUDE_BUSY = (240, 480)
CODEX_BUSY = (60, 120)
IDLE = (1800, 3600)
IDLE_AFTER = 600       # an agent that ran in the last 10 minutes still counts as busy
SOON = (20, 60)        # when an agent starts again, check this soon
SPACING = 120          # but never two requests closer together than this
CLAUDE_POLL, CODEX_POLL = CLAUDE_BUSY[1], CODEX_BUSY[1]
CLAUDE_RETRY = 600
CLAUDE_BACKOFF_MOST = 3600
CLAUDE_AGENTS, CODEX_AGENTS = {"claude-code", "claude-desktop"}, {"codex"}
CLAUDE_LOGIN_ITEM = "Claude Code-credentials"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
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


def parse_codex_usage(payload):
    """ChatGPT's usage endpoint, which Codex itself reads: `rate_limit` with a primary and a
    secondary window, each `used_percent`, `limit_window_seconds` and `reset_at`."""
    limit = payload.get("rate_limit") if isinstance(payload, dict) else None
    if not isinstance(limit, dict):
        return None
    slots = [limit.get(slot) for slot in ("primary_window", "secondary_window")]
    windows = []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        seconds = slot.get("limit_window_seconds")
        minutes = seconds // 60 if isinstance(seconds, int) and not isinstance(seconds, bool) else None
        windows.append(window(codex_window_name(minutes), slot.get("used_percent"), slot.get("reset_at")))
    return reading(windows)


def parse_claude(payload):
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return None
    slots = [("5-hour", limits.get("five_hour")), ("weekly", limits.get("seven_day"))]
    return reading([window(name, slot.get("used_percentage"), slot.get("resets_at"))
                    for name, slot in slots if isinstance(slot, dict)])


def iso_epoch(value):
    if not isinstance(value, str):
        return None
    try:
        return epoch(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def parse_claude_login_usage(payload):
    """The usage endpoint's windows, as utilization percent used and an ISO reset time."""
    if not isinstance(payload, dict):
        return None
    slots = [("5-hour", payload.get("five_hour")), ("weekly", payload.get("seven_day"))]
    return reading([window(name, slot.get("utilization"), iso_epoch(slot.get("resets_at")))
                    for name, slot in slots if isinstance(slot, dict)])


def login_path():
    return data_dir() / "usage.json"


def claude_login_enabled():
    try:
        return json.loads(login_path().read_text()).get("claude_login") is True
    except (OSError, ValueError, AttributeError):
        return False


def set_claude_login(enabled):
    path = login_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps({"claude_login": bool(enabled)}) + "\n")
    os.chmod(path, 0o600)
    return {"claude_login": "on" if enabled else "off",
            "note": "Restart HQ for this to take effect: hq stop, then hq float" if enabled else "Claude usage now comes only from a status line, if you have one"}


class ClaudeLogin:
    """Reads Claude usage with Claude Code's own login, only when the user turned it on."""

    def __init__(self, enabled=claude_login_enabled, run=subprocess.run, open_url=urllib.request.urlopen, now=time.time):
        self.enabled, self.run, self.open_url, self.now = enabled, run, open_url, now
        self.token, self.expires = None, 0
        self.retry_after = None

    def load(self):
        # macOS asks the user before handing this over, the first time and after changes.
        done = self.run(["security", "find-generic-password", "-s", CLAUDE_LOGIN_ITEM, "-w"],
                        capture_output=True, text=True, timeout=60)
        self.token, self.expires = None, 0
        if done.returncode:
            return
        login = (json.loads(done.stdout) or {}).get("claudeAiOauth") or {}
        if isinstance(login.get("accessToken"), str) and isinstance(login.get("expiresAt"), (int, float)):
            self.token, self.expires = login["accessToken"], login["expiresAt"] / 1000

    def ask(self):
        request = urllib.request.Request(CLAUDE_USAGE_URL, headers={
            "Authorization": f"Bearer {self.token}", "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json", "User-Agent": "hq"})
        with self.open_url(request, timeout=15) as response:
            return parse_claude_login_usage(json.load(response))

    def read(self):
        if not self.enabled():
            self.token, self.expires = None, 0
            return None
        if not self.token or self.expires <= self.now() + 60:
            self.load()
        # An expired login is Claude Code's to renew, next time it runs.
        if not self.token or self.expires <= self.now() + 60:
            return None
        self.retry_after = None
        try:
            return self.ask()
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 429:
                try:
                    self.retry_after = max(0, int(error.headers.get("Retry-After", "")))
                except (TypeError, ValueError, AttributeError):
                    self.retry_after = None
                return None
            if error.code != 401:
                return None
            # Claude Code may have renewed it since: read it again, once.
            self.load()
            if not self.token:
                return None
            try:
                return self.ask()
            except (urllib.error.URLError, OSError, ValueError):
                return None
        except (urllib.error.URLError, OSError, ValueError):
            return None


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
    def __init__(self, usage, claude=read_claude, codex=read_codex, clock=time.monotonic, login=None, cache=None, now=time.time,
                 activity=None, rand=random.uniform):
        self.usage, self.claude, self.codex, self.clock = usage, claude, codex, clock
        self.activity, self.rand, self.now = activity, rand, now
        self.busy_before = {"claude": True, "codex": True}
        self.asked = {"claude": -math.inf, "codex": -math.inf}
        self.login = login or ClaudeLogin()
        self.stop = threading.Event()
        self.next_codex = self.next_login = -math.inf
        self.from_login, self.backoff, self.cache = None, CLAUDE_RETRY, cache
        # The last login reading survives a restart, so a restart neither blanks the circle
        # nor spends a request the endpoint may refuse.
        if cache and self.login.enabled():
            try:
                kept = valid_reading(json.loads(Path(cache).read_text()))
            except (OSError, ValueError):
                kept = None
            if kept:
                self.from_login = kept
                age = max(0, now() - kept["as_of"])
                if age < CLAUDE_POLL:
                    self.next_login = clock() + CLAUDE_POLL - age

    def forget(self, tool):
        """Another account is signed in now: its numbers are not the last one's."""
        if tool == "claude":
            self.login.token, self.login.expires, self.from_login = None, 0, None
            if self.cache:
                Path(self.cache).unlink(missing_ok=True)
            self.next_login, self.backoff = self.clock(), CLAUDE_RETRY
        elif tool == "codex":
            self.next_codex = self.clock()
        self.usage.record(tool, None)

    def keep(self, value):
        if not self.cache:
            return
        try:
            path = Path(self.cache)
            temp = path.with_name(path.name + ".tmp")
            temp.write_text(json.dumps(value))
            os.chmod(temp, 0o600)
            os.replace(temp, path)
        except OSError:
            pass

    def busy(self, agents):
        """Whether one of these agents is at work, or was in the last few minutes."""
        if self.activity is None:
            return True
        try:
            seen, now = self.activity(), self.now()
        except Exception:
            return True
        return any(state["running"] or (state["last"] and now - state["last"] < IDLE_AFTER)
                   for provider, state in seen.items() if provider in agents)

    def woke(self, tool, busy, due):
        """An agent at work again after a quiet spell: check soon, not at the idle time."""
        before, self.busy_before[tool] = self.busy_before[tool], busy
        if busy and not before:
            return min(due, max(self.clock() + self.rand(*SOON), self.asked[tool] + SPACING))
        return due

    def step(self):
        claude_busy, codex_busy = self.busy(CLAUDE_AGENTS), self.busy(CODEX_AGENTS)
        if self.backoff == CLAUDE_RETRY:  # never cut a rate-limit wait short
            self.next_login = self.woke("claude", claude_busy, self.next_login)
        self.next_codex = self.woke("codex", codex_busy, self.next_codex)
        if self.usage.is_watched() and self.clock() >= self.next_login:
            self.asked["claude"] = self.clock()
            value = self.login.read()
            if value:
                self.from_login, self.backoff = value, CLAUDE_RETRY
                self.keep(value)
                delay = self.rand(*(CLAUDE_BUSY if claude_busy else IDLE))
            else:
                # A refusal keeps the last number; its as_of says how old it is.
                self.from_login = self.from_login if self.login.enabled() else None
                hint = getattr(self.login, "retry_after", None)
                delay = max(hint if isinstance(hint, (int, float)) else 0, self.backoff) * self.rand(1, 1.25)
                self.backoff = min(self.backoff * 2, CLAUDE_BACKOFF_MOST)
            self.next_login = self.clock() + delay
        # The login, when it is on and answering; otherwise whatever the status line recorded.
        self.usage.record("claude", self.from_login or self.claude())
        if not self.usage.is_watched() or self.clock() < self.next_codex:
            return
        self.asked["codex"] = self.clock()
        value = self.codex()
        # A failed read keeps the last number: its as_of says how old it is, and fresh()
        # retires it. Offline for a minute should not blank the circle.
        if value:
            self.usage.record("codex", value)
        self.next_codex = self.clock() + (self.rand(*(CODEX_BUSY if codex_busy else IDLE)) if value else CODEX_RETRY * self.rand(1, 1.25))

    def run(self):
        delay = 1  # Not during startup, and never for a server already on its way out.
        while not self.stop.wait(delay):
            try:
                self.step()
            except READ_ERRORS:
                pass
            delay = TICK

"""hq doctor: check this machine's HQ setup and print a report to paste to whoever helps you.

It only reads, and changes nothing. It never prints a secret: it reads no login, no token
and nothing from the Keychain, and of the agents' config files it reports only whether
HQ's entries are there and which program they run. Each check says ok, warn or fail in
plain words, and how to fix it when it is not ok.
"""
import ctypes
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request

from . import floating, version
from .connections import HOOK_EVENTS, hq_command, is_hq_hook, statusline_state
from .onboard import AGENTS, hook_file
from .store import SCHEMA, Store, data_dir
from .version import short

OK, WARN, FAIL = "ok", "warn", "fail"
DEFAULT_PORT = 4310
RESTART = "hq stop, then hq"


def check(section, name, status, summary, fix=""):
    result = {"section": section, "check": name, "status": status, "summary": summary}
    if fix and status != OK:
        result["fix"] = fix
    return result


def quoted(path):
    """A path to paste into a shell: ~/... when that needs no quoting, else the full path quoted."""
    return short(path) if re.fullmatch(r"[~\w@%+=:,./-]+", short(path)) else shlex.quote(str(path))


# HQ itself

def open_inbox(path):
    """The inbox, read-only: the doctor never migrates or writes it."""
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


def schema(db):
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {table: {row[1] for row in db.execute(f"PRAGMA table_info({table})")} for table in tables}


def expected_schema():
    """Every table and column this build makes, from a throwaway inbox built by the real code."""
    with tempfile.TemporaryDirectory(prefix="hq-doctor-") as folder:
        path = Path(folder) / "hq.sqlite3"
        Store(path)
        db = sqlite3.connect(path)
        try:
            return schema(db)
        finally:
            db.close()


def health(port, timeout=1):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/api/health", timeout=timeout) as response:
            answer = json.load(response)
        return answer if isinstance(answer, dict) and answer.get("app") == "hq" else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, TypeError, ValueError):
        return True
    return True


def hq_checks(build):
    section = "HQ"
    line = version.describe(build)
    if build["from"] == "unknown":
        yield check(section, "Version", WARN, line, "Run hq update: it reinstalls HQ and records which build it is")
    else:
        yield check(section, "Version", OK, line)
    # HQ needs 3.11, so an older Python never gets this far.
    yield check(section, "Python", OK, f"Python {platform.python_version()} at {short(sys.executable)}")

    home = data_dir()
    if not home.is_dir():
        yield check(section, "Data folder", WARN, f"{short(home)} does not exist yet", "Run hq once; it makes the folder")
        yield check(section, "Inbox", WARN, "No inbox yet", "Run hq once; it makes the inbox")
        yield server_check(home, build)
        return
    info = home.stat()
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != os.getuid():
        yield check(section, "Data folder", FAIL, f"{short(home)} belongs to another user",
                    f"sudo chown -R \"$(whoami)\" {quoted(home)}")
    elif not os.access(home, os.W_OK):
        yield check(section, "Data folder", FAIL, f"{short(home)} is not writable", f"chmod u+rwx {quoted(home)}")
    elif mode & 0o077:
        yield check(section, "Data folder", WARN, f"{short(home)} can be read by other users of this machine (mode {mode:o})",
                    f"chmod 700 {quoted(home)}")
    else:
        yield check(section, "Data folder", OK, f"{short(home)}, private to you")
    yield inbox_check(home / "hq.sqlite3")
    yield server_check(home, build)


def inbox_check(path):
    section = "HQ"
    if not path.exists():
        return check(section, "Inbox", WARN, "No inbox yet", "Run hq once; it makes the inbox")
    mode = stat.S_IMODE(path.stat().st_mode)
    try:
        db = open_inbox(path)
        try:
            found_version = db.execute("PRAGMA user_version").fetchone()[0]
            found = schema(db)
            counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("items", "sessions") if table in found}
        finally:
            db.close()
    except sqlite3.Error as error:
        return check(section, "Inbox", FAIL, f"Cannot read {short(path)}: {error}",
                     f"{RESTART}. If this stays, send this report; keep a copy of {short(path.parent)} before changing it")
    expected = expected_schema()
    missing = [table for table in expected if table not in found]
    missing += [f"{table}.{column}" for table in expected if table in found
                for column in sorted(expected[table] - found[table])]
    if missing:
        return check(section, "Inbox", FAIL, f"Schema {found_version} is missing {', '.join(missing)}",
                     f"{RESTART}: HQ adds what is missing when it opens the inbox. If this stays, send this report")
    if found_version > SCHEMA:
        return check(section, "Inbox", WARN, f"Schema {found_version} is from a newer HQ; this one knows {SCHEMA}", "hq update")
    if found_version < SCHEMA:
        return check(section, "Inbox", WARN, f"Schema {found_version}; HQ upgrades it to {SCHEMA} the next time it opens it",
                     "Run hq once")
    summary = (f"Schema {found_version}, every table and column present; "
               f"{counts.get('items', 0)} items from {counts.get('sessions', 0)} conversations")
    if mode & 0o077:
        return check(section, "Inbox", WARN, summary + f"; but the file can be read by others (mode {mode:o})",
                     f"chmod 600 {quoted(path)}")
    return check(section, "Inbox", OK, summary)


def server_check(home, build):
    section = "HQ"
    try:
        state = json.loads((home / "server.json").read_text())
        port, pid, instance = int(state["port"]), state.get("pid"), state.get("instance")
    except FileNotFoundError:
        state = None
    except (OSError, ValueError, KeyError, TypeError):
        return check(section, "Server", WARN, "server.json is unreadable", RESTART)
    if state is None:
        stray = health(DEFAULT_PORT, timeout=0.5)
        if stray:
            return check(section, "Server", WARN,
                         f"Not running from this data folder, but another HQ (pid {stray.get('pid')}) answers on port {DEFAULT_PORT}",
                         "Use the same HQ_HOME everywhere, or stop that one and run hq")
        return check(section, "Server", WARN, "Not running; it starts when you run hq or the pill starts", "hq")
    answer = health(port)
    if not answer:
        if not alive(pid):
            return check(section, "Server", WARN, f"Not running; it stopped without tidying up (pid {pid} is gone)", "hq")
        return check(section, "Server", FAIL, f"pid {pid} should answer on 127.0.0.1:{port}, but nothing does",
                     f"{RESTART}. If it fails again, read {short(home / 'server.log')}")
    if answer.get("instance") != instance or answer.get("pid") != pid:
        return check(section, "Server", WARN, f"Another HQ (pid {answer.get('pid')}) answers on port {port}", RESTART)
    summary = f"Answering on 127.0.0.1:{port} (pid {pid})"
    installed = build.get("commit") or ""
    running = answer.get("commit")
    if installed and running is None:
        return check(section, "Server", WARN, summary + ", but it started before an update", RESTART)
    if installed and running and running != installed:
        return check(section, "Server", WARN, summary + f", but it runs {running[:7]} while {installed[:7]} is installed",
                     RESTART)
    return check(section, "Server", OK, summary + (f", running {running[:7]}" if running else ""))


# Agents

def read_json(path):
    data = json.loads(path.read_text())
    return data if isinstance(data, dict) else {}


def registered(agent):
    """HQ's MCP entry for this agent, read from its config file: (file, command words, entry, projects).

    projects counts Claude Code projects that have HQ only for themselves (local scope)."""
    projects = 0
    if agent == "claude-code":
        path = Path.home() / ".claude.json"
        data = read_json(path) if path.exists() else {}
        servers = data.get("mcpServers")
        projects = sum(isinstance(project, dict) and isinstance(project.get("mcpServers"), dict)
                       and "hq" in project["mcpServers"] for project in (data.get("projects") or {}).values())
    else:
        path = Path.home() / ".codex/config.toml"
        servers = tomllib.loads(path.read_text()).get("mcp_servers") if path.exists() else None
    entry = servers.get("hq") if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        return path, None, None, projects
    args = entry.get("args") if isinstance(entry.get("args"), list) else []
    return path, [str(entry.get("command") or ""), *map(str, args)], entry, projects


def program(words):
    """The program a command runs and, from a clone, its bin/hq: paths only, never arguments."""
    script = next((word for word in words[1:] if word.endswith("/bin/hq")), None)
    return [word for word in (words[0] if words else "", script) if word]


def gone(words):
    return [word for word in program(words) if os.path.isabs(word) and not os.path.exists(word)]


def same_file(one, other):
    """One program: the same path, or two names in one folder for one file. uv's reinstall
    moves hq from its environment's python to python3, the same interpreter."""
    one, other = os.path.abspath(one), os.path.abspath(other)
    try:
        return one == other or (os.path.dirname(one) == os.path.dirname(other) and os.path.samefile(one, other))
    except OSError:
        return False


def same(words, mine):
    theirs, ours = program(words), program(mine)
    return len(theirs) == len(ours) and all(map(same_file, theirs, ours))


def removal(agent):
    return "claude mcp remove --scope user hq" if agent == "claude-code" else "codex mcp remove hq"


def tools_check(agent):
    name = AGENTS[agent]["name"]
    try:
        path, words, entry, projects = registered(agent)
    except (OSError, ValueError, AttributeError, tomllib.TOMLDecodeError) as error:
        return check("Agents", f"{name} tools", FAIL, f"Cannot read its config: {error}", "Fix that file, then hq setup")
    if words is None and projects:
        return check("Agents", f"{name} tools", WARN, f"HQ's tools are registered for {plural(projects, 'project')} only, "
                     "not for every conversation", "hq setup")
    if words is None:
        return check("Agents", f"{name} tools", FAIL, f"HQ's tools are not registered in {short(path)}", "hq setup")
    runs = " ".join(short(word) for word in program(words)) + (" -m hq" if "-m" in words else "")
    if gone(words):
        return check("Agents", f"{name} tools", FAIL, f"HQ's tools point at {short(gone(words)[0])}, which is gone",
                     f"{removal(agent)}, then hq setup")
    if not same(words, hq_command()):
        return check("Agents", f"{name} tools", WARN, f"HQ's tools run {runs}, not this hq",
                     f"If that is an old copy: {removal(agent)}, then hq setup")
    if agent == "codex":
        timeout = entry.get("tool_timeout_sec")
        if not isinstance(timeout, (int, float)) or timeout < 3600:
            setting = "not set" if timeout is None else f"{timeout}"
            return check("Agents", f"{name} tools", WARN, f"Registered, but tool_timeout_sec is {setting}, so waiting "
                         "for your answer can time out", f"{removal(agent)}, then hq setup")
    return check("Agents", f"{name} tools", OK, f"Registered in {short(path)}, running {runs}")


def hooks_check(agent):
    name = AGENTS[agent]["name"]
    path = hook_file(agent)
    try:
        data = read_json(path) if path.exists() else {}
    except (OSError, ValueError) as error:
        return check("Agents", f"{name} hooks", FAIL, f"Cannot read {short(path)}: {error}", "Fix that file, then hq setup")
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    found = {}
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else []:
            for hook in group.get("hooks", []) if isinstance(group, dict) else []:
                if is_hq_hook(hook, agent):
                    found.setdefault(event, shlex.split(hook["command"]))
    if not found:
        return check("Agents", f"{name} hooks", FAIL, f"HQ's hooks are not in {short(path)}", "hq setup")
    missing = [event for event in HOOK_EVENTS if event not in found]
    broken = sorted({word for words in found.values() for word in gone(words)})
    if broken:
        return check("Agents", f"{name} hooks", FAIL, f"HQ's hooks point at {short(broken[0])}, which is gone",
                     "hq setup (it points them at this hq)")
    if missing:
        return check("Agents", f"{name} hooks", WARN, f"HQ's hooks are missing for {', '.join(missing)}", "hq setup")
    if agent == "claude-code" and data.get("disableAllHooks") is True:
        return check("Agents", f"{name} hooks", WARN, "Installed, but disableAllHooks turns every hook off",
                     f"Remove disableAllHooks from {short(path)}")
    if not all(same(words, hq_command()) for words in found.values()):
        return check("Agents", f"{name} hooks", WARN, "HQ's hooks run another copy of HQ, not this hq",
                     "hq setup (it points them at this hq)")
    summary = f"All {len(HOOK_EVENTS)} in {short(path)}"
    if agent == "codex":
        summary += "; Codex runs them only once you trust them in /hooks, which HQ cannot see"
    return check("Agents", f"{name} hooks", OK, summary)


def agent_checks():
    for agent, info in AGENTS.items():
        name = info["name"]
        binary = shutil.which(info["binary"])
        if not binary:
            yield check("Agents", name, WARN, f"{info['binary']} is not on your PATH; skip this if you do not use {name}",
                        f"Install {name}, then hq setup")
            continue
        yield check("Agents", name, OK, f"Found at {short(binary)}")
        yield tools_check(agent)
        yield hooks_check(agent)
        if agent == "claude-code":
            yield usage_check()


def usage_check():
    """Where Claude usage comes from: it is opt-in, so every answer is ok."""
    from .usage import claude_login_enabled
    line = statusline_state()
    if claude_login_enabled():
        summary = "Claude usage is read with Claude Code's saved login (hq usage login)"
    elif line == "hq":
        summary = "Claude usage comes from HQ's status line"
    elif line == "other":
        summary = "Your own status line is set, so Claude usage reaches HQ only if it pipes to hq statusline"
    else:
        summary = "Claude usage is off (optional): hq usage install or hq usage login turns it on"
    return check("Agents", "Claude usage", OK, summary)


# The pill

def pill_running():
    """Like hq float status, but without making the lock file when there is none."""
    try:
        lock = floating.lock_path().open("r")
    except OSError:
        return False
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
        return False


def executable(pid):
    try:
        done = subprocess.run(["ps", "-p", str(int(pid)), "-o", "comm="], capture_output=True, text=True, timeout=2)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None


class Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class Size(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class Rect(ctypes.Structure):
    _fields_ = [("origin", Point), ("size", Size)]


def displays():
    """Each active display's frame in points (y grows down from the main one), from
    CoreGraphics: fast, and it needs no permission."""
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    display, mode = ctypes.c_uint32, ctypes.c_void_p
    for name, result, arguments in (
            ("CGGetActiveDisplayList", ctypes.c_int32, [ctypes.c_uint32, ctypes.POINTER(display), ctypes.POINTER(ctypes.c_uint32)]),
            ("CGMainDisplayID", display, []), ("CGDisplayBounds", Rect, [display]),
            ("CGDisplayIsBuiltin", ctypes.c_uint32, [display]), ("CGDisplayMirrorsDisplay", display, [display]),
            ("CGDisplayCopyDisplayMode", mode, [display]), ("CGDisplayModeGetPixelWidth", ctypes.c_size_t, [mode]),
            ("CGDisplayModeRelease", None, [mode])):
        getattr(cg, name).restype, getattr(cg, name).argtypes = result, arguments
    ids, count = (display * 32)(), ctypes.c_uint32()
    if cg.CGGetActiveDisplayList(32, ids, ctypes.byref(count)) != 0:
        return []
    main = cg.CGMainDisplayID()
    found = []
    for id in ids[:count.value]:
        frame, current = cg.CGDisplayBounds(id), cg.CGDisplayCopyDisplayMode(id)
        pixels = cg.CGDisplayModeGetPixelWidth(current) if current else 0
        if current:
            cg.CGDisplayModeRelease(current)
        width = frame.size.width
        found.append({"main": id == main, "builtin": bool(cg.CGDisplayIsBuiltin(id)), "mirrors": bool(cg.CGDisplayMirrorsDisplay(id)),
                      "x": int(frame.origin.x), "y": int(frame.origin.y), "width": int(width), "height": int(frame.size.height),
                      "scale": round(pixels / width, 2) if pixels and width else 1})
    return sorted(found, key=lambda d: (not d["main"], d["x"], d["y"]))


def where(display, main):
    """Where a display sits beside the main one, in words."""
    if display["main"]:
        return "main"
    if display["mirrors"]:
        return "mirrors another"
    sides = []
    if display["y"] + display["height"] <= main["y"]:
        sides.append("above")
    elif display["y"] >= main["y"] + main["height"]:
        sides.append("below")
    if display["x"] >= main["x"] + main["width"]:
        sides.append("right")
    elif display["x"] + display["width"] <= main["x"]:
        sides.append("left")
    return ("-".join(sides) or "overlapping") + f" at ({display['x']}, {display['y']})"


def arrangement(found):
    main = next((d for d in found if d["main"]), found[0])
    return "; ".join(f"{'built-in' if d['builtin'] else 'external'} {d['width']}x{d['height']}"
                     + (f" @{d['scale']:g}x" if d["scale"] != 1 else "") + f", {where(d, main)}" for d in found)


def pill_checks():
    section = "Pill"
    if sys.platform != "darwin":
        yield check(section, "Pill", OK, "Not a Mac: the pill is macOS only, and the dashboard works here")
        return
    tools = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True, timeout=5)
    binary = floating.binary_path()
    if tools.returncode == 0:
        yield check(section, "Command line tools", OK, f"At {tools.stdout.strip()}")
    else:
        yield check(section, "Command line tools", WARN if binary.exists() else FAIL,
                    "Apple's command line tools are missing; the pill needs them to build itself",
                    "xcode-select --install, then hq float")
    running = pill_running()
    pid = floating.read_pid() if running else None
    at_login = floating.plist_path().exists()
    login = "starts at login" if at_login else "does not start at login"
    if not running:
        yield check(section, "Running", WARN, f"Not running; it {login}",
                    "hq float" + ("" if at_login else ", and hq float install to start it at login"))
    else:
        current = executable(pid)
        summary = f"Running (pid {pid}), docked {floating.saved_dock()}, {login}"
        if current and binary.exists() and os.path.abspath(current) != str(binary):
            yield check(section, "Running", WARN, summary + ", but on an older build of its window", "hq float stop, then hq float")
        else:
            yield check(section, "Running", OK, summary)
    if binary.exists():
        yield check(section, "Built", OK, f"Built for this version at {short(binary)}")
    else:
        yield check(section, "Built", OK, "Not built for this version yet; it builds itself when it next starts (about a minute)")
    try:
        found = displays()
    except (OSError, AttributeError) as error:
        yield check(section, "Displays", WARN, f"Could not list displays: {error}")
        return
    if not found:
        yield check(section, "Displays", WARN, "No displays found: is anyone logged in at the screen?")
    else:
        yield check(section, "Displays", OK, f"{len(found)}: {arrangement(found)}")


# Environment

def environment_checks(build):
    section = "Environment"
    values = {name: os.environ.get(name) for name in ("LC_ALL", "LC_CTYPE", "LANG")}
    shown = ", ".join(f"{name}={value}" for name, value in values.items() if value) or "none of LANG, LC_ALL or LC_CTYPE set"
    effective = (values["LC_ALL"] or values["LC_CTYPE"] or values["LANG"] or "").lower().replace("utf8", "utf-8")
    if "utf-8" in effective:
        yield check(section, "Locale", OK, shown)
    else:
        yield check(section, "Locale", WARN, f"Not UTF-8 ({shown}), so some of HQ's symbols may look wrong",
                    "Add export LANG=en_US.UTF-8 to your shell's profile, then open a new terminal")
    found = shutil.which("hq")
    if build["from"] == "clone":
        yield check(section, "PATH", OK, f"This hq runs from its clone; the hq on your PATH is {short(found) if found else 'none'}")
        return
    # Where the installer put hq, else the hq running now, else uv's usual folder.
    if build.get("hq"):
        expected = Path(build["hq"])
    elif Path(sys.argv[0]).name == "hq":
        expected = Path(sys.argv[0]).absolute()
    else:
        expected = Path.home() / ".local/bin/hq"
    folders = [os.path.abspath(os.path.expanduser(entry)) for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    if os.path.abspath(expected.parent) not in folders:
        yield check(section, "PATH", FAIL, f"{short(expected.parent)}, where hq is installed, is not on your PATH",
                    "uv tool update-shell, then open a new terminal")
    elif not found:
        yield check(section, "PATH", FAIL, f"No hq in {short(expected.parent)}", f"Install HQ again: {version.INSTALL_LINE}")
    elif expected.exists() and not os.path.samefile(found, expected):
        yield check(section, "PATH", WARN, f"Your PATH finds another hq first, at {short(found)}",
                    f"Remove that one, or put {short(expected.parent)} earlier in your PATH")
    else:
        yield check(section, "PATH", OK, f"hq is on your PATH, at {short(found)}")


# Accounts

def account_checks():
    section = "Accounts"
    path = data_dir() / "hq.sqlite3"
    if not path.exists():
        yield check(section, "Accounts", OK, "None saved (optional)")
        return
    try:
        db = open_inbox(path)
        try:
            columns = schema(db).get("accounts")
            if columns is None:
                rows = []
            else:
                # Marked by HQ when a saved login was refused, or once its renewal has expired.
                stale = "stale" if "stale" in columns else "0"
                renew = "refresh_expires" if "refresh_expires" in columns else "NULL"
                rows = db.execute(f"""SELECT tool, COUNT(*), SUM(CASE WHEN {stale} OR ({renew} > 0 AND {renew} <= ?)
                                      THEN 1 ELSE 0 END) FROM accounts GROUP BY tool""", (time.time(),)).fetchall()
        finally:
            db.close()
    except sqlite3.Error as error:
        yield check(section, "Accounts", WARN, f"Could not read saved accounts: {error}")
        return
    counts = {tool: (saved, stale or 0) for tool, saved, stale in rows}
    for tool, name in (("claude", "Claude"), ("codex", "Codex")):
        saved, stale = counts.get(tool, (0, 0))
        if not saved:
            yield check(section, name, OK, "None saved (optional)")
        elif stale:
            yield check(section, name, WARN, f"{saved} saved; {stale} marked needs sign-in (fine if it is the one signed in now)",
                        f"Sign in to it again from the pill's Usage, or remove it: hq accounts remove {tool} EMAIL")
        else:
            yield check(section, name, OK, f"{saved} saved; none needs sign-in")


# The report

def system():
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0]} ({platform.machine()})"
    return f"{platform.system()} {platform.release()} ({platform.machine()})"


def report():
    build = version.build()
    checks = []
    for section, probe in (("HQ", lambda: hq_checks(build)), ("Agents", agent_checks), ("Pill", pill_checks),
                           ("Environment", lambda: environment_checks(build)), ("Accounts", account_checks)):
        try:
            checks.extend(probe())
        except Exception as error:  # One broken check must not hide the rest of the report.
            checks.append(check(section, "Doctor", FAIL, f"This check broke: {type(error).__name__}: {error}",
                                "Send this report to whoever helps you"))
    return {"hq": version.describe(build), "system": system(), "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
            "checks": checks, "counts": {status: sum(c["status"] == status for c in checks) for status in (OK, WARN, FAIL)}}


def plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def text(result):
    lines = [f"HQ doctor · {result['generated'][:16].replace('T', ' ')} · {result['system']}",
             "No tokens, passwords or Keychain contents are included, so this is safe to paste.", ""]
    width = max(len(c["check"]) for c in result["checks"]) + 2
    section = None
    for item in result["checks"]:
        if item["section"] != section:
            if section:
                lines.append("")
            section = item["section"]
            lines.append(section)
        lines.append(f"  {item['status']:<5} {item['check']:<{width}}{item['summary']}")
        if item.get("fix"):
            lines.append(f"  {'':<5} {'':<{width}}Fix: {item['fix']}")
    counts = result["counts"]
    lines.append("")
    if counts[FAIL] or counts[WARN]:
        lines.append(f"{plural(counts[FAIL], 'problem')}, {plural(counts[WARN], 'warning')}."
                     + (" Fix the problems first." if counts[FAIL] else ""))
    else:
        lines.append("Everything looks right.")
    return "\n".join(lines)


def run(as_json=False):
    result = report()
    print(json.dumps(result, indent=2) if as_json else text(result))
    return 1 if result["counts"][FAIL] else 0

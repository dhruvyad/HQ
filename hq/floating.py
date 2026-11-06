"""The floating panel: a native window around the /float page. macOS only.

FloatPanel.swift only moves a window, and it is compiled on first use and cached,
so HQ still has no runtime dependencies. `hq float lab` runs the same panel on a
throwaway HQ full of fixtures, so trying designs can never reach a real agent.
"""
from datetime import date, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from .store import Store, data_dir

SOURCE = Path(__file__).with_name("FloatPanel.swift")
LABEL = "local.hq.float"
DEFAULT_DOCK = "top-left"


def lock_path():
    return data_dir() / "float.lock"


def pid_path():
    return data_dir() / "float.pid"


def state_path():
    return data_dir() / "float.json"


def log_path():
    return data_dir() / "float.log"


def picks_path():
    return data_dir() / "float-lab-picks.json"


def plist_path():
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def require_macos():
    if sys.platform != "darwin":
        raise ValueError("The floating window needs macOS. The dashboard works everywhere: hq start")


def hq_command():
    # Absolute interpreter and script, so it also works from launchd with no PATH.
    executable = Path(__file__).resolve().parents[1] / "bin/hq"
    return [sys.executable, str(executable)] if executable.exists() else [sys.executable, "-m", "hq"]


def binary_path(source=None):
    """Where the window compiled from this source lives; one folder per source version."""
    digest = hashlib.sha256(Path(source or SOURCE).read_bytes()).hexdigest()[:12]
    # The file name stays the same across versions because WebKit keys its storage on it.
    return data_dir() / "float" / digest / "hq-float"


def build(source=None):
    """Compile once per source version and return the binary."""
    require_macos()
    source = Path(source or SOURCE)
    if not source.is_file():
        raise ValueError(f"The floating window source is missing: {source}")
    binary = binary_path(source)
    root, digest = binary.parent.parent, binary.parent.name
    if not binary.exists():
        if not shutil.which("swiftc"):
            raise ValueError("Building the floating window needs swiftc. Install Apple's command line tools: xcode-select --install")
        binary.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Build beside the target and rename, so an interrupted compile is never run.
        partial = binary.with_name(binary.name + ".partial")
        result = subprocess.run(["swiftc", "-O", "-o", str(partial), str(source)], capture_output=True, text=True)
        if result.returncode != 0:
            partial.unlink(missing_ok=True)
            raise ValueError("The floating window did not compile:\n" + result.stderr.strip()[-2000:])
        os.replace(partial, binary)
    for entry in root.iterdir():
        if entry.is_dir() and entry.name != digest:
            shutil.rmtree(entry, ignore_errors=True)
    return binary


def take_lock():
    """Return a locked file, or None while a panel holds it."""
    home = data_dir()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = lock_path().open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def panel_running():
    lock = take_lock()
    if lock is None:
        return True
    lock.close()
    return False


def read_pid():
    try:
        return int(pid_path().read_text())
    except (OSError, ValueError):
        return None


def write_pid(pid):
    path = pid_path()
    path.write_text(str(pid))
    os.chmod(path, 0o600)


def saved_dock():
    try:
        state = json.loads(state_path().read_text())
        return state.get("dock") or DEFAULT_DOCK
    except (OSError, ValueError, AttributeError):
        return DEFAULT_DOCK


def panel_url(state):
    return f"http://127.0.0.1:{state['port']}/float"


def require_route(url):
    """A server started before this version has no /float, and would show nothing."""
    try:
        with urllib.request.urlopen(url, timeout=2):
            return
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        if code == 404:
            raise ValueError("The running HQ is an older version without the floating window. Run hq stop, then hq float") from None
        raise ValueError(f"HQ answered {code} for the floating window") from None
    except (OSError, urllib.error.URLError) as error:
        raise ValueError(f"HQ is not answering at {url}: {error}") from None


def wake(pid):
    # A panel hidden for an hour comes back when you ask for it again.
    if pid:
        try:
            os.kill(pid, signal.SIGUSR1)
        except ProcessLookupError:
            pass


def start(port, ensure_server):
    require_macos()
    url = panel_url(ensure_server(port))
    require_route(url)
    if panel_running():
        pid = read_pid()
        wake(pid)
        return {"status": "already running", "pid": pid, "url": url}
    binary = build()
    lock = take_lock()
    if lock is None:  # Another start won the race while this one compiled.
        return {"status": "already running", "pid": read_pid(), "url": url}
    try:
        log = log_path()
        with log.open("ab") as output:
            os.chmod(log, 0o600)
            # The child inherits the locked descriptor, so the lock lives exactly as long as the panel.
            child = subprocess.Popen([str(binary), url, str(state_path())], stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=output, start_new_session=True,
                                     pass_fds=(lock.fileno(),))
        write_pid(child.pid)
        for _ in range(5):
            if child.poll() is not None:
                raise ValueError(f"The floating window exited right away. See {log}")
            time.sleep(0.05)
    finally:
        lock.close()
    return {"status": "started", "pid": child.pid, "url": url, "log": str(log)}


def run(port, ensure_server):
    """Foreground start for launchd. Exiting 0 when one is already up stops launchd retrying."""
    require_macos()
    url = panel_url(ensure_server(port))
    require_route(url)
    binary = build()
    lock = take_lock()
    if lock is None:
        return {"status": "already running", "pid": read_pid(), "url": url}
    write_pid(os.getpid())
    # exec keeps this pid and, with the descriptor inheritable, the lock.
    os.set_inheritable(lock.fileno(), True)
    try:
        os.execv(str(binary), [str(binary), url, str(state_path())])
    finally:
        lock.close()  # Reached only if exec failed.


def stop():
    if not panel_running():
        return {"status": "not running"}
    pid = read_pid()
    if pid is None:
        raise ValueError(f"A floating window holds {lock_path()} but its pid file is missing")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(100):
        if not panel_running():
            pid_path().unlink(missing_ok=True)
            return {"status": "stopped", "pid": pid}
        time.sleep(0.05)
    raise ValueError(f"The floating window (pid {pid}) is still shutting down; check hq float status")


def status():
    running = panel_running()
    return {"running": running, "pid": read_pid() if running else None, "dock": saved_dock(),
            "installed_at_login": plist_path().exists()}


def launch_agent(port):
    agent = {"Label": LABEL, "ProgramArguments": hq_command() + ["float", "run", "--port", str(port)],
             "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "AbandonProcessGroup": True,
             # A panel needs a window server; this stops it starting in a background session.
             "LimitLoadToSessionType": "Aqua",
             "StandardOutPath": str(log_path()), "StandardErrorPath": str(log_path())}
    if "HQ_HOME" in os.environ:
        agent["EnvironmentVariables"] = {"HQ_HOME": os.environ["HQ_HOME"]}
    return agent


def launchctl(*arguments):
    return subprocess.run(["launchctl", *arguments], capture_output=True, text=True)


def install(port):
    require_macos()
    build()
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    # Bootstrapping a loaded job fails, so a reinstall unloads the old one first.
    launchctl("bootout", f"{domain}/{LABEL}")
    path.write_bytes(plistlib.dumps(launch_agent(port)))
    result = launchctl("bootstrap", domain, str(path))
    if result.returncode != 0:
        path.unlink(missing_ok=True)
        raise ValueError("launchctl could not load the floating window: " + (result.stderr or result.stdout).strip())
    return {"status": "installed", "plist": str(path), "port": port}


def uninstall():
    require_macos()
    path = plist_path()
    if not path.exists():
        return {"status": "not installed"}
    # Fails harmlessly when the job is not loaded; the plist is what brings it back at login.
    launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    path.unlink()
    return {"status": "removed", "plist": str(path)}


def seed_lab(store):
    """Every shape the panel has to draw. Fictional, so a screenshot of the lab shares nothing."""
    session = "lab:fixtures"
    store.register(session, "Lab fixtures", "other")
    today = date.today()
    why = {"matters": {"lead": "Unblocks the next release", "points": ["Two teams are waiting on it"]},
           "needs_you": {"lead": "This is your call", "points": ["Nobody else has the context"]}}
    harbor = {"project": "Harbor", "source": "lab · fixtures"}
    lantern = {"project": "Lantern", "source": "lab · fixtures"}
    release, onboarding, infra = {"workstream": "Release"}, {"workstream": "Onboarding"}, {"workstream": "Infrastructure"}
    # (minutes old, title, add() arguments)
    fixtures = [
        (6, "Choose the launch date for the spring release",
         {"kind": "question", "owner": "human", "minutes": 2, "rank": 40, **why, **harbor, **release,
          "options": [{"label": "Tuesday the 12th", "note": "Gives support a full week before the holidays.", "recommended": True},
                      {"label": "Thursday the 14th", "note": "Two more days for the docs, less room after launch."},
                      {"label": "After the holidays", "note": "Safest, but the partner demo slips with it."}]}),
        (14, "Send one message to the demo bot, then check what it asks",
         {"owner": "human", "minutes": 3, "rank": 210, **why, **harbor, **onboarding,
          "steps": [{"text": "Open the demo", "link": {"label": "example.com/demo", "url": "https://example.com/demo"}},
                    {"text": "Send this message", "copy": "Set up a weekly report and send it to the team channel"},
                    {"text": "Check what it asks you", "checks": ["A list of channels to pick from",
                                                                   "No question about the report's format",
                                                                   "A preview before anything is sent"]}]}),
        (35, "Approve the staging certificate so deploys can resume",
         {"kind": "blocker", "owner": "human", "minutes": 1, "rank": 20, **why, **lantern, **infra,
          "steps": ["Open the certificate request", "Approve it for staging only"]}),
        (75, "Post the release note in the team channel",
         {"owner": "human", "minutes": 2, "rank": 240, **why, **harbor, **release,
          "steps": ["Copy the note below", "Post it in the release channel"],
          "paste": [{"label": "Release note", "text": "Spring release is live. Faster reports, a new onboarding flow, and fixes for the export bug."},
                    {"label": "Rollback command", "text": "deploy rollback --service reports --to previous", "mono": True}]}),
        (150, "Renew the example.com domain before it lapses",
         {"owner": "human", "minutes": 4, "rank": 200, "due": today.isoformat(), **why, **lantern, **infra,
          "steps": [{"text": "Open the registrar", "link": {"label": "registrar", "url": "https://example.com/registrar"}},
                    "Renew for one year"]}),
        (300, "Sign the vendor renewal",
         {"owner": "human", "minutes": 5, "rank": 220, "due": (today - timedelta(days=3)).isoformat(), **why, **lantern, **infra,
          "steps": ["Read the two changed clauses", "Sign and return it"]}),
        (540, "Pick who gets the invite to the partner preview",
         {"kind": "question", "owner": "human", "minutes": 1, "rank": 120, **why, **harbor, **onboarding,
          "options": ["Everyone on the waitlist", "Only paying partners"]}),
        (60 * 26, "Decide whether the reports team can have a second staging environment for the migration, given that "
                  "the current one is shared with onboarding and the certificate work is still open, or whether they "
                  "should wait for the new cluster next quarter",
         {"kind": "question", "owner": "human", "minutes": 3, "rank": 140, **why, **lantern, **infra,
          "recommendation": "Let them wait. The cluster is close and a second environment needs its own certificates.",
          "options": ["Add a second environment", {"label": "Wait for the new cluster", "recommended": True}]}),
        (60 * 50, "Review the pricing page copy before it ships",
         {"owner": "human", "minutes": 6, "rank": 300, "pinned": True, **why, **harbor, **release,
          "steps": [{"text": "Read the draft", "link": {"label": "draft", "url": "https://example.com/pricing-draft"}},
                    {"text": "Check the numbers", "checks": ["Annual price matches the plan table", "The trial length is right"]}]}),
        (60 * 75, "Confirm the backup restore finished",
         {"owner": "human", "minutes": 2, "rank": 320, **why, **lantern, **infra,
          "steps": ["Open the restore log", "Look for the final success line"]}),
        (60 * 100, "Check the billing export",
         {"owner": "human", "kind": "todo", **harbor, **release, "readable": False,
          "detail": "Older items arrive as one paragraph with no steps. The export looked short last week, so someone "
                    "should compare the totals to the invoice."}),
        (60 * 130, "Tell the design team which onboarding illustration to keep",
         {"kind": "question", "owner": "human", "minutes": 1, "rank": 340, **why, **harbor, **onboarding,
          "options": [{"label": "The line drawing", "note": "Quieter, and matches the rest of the app."},
                      {"label": "The photo", "note": "Warmer, but needs a dark variant."}]}),
        # With the agent, so it must never count as needing the human.
        (8, "Retry the failed import",
         {"owner": "agent", **harbor, **release, "detail": "Running again with the corrected file."}),
    ]
    ids, now = [], time.time()
    for age, title, fields in fixtures:
        fields = dict(fields)
        pinned = fields.pop("pinned", False)
        readable = fields.pop("readable", fields.get("owner") == "human")
        kind = fields.pop("kind", "todo")
        owner = fields.pop("owner")
        detail = fields.pop("detail", "")
        options = fields.pop("options", None)
        item = store.add(session, title, detail, kind, options=options, owner=owner, readable=readable, **fields)
        if pinned:
            store.update(item["id"], session, pinned=True)
        # add() stamps the current time; only this throwaway database gets older dates.
        with store.db() as db:
            db.execute("UPDATE items SET created=?,updated=? WHERE id=?", (now - age * 60, now - age * 60, item["id"]))
        ids.append(item["id"])
    return ids


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for_health(origin, process, log):
    for _ in range(100):
        if process.poll() is not None:
            break
        try:
            with urllib.request.urlopen(origin + "/api/health", timeout=1) as response:
                if json.load(response).get("app") == "hq":
                    return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    try:
        detail = log.read_text()[-1000:].strip()
    except OSError:
        detail = ""
    raise ValueError("The lab's HQ did not start" + (f":\n{detail}" if detail else ""))


def halt(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def read_picks(stream):
    # The panel prints only this line, but one stray line must not lose a sitting's work.
    picks = None
    for line in stream:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("picks"), str):
            picks = value["picks"]
    return picks


def save_picks(picks, item):
    path = picks_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps({"picks": picks, "item": item, "saved": time.time()}, indent=2) + "\n")
    os.chmod(partial, 0o600)
    os.replace(partial, path)
    return path


def lab(item=None):
    """Run the panel and its bench on throwaway data, and keep only the picks."""
    require_macos()
    if item:
        # Before the sitting, not after it: a closed item would throw away the picks' delivery.
        if Store().item(item)["status"] != "open":
            raise ValueError(f"Item {item} is not open, so it cannot take the lab's picks")
    binary = build()
    home = Path(tempfile.mkdtemp(prefix="hq-float-lab-"))
    server = panel = None
    try:
        seed_lab(Store(home / "hq.sqlite3"))
        origin = f"http://127.0.0.1:{free_port()}"
        environment = {**os.environ, "HQ_HOME": str(home), "HQ_FLOAT_LAB": "1"}
        log = home / "server.log"
        with log.open("ab") as output:
            # Own sessions: Ctrl-C reaches this process alone, which then stops both in order.
            server = subprocess.Popen(hq_command() + ["serve", "--port", origin.rsplit(":", 1)[1]],
                                      env=environment, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                      start_new_session=True)
        wait_for_health(origin, server, log)
        panel = subprocess.Popen([str(binary), f"{origin}/float?lab=1", str(home / "float.json"), "--bench", f"{origin}/float-lab"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True, start_new_session=True)
        picks = read_picks(panel.stdout)
        code = panel.wait()
    finally:
        for process in (panel, server):
            if process:
                halt(process)
        if panel and panel.stdout:
            panel.stdout.close()
        shutil.rmtree(home, ignore_errors=True)
    if picks is None:
        if code != 0:
            raise ValueError(f"The lab window exited with status {code}")
        return {"status": "closed without picks"}
    path = save_picks(picks, item)
    result = {"status": "saved", "picks": picks, "file": str(path), "item": item}
    if item:
        try:
            saved = Store().reply(item, picks)
        except ValueError as error:
            raise ValueError(f"{error}. Your picks are saved in {path}") from None
        # Saved for the agent, not received by it: only its acknowledgement can say that.
        result["delivery"] = saved.get("delivery")
    return result

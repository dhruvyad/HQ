"""Which build of HQ this is, and hq update.

The installer resolves the commit it installs and records it in install.json, in HQ's
data folder, so `hq --version` can say which build you have. A clone answers from git
instead. hq update runs the published installer again for the same branch, without
setup, then restarts whatever was running so the new code is live. It never opens your
inbox.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

from . import __version__
from .store import data_dir

REPO = "dhruvyad/HQ"
INSTALL_LINE = f"curl -fsSL https://raw.githubusercontent.com/{REPO}/main/install.sh | sh"
ROOT = Path(__file__).resolve().parents[1]


def stamp_path():
    return data_dir() / "install.json"


def checkout():
    """The clone this hq runs from (it has bin/hq beside the package), or None when installed."""
    return ROOT if (ROOT / "bin/hq").exists() else None


def read_stamp():
    try:
        stamp = json.loads(stamp_path().read_text())
    except (OSError, ValueError):
        return None
    return stamp if isinstance(stamp, dict) else None


def ours(stamp):
    """Whether a stamp describes this hq: the installer names the environment it filled."""
    env = stamp.get("env")
    return not env or Path(env).expanduser().resolve() == Path(sys.prefix).resolve()


def git(*args):
    try:
        # No optional locks: reading the version must not refresh the clone's index.
        done = subprocess.run(["git", "--no-optional-locks", "-C", str(ROOT), *args],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def build():
    """What this hq is: from a clone, from the installer, or not recorded."""
    if checkout():
        info = {"from": "clone", "source": str(ROOT)}
        # Only a real clone asks git: on a Mac without the command line tools, git is a
        # stand-in that pops up an installer.
        commit = git("rev-parse", "HEAD") if (ROOT / ".git").exists() else None
        if commit:
            info.update(commit=commit, ref=git("rev-parse", "--abbrev-ref", "HEAD") or "",
                        dirty=bool(git("status", "--porcelain", "--untracked-files=no")))
        return info
    stamp = read_stamp()
    if stamp and ours(stamp):
        return {**stamp, "from": "install"}
    return {"from": "unknown"}


def local_day(stamp):
    try:
        return datetime.fromisoformat(stamp).astimezone().date().isoformat()
    except (TypeError, ValueError):
        return None


def short(path):
    home = str(Path.home())
    path = str(path)
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def describe(info=None):
    """One line, such as: hq 0.1.0 (abc1234, installed 2026-11-06)."""
    info = build() if info is None else info
    parts = []
    if info.get("commit"):
        ref = info.get("ref") or ""
        parts.append(info["commit"][:7] + (f" on {ref}" if ref not in {"", "main", "HEAD"} else "")
                     + (" with local changes" if info.get("dirty") else ""))
    if info["from"] == "clone":
        parts.append(f"from the clone at {short(info['source'])}")
    elif info["from"] == "install":
        if not info.get("commit"):
            parts.append(f"from {info.get('ref') or info.get('source') or 'an unknown source'}")
        if local_day(info.get("installed_at")):
            parts.append(f"installed {local_day(info.get('installed_at'))}")
    else:
        parts.append("build not recorded")
    return f"hq {__version__} ({', '.join(parts)})"


def say(text=""):
    print(text, flush=True)


def fetch(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode()


def update(server, stop_server, download=fetch):
    """Install the latest build of the branch you installed, then restart what was running.

    server() returns the running server's state or None, and stop_server() stops it: the
    CLI's own, so this module needs nothing from it.
    """
    from . import floating
    if checkout():
        raise ValueError(f"This hq runs from the clone at {short(ROOT)}. Update it there with git pull; "
                         "./install.sh in it also updates the installed hq")
    before = read_stamp() or {}
    if before and not ours(before):
        raise ValueError("This hq is not the one HQ's installer put in place. Update it the way you installed it, "
                         f"or update that one: {short(before.get('hq') or 'hq')} update")
    environment = {**os.environ, "HQ_NO_SETUP": "1"}
    source = before.get("source") or ""
    github = not source or source.startswith(("https://", "http://"))
    if not github:
        # Installed from a clone: its own installer reinstalls it as it is now.
        script = Path(source) / "install.sh"
        if not script.is_file():
            raise ValueError(f"HQ was installed from {short(source)}, which is gone. Install it again: {INSTALL_LINE}")
        command, text, ref = ["sh", str(script)], None, before.get("ref") or ""
        say(f"Updating HQ from your clone at {short(source)}…")
    else:
        ref = os.environ.get("HQ_REF") or before.get("ref") or "main"
        environment["HQ_REF"] = ref
        url = f"https://raw.githubusercontent.com/{REPO}/{urllib.parse.quote(ref, safe='/')}/install.sh"
        try:
            text = download(url)
        except OSError as error:
            raise ValueError(f"Could not download the installer from {url}: {error}") from None
        command = ["sh", "-s"]
        say(f"Updating HQ from GitHub ({ref})…")
    state = server()
    pill = sys.platform == "darwin" and floating.panel_running()
    done = subprocess.run(command, input=text, env=environment, capture_output=True, text=True)
    if done.returncode != 0:
        say((done.stdout + done.stderr).strip())
        raise ValueError("The installer stopped, so nothing was restarted. Your inbox is untouched")
    after = read_stamp() or {}
    old, new = before.get("commit") or "", after.get("commit") or ""
    if after.get("installed_at") == before.get("installed_at"):
        new = ""  # An installer from before builds were recorded, so the new one is unknown.
    if old and old == new and not before.get("dirty") and not after.get("dirty"):
        say(f"Already up to date: {new[:7]}" + (f" ({ref})" if ref else ""))
        return 0
    if old and old == new:
        say(f"Reinstalled {new[:7]} with its local changes" + (f" ({ref})" if ref else ""))
    else:
        say(f"Updated HQ: {old[:7] or 'an unrecorded build'} -> {new[:7] or 'a build it did not record'}"
            + (f" ({ref})" if ref else ""))
    if github and old and new:
        say(f"What changed: https://github.com/{REPO}/compare/{old[:12]}...{new[:12]}")
    restart(state, pill, stop_server, after.get("hq") or shutil.which("hq"))
    return 0


def restart(state, pill, stop_server, hq):
    """Stop what was running and start it again with the new hq, so the new code is live."""
    from . import floating
    if not state and not pill:
        say("Nothing was running, so nothing needed a restart.")
        return
    if not hq or not Path(hq).exists():
        raise ValueError("Installed, but the new hq was not found to restart HQ. Run hq stop, then hq")
    port = str((state or {}).get("port") or 4310)
    if pill:
        floating.stop()
    if state:
        stop_server()
        start = subprocess.run([hq, "start", "--no-open", "--port", port], capture_output=True, text=True)
        if start.returncode != 0:
            raise ValueError("The dashboard server did not start again: " + (start.stderr or start.stdout).strip())
        say("✓ Dashboard server restarted")
    if pill:
        # The pill compiles its window again when that code changed, which takes about a minute.
        say("· Starting the pill again…")
        start = subprocess.run([hq, "float", "start", "--port", port], capture_output=True, text=True)
        if start.returncode != 0:
            raise ValueError("The pill did not start again: " + (start.stderr or start.stdout).strip())
        say("✓ Pill restarted")
    say("Agent conversations already open keep HQ's previous tools until you restart them.")

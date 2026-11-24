"""HQ Links, the small app macOS hands hq:// item links to, so a click in a terminal opens the
item in the pill without going through the browser. macOS only.

It is built from LinkOpener.swift beside the pill, ad hoc signed, and registered with Launch
Services as the app for the hq scheme, which nothing else on a Mac claims. It is rebuilt only
when its source or the command it runs changes, and `hq setup --undo` removes it.
"""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

from .store import data_dir

SOURCE = Path(__file__).with_name("LinkOpener.swift")
NAME = "HQ Links.app"
IDENTIFIER = "local.hq.links"
EXECUTABLE = "hq-links"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
              "/Support/lsregister")


def app_path():
    return data_dir() / NAME


def hq_command():
    from .floating import hq_command as command
    return command()


def info(key):
    return {
        "CFBundleIdentifier": IDENTIFIER, "CFBundleName": "HQ Links", "CFBundleDisplayName": "HQ Links",
        "CFBundleExecutable": EXECUTABLE, "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1",
        "CFBundleVersion": key, "LSMinimumSystemVersion": "11.0",
        # Never in the Dock and never frontmost, so a click leaves focus where it was.
        "LSBackgroundOnly": True,
        "CFBundleURLTypes": [{"CFBundleURLName": "HQ item link", "CFBundleURLSchemes": ["hq"]}],
        # What it runs, absolute, because Launch Services starts it with no PATH and no HQ_HOME.
        "HQCommand": hq_command(), "HQHome": str(data_dir()),
    }


def build_key():
    """What the app is made from: its source and the command it runs."""
    digest = hashlib.sha256(SOURCE.read_bytes())
    digest.update(json.dumps([hq_command(), str(data_dir())]).encode())
    return digest.hexdigest()[:12]


def installed_key(app=None):
    try:
        with ((app or app_path()) / "Contents/Info.plist").open("rb") as file:
            return plistlib.load(file).get("CFBundleVersion")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def ready():
    """Whether this Mac has HQ Links, so an hq:// link will open."""
    return sys.platform == "darwin" and installed_key() is not None


def run(*command):
    return subprocess.run(list(command), capture_output=True, text=True)


def ensure():
    """Build and register HQ Links unless the current one is already in place."""
    if sys.platform != "darwin":
        raise ValueError("Item links that skip the browser need macOS")
    key, app = build_key(), app_path()
    if installed_key(app) == key:
        return {"status": "ready", "app": str(app)}
    if not shutil.which("swiftc"):
        raise ValueError("Building HQ Links needs swiftc. Install Apple's command line tools: xcode-select --install")
    app.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Built beside the target and swapped in whole, so a half-built app is never registered.
    staging = Path(tempfile.mkdtemp(prefix=".hq-links-", dir=app.parent))
    try:
        fresh = staging / NAME
        (fresh / "Contents/MacOS").mkdir(parents=True)
        result = run("swiftc", "-O", "-o", str(fresh / "Contents/MacOS" / EXECUTABLE), str(SOURCE))
        if result.returncode != 0:
            raise ValueError("HQ Links did not compile:\n" + result.stderr.strip()[-2000:])
        with (fresh / "Contents/Info.plist").open("wb") as file:
            plistlib.dump(info(key), file)
        signed = run("codesign", "--force", "--sign", "-", str(fresh))
        if signed.returncode != 0:
            raise ValueError("HQ Links could not be signed: " + (signed.stderr or signed.stdout).strip())
        if app.exists():
            run(LSREGISTER, "-u", str(app))
            shutil.rmtree(app)
        os.replace(fresh, app)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    registered = run(LSREGISTER, "-f", str(app))
    if registered.returncode != 0:
        shutil.rmtree(app, ignore_errors=True)
        raise ValueError("macOS would not register HQ Links: " + (registered.stderr or registered.stdout).strip())
    return {"status": "built", "app": str(app)}


def remove():
    app = app_path()
    if not app.exists():
        return {"status": "not installed"}
    if sys.platform == "darwin":
        run(LSREGISTER, "-u", str(app))
    shutil.rmtree(app, ignore_errors=True)
    return {"status": "removed", "app": str(app)}

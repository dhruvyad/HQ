"""An opt-in macOS launcher; only the native primary runtime is shared with HQ."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from .desktop import bridge
from .desktop_control import MARKER as RUNTIME_MARKER
from .store import Store, data_dir

MARKER = "hq.codex-desktop-launcher.v1"
OVERRIDES = ("CODEX_CLI_PATH", "CODEX_APP_SERVER_WS_URL",
             "CODEX_APP_SERVER_FORCE_CLI", "CODEX_APP_SERVER_USE_LOCAL_DAEMON")


def native_paths(app=None):
    candidates = [Path(app).expanduser()] if app else [
        Path("/Applications/Codex.app"), Path("/Applications/ChatGPT.app")]
    for path in candidates:
        try:
            info = plistlib.loads((path / "Contents/Info.plist").read_bytes())
            binary = path / "Contents/MacOS" / info["CFBundleExecutable"]
            cli = path / "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
            if binary.is_file() and cli.is_file():
                return path.resolve(), binary, cli
        except (OSError, ValueError, KeyError):
            continue
    raise ValueError("Could not find a Codex desktop installation with its bundled runtime")


def primary_arguments(arguments):
    """Fail closed on new startup shapes, helper sandboxes, and custom listeners.

    The installed desktop identifies its primary server with analytics and the
    Code Mode host feature. Auxiliary servers lack this signature. Preserve
    every argument verbatim; unknown configuration falls back to native stdio.
    """
    allowed = {"features.code_mode_host", "chatgpt_base_url", "openai_base_url"}
    config, flags, i = {}, [], 0
    while i < len(arguments):
        value = arguments[i]
        if value in {"-c", "--config"}:
            i += 1
            if i == len(arguments) or "=" not in arguments[i]:
                return False
            key, setting = arguments[i].split("=", 1)
            if key in config:
                return False
            if key not in allowed and not (
                key.startswith("plugins.") and ".mcp_servers." in key
                and key.endswith(".enabled") and setting in {"true", "false"}
            ):
                return False
            config[key] = setting
        else:
            flags.append(value)
        i += 1
    return (flags == ["app-server", "--analytics-default-enabled"]
            and config.get("features.code_mode_host") == "true")


def private_json(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        try:
            os.chmod(file.name, 0o600)
            json.dump(value, file)
            file.write("\n")
            file.close()
            os.replace(file.name, path)
        finally:
            Path(file.name).unlink(missing_ok=True)


def runtime_path(app, home=None):
    """Where the launcher keeps this app's runtime, without making it."""
    home = Path(home or os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
    key = hashlib.sha256((str(app) + "\0" + str(home) + "\0" + str(data_dir())).encode()).hexdigest()[:12]
    path = data_dir() / "desktop" / key
    if len(os.fsencode(path / "runtime.sock")) > 100:
        path = Path(tempfile.gettempdir()) / ("hq-desktop-" + str(os.getuid())) / key
    return path


def runtime_directory(app, home=None):
    path = runtime_path(app, home)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise ValueError("The desktop runtime directory must be owned by this user")
    os.chmod(path, 0o700)
    return path


def entry_script(function, argument=""):
    # Absolute interpreter and import path work when launched from Finder.
    return ("#!" + sys.executable + "\nimport sys\n"
            + "sys.path.insert(0, " + repr(str(Path(__file__).resolve().parents[1])) + ")\n"
            + "from hq.desktop_launch import " + function + "\n"
            + function + "(" + argument + ")\n")


def proxy_main():
    app, _, cli = native_paths(os.environ.get("HQ_DESKTOP_APP"))
    arguments = sys.argv[1:]
    env = dict(os.environ)
    # Runtime-spawned helpers use the real CLI, never this wrapper.
    env["CODEX_CLI_PATH"] = str(cli)
    if not primary_arguments(arguments):
        os.execve(cli, [str(cli), *arguments], env)
    directory = runtime_directory(app)
    endpoint = directory / "runtime.sock"
    with (directory / "runtime.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("The desktop already has an HQ runtime; refusing a second owner") from None
        endpoint.unlink(missing_ok=True)
        (directory / "desktop-managed").write_text(RUNTIME_MARKER)
        os.chmod(directory / "desktop-managed", 0o600)
        child = None
        def interrupted(signum, frame):
            raise SystemExit(128 + signum)
        handlers = {signum: signal.signal(signum, interrupted)
                    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        try:
            with (directory / "runtime.log").open("ab") as log:
                os.chmod(log.name, 0o600)
                child = subprocess.Popen([str(cli), *arguments, "--listen", "unix://" + str(endpoint)],
                                         env=env, stdout=log, stderr=log, start_new_session=True)
            deadline = time.monotonic() + 20
            while not endpoint.exists():
                if child.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Codex runtime did not start; see " + str(directory / "runtime.log"))
                time.sleep(0.05)
            os.chmod(endpoint, 0o600)
            private_json(directory / "runtime.json", {"pid": child.pid, "wrapper_pid": os.getpid(),
                         "socket": str(endpoint), "started": time.time(), "app": str(app),
                         "codex_home": str(Path(env.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve())})
            Store().watch_codex(str(endpoint))
            bridge(endpoint, control_path=directory / "desktop.sock")
        finally:
            # Reap this exact runtime and its helpers on quit or transport loss.
            # Desktop's native reconnect starts a fresh bridge; uncertain HQ
            # deliveries remain saved and require an explicit retry.
            if child:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
            endpoint.unlink(missing_ok=True)
            (directory / "runtime.json").unlink(missing_ok=True)
            for signum, handler in handlers.items():
                signal.signal(signum, handler)


def desktop_running(binary):
    output = subprocess.check_output(["ps", "-axo", "pid=,comm="], text=True)
    return any(line.strip().split(None, 1)[-1] == str(binary) for line in output.splitlines() if line.strip())


def launch_desktop(app=None, *, environment=None, profile=None, arguments=(), start_hq=True):
    app, binary, _ = native_paths(app)
    env = dict(os.environ if environment is None else environment)
    if any(env.get(key) for key in OVERRIDES):
        raise ValueError("Remove custom Codex runtime overrides before using the HQ desktop launcher")
    if profile is None and desktop_running(binary):
        raise ValueError("Codex is already open. Finish running turns, quit the app, then open Codex + HQ")
    if start_hq:
        from .cli import launch
        launch(4310, open_browser=False)
    directory = runtime_directory(app, env.get("CODEX_HOME"))
    wrapper = directory / "codex-hq"
    wrapper.write_text(entry_script("proxy_main"))
    os.chmod(wrapper, 0o700)
    env.update({"CODEX_CLI_PATH": str(wrapper), "HQ_DESKTOP_APP": str(app)})
    argv = [str(binary)]
    if profile is not None:
        env["CODEX_ELECTRON_USER_DATA_PATH"] = str(profile)
        argv.append("--user-data-dir=" + str(profile))
    argv.extend(arguments)
    with (directory / "desktop.log").open("ab") as log:
        os.chmod(log.name, 0o600)
        child = subprocess.Popen(argv, env=env, stdout=log, stderr=log, start_new_session=True)
    return {"started": True, "pid": child.pid, "app": str(app),
            "message": "Chats connect as the desktop loads them; use hq desktop status to verify."}


def install_launcher(app=None, destination=None):
    app, _, _ = native_paths(app)
    target = Path(destination) if destination else Path.home() / "Applications/Codex + HQ.app"
    if target.is_symlink() or (target.exists() and not managed_launcher(target)):
        raise ValueError("The launcher destination already exists and is not managed by HQ")
    contents = target / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    info = {"CFBundleIdentifier": "local.hq.codex-launcher", "CFBundleName": "Codex + HQ",
            "CFBundleExecutable": "codex-hq", "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1", "HQLauncher": MARKER}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    (contents / "MacOS/codex-hq").write_text(entry_script("launcher_main", repr(str(app))))
    os.chmod(contents / "MacOS/codex-hq", 0o755)
    return {"installed": True, "path": str(target), "connected": False,
            "next_step": "Finish active turns, quit Codex, then open Codex + HQ. Use this launcher on future starts."}


def launcher_main(app):
    """Finder has no terminal; show an actionable error if launch is refused."""
    try:
        launch_desktop(app)
    except (OSError, ValueError, RuntimeError) as error:
        message = str(error)
        print("Codex + HQ: " + message, file=sys.stderr)
        try:
            subprocess.run(["/usr/bin/osascript", "-e", 'display alert "Codex + HQ" message '
                            + json.dumps(message, ensure_ascii=False)], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise SystemExit(1) from None


def managed_launcher(target):
    try:
        return plistlib.loads((target / "Contents/Info.plist").read_bytes()).get("HQLauncher") == MARKER
    except (OSError, ValueError):
        return False


def uninstall_launcher(destination=None):
    target = Path(destination) if destination else Path.home() / "Applications/Codex + HQ.app"
    if target.is_symlink():
        raise ValueError("Refusing to remove a linked application")
    if target.exists():
        if not managed_launcher(target):
            raise ValueError("Refusing to remove a launcher not managed by HQ")
        shutil.rmtree(target)
    return {"removed": True, "message": "Open the original Codex app on the next start; history and settings are unchanged."}


def desktop_status(app=None):
    app, _, _ = native_paths(app)
    directory = runtime_directory(app)
    try:
        state = json.loads((directory / "runtime.json").read_text())
        # A live, initialized connection, rather than a PID or socket file, is
        # the proof. Metadata-only inventory never resumes historical chats.
        from .adapters import CodexClient, sync_codex
        client = CodexClient(state["socket"])
        try:
            result = sync_codex(Store(), client, state["socket"])
        finally:
            client.close()
        return {"runtime_connected": True, **result, "app": str(app)}
    except (OSError, ValueError, RuntimeError, KeyError):
        return {"runtime_connected": False, "app": str(app), "connected": 0,
                "next_step": "Finish active turns, quit Codex, then open Codex + HQ."}

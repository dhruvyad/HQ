"""Explicit local setup and per-conversation launchers."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from .adapters import CodexClient, sync_codex
from .store import Store, data_dir


def hq_command():
    source = Path(__file__).resolve().parents[1] / "bin/hq"
    return [sys.executable, str(source)] if source.exists() else [sys.executable, "-m", "hq"]


def write_config(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".hq-backup-" + str(time.time_ns()))
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    # Atomic replacement keeps client reloads from reading a partial config.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temp:
        json.dump(data, temp, indent=2)
        temp.write("\n")
    os.replace(temp.name, path)


def is_hq_hook(hook, provider):
    """HQ's hook for this agent wherever HQ was installed from: `.../bin/hq hook <agent>` or `... -m hq hook <agent>`."""
    command = hook.get("command") if isinstance(hook, dict) else None
    try:
        words = shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return False
    return len(words) >= 3 and words[-2:] == ["hook", provider] and (
        words[-3].endswith("/bin/hq") or words[-4:-2] == ["-m", "hq"])


def install_hooks(provider):
    home = Path.home()
    path = home / (".codex/hooks.json" if provider == "codex" else ".claude/settings.json")
    command = shlex.join(hq_command() + ["hook", provider])
    data = json.loads(path.read_text()) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    changed = False
    for event in ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop", "SessionEnd"]:
        groups = hooks.setdefault(event, [])
        mine = [h for group in groups for h in group.get("hooks", []) if is_hq_hook(h, provider)]
        if any(h.get("command") == command for h in mine):
            continue
        if mine:
            # HQ moved, say from a checkout to an installed copy: repoint its hook rather than run both.
            mine[0]["command"] = command
        else:
            groups.append({"hooks": [{"type": "command", "command": command, "timeout": 5}]})
        changed = True
    if changed:
        write_config(path, data)
    return {"hooks": "configured" if changed else "already configured", "path": str(path),
            "next_step": "Review and trust HQ hooks in Codex /hooks, then resume the conversation." if provider == "codex"
                else "Restart or resume Claude Code to load HQ hooks."}


def claude_settings():
    path = Path.home() / ".claude/settings.json"
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        raise ValueError(f"{path} is not valid JSON; fix it first, HQ will not overwrite it") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a settings object; HQ will not overwrite it")
    return path, data


def statusline_command():
    return shlex.join(hq_command() + ["statusline"])


def statusline_state():
    # Which status line is set, by comparing commands: a moved checkout still counts as HQ's.
    line = claude_settings()[1].get("statusLine")
    if not line:
        return "none"
    command = line.get("command") if isinstance(line, dict) else None
    return "hq" if isinstance(command, str) and command.endswith(("/bin/hq statusline", "-m hq statusline")) else "other"


def install_statusline():
    """Claude Code gives its rate limits only to a status line, so opting in means setting one."""
    path, data = claude_settings()
    state = statusline_state()
    if state == "hq":
        return {"status_line": "already configured", "path": str(path)}
    if state == "other":
        return {"status_line": "kept yours", "path": str(path),
                "next_step": f"Yours stays as it is. To also report usage to HQ, pipe its input to: {statusline_command()}"}
    data["statusLine"] = {"type": "command", "command": statusline_command()}
    write_config(path, data)
    return {"status_line": "configured", "path": str(path),
            "next_step": "Claude Code reports usage after each session's first response.",
            "note": "While any status line is set, Claude Code hides most footer hints, such as esc to interrupt. "
                    "hq usage uninstall brings them back."}


def uninstall_statusline():
    path, data = claude_settings()
    state = statusline_state()
    if state == "none":
        return {"status_line": "not configured", "path": str(path)}
    if state == "other":
        return {"status_line": "kept yours", "path": str(path)}
    del data["statusLine"]
    write_config(path, data)
    return {"status_line": "removed", "path": str(path)}


def connect_codex(store, endpoint=None):
    client = CodexClient(endpoint)
    try:
        result = sync_codex(store, client, endpoint)
        store.watch_codex(endpoint)
        return {**result, "watching": True,
                "message": "Only threads loaded on this server were bound. HQ will discover further loaded threads while running."}
    finally:
        client.close()


def connect_chatgpt(tunnel_id=None, key_ref="env:CONTROL_PLANE_API_KEY"):
    """Use OpenAI's supervised private stdio tunnel; never store a key value."""
    binary = shutil.which("tunnel-client")
    if not binary:
        raise ValueError("Install OpenAI tunnel-client from Platform tunnel settings first")
    tunnel_id = tunnel_id or os.environ.get("CONTROL_PLANE_TUNNEL_ID", "")
    if not re.fullmatch(r"tunnel_[A-Za-z0-9_-]+", tunnel_id):
        raise ValueError("Pass --tunnel-id from Platform tunnel settings, or set CONTROL_PLANE_TUNNEL_ID")
    if key_ref.startswith("env:") and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_ref[4:]):
        available = bool(os.environ.get(key_ref[4:]))
    elif key_ref.startswith("file:") and Path(key_ref[5:]).is_absolute():
        available = Path(key_ref[5:]).is_file() and Path(key_ref[5:]).stat().st_size > 0
    else:
        raise ValueError("Use a key reference: env:VARIABLE or file:/absolute/path; never pass the key itself")
    if not available:
        raise ValueError("The runtime key reference is unavailable. Configure it locally; do not paste credentials into HQ")
    def call(arguments):
        try:
            result = subprocess.run([binary, "runtimes", *arguments, "--json"],
                                    capture_output=True, text=True, timeout=45, check=True)
            return json.loads(result.stdout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            raise RuntimeError("Tunnel setup could not be verified. Inspect tunnel-client runtimes status hq --json before retrying") from None
    aliases = call(["list"]).get("aliases", [])
    existing = next((a for a in aliases if a.get("alias") == "hq"), None)
    if existing and existing.get("tunnel_id") != tunnel_id:
        raise ValueError("The hq tunnel alias already points to another tunnel; inspect it before changing its connection")
    profiles = data_dir() / "tunnel-profiles"
    profiles.mkdir(parents=True, exist_ok=True, mode=0o700)
    call(["connect", "--alias", "hq", "--tunnel-id", tunnel_id, "--profile", "hq",
          "--profile-dir", str(profiles), "--runtime-api-key", key_ref,
          "--mcp-command", shlex.join(hq_command() + ["mcp", "--provider", "chatgpt"])])
    status = call(["status", "hq"])
    health = {name: status.get(name) is True for name in ("process_running", "healthy", "ready")}
    return {"alias": "hq", "tunnel_id": tunnel_id, **health, "connected": all(health.values()),
            "next_step": "Add HQ as a Tunnel connection in ChatGPT Plugins, then test its tools in the intended chat."
                if all(health.values()) else "Inspect tunnel-client runtimes status hq --json; the tunnel is not verified ready.",
            "message": "This connects HQ tools. It does not inject turns into ordinary ChatGPT chats."}


def launch(provider, arguments, resume=None, endpoint=None):
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if provider == "codex":
        if resume:
            raise ValueError("For Codex, pass resume THREAD_ID after --")
        if not endpoint:
            subprocess.run(["codex", "app-server", "daemon", "start"], check=True)
        connect_codex(Store(), endpoint)
        return subprocess.call(["codex", "--remote", "unix://" + (endpoint or ""), *arguments])
    if endpoint:
        raise ValueError("--socket is for Codex only")
    if any(a.split("=", 1)[0] in {"--session-id", "--resume", "-r", "--continue", "-c", "--fork-session", "--mcp-config", "--dangerously-load-development-channels"} for a in arguments):
        raise ValueError("Use hq launch --resume UUID for an existing Claude conversation; HQ supplies its session/channel flags")
    if any(a in {"-p", "--print"} or a.startswith("--print=") for a in arguments):
        raise ValueError("Claude channels require an interactive session; use ordinary claude -p with HQ hooks instead")
    external_id = str(uuid.UUID(resume)) if resume else str(uuid.uuid4())
    sid = "claude-code:" + external_id
    config = {"mcpServers": {"hq-channel": {"command": hq_command()[0], "args": hq_command()[1:] +
        ["mcp", "--provider", "claude-code", "--channel", "--session-id", sid]}}}
    with tempfile.TemporaryDirectory(prefix="hq-claude-") as directory:
        path = Path(directory) / "mcp.json"
        path.write_text(json.dumps(config))
        os.chmod(path, 0o600)
        identity = ["--resume", external_id] if resume else ["--session-id", external_id]
        # Claude shows its own channel confirmation; no permission or trust bypass.
        return subprocess.call(["claude", *identity, "--mcp-config", str(path),
            "--dangerously-load-development-channels", "server:hq-channel", *arguments])

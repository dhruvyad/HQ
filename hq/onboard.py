"""One command to start using HQ, and one to undo it: hq setup and hq setup --undo.

Setup shows every change before making it, and each change is one HQ already offers on
its own (hq install, hq float install). Undo removes only what is HQ's, found by its
command, so your own hooks and servers stay as they are, and it keeps your inbox.
"""
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from .connections import is_hq_hook, statusline_state, uninstall_statusline, write_config
from .store import data_dir

AGENTS = {"claude-code": {"name": "Claude Code", "binary": "claude", "hooks": ".claude/settings.json",
                          "tools": "claude mcp add --scope user"},
          "codex": {"name": "Codex", "binary": "codex", "hooks": ".codex/hooks.json",
                    "tools": "~/.codex/config.toml"}}


def agents():
    """The agents installed on this machine, found by their command."""
    return [agent for agent, info in AGENTS.items() if shutil.which(info["binary"])]


def hook_file(agent):
    return Path.home() / AGENTS[agent]["hooks"]


def remove_hooks(agent):
    path = hook_file(agent)
    if not path.exists():
        return {"hooks": "none", "path": str(path)}
    data = json.loads(path.read_text())
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return {"hooks": "none", "path": str(path)}
    removed = 0
    for event in list(hooks):
        groups = hooks[event] if isinstance(hooks[event], list) else []
        kept, found = [], 0
        for group in groups:
            inner = group.get("hooks", []) if isinstance(group, dict) else []
            mine = [hook for hook in inner if is_hq_hook(hook, agent)]
            found += len(mine)
            if not mine:
                kept.append(group)
            elif len(mine) < len(inner):
                kept.append({**group, "hooks": [hook for hook in inner if hook not in mine]})
        removed += found
        if found:
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if not removed:
        return {"hooks": "none", "path": str(path)}
    if not hooks:
        del data["hooks"]
    write_config(path, data)
    return {"hooks": f"removed {removed}", "path": str(path)}


def swift_ready():
    # /usr/bin/swiftc exists on every Mac as a stub that offers to install the tools, so ask
    # xcode-select whether they are really there.
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        return False
    return subprocess.run(["xcode-select", "-p"], capture_output=True).returncode == 0


def plan(port, pill=True):
    """What setup will do here, as (what, how) pairs, before anything changes."""
    steps = [(f"Connect {AGENTS[agent]['name']}",
              f"add HQ's tools ({AGENTS[agent]['tools']}) and its hooks (~/{AGENTS[agent]['hooks']}); "
              "each file is backed up first")
             for agent in agents()]
    if pill and sys.platform == "darwin":
        steps.append(("Put the pill on screen", "now and at every login, over all your apps"))
    steps.append(("Open the dashboard", f"http://127.0.0.1:{port}, on this Mac only"))
    return steps


def ask(question):
    """Read the answer from the terminal itself, so this also works under `curl ... | sh`."""
    try:
        with open("/dev/tty") as answer, open("/dev/tty", "w") as prompt:
            prompt.write(question)
            prompt.flush()
            return answer.readline().strip().lower() in {"", "y", "yes"}
    except OSError:
        raise ValueError("hq setup needs a terminal to ask before it changes anything; "
                         "run it in one, or pass --yes") from None


def say(text=""):
    print(text, flush=True)


def setup(port, yes=False, pill=True, install=None, launch=None):
    from . import floating
    found = agents()
    steps = plan(port, pill)
    say("HQ setup will:")
    for n, (what, how) in enumerate(steps, 1):
        say(f"  {n}. {what}: {how}")
    if not found:
        say("  (No Claude Code or Codex found on your PATH. Install one, then run hq setup again.)")
    if not yes and not ask("Go ahead? [Y/n] "):
        say("Nothing changed.")
        return 1
    say()
    for agent in found:
        try:
            result = install(agent)["clients"][0]
        except (OSError, ValueError) as error:
            say(f"✗ {AGENTS[agent]['name']}: {error}")
            continue
        if result["status"] == "configured":
            tools = "added"
        elif "already exists" in result.get("message", ""):
            tools = "already there"
        else:
            tools = "not added (" + (result.get("message") or "no reason given")[:200] + ")"
        say(f"✓ {AGENTS[agent]['name']}: tools {tools}, hooks {result.get('hooks', 'unchanged')}")
    placed = False
    if pill and sys.platform == "darwin":
        if not swift_ready():
            say("· Pill skipped: it needs Apple's command line tools. Run xcode-select --install, then hq setup.")
        else:
            say("· Building the pill (once, about a minute)…")
            try:
                floating.install(port)
                placed = True
                say("✓ Pill on screen, and it starts at login")
            except ValueError as error:
                say(f"✗ Pill: {error}")
    launch(port)
    say("✓ Dashboard open")
    say()
    say("Next:")
    if found:
        say("  Start a new agent conversation, or restart one, and it reports to HQ.")
    if "codex" in found:
        say("  In Codex, open /hooks once and trust HQ's hooks.")
    if placed:
        say("  Hover the pill to see what each circle counts; click it to see what needs you.")
    say("  Claude usage in the pill is opt-in: hq usage login")
    say("  Update HQ later: hq update")
    say("  Undo all of this: hq setup --undo")
    return 0


def undo(stop_server):
    from . import floating
    from .usage import claude_login_enabled, set_claude_login
    for agent, info in AGENTS.items():
        if shutil.which(info["binary"]):
            remove = [info["binary"], "mcp", "remove", "hq"] + (["--scope", "user"] if agent == "claude-code" else [])
            gone = subprocess.run(remove, capture_output=True, text=True).returncode == 0
            say(f"✓ {info['name']}: tools {'removed' if gone else 'were not set'}, hooks {remove_hooks(agent)['hooks']}")
        elif hook_file(agent).exists():
            say(f"✓ {info['name']}: hooks {remove_hooks(agent)['hooks']}")
    if hook_file("claude-code").exists() and statusline_state() == "hq":
        uninstall_statusline()
        say("✓ Claude Code: HQ's status line removed")
    if claude_login_enabled():
        set_claude_login(False)
        say("✓ Claude usage login turned off")
    if sys.platform == "darwin":
        closed = floating.stop()["status"] == "stopped"
        removed = floating.uninstall()["status"] == "removed"
        if closed or removed:
            say("✓ Pill closed, and it no longer starts at login")
    if stop_server()["status"] == "stopped":
        say("✓ Dashboard stopped")
    say()
    say(f"Your inbox is kept at {data_dir()}. To delete it too: rm -rf {shlex.quote(str(data_dir()))}")
    say("To remove the hq command: uv tool uninstall hq-local")
    return 0

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from pathlib import Path

from .discovery import discover
from .mcp import AGENT_INSTRUCTIONS, run as run_mcp, with_notes
from .server import serve
from . import workstreams
from .store import Store, data_dir
from .connections import (connect_chatgpt, connect_codex, hq_command, install_hooks, install_statusline,
                          launch as launch_agent, statusline_state, uninstall_statusline)
from .hooks import run as run_hook
from . import usage as subscription_usage


def output(value):
    print(json.dumps(value, indent=2))


def running():
    try:
        state = json.loads((data_dir() / "server.json").read_text())
        with urllib.request.urlopen(f"http://127.0.0.1:{state['port']}/api/health", timeout=1) as response:
            health = json.load(response)
        return state if health.get("instance") == state["instance"] and health.get("pid") == state["pid"] else None
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return None


def ensure_server(port):
    """Start the local service if it is down, and return its state."""
    state = running()
    if state:
        return state
    home = data_dir()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = home / "server.log"
    with log_path.open("ab") as log:
        os.chmod(log_path, 0o600)
        executable = Path(__file__).resolve().parents[1] / "bin/hq"
        command = [sys.executable, str(executable)] if executable.exists() else [sys.executable, "-m", "hq"]
        child = subprocess.Popen(command + ["serve", "--port", str(port)], stdout=log, stderr=log, start_new_session=True)
    for _ in range(50):
        state = running()
        if state:
            return state
        if child.poll() is not None:
            raise ValueError(f"HQ could not start. See {log_path}")
        time.sleep(0.1)
    raise ValueError(f"HQ did not start in time. See {log_path}")


def stop_service():
    state = running()
    if state:
        os.kill(state["pid"], signal.SIGTERM)
        for _ in range(60):
            if not running():
                break
            time.sleep(0.1)
        else:
            raise ValueError("HQ is still shutting down; check hq status before restarting")
    return {"status": "stopped" if state else "not running"}


def launch(port, open_browser=True):
    state = ensure_server(port)
    url = f"http://127.0.0.1:{state['port']}"
    if open_browser:
        webbrowser.open(url)
    print(url)


def install(target):
    # Absolute interpreter and module, so clients find HQ even before its folder is on PATH.
    command = hq_command() + ["mcp", "--provider"]
    targets = ["codex", "claude-code", "claude-desktop"] if target == "all" else [target]
    results = []
    for client in targets:
        if client in {"codex", "claude-code"}:
            binary = "codex" if client == "codex" else "claude"
            if not shutil.which(binary):
                results.append({"client": client, "status": "not installed"})
                continue
            config = Path.home() / (".codex/config.toml" if client == "codex" else ".claude.json")
            if config.exists():
                shutil.copy2(config, config.with_name(config.name + ".hq-backup-" + str(int(time.time()))))
            args = [binary, "mcp", "add"]
            if client == "claude-code":
                args += ["--scope", "user"]
            args += ["hq", "--", *command, client]
            result = subprocess.run(args, capture_output=True, text=True)
            if client == "codex" and result.returncode == 0 and config.exists():
                # Only update the server entry the CLI just created for HQ.
                text = config.read_text()
                match = re.search(r"(?m)^\[mcp_servers\.hq\]\s*$", text)
                if match:
                    end = re.search(r"(?m)^\[", text[match.end():])
                    end = match.end() + end.start() if end else len(text)
                    section = text[match.start():end]
                    if re.search(r"(?m)^tool_timeout_sec\s*=", section):
                        section = re.sub(r"(?m)^tool_timeout_sec\s*=.*$", "tool_timeout_sec = 3600", section)
                    else:
                        section = section.rstrip() + "\ntool_timeout_sec = 3600\n\n"
                    config.write_text(text[:match.start()] + section + text[end:])
            results.append({"client": client, "status": "configured" if result.returncode == 0 else "unchanged",
                            "message": (result.stdout + result.stderr).strip(), **install_hooks(client)})
        else:
            config = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
            config.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(config.read_text()) if config.exists() else {}
            if "hq" in data.get("mcpServers", {}):
                results.append({"client": client, "status": "already configured; preserved existing hq entry"})
                continue
            if config.exists():
                shutil.copy2(config, config.with_name(config.name + ".hq-backup-" + str(int(time.time()))))
            data.setdefault("mcpServers", {})["hq"] = {"command": command[0], "args": [*command[1:], client]}
            config.write_text(json.dumps(data, indent=2) + "\n")
            results.append({"client": client, "status": "configured; restart Claude Desktop to load"})
    return {"clients": results, "message": "Reload MCP tools or restart sessions to load HQ. Existing conversations are not interrupted."}


class Parser(argparse.ArgumentParser):
    """Whatever follows `--` on `hq launch` goes to the agent untouched.

    Before Python 3.12, argparse fills an optional positional early and then rejects
    these as unrecognized, so split them off before it sees them.
    """
    def parse_args(self, args=None, namespace=None):
        args = list(sys.argv[1:] if args is None else args)
        if args[:1] != ["launch"] or "--" not in args:
            return super().parse_args(args, namespace)
        at = args.index("--")
        parsed = super().parse_args(args[:at], namespace)
        parsed.arguments = args[at + 1:]
        return parsed


def parser():
    root = Parser(prog="hq", description="Your local inbox for AI agents. Run hq to open the dashboard.")
    root.add_argument("--version", action="version", version="hq 0.1.0")
    commands = root.add_subparsers(dest="command")
    start = commands.add_parser("start", help="Start HQ in the background and open the dashboard")
    start.add_argument("--port", type=int, default=4310)
    start.add_argument("--no-open", action="store_true")
    server = commands.add_parser("serve", help="Run the local service in the foreground")
    server.add_argument("--port", type=int, default=4310)
    commands.add_parser("stop", help="Stop the local dashboard service")
    commands.add_parser("status", help="Show service and database status")
    commands.add_parser("instructions", help="Read the human-readable reporting format")
    report = commands.add_parser("report", help="Report a structured item from a JSON file")
    report.add_argument("file", type=Path)
    report.add_argument("--session", required=True)
    session = commands.add_parser("session", help="Register or list conversations")
    sub = session.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    add_session = sub.add_parser("add")
    add_session.add_argument("id")
    add_session.add_argument("--name", required=True)
    add_session.add_argument("--provider", default="other", choices=["other", "codex", "claude-code", "claude-desktop", "chatgpt"])
    add_session.add_argument("--external-id")
    add_session.add_argument("--cwd", default=str(Path.cwd()))
    for command in ("add", "ask"):
        action = commands.add_parser(command, help="Report work" if command == "add" else "Ask for human input and receive the answer")
        action.add_argument("title")
        action.add_argument("--session", required=True)
        action.add_argument("--detail", default="")
        action.add_argument("--tags", default="", help="Comma-separated workstream tags")
        action.add_argument("--fields", type=Path, help="JSON file with readable fields: steps, matters, needs_you, minutes, …")
        if command == "add":
            action.add_argument("--kind", choices=["todo", "blocker", "question"], default="todo")
            action.add_argument("--owner", choices=["human", "agent", "external"])
            action.add_argument("--waiting-on", default="")
            action.add_argument("--defer-until", default="", help="Show again on YYYY-MM-DD")
        else:
            action.add_argument("--option", action="append", default=[])
            action.add_argument("--timeout", type=float, default=3600)
            action.add_argument("--no-wait", action="store_true")
    update = commands.add_parser("update", help="Update one existing open item")
    update.add_argument("item_id")
    update.add_argument("--session", required=True)
    update.add_argument("--owner", choices=["human", "agent", "external"])
    update.add_argument("--waiting-on")
    update.add_argument("--defer-until", help="YYYY-MM-DD; empty clears deferral")
    update.add_argument("--title")
    update.add_argument("--detail")
    listing = commands.add_parser("list", help="List items, with optional filters")
    listing.add_argument("--session")
    listing.add_argument("--tag")
    listing.add_argument("--status", choices=["open", "answered", "done"])
    listing.add_argument("--bucket", choices=["needs", "pending", "waiting", "later", "done"])
    reply = commands.add_parser("reply", help="Save a human answer and deliver it to the owning conversation")
    reply.add_argument("item_id")
    reply.add_argument("text")
    question = commands.add_parser("question", help="Ask the agent for clarification on an unfinished item")
    question.add_argument("item_id")
    question.add_argument("text")
    comments = commands.add_parser("comments", help="Read this session's item conversations")
    comments.add_argument("--session", required=True)
    comments.add_argument("--item", dest="item_id")
    comments.add_argument("--pending", action="store_true")
    respond = commands.add_parser("respond", help="Answer a human clarification in its item thread")
    respond.add_argument("comment_id")
    respond.add_argument("text")
    respond.add_argument("--session", required=True)
    for name in ("comment-ack", "comment-retry", "comment-cancel"):
        command = commands.add_parser(name)
        command.add_argument("comment_id")
        if name == "comment-ack":
            command.add_argument("--session", required=True)
    wait = commands.add_parser("await", help="Wait on an existing question without duplicating it")
    wait.add_argument("item_id")
    wait.add_argument("--session", required=True)
    wait.add_argument("--timeout", type=float, default=3600)
    inbox = commands.add_parser("inbox", help="Read responses saved for one conversation")
    inbox.add_argument("--session", required=True)
    adoptable = commands.add_parser("adoptable", help="List decisions in this conversation's project whose own agent stopped first")
    adoptable.add_argument("--session", required=True)
    adopt = commands.add_parser("adopt", help="Take over a decision whose own agent stopped first; act on it, then hq done")
    adopt.add_argument("item_id")
    adopt.add_argument("--session", required=True)
    for command in ("ack", "done"):
        action = commands.add_parser(command, help="Acknowledge a response" if command == "ack" else "Complete an item")
        action.add_argument("item_id")
        action.add_argument("--session", required=True)
        if command == "done":
            action.add_argument("--note", default="")
    retry = commands.add_parser("retry", help="Retry a failed native delivery after checking the conversation")
    retry.add_argument("item_id")
    discovery = commands.add_parser("discover", help="Import recent local conversation metadata; no transcript scanning")
    discovery.add_argument("--limit", type=int, default=50)
    integration = commands.add_parser("install", help="Configure local MCP clients, preserving and backing up their settings")
    integration.add_argument("target", choices=["all", "codex", "claude-code", "claude-desktop"])
    mcp = commands.add_parser("mcp", help="Serve agent tools over stdio")
    mcp.add_argument("--provider", default="other")
    mcp.add_argument("--channel", action="store_true", help="Enable Claude Code channel notifications (requires client opt-in)")
    mcp.add_argument("--session-id", help="Bind a channel to this stable conversation id")
    connect = commands.add_parser("connect", help="Connect an owning Codex server or a private ChatGPT tool tunnel")
    connect.add_argument("provider", choices=["codex", "chatgpt"])
    connect.add_argument("--socket", help="Control socket of the app-server that owns the conversations")
    connect.add_argument("--tunnel-id", help="Existing OpenAI tunnel id (or CONTROL_PLANE_TUNNEL_ID)")
    connect.add_argument("--key-ref", default="env:CONTROL_PLANE_API_KEY", help="Runtime key reference, env:VARIABLE or file:/absolute/path")
    hook = commands.add_parser("hook", help="Receive a native client lifecycle event on stdin")
    hook.add_argument("provider", choices=["codex", "claude-code"])
    agent = commands.add_parser("launch", help="Launch an agent with an exact HQ conversation connection")
    agent.add_argument("provider", choices=["codex", "codex-desktop", "claude-code"])
    agent.add_argument("--app", help="Codex desktop .app installation")
    agent.add_argument("--resume", help="Claude Code conversation UUID to resume")
    agent.add_argument("--socket", help="Existing Codex owning control socket")
    agent.add_argument("arguments", nargs="*", help="Agent arguments after --")
    proxy = commands.add_parser("codex-desktop-proxy", help="Experimental desktop stdio bridge to its shared runtime")
    proxy.add_argument("--socket", required=True)
    desktop = commands.add_parser("desktop", help="Install, verify, or remove the Codex + HQ launcher")
    desktop.add_argument("action", choices=["install", "status", "uninstall"])
    desktop.add_argument("--app", help="Codex desktop .app installation")
    onboarding = commands.add_parser("setup", help="Connect the agents you use, put the pill on screen and open the dashboard; "
                                                   "shows each change first")
    onboarding.add_argument("--undo", action="store_true", help="Remove what setup added; your inbox is kept")
    onboarding.add_argument("--yes", action="store_true", help="Do not ask first")
    onboarding.add_argument("--no-pill", action="store_true", help="Skip the floating pill")
    onboarding.add_argument("--port", type=int, default=4310)
    plan = commands.add_parser("usage", help="Show subscription usage left; install connects Claude Code's status line, "
                                             "login reads Claude usage with Claude Code's saved login instead")
    plan.add_argument("action", nargs="?", default="status", choices=["status", "install", "uninstall", "login", "logout"])
    commands.add_parser("statusline", help="Record Claude Code's rate limits from its status line; Claude Code runs this")
    people = commands.add_parser("accounts", help="Your Claude and Codex accounts: list them, add one with a browser sign-in, "
                                                  "switch the real login to one, or remove one")
    people.add_argument("action", nargs="?", default="list", choices=["list", "add", "use", "remove"])
    people.add_argument("tool", nargs="?", choices=["claude", "codex"])
    people.add_argument("who", nargs="?", help="use/remove: the account's email or key")
    panel = commands.add_parser("float", help="Show the always-on-top HQ window (macOS)")
    panel.add_argument("action", nargs="?", default="start", choices=["start", "stop", "status", "install", "uninstall", "lab", "run"],
                       help="install starts it at login; lab tries designs on throwaway data; run is for launchd")
    panel.add_argument("--port", type=int, default=4310)
    panel.add_argument("--item", help="lab: reply to this HQ item with your picks")
    return root


def usage_command(action):
    if action == "install":
        return install_statusline()
    if action == "uninstall":
        return uninstall_statusline()
    if action in {"login", "logout"}:
        return subscription_usage.set_claude_login(action == "login")
    login = subscription_usage.claude_login_enabled()
    claude = (subscription_usage.ClaudeLogin().read() if login else None) or subscription_usage.fresh(subscription_usage.read_claude())
    return {"claude": claude, "codex": subscription_usage.read_codex(),
            "claude_login": "on" if login else "off", "claude_status_line": statusline_state()}


def accounts_command(args):
    from .accounts import AccountError, Accounts
    accounts = Accounts(Store())
    if args.action == "list":
        return accounts.list()
    if not args.tool:
        raise ValueError(f"Say which tool: hq accounts {args.action} claude|codex")
    if args.action == "add":
        job = accounts.add(args.tool)
        print(job["message"], file=sys.stderr, flush=True)
        try:
            while job["state"] in {"starting", "waiting"}:
                time.sleep(1)
                job = accounts.job(job["id"])
        except KeyboardInterrupt:
            accounts.cancel(job["id"])
            while accounts.job(job["id"])["state"] in {"starting", "waiting"}:
                time.sleep(0.2)
            raise ValueError("Cancelled") from None
        if job["state"] != "added":
            raise ValueError(job["message"])
        return job
    if not args.who:
        raise ValueError(f"Say which account: hq accounts {args.action} {args.tool} <email or key>")
    wanted = args.who.lower()
    matches = [a for a in accounts.list()[args.tool]["accounts"] if a["saved"] and wanted in {a["key"], a["email"].lower()}]
    if len(matches) != 1:
        raise AccountError(f"No single saved {args.tool} account matches {args.who}")
    key = matches[0]["key"]
    return accounts.use(args.tool, key) if args.action == "use" else accounts.remove(args.tool, key)


def float_panel(args):
    from . import floating
    if args.item and args.action != "lab":
        raise ValueError("--item is for hq float lab")
    if args.action == "start":
        return floating.start(args.port, ensure_server)
    if args.action == "run":
        return floating.run(args.port, ensure_server)
    if args.action == "install":
        return floating.install(args.port)
    if args.action == "lab":
        return floating.lab(args.item)
    return {"stop": floating.stop, "status": floating.status, "uninstall": floating.uninstall}[args.action]()


def main():
    args = parser().parse_args()
    try:
        if args.command in {None, "start"}:
            return launch(getattr(args, "port", 4310), not getattr(args, "no_open", False))
        if args.command == "serve":
            return serve(args.port)
        if args.command == "mcp":
            if args.channel and not args.session_id:
                raise ValueError("Channel mode requires --session-id to avoid routing to a different conversation")
            return run_mcp(args.provider, args.channel, args.session_id)
        if args.command == "install":
            return output(install(args.target))
        if args.command == "setup":
            from . import onboard
            if args.undo:
                return sys.exit(onboard.undo(stop_service))
            return sys.exit(onboard.setup(args.port, args.yes, not args.no_pill, install, launch))
        if args.command == "hook":
            return run_hook(args.provider)
        if args.command == "launch":
            if args.provider == "codex-desktop":
                if args.resume or args.socket:
                    raise ValueError("Desktop connects existing chats automatically; resume/socket options are for CLI clients")
                from .desktop_launch import launch_desktop
                return output(launch_desktop(args.app, arguments=args.arguments))
            if args.app:
                raise ValueError("--app is for Codex desktop only")
            return sys.exit(launch_agent(args.provider, args.arguments, args.resume, args.socket))
        if args.command == "codex-desktop-proxy":
            from .desktop import bridge
            return bridge(args.socket)
        if args.command == "desktop":
            from .desktop_launch import desktop_status, install_launcher, uninstall_launcher
            if args.action == "uninstall":
                return output(uninstall_launcher())
            return output(install_launcher(args.app) if args.action == "install" else desktop_status(args.app))
        if args.command == "float":
            return output(float_panel(args))
        if args.command == "statusline":
            return subscription_usage.statusline()
        if args.command == "usage":
            return output(usage_command(args.action))
        if args.command == "accounts":
            return output(accounts_command(args))
        store = Store()
        if args.command == "instructions":
            print(AGENT_INSTRUCTIONS)
            print("Projects and their workstreams (reuse one when you report):\n")
            for project in workstreams.index(store.snapshot()["items"]).values():
                print(f"- {project['name']}: {', '.join(project['workstreams'])}")
            return
        if args.command == "report":
            fields = json.loads(args.file.read_text())
            if not isinstance(fields, dict) or "session_id" in fields:
                raise ValueError("Report file must be an item object; pass the conversation with --session")
            try:
                return output(with_notes(store.add(args.session, **fields, readable=True)))
            except TypeError as error:
                raise ValueError(f"Invalid report fields: {error}") from None
        if args.command == "stop":
            return output(stop_service())
        if args.command == "status":
            return output({"server": running(), "database": str(store.path)})
        if args.command == "session":
            return output(store.register(args.id, args.name, args.provider, args.external_id, args.cwd)
                          if args.action == "add" else store.snapshot()["sessions"])
        if args.command in {"add", "ask"}:
            if args.command == "ask" and not 0 < args.timeout <= 86400:
                raise ValueError("timeout must be between 1 and 86400 seconds")
            fields = json.loads(args.fields.read_text()) if args.fields else {}
            if not isinstance(fields, dict) or set(fields) & {"session_id", "title", "detail", "kind", "tags", "options", "owner", "waiting_on", "defer_until"}:
                raise ValueError("--fields holds readable fields only (steps, matters, needs_you, minutes, …); pass the rest as flags")
            item = store.add(args.session, args.title, args.detail,
                             getattr(args, "kind", "question"), [t.strip() for t in args.tags.split(",") if t.strip()],
                             getattr(args, "option", []), getattr(args, "owner", None),
                             getattr(args, "waiting_on", ""), getattr(args, "defer_until", ""), **fields, readable=True)
            if args.command == "ask" and not args.no_wait:
                print(f"Waiting for a human answer in HQ. Item: {item['id']}", file=sys.stderr)
                return output(store.wait(item["id"], args.session, args.timeout))
            return output(with_notes(item))
        if args.command == "await":
            return output(store.wait(args.item_id, args.session, args.timeout))
        if args.command == "update":
            changes = {key: getattr(args, key) for key in ("owner", "waiting_on", "defer_until", "title", "detail") if getattr(args, key) is not None}
            return output(store.update(args.item_id, args.session, **changes))
        if args.command == "list":
            return output(store.snapshot(args.session, args.tag, args.status, args.bucket)["items"])
        if args.command == "reply":
            return output(store.reply(args.item_id, args.text))
        if args.command == "question":
            return output(store.ask_agent(args.item_id, args.text))
        if args.command == "comments":
            return output(store.comments(args.session, args.item_id, args.pending))
        if args.command == "respond":
            return output(store.respond(args.comment_id, args.session, args.text))
        if args.command == "comment-ack":
            return output(store.acknowledge_comment(args.comment_id, args.session))
        if args.command == "comment-retry":
            return output(store.retry_comment(args.comment_id))
        if args.command == "comment-cancel":
            return output(store.cancel_comment(args.comment_id))
        if args.command == "inbox":
            return output(store.inbox(args.session))
        if args.command == "adoptable":
            from .mcp import adoptable_entry
            return output([adoptable_entry(i) for i in store.adoptable(args.session)])
        if args.command == "adopt":
            return output(store.adopt(args.item_id, args.session))
        if args.command in {"ack", "done"}:
            if store.item(args.item_id)["session_id"] != args.session:
                raise ValueError("Item belongs to another session")
            return output(store.acknowledge(args.item_id, args.session) if args.command == "ack" else store.resolve(args.item_id, args.note))
        if args.command == "retry":
            return output(store.retry(args.item_id))
        if args.command == "discover":
            if not 1 <= args.limit <= 500:
                raise ValueError("limit must be between 1 and 500")
            return output(discover(store, args.limit))
        if args.command == "connect":
            if args.provider == "chatgpt":
                if args.socket:
                    raise ValueError("--socket is for Codex only")
                return output(connect_chatgpt(args.tunnel_id, args.key_ref))
            if args.tunnel_id or args.key_ref != "env:CONTROL_PLANE_API_KEY":
                raise ValueError("Tunnel options are for ChatGPT only")
            return output(connect_codex(store, args.socket))
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"hq: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("HQ interrupted; saved items and responses are preserved.", file=sys.stderr)
        sys.exit(130)

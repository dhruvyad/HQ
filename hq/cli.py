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

from .adapters import CodexClient
from .discovery import discover
from .mcp import run as run_mcp
from .server import serve
from .store import Store, data_dir


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


def launch(port, open_browser=True):
    state = running()
    if not state:
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
                break
            if child.poll() is not None:
                raise ValueError(f"HQ could not start. See {log_path}")
            time.sleep(0.1)
        if not state:
            raise ValueError(f"HQ did not start in time. See {log_path}")
    url = f"http://127.0.0.1:{state['port']}"
    if open_browser:
        webbrowser.open(url)
    print(url)


def install(target):
    executable = str(Path(__file__).resolve().parents[1] / "bin/hq")
    if not Path(executable).exists():
        executable = shutil.which("hq")
    if not executable:
        raise ValueError("Install hq on your PATH first")
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
            args += ["hq", "--", sys.executable, executable, "mcp", "--provider", client]
            result = subprocess.run(args, capture_output=True, text=True)
            if client == "codex" and result.returncode == 0:
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
                            "message": (result.stdout + result.stderr).strip()})
        else:
            config = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
            config.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(config.read_text()) if config.exists() else {}
            if "hq" in data.get("mcpServers", {}):
                results.append({"client": client, "status": "already configured; preserved existing hq entry"})
                continue
            if config.exists():
                shutil.copy2(config, config.with_name(config.name + ".hq-backup-" + str(int(time.time()))))
            data.setdefault("mcpServers", {})["hq"] = {"command": sys.executable, "args": [executable, "mcp", "--provider", client]}
            config.write_text(json.dumps(data, indent=2) + "\n")
            results.append({"client": client, "status": "configured; restart Claude Desktop to load"})
    return {"clients": results, "message": "Reload MCP tools or restart sessions to load HQ. Existing conversations are not interrupted."}


def parser():
    root = argparse.ArgumentParser(prog="hq", description="Your local inbox for AI agents. Run hq to open the dashboard.")
    root.add_argument("--version", action="version", version="hq 0.1.0")
    commands = root.add_subparsers(dest="command")
    start = commands.add_parser("start", help="Start HQ in the background and open the dashboard")
    start.add_argument("--port", type=int, default=4310)
    start.add_argument("--no-open", action="store_true")
    server = commands.add_parser("serve", help="Run the local service in the foreground")
    server.add_argument("--port", type=int, default=4310)
    commands.add_parser("stop", help="Stop the local dashboard service")
    commands.add_parser("status", help="Show service and database status")
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
    wait = commands.add_parser("await", help="Wait on an existing question without duplicating it")
    wait.add_argument("item_id")
    wait.add_argument("--session", required=True)
    wait.add_argument("--timeout", type=float, default=3600)
    inbox = commands.add_parser("inbox", help="Read responses saved for one conversation")
    inbox.add_argument("--session", required=True)
    for command in ("ack", "done"):
        action = commands.add_parser(command, help="Acknowledge a response" if command == "ack" else "Complete an item")
        action.add_argument("item_id")
        action.add_argument("--session", required=True)
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
    connect = commands.add_parser("connect", help="Connect to a running Codex app-server for automatic user turns")
    connect.add_argument("provider", choices=["codex"])
    connect.add_argument("--socket", help="Control socket of the app-server that owns the conversations")
    return root


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
        store = Store()
        if args.command == "stop":
            state = running()
            if state:
                os.kill(state["pid"], signal.SIGTERM)
                for _ in range(60):
                    if not running():
                        break
                    time.sleep(0.1)
                else:
                    raise ValueError("HQ is still shutting down; check hq status before restarting")
            return output({"status": "stopped" if state else "not running"})
        if args.command == "status":
            return output({"server": running(), "database": str(store.path)})
        if args.command == "session":
            return output(store.register(args.id, args.name, args.provider, args.external_id, args.cwd)
                          if args.action == "add" else store.snapshot()["sessions"])
        if args.command in {"add", "ask"}:
            if args.command == "ask" and not 0 < args.timeout <= 86400:
                raise ValueError("timeout must be between 1 and 86400 seconds")
            item = store.add(args.session, args.title, args.detail,
                             getattr(args, "kind", "question"), [t.strip() for t in args.tags.split(",") if t.strip()],
                             getattr(args, "option", []), getattr(args, "owner", None),
                             getattr(args, "waiting_on", ""), getattr(args, "defer_until", ""))
            if args.command == "ask" and not args.no_wait:
                print(f"Waiting for a human answer in HQ. Item: {item['id']}", file=sys.stderr)
                return output(store.wait(item["id"], args.session, args.timeout))
            return output(item)
        if args.command == "await":
            return output(store.wait(args.item_id, args.session, args.timeout))
        if args.command == "update":
            changes = {key: getattr(args, key) for key in ("owner", "waiting_on", "defer_until", "title", "detail") if getattr(args, key) is not None}
            return output(store.update(args.item_id, args.session, **changes))
        if args.command == "list":
            return output(store.snapshot(args.session, args.tag, args.status, args.bucket)["items"])
        if args.command == "reply":
            return output(store.reply(args.item_id, args.text))
        if args.command == "inbox":
            return output(store.inbox(args.session))
        if args.command in {"ack", "done"}:
            if store.item(args.item_id)["session_id"] != args.session:
                raise ValueError("Item belongs to another session")
            return output(store.acknowledge(args.item_id, args.session) if args.command == "ack" else store.resolve(args.item_id))
        if args.command == "retry":
            return output(store.retry(args.item_id))
        if args.command == "discover":
            if not 1 <= args.limit <= 500:
                raise ValueError("limit must be between 1 and 500")
            return output(discover(store, args.limit))
        if args.command == "connect":
            client = CodexClient(args.socket)
            try:
                ids, cursor = [], None
                while True:
                    params = {"limit": 100, "sourceKinds": ["cli", "vscode", "appServer"], "useStateDbOnly": True}
                    if cursor:
                        params["cursor"] = cursor
                    result = client.call("thread/list", params)
                    for thread in result["data"]:
                        id = "codex:" + thread["id"]
                        name = (thread.get("name") or thread.get("preview") or thread["id"]).strip()[:500]
                        store.register(id, name or thread["id"], "codex", thread["id"], thread.get("cwd"))
                        store.bind_codex(id, thread["id"], args.socket)
                        ids.append(id)
                    cursor = result.get("nextCursor")
                    if not cursor:
                        break
                output({"connected": len(ids), "message": "Start hq to dispatch saved answers through this Codex control socket."})
            finally:
                client.close()
    except (ValueError, OSError, RuntimeError) as error:
        print(f"hq: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("HQ interrupted; saved items and responses are preserved.", file=sys.stderr)
        sys.exit(130)

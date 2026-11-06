"""Supported lifecycle context, without transcript scanning or idle push claims."""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import sys

from .store import Store, required

EVENTS = {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop", "SessionEnd"}
# Background work that is an agent at work. A dev server or a watch can run for days,
# so a background shell or monitor alone does not make a conversation active.
AGENT_WORK = {"subagent", "workflow", "teammate", "cloud session"}
# Subagents report each tool call, which renews this; a final Stop or SubagentStop ends it.
# Background observations rank below everything else: they only ever add "still working"
# to a conversation that is otherwise idle (see Store.attach_activity).
BACKGROUND_LEASE = 900


def agents_working(payload):
    tasks = payload.get("background_tasks")
    return isinstance(tasks, list) and any(isinstance(task, dict) and task.get("type") in AGENT_WORK for task in tasks)


# Codex names a conversation after its first message, so by the prompt or the end of a turn it has one.
NAMED = {"SessionStart", "UserPromptSubmit", "Stop"}


def codex_title(thread_id, home=None):
    """The name Codex shows for one of its conversations, read-only from its own metadata (as
    hq discover does): the name ChatGPT gave it, else its first line. None when it has none yet."""
    folder = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    versions = sorted(folder.glob("state_*.sqlite"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))
    if not versions:
        return None
    try:
        with closing(sqlite3.connect(versions[-1].resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    for column in ("name", "title"):
        text = row[column] if column in row.keys() else None
        line = " ".join(str(text or "").split("\n", 1)[0].split())
        if line:
            return line if len(line) <= 80 else line[:79].rstrip() + "…"
    return None


def handle(provider, payload, store, emit, title=codex_title):
    if provider not in {"codex", "claude-code"} or not isinstance(payload, dict):
        raise ValueError("Expected a Codex or Claude Code hook event")
    event = payload.get("hook_event_name")
    if event not in EVENTS:
        raise ValueError("Unsupported HQ hook event")
    external_id = required(payload.get("session_id"), "session id", 150)
    sid = f"{provider}:{external_id}"
    # Codex runs short helper conversations of its own (naming a chat, for one) that it never
    # saves, so they have no transcript. They are not yours: they never become agents.
    if provider == "codex" and "transcript_path" in payload and payload["transcript_path"] is None:
        return
    # A subagent's events carry the parent's session id. They must never consume the
    # parent's messages, but they do show the conversation is still at work.
    if payload.get("agent_id") or event == "SubagentStop":
        try:
            store.session(sid)
        except ValueError:
            return
        working = event != "SubagentStop" or agents_working(payload)
        store.observe_activity(sid, "active" if working else "idle", "background",
                               lease=BACKGROUND_LEASE if working else 6, priority=0)
        return
    cwd = payload.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError("cwd must be text")
    # A conversation goes by the name its app shows, and follows it when it changes: for Codex the
    # one ChatGPT gave it; for Claude Code a title you set (/rename, --name), which Claude Code
    # sends with these events. Its generated title it keeps to itself.
    if provider == "codex":
        named = title(external_id) if event in NAMED else None
    else:
        named = payload.get("session_title")
        named = " ".join(named.split())[:80] if isinstance(named, str) and named.strip() else None
    try:
        session = store.session(sid)
    except ValueError:
        label = "Codex" if provider == "codex" else "Claude Code"
        session = store.register(sid, named or f"{label} · {Path(cwd).name if cwd else external_id[:8]}", provider, external_id, cwd)
    if named and session["name"] != named:
        session = store.register(sid, named, provider, external_id, cwd)
    if event == "SessionEnd":
        store.disconnect(sid, "hooks")
        store.observe_activity(sid, "offline", "hooks", lease=30, priority=50)
        store.observe_activity(sid, "idle", "background", lease=6, priority=0)
        return
    if event == "Stop":
        # A turn can end with subagents still running: the conversation is still at work.
        working = agents_working(payload)
        store.observe_activity(sid, "active" if working else "idle", "background",
                               lease=BACKGROUND_LEASE if working else 6, priority=0)
    store.heartbeat(sid, "hooks")
    replies, questions = store.claim_hook_messages(sid)
    activity = "active" if event in {"UserPromptSubmit", "PostToolUse"} or (event == "Stop" and (replies or questions)) else "idle"
    # Lifecycle evidence expires: it cannot prove a process stayed alive forever.
    store.observe_activity(sid, activity, "hooks", lease=30, priority=50)
    quoted = shlex.quote(sid)
    context = []
    if event in {"SessionStart", "UserPromptSubmit"}:
        context.append(
            f"This exact conversation is registered in HQ as {sid}. "
            "Use this session id with all HQ CLI/MCP calls. Read hq_reporting_guide or `hq instructions`; "
            "report every unfinished action/blocker separately with workstream tags and the right owner. "
            "When you need the user to decide, approve or do something, ask it in HQ (hq_report: a question with "
            "options, or a todo) and say in the chat that it is waiting there; the user may not be watching this chat. "
            "Keep existing items updated. Check and answer HQ clarification threads before finishing. "
            f"CLI: hq list --session {quoted}; hq comments --session {quoted} --pending; hq inbox --session {quoted}."
        )
    # Decisions this conversation heard but never closed: the agent is reminded, at most every few hours.
    if event in {"SessionStart", "UserPromptSubmit"}:
        for item in store.unclosed(sid):
            context.append(f"HQ reminder (item {item['item_id']}): you heard the user's answer to " + json.dumps(item["title"], ensure_ascii=False)
                           + " but never closed it. Answer: " + json.dumps(item["answer"][:600], ensure_ascii=False)
                           + f"\nIf it is done, close it with hq done {shlex.quote(item['item_id'])} --session {quoted} --note 'what you did'. "
                           + "If not, do it now, or tell the user why you can't. Until you close it, HQ counts the decision as not acted on.")
    # Decisions whose own agent stopped first are offered to the next agent in the same project.
    if event == "SessionStart":
        offered = store.adoptable(sid)
        if offered:
            lines = [f"- {i['title']} (item {i['id']}): " + json.dumps((i["answer"] or "")[:300], ensure_ascii=False) for i in offered[:5]]
            more = f"\n…and {len(offered) - 5} more: hq adoptable --session {quoted}" if len(offered) > 5 else ""
            context.append(f"HQ: the user decided {len(offered)} thing{'s' if len(offered) != 1 else ''} in this project for agents that stopped before acting on them:\n"
                           + "\n".join(lines) + more
                           + f"\nTell the user. Adopt the ones that fit your task, or that the user asks you to, with hq adopt <item> --session {quoted}; "
                           + "act on each one and close it with hq done. Leave the rest for another agent.")
    for reply in replies:
        context.append(f"HQ human response (item {reply['item_id']}): " + json.dumps({"title": reply["title"], "answer": reply["text"][:1600]}, ensure_ascii=False)
                       + f"\nRead the full response with hq inbox --session {quoted}, then acknowledge it with "
                       + f"hq ack {shlex.quote(reply['item_id'])} --session {quoted}. Act on the response and mark done only after the work is complete.")
    for question in questions:
        context.append(f"HQ human clarification (comment {question['id']}, item {question['item_id']}): "
                       + json.dumps({"title": question["title"], "question": question["text"][:1600]}, ensure_ascii=False)
                       + f"\nRead the full question with hq comments --session {quoted} --pending. "
                       + f"Reply with hq_comment_reply or hq respond {shlex.quote(question['id'])} 'Your reply' --session {quoted}. This does not complete the item.")
    if not context:
        return
    text = "\n\n".join(context)
    if event == "Stop":
        # Both runtimes support this continuation shape; do not spin on sent messages.
        output = {"decision": "block", "reason": text}
    else:
        output = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    try:
        emit(output)
    except Exception:
        for reply in replies:
            store.delivery_result(reply["id"], "failed", "HQ hook output interrupted; inspect the conversation before retrying.")
        for question in questions:
            store.comment_delivery(question["id"], "failed", "HQ hook output interrupted; inspect the conversation before retrying.")
        raise
    for reply in replies:
        store.delivery_result(reply["id"], "sent")
    for question in questions:
        store.comment_delivery(question["id"], "sent")


def run(provider):
    try:
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Hook input is too large")
        def emit(value):
            print(json.dumps(value), flush=True)
        handle(provider, json.loads(raw), Store(), emit)
    except Exception as error:
        # A broken inbox must never block the agent's ordinary work.
        print(f"HQ hook unavailable: {error}", file=sys.stderr)

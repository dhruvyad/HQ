"""Supported lifecycle context, without transcript scanning or idle push claims."""
import json
from pathlib import Path
import shlex
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


def handle(provider, payload, store, emit):
    if provider not in {"codex", "claude-code"} or not isinstance(payload, dict):
        raise ValueError("Expected a Codex or Claude Code hook event")
    event = payload.get("hook_event_name")
    if event not in EVENTS:
        raise ValueError("Unsupported HQ hook event")
    external_id = required(payload.get("session_id"), "session id", 150)
    sid = f"{provider}:{external_id}"
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
    try:
        session = store.session(sid)
    except ValueError:
        label = "Codex" if provider == "codex" else "Claude Code"
        session = store.register(sid, f"{label} · {Path(cwd).name if cwd else external_id[:8]}", provider, external_id, cwd)
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
            "Keep existing items updated. Check and answer HQ clarification threads before finishing. "
            f"CLI: hq list --session {quoted}; hq comments --session {quoted} --pending; hq inbox --session {quoted}."
        )
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

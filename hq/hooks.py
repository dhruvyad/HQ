"""Supported lifecycle context, without transcript scanning or idle push claims."""
import json
from pathlib import Path
import shlex
import sys

from .store import Store, required

EVENTS = {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"}


def handle(provider, payload, store, emit):
    if provider not in {"codex", "claude-code"} or not isinstance(payload, dict):
        raise ValueError("Expected a Codex or Claude Code hook event")
    event = payload.get("hook_event_name")
    if event not in EVENTS:
        raise ValueError("Unsupported HQ hook event")
    external_id = required(payload.get("session_id"), "session id", 150)
    # Parent-context callbacks must never consume a child's messages (or vice versa).
    if payload.get("agent_id"):
        return
    sid = f"{provider}:{external_id}"
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
        return
    store.heartbeat(sid, "hooks")
    replies, questions = store.claim_hook_messages(sid)
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

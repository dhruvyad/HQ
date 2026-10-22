"""Local newline-delimited MCP with concurrent calls and cancellable waiting."""
import json
import fcntl
import hashlib
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .store import Store


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TEXT = {"type": "string"}
STRINGS = {"type": "array", "items": TEXT}
ATTENTION = {"owner": {"enum": ["human", "agent", "external"]}, "waiting_on": TEXT, "defer_until": TEXT}
READABLE = {
    "unlocks": {"type": "string", "description": "One short sentence: what this action makes possible."},
    "recommendation": {"type": "string", "description": "Recommend one choice and explain why briefly."},
    "steps": {"type": "array", "items": {"anyOf": [TEXT, schema({"text": TEXT, "link": schema({"label": TEXT, "url": TEXT}, ["label", "url"]), "copy": TEXT, "checks": STRINGS}, ["text"])]},
              "description": "At most 5 numbered actions for the human, each one action in plain words. A step carries what it needs: link {label, url} to open, copy (exact text to paste), or checks (what to look for; the human ticks them)."},
    "matters": schema({"lead": TEXT, "points": STRINGS}, ["lead"]) | {"description": "Why it matters: lead = one line on what this unblocks; points = up to 3 short facts."},
    "needs_you": schema({"lead": TEXT, "points": STRINGS}, ["lead"]) | {"description": "Why the human, not you: lead = one line; points = up to 3 short facts (a sign-in, a payment, a decision only they can make)."},
    "link": schema({"label": TEXT, "url": {"type": "string", "description": "Direct HTTPS destination, without credentials."}}, ["label", "url"]),
    "paste": {"type": "array", "items": schema({"label": TEXT, "text": TEXT, "mono": {"type": "boolean"}}, ["label", "text"]), "description": "Ready-to-copy text; key names only, never secret values."},
    "minutes": {"type": "integer", "minimum": 0, "description": "The human's own effort in minutes, excluding vendor/agent wait. Required when owner=human."},
    "rank": {"type": "integer", "minimum": 0, "description": "Lower means sooner; urgent decisions 10–150, actions 200–370, waits 400+."},
    "source": {"type": "string", "description": "Agent/worktree · task."},
}
CHOICES = {"type": "array", "items": {"anyOf": [TEXT, schema({"id": TEXT, "label": TEXT, "note": TEXT, "recommended": {"type": "boolean"}}, ["label"]) ]}}
AGENT_INSTRUCTIONS = Path(__file__).with_name("AGENT_GUIDE.md").read_text()
TOOLS = [
    ("hq_reporting_guide", "Read the human-readable item format and reporting examples before adding work to HQ.", schema({})),
    ("hq_register_session", "Identify this exact conversation; reuse its stable session id on every call.",
     schema({"session_id": TEXT, "name": TEXT, "provider": TEXT, "external_id": TEXT, "cwd": TEXT}, ["session_id", "name"])),
    ("hq_report", "Proactively report every unfinished requirement, setup gap, and unverified behavior as an independent item; a conversation can have many. owner=human needs action, agent is pending work, external waits on waiting_on. defer_until is an optional YYYY-MM-DD date. Defaults: tasks belong to agent, questions/blockers to human.",
     schema({"session_id": TEXT, "title": {"type": "string", "description": "The one action, in plain words, under 70 characters: Choose…, Send…, Add…. No jargon or internal names."}, "detail": {"type": "string", "description": "Optional background, shown collapsed under More context, under 600 characters. Never put the steps or the why here."}, "kind": {"enum": ["todo", "blocker", "question"]}, "tags": STRINGS, "options": CHOICES, **ATTENTION, **READABLE}, ["session_id", "title"])),
    ("hq_update", "Update an existing open item instead of duplicating it. Change ownership, who it waits on, context, or defer_until (empty clears deferral). Other items in this conversation remain independent.",
     schema({"session_id": TEXT, "item_id": TEXT, "title": TEXT, "detail": TEXT, **ATTENTION, **READABLE}, ["session_id", "item_id"])),
    ("hq_list", "List pending work and answers across conversations, optionally filtered by session, tag, or status.",
     schema({"session_id": TEXT, "tag": TEXT, "status": {"enum": ["open", "answered", "done"]}, "bucket": {"enum": ["needs", "pending", "waiting", "later", "done"]}})),
    ("hq_request_input", "Ask the human in HQ and wait for their answer in this same tool call. May return status=clarification; answer each using hq_comment_reply then hq_wait on the same item. On timeout, use hq_wait or hq_inbox later. Use up to 120 seconds to fit your client's timeout.",
     schema({"session_id": TEXT, "title": TEXT, "detail": TEXT, "tags": STRINGS, "options": CHOICES, **READABLE, "timeout": {"type": "number", "minimum": 1, "maximum": 86400}}, ["session_id", "title"])),
    ("hq_wait", "Wait for the response to an existing question. If status=clarification, answer the returned comments with hq_comment_reply then wait on the same item again.",
     schema({"session_id": TEXT, "item_id": TEXT, "timeout": {"type": "number", "minimum": 1, "maximum": 86400}}, ["session_id", "item_id"])),
    ("hq_inbox", "Read saved human responses for this session, including replies received while disconnected. Acknowledge each after reading.",
     schema({"session_id": TEXT}, ["session_id"])),
    ("hq_comments", "Read clarification threads for this session; pending=true lists every unanswered human question, including ones already delivered. Reply with hq_comment_reply.",
     schema({"session_id": TEXT, "item_id": TEXT, "pending": {"type": "boolean"}}, ["session_id"])),
    ("hq_comment_reply", "Answer a specific human clarification in its item thread. This is separate from a decision or task completion; if you need a new decision, report a new question item.",
     schema({"session_id": TEXT, "comment_id": TEXT, "text": TEXT}, ["session_id", "comment_id", "text"])),
    ("hq_comment_acknowledge", "Confirm this session received a clarification. Receipt does not answer it; use hq_comment_reply after reading.",
     schema({"session_id": TEXT, "comment_id": TEXT}, ["session_id", "comment_id"])),
    ("hq_acknowledge", "Confirm that this conversation has received its human response.",
     schema({"session_id": TEXT, "item_id": TEXT}, ["session_id", "item_id"])),
    ("hq_resolve", "Close the loop after acting on an item; note is a one-line account of what you did. A response must be received before closing an answered question.",
     schema({"session_id": TEXT, "item_id": TEXT, "note": TEXT}, ["session_id", "item_id"])),
]


def with_notes(item):
    notes = Store.readability_notes(item)
    return {**item, "readability": notes} if notes else item


def run(provider="other", channel=False, session_id=None):
    store = Store()
    write_lock, closed, initialized = threading.Lock(), threading.Event(), threading.Event()
    cancellations = {}
    bound_id = session_id or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_SESSION_ID") or "connection:" + str(uuid.uuid4())

    def send(value):
        with write_lock:
            if not closed.is_set():
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", **value}) + "\n")
                sys.stdout.flush()

    def execute(name, arguments, cancel):
        args = dict(arguments)
        id = args.get("session_id")
        if channel:
            if id and id != bound_id:
                raise ValueError("This HQ channel belongs to a different conversation")
            if name == "hq_list":
                args["session_id"] = bound_id
        if name == "hq_reporting_guide":
            return {"instructions": AGENT_INSTRUCTIONS}
        if name == "hq_register_session":
            args["id"] = args.pop("session_id")
            args.setdefault("provider", provider)
            result = store.register(**args)
            store.heartbeat(result["id"])
            return result
        if name == "hq_list":
            return store.snapshot(**args)
        if id:
            store.heartbeat(id)
        if name in {"hq_report", "hq_request_input"}:
            timeout = args.pop("timeout", 120)
            if name == "hq_request_input":
                if not isinstance(timeout, (int, float)) or not 0 < timeout <= 86400:
                    raise ValueError("timeout must be between 1 and 86400 seconds")
                args["kind"] = "question"
            item = store.add(**args, readable=True)
            return store.wait(item["id"], id, timeout, cancel) if name == "hq_request_input" else with_notes(item)
        if name == "hq_update":
            return with_notes(store.update(**args))
        if name == "hq_wait":
            return store.wait(args["item_id"], id, args.get("timeout", 120), cancel)
        if name == "hq_inbox":
            return store.inbox(id)
        if name == "hq_comments":
            return store.comments(**args)
        if name == "hq_comment_reply":
            return store.respond(**args)
        if name == "hq_comment_acknowledge":
            return store.acknowledge_comment(args["comment_id"], id)
        if name == "hq_acknowledge":
            return store.acknowledge(args["item_id"], id)
        if name == "hq_resolve":
            if store.item(args["item_id"])["session_id"] != id:
                raise ValueError("Item belongs to a different session")
            return store.resolve(args["item_id"], args.get("note", ""))
        raise ValueError("Unknown HQ tool")

    def call(message):
        request_id = message["id"]
        cancel = cancellations[request_id]
        try:
            params = message.get("params", {})
            result = execute(params["name"], params.get("arguments", {}), cancel)
            send({"id": request_id, "result": {"content": [{"type": "text", "text": json.dumps(result)}], "isError": False}})
        except Exception as error:
            send({"id": request_id, "result": {"content": [{"type": "text", "text": str(error)}], "isError": True}})
        finally:
            cancellations.pop(request_id, None)

    def push_channel():
        # Only an explicitly bound session can receive channel notifications.
        initialized.wait()
        while not closed.is_set():
            store.heartbeat(bound_id, "channel")
            for reply in store.inbox(bound_id):
                if reply["state"] == "pending" and not store.has_waiter(reply["item_id"]):
                    send({"method": "notifications/claude/channel", "params": {
                        "content": f"Human response to {reply['title']}: {reply['text']}",
                        "meta": {"item_id": reply["item_id"], "session_id": bound_id}}})
                    # Emission isn't proof that Claude acted; require hq_acknowledge.
                    store.delivery_result(reply["id"], "sent")
            question = store.claim_comment(bound_id)
            if question:
                send({"method": "notifications/claude/channel", "params": {
                    "content": f"Human clarification on {question['title']}: {question['text']}\nReply in HQ using hq_comment_reply with comment_id {question['id']} and session_id {bound_id}.",
                    "meta": {"kind": "clarification", "comment_id": question["id"], "item_id": question["item_id"], "session_id": bound_id}}})
                store.comment_delivery(question["id"], "sent")
            closed.wait(0.5)

    if channel:
        lock_path = store.path.parent / ("channel-" + hashlib.sha256(bound_id.encode()).hexdigest() + ".lock")
        channel_lock = lock_path.open("w")
        try:
            fcntl.flock(channel_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            channel_lock.close()
            raise ValueError("This conversation already has a running HQ channel") from None
        store.register(bound_id, f"Claude channel · {Path.cwd().name}", "claude-code", cwd=str(Path.cwd()))
        threading.Thread(target=push_channel, daemon=True).start()
    with ThreadPoolExecutor(max_workers=16) as pool:
        try:
            for line in sys.stdin:
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError("Expected an object")
                    method = message.get("method")
                    id = message.get("id")
                    if method == "initialize":
                        capabilities = {"tools": {}}
                        if channel:
                            capabilities["experimental"] = {"claude/channel": {}}
                        send({"id": id, "result": {"protocolVersion": "2024-11-05", "capabilities": capabilities,
                            "serverInfo": {"name": "hq", "version": "0.1.0"},
                            "instructions": AGENT_INSTRUCTIONS + "\n"
                                + (f"This channel is bound to session {bound_id}; use this id for every report. "
                                   "Acknowledge channel responses using hq_acknowledge with item_id and session_id from the event before acting." if channel else "")}})
                    elif method == "notifications/initialized":
                        initialized.set()
                    elif method == "notifications/cancelled":
                        event = cancellations.get(message.get("params", {}).get("requestId"))
                        if event:
                            event.set()
                    elif method == "ping":
                        send({"id": id, "result": {}})
                    elif method == "tools/list":
                        send({"id": id, "result": {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]}})
                    elif method == "tools/call" and id is not None:
                        cancellations[id] = threading.Event()
                        pool.submit(call, message)
                    elif id is not None:
                        send({"id": id, "error": {"code": -32601, "message": "Method not found"}})
                except (ValueError, TypeError) as error:
                    send({"id": None, "error": {"code": -32700, "message": str(error)}})
        finally:
            closed.set()
            initialized.set()
            for event in list(cancellations.values()):
                event.set()
            if channel:
                store.disconnect(bound_id, "channel")
                channel_lock.close()

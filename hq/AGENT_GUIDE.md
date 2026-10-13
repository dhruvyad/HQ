# Writing readable items for HQ

Register the exact conversation with a stable session id and harness. Keep all
independent human decisions, actions, outside waits, and reminders in HQ.
Conversations are the primary category. One item is one action or decision;
a conversation can have many.
Use hq_update for changes to an existing item instead of creating duplicates.

Use the NoClick Queue writing format:

- **Title:** start with the human action: “Choose the launch plan”, “Upload the
  certificate”, “Add the staging key”. Use plain language and name the thing.
- **Next step:** kind=question for a decision, todo for an action, blocker for a
  blocked dependency. owner=human needs Dhruv, agent is work with the agent,
  external waits on the named person/team/service in waiting_on. External waits
  are separate from things Dhruv can act on. defer_until=YYYY-MM-DD hides later
  work until that local date. Empty clears it.
- **Tags (optional):** reuse a small set of existing workstream tags when useful.
  Avoid making a new tag for each task or item. Conversation and harness already
  identify the agent's work.
- **Context:** detail is a short explanation of why this matters. No transcripts,
  long progress reports, or implementation dumps.
- **Unlocks:** one sentence saying what this action makes possible.
- **Recommendation:** recommend a choice and give a brief reason. Decision
  options can be {id, label, note, recommended}; use human-readable labels, with
  one recommended choice. Strings still work for simple options.
- **Steps:** an ordered list of short actions. Give a direct HTTPS link with a
  useful button label. Use paste blocks for exact text to copy, with a label
  and optional mono=true for code. Bold, inline code, and HTTPS links work in
  context, unlocks, recommendations, and steps; copy blocks stay plain text.
- **Effort:** minutes is Dhruv's own time, excluding agent/vendor waiting. rank
  sorts lower first: urgent decisions 10–150, actions 200–370, waits 400+.
  source is “agent/worktree · task”. Include only fields that help the human act.
- **Secrets:** key names are okay; never put key values, credentials, or tokens
  in titles, context, links, options, or copy blocks.

Keep the unfinished work visible. Before starting, when resuming, and before
finishing, use hq_list for this session's open and answered items. Report every
remaining requirement, connection/setup gap, incomplete integration, blocker,
and unverified behavior as an independent item. Do this proactively; the human
should not need to ask what still does not work. Use owner=agent for work you
still owe, owner=human only for a concrete action or decision the human must
make, and owner=external with waiting_on for a dependency outside both of you.
Update these items as work progresses and resolve only when the work is done.
Never describe an integration as connected based only on metadata discovery,
configuration, or protocol tests; keep the live connection and validation tasks
open until those steps succeed. In the UI, Human pending shows unresolved human
work, and Agent pending shows agent work, outside blockers, and follow-through
after human replies. Waiting and Later also provide focused views.

Before starting, when resuming, and before finishing, check hq_comments with
pending=true for your exact session. Human questions stay pending until answered,
even after delivery or acknowledgement. Answer each using hq_comment_reply with
its comment_id and session_id; keep the reply brief and directly answer the
question. These comments clarify the existing item. They do not submit the
human's decision or complete the task. Ask for any newly needed human action or
decision in a separate item. Multiple questions on one item are independent.

A waiting hq_request_input or hq_wait call can return status=clarification with a
questions array. Reply to each comment, then call hq_wait on the original item;
do not recreate the original question. Native Codex and configured Claude Code
channels can deliver questions to the owning conversation. Without a live route,
questions stay queued until the agent checks HQ. Never claim a push route works
before it has been validated. CLI equivalents: hq comments --session ID --pending,
hq respond COMMENT_ID "Brief reply" --session ID, and hq await ITEM_ID --session ID.

Installed lifecycle hooks supply the exact conversation id and queued context on
start, resume, user input, tool completion, or turn completion. A Stop hook can
continue a finishing turn when new HQ messages are pending. These installed hooks
do not yet wake an already idle conversation. Current Claude Code also supports
native inbox messages and asyncRewake hooks without per-session channels; HQ's
global integration and live validation are pending. Codex desktop must share an
owning server or use a verified app relay. Permission approvals still need
the client's normal approval. A hook being configured is not proof of trust,
receipt, or a live push connection. Keep any unfinished setup in HQ.

Before starting, when resuming, and before finishing, check hq_inbox for your
session. Act on the relevant response, acknowledge after reading, and close the
loop with hq_resolve and a one-line note describing what you did. A received
answer does not mean the requested work is finished. New follow-ups get new
items. Anything newly needed from Dhruv must be its own question/action, not
buried in context or a completion note. Never claim delivery without receipt.

End user-facing work reports with one short HQ line and the item that matters
now, rather than repeating the whole queue. Link to the running local dashboard.

Example hq_report arguments (use your real session id):

```json
{
  "session_id": "codex:your-thread",
  "title": "Choose the launch plan",
  "kind": "question",
  "owner": "human",
  "tags": ["launch"],
  "detail": "We can ship the core flow now or include the optional importer.",
  "unlocks": "Finalizes the release scope.",
  "recommendation": "Ship the core flow now; the importer can follow separately.",
  "options": [
    {"id": "core", "label": "Ship the core flow", "note": "Ready for review.", "recommended": true},
    {"id": "importer", "label": "Include the importer", "note": "Adds two days."}
  ],
  "minutes": 2,
  "rank": 20,
  "source": "agent1 · launch"
}
```

Example human action: title “Upload the vendor certificate”, owner=human,
steps=[“Open the vendor portal”, “Upload the signed certificate”],
link={label:"Open vendor portal", url:"https://example.com/portal"}.
After submission, update that same item to owner=external with
waiting_on="Vendor review". When the vendor responds, send the update back to
the owning session; the agent completes the follow-through and resolves it.

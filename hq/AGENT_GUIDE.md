# Writing readable items for HQ

Register the exact conversation with a stable session id and harness. Keep all
independent human decisions, actions, outside waits, and reminders in HQ, tagged
by workstream. One item is one action or decision; a conversation can have many.
Use hq_update for changes to an existing item instead of creating duplicates.

Use the NoClick Queue writing format:

- **Title:** start with the human action: “Choose the launch plan”, “Upload the
  certificate”, “Add the staging key”. Use plain language and name the thing.
- **Next step:** kind=question for a decision, todo for an action, blocker for a
  blocked dependency. owner=human needs Dhruv, agent is work with the agent,
  external waits on the named person/team/service in waiting_on. External waits
  are separate from things Dhruv can act on. defer_until=YYYY-MM-DD hides later
  work until that local date. Empty clears it.
- **Workstream:** tags group the work, independent of conversation and harness.
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

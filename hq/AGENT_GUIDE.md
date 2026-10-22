# Writing readable items for HQ

Register the exact conversation with a stable session id and harness. Keep all
independent human decisions, actions, outside waits, and reminders in HQ.
Conversations are the primary category. One item is one action or decision;
a conversation can have many.
Use hq_update for changes to an existing item instead of creating duplicates.

Write every item so a reader with ADHD can act on it in one glance. An item
that needs the human (owner=human) is refused unless it has **minutes**,
**steps** (or **options** for a decision), **matters** and **needs_you**. The
report result can also carry `readability` notes: fix them with hq_update
before you finish.

- **Title = the one action**, under 70 characters, in plain words: "Send one
  message to NoClick, then check what it asks". No internal names or jargon
  ("pickers", "wand builder", file paths). Not a summary, not a question.
- **minutes:** the human's own time, specific (3, not "a bit").
- **steps:** at most 5, one action each, numbered by the dashboard. A step
  carries what it needs instead of describing it: `link` {label, url} to open,
  `copy` with the exact text to paste, `checks` listing what to look for (the
  human ticks them, and the item ends in Looks right / Something was off).
  Plain-text steps still work for simple actions.
- **options:** for a decision, 2–4 human-readable choices, one recommended,
  each with a one-line trade-off in note.
- **matters** {lead, points}: *Why it matters*. lead = what this unblocks, in
  one line; points = up to 3 short facts. No hedging.
- **needs_you** {lead, points}: *Why you, not the agent*. lead = the real
  reason (a sign-in, a payment, an account only they have, a call only they
  can make); points = up to 3 short facts. If you can do it yourself, do it
  instead of reporting it.
- **detail:** optional background, shown collapsed under More context, under
  600 characters. Never put the steps or the why here.
- **kind/owner:** kind=question for a decision, todo for an action, blocker
  for a blocked dependency. owner=human needs Dhruv, agent is work with the
  agent, external waits on the named person/team/service in waiting_on.
  defer_until=YYYY-MM-DD hides the item until that local date; empty clears it.
- **workstream:** the ongoing line of work this belongs to (Coordinator,
  Support, Platform SDK…). Reuse one from the list hq_reporting_guide returns;
  name a new one only for genuinely new work. **project** defaults to your
  repo; set it only when the work belongs elsewhere.
- **due:** YYYY-MM-DD only for a real deadline (an appointment, an expiring
  link, a scheduled run). Never invent one; items with due dates lead the list.
- **Tags (optional):** a few search words; they also place items when no
  workstream is given.
- **rank** sorts lower first: urgent decisions 10–150, actions 200–370,
  waits 400+. source is "agent/worktree · task".
- **Secrets:** key names are okay; never put key values, credentials, or
  tokens in any field.

Worked example (owner=human action):

```json
{
  "session_id": "claude-code:your-session",
  "title": "Send one message to NoClick, then check what it asks",
  "owner": "human",
  "minutes": 3,
  "steps": [
    {"text": "Open NoClick", "link": {"label": "noclick.com", "url": "https://noclick.com"}},
    {"text": "Send this message", "copy": "build me an agent that runs whenever a github issue is created and creates a pr and linear ticket"},
    {"text": "Check what it asks you", "checks": [
      "A list of your GitHub repositories to pick from (no question if you have only one)",
      "A list of your Linear teams, the same way",
      "No question about which issues are unclear"]}
  ],
  "matters": {"lead": "Proves the account lists work for a real person, not just test accounts",
    "points": ["NoClick now shows a list from your account instead of asking you to type a name (live since 10-18)",
               "It has only run on test accounts so far",
               "If a list is wrong, the agent learns which one and fixes it"]},
  "needs_you": {"lead": "It needs a real GitHub and Linear, and only you can sign in to them",
    "points": ["Test accounts have no GitHub or Linear connected, so there is no list to show",
               "Connecting one means signing in as a person, which the agent can't do"]},
  "tags": ["coordinator"],
  "rank": 300,
  "source": "noclick4 · coordinator pickers"
}
```

A decision uses options instead of steps, with the same matters and
needs_you. After the human acts, update the same item (for example
owner=external with waiting_on="Vendor review") rather than reporting a new one.

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

A reply that starts with "Skipped:" means Dhruv decided not to do the item.
The item is already closed: acknowledge it, stop work that depends on it, and
don't report it again unless something new makes it worth his time.

Before starting, when resuming, and before finishing, check hq_inbox for your
session. Act on the relevant response, acknowledge after reading, and close the
loop with hq_resolve and a one-line note describing what you did. A received
answer does not mean the requested work is finished. New follow-ups get new
items. Anything newly needed from Dhruv must be its own question/action, not
buried in context or a completion note. Never claim delivery without receipt.

End user-facing work reports with one short HQ line and the item that matters
now, rather than repeating the whole queue. Link to the running local dashboard.

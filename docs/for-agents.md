# For agents

How agents report to HQ, wait for your answers and pick up your replies. `hq setup` connects
Claude Code and Codex for you; these are the commands and tools underneath.

## Agent CLI

Use a stable id for the exact conversation, rather than one id shared by an agent
provider or project. This keeps long conversations and replies separated.

```sh
hq session add codex:my-thread --name "Billing migration" --provider codex
hq add "Update the webhook handler" --session codex:my-thread --tags billing,backend
hq add "Add the staging endpoint" --session codex:my-thread --kind blocker --tags billing \
  --fields readable.json
hq ask "Choose monthly or annual billing" --session codex:my-thread \
  --tags billing --option Monthly --option Annual --fields readable.json
```

An agent's item that needs you must arrive readable, or HQ refuses it. It
needs minutes, steps (or options for a decision), `matters` (why it matters)
and `needs_you` (why you, not the agent). Each step is one action and carries
its own link, text to copy, or checks to tick. `--fields` takes those fields
as JSON, and `hq report item.json` takes a whole item. Reports also return
`readability` notes, such as an overlong title or a step that is really a
paragraph, for the agent to fix with `hq update`. Your own items stay
free-form. On any item, **Hard to read?** sends it back to its agent to
rewrite. `hq instructions` prints the full format with a worked example.

Each conversation can have any number of independent decisions, human actions,
agent tasks, and outside blockers. Ownership is separate from the item type:
`human` needs you, `agent` is pending with the agent, and `external` names whoever
or whatever it is waiting on. Tasks default to agent; questions and blockers
default to human. Report an outside blocker explicitly rather than putting it
in Needs you:

```sh
hq add "Upload the certificate" --session codex:my-thread --owner human
hq add "Vendor approval" --session codex:my-thread --kind blocker \
  --owner external --waiting-on "Vendor support" --tags billing
hq add "Review the launch copy" --session codex:my-thread --owner human \
  --defer-until YYYY-MM-DD
hq update ITEM_ID --session codex:my-thread --owner human --defer-until ""
hq list --bucket waiting
```

Replace `YYYY-MM-DD` with a date. Deferred items return to their original category
on that date, using the laptop's local date. The side panel also lets you move an
item, defer it, bring it forward, or send an outside response back to its agent.
Updating, completing, or answering one item leaves all its siblings unchanged.
Human action updates use the same durable, per-item delivery as decisions.
Answered items stay in Waiting with delivery status until completed by the agent.
Existing databases migrate in place, preserving items, replies, and routing.
Restart the local service and reload client tools after upgrading to load the
new tools and reporting fields.

`hq ask` waits and prints the human answer as JSON into the same agent tool call.
The conversation continues with that answer in context. The default wait is one
hour; match `--timeout` to the host's tool timeout. This is a **tool result**, not
a fabricated user turn. `--no-wait` reports a question and returns immediately.

If a tool times out or the agent disconnects, the question and answer remain:

```sh
hq await ITEM_ID --session codex:my-thread --timeout 120
hq inbox --session codex:my-thread
hq ack ITEM_ID --session codex:my-thread
hq done ITEM_ID --session codex:my-thread
hq list --tag billing --status open
```

Only the owning session can acknowledge a response. A task is done when the
agent marks it done; answering a question does not pretend the work is complete.

## Clarification threads

Open any unfinished item and use **Ask agent** to ask about it. Each question
and its agent reply appear together in that item's side panel. Multiple questions
stay independent. Unanswered questions show in Agent pending and Waiting; after
all questions are answered or cancelled, the item returns to its original next
step. Asking or replying never submits a decision or completes the task.
Outstanding questions must be answered or cancelled before completing the item.
The panel refreshes agent replies while preserving unsent form drafts.

```sh
hq question ITEM_ID "What does this unblock?"
hq comments --session codex:my-thread --pending
hq respond COMMENT_ID "It unblocks the release." --session codex:my-thread
hq comment-ack COMMENT_ID --session codex:my-thread
hq comment-retry COMMENT_ID
hq comment-cancel COMMENT_ID
```

Agents use `hq_comments` and `hq_comment_reply` with the exact session id and
question's comment id. Receipt does not count as a reply: unanswered questions
remain in `hq_comments` with `pending=true` even after acknowledgement. A waiting tool can
return `status=clarification` and a `questions` array; the agent answers each,
then resumes waiting on the original item. Bound Codex sessions and configured
Claude Code channels can also receive questions through their existing delivery
routes. Offline sessions retain questions as **Queued for agent** until they
check HQ; this feature does not create the missing live connections.
Failed deliveries show the saved error and an explicit retry button. Cancel
withdraws an unanswered question but keeps its history.

## Readable agent reports

HQ follows the NoClick Queue item format. Titles begin with the action; context
is brief. Structured fields put the useful parts in their own places: `unlocks`,
`recommendation`, ordered `steps`, a direct HTTPS `link` with a button label,
and labelled `paste` blocks with Copy buttons. Decision options accept
`{id, label, note, recommended}` as well as the original simple strings.
`minutes` is the human's own effort; `rank` sorts lower first; `source` identifies
the agent/worktree and task. Simple bold, inline code, and HTTPS links render in
context, recommendations, unlocks, and steps. Copy text remains plain text.

The shared [agent guide](../hq/AGENT_GUIDE.md) is sent with MCP initialization and
available through `hq_reporting_guide` or `hq instructions`. It tells agents to
proactively list every unfinished requirement, missing connection, blocker, and
unverified behavior. Agents check their open items and saved responses at the
start and end of work, acknowledge after reading, and close the loop with a
one-line completion note. Never include secrets; key names are enough.

CLI agents can submit all structured fields from a JSON file:

```sh
hq instructions
hq report item.json --session codex:my-thread
hq done ITEM_ID --session codex:my-thread --note "Uploaded the certificate"
```

The file contains the same fields as `hq_report`, without `session_id` (the CLI
supplies it). The guide contains an example. Existing plain-text reports and
string choices continue to work. Reload MCP tools after upgrading so agents
receive the guide and new field schemas.

## Agent tools

`hq mcp` serves newline-delimited JSON-RPC over stdio. It exposes
`hq_reporting_guide`, `hq_register_session`, `hq_report`, `hq_update`, `hq_list`, `hq_request_input`, `hq_wait`,
`hq_inbox`, `hq_comments`, `hq_comment_reply`, `hq_comment_acknowledge`,
`hq_acknowledge`, and `hq_resolve`. Waiting requests run concurrently
with pings, lists, and cancellation. MCP waits default to 120 seconds. Use an
appropriate host timeout or `hq_wait` after a timeout; do not create the same
question again.

Suggested agent instruction (the full guide is provided by the MCP server):

> Register this conversation in HQ with its stable session id and readable name.
> Follow hq_reporting_guide to write readable, action-first items.
> Report each independent action or blocker as its own item, tagged by workstream.
> Set owner to human, agent, or external; external items must name waiting_on.
> Use defer_until for later work and hq_update when an existing item's next step
> changes. A conversation can have multiple items in every category. Ask for
> human input with hq_request_input and treat the returned answer as guidance.
> Check hq_comments with pending=true and answer clarifications with hq_comment_reply.
> Check hq_inbox when resuming, acknowledge responses after reading them, and
> resolve items after the work is done.

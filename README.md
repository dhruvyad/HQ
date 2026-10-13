# HQ

A small, private inbox for the work your AI agents are doing. Run `hq` to open
a dark dashboard on your laptop. Filter by workstream tags,
answer blockers, and let agents pick up your response.

The dashboard uses black and neutral charcoal surfaces. Conversation badges show
their registered harness (Codex, Claude Code, Claude Desktop, ChatGPT, or Other),
while sidebar navigation shows only workstream tags. Connect agents is in the
top bar. The default All view groups independent items under their conversation,
with a harness badge and counts. Human pending shows unfinished human actions and decisions; Agent pending shows
agent work, outside blockers, and follow-through after human replies. Waiting,
Later, and Done provide focused views across every conversation. Rows show just the title, next step, and
tags; context and replies stay in the side panel. Decorative dates and copy are
omitted; dates appear only for deferred items.

**Python 3.11+. Zero runtime dependencies. SQLite. No cloud storage.**

## Start

```sh
cd ~/Documents/code/HQ
./bin/hq
```

Optional installation: `python3 -m pip install .` (prefer a virtual environment).
The source launcher can also be linked onto your PATH. `hq` starts a background
service on `http://127.0.0.1:4310` and opens your browser. `hq stop` stops it;
`hq serve` runs in the foreground; `hq start --no-open` starts without a browser.
Use `hq start --port 4311` if that port is already occupied.

All items and responses live at `~/.local/share/hq/hq.sqlite3`. Set `HQ_HOME`
to use a separate inbox. Back up that directory with the service stopped.
The browser is a view onto SQLite, not the source of truth.

## Agent CLI

Use a stable id for the exact conversation, rather than one id shared by an agent
provider or project. This keeps long conversations and replies separated.

```sh
hq session add codex:my-thread --name "Billing migration" --provider codex
hq add "Update the webhook handler" --session codex:my-thread --tags billing,backend
hq add "Need the staging endpoint" --session codex:my-thread --kind blocker --tags billing
hq ask "Monthly or annual billing?" --session codex:my-thread \
  --tags billing --option Monthly --option Annual
```

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

The shared [agent guide](hq/AGENT_GUIDE.md) is sent with MCP initialization and
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

## Connections

```sh
hq install all
hq discover --limit 50
```

The installer configures **Codex, Claude Code, and Claude Desktop** local MCP
tools, preserving other servers and backing up client settings. Reload tools or
restart the app to load the connection. It does not interrupt running sessions.
An existing HQ entry in Claude Desktop is preserved; client CLIs handle existing
server names according to their own behavior.

Discovery reads recent Codex and Claude Code conversation metadata. It does not
read whole transcripts, infer blockers, establish a live connection, or claim a
historical conversation is currently running. Agents explicitly report work.
Desktop Claude and ordinary ChatGPT chat histories have no metadata discovery
adapter here. Register those conversations through tools when available.

| Client | Report and receive replies | Unsolicited steering |
| --- | --- | --- |
| Codex CLI / desktop | HQ CLI or local MCP; a waiting call receives answers | Supported through a running app-server control socket that owns the thread |
| Claude Code | Local MCP or CLI | Optional Claude Code channel, enabled per session |
| Claude Desktop chat | Local MCP after restart | Replies return through waiting tools or inbox polling; no general push injector |
| ChatGPT with a local agent executor | CLI from that executor | Depends on the executor's conversation controls |
| Ordinary ChatGPT desktop / web chats | Requires a separately configured reachable MCP connector | Not automatically connected by this local-only version |

**This version does not attach to every already-running AI conversation.** It
provides working reporting, waiting tools, durable offline replies, metadata
discovery, and opt-in native steering. It never types into an arbitrary active
window or modifies another app's database to simulate a delivered message.

### Codex native user turns

`hq connect codex --socket /absolute/path/to/control.sock` connects to an existing
Codex app-server, lists its threads, and binds them to its control socket. Omit
`--socket` for the managed daemon's default control socket. The socket must belong
to the server that owns the target conversations; launching another server does
not attach to the desktop app's active processes.

While the HQ dashboard service is running, answered items for bound Codex sessions
automatically become `turn/steer` input on active turns or `turn/start` input on
idle threads. The adapter requires an acknowledgement before marking a reply
received. It preserves thread permissions and keeps the connection alive.
The waiting-tool route takes precedence to prevent a duplicate native message.

If delivery fails, the answer stays saved with the exact error. Check the target
conversation before using `hq retry ITEM_ID`; a connection failure can be
ambiguous, so HQ never retries user turns blindly.

### Claude Code channel (optional research preview)

Bind a dedicated MCP server to one conversation:

```sh
claude mcp add --scope user hq-channel -- \
  /absolute/path/to/python3 /absolute/path/to/HQ/bin/hq mcp \
  --provider claude-code --channel --session-id claude-code:billing
claude --dangerously-load-development-channels server:hq-channel
```

Claude's development-channel confirmation is required by its current preview.
This flag enables this channel's allowlist exception; it does not bypass tool
permissions. Report items using the bound session id. Only one running client
may use a given channel session id. Received events carry an item id, and the
agent acknowledges them with `hq_acknowledge`. Emission alone is shown as
“Sent · awaiting acknowledgement.” This path is protocol-tested; it has not
been validated against a live Claude channel on this laptop.

### ChatGPT connector boundary

Ordinary ChatGPT runs outside the laptop and cannot reach loopback directly.
The published connection paths require a reachable MCP server or a private
tunnel. HQ currently exposes stdio MCP only; it does **not** open a public port
or create a tunnel. Remote MCP transport, authentication, and ChatGPT event
subscriptions are future work. Existing ChatGPT chats cannot be silently
subscribed to a new connector. Local-executor Work chats can use `hq` directly.

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

## Local boundaries

The dashboard binds only to `127.0.0.1`. It rejects cross-origin requests, DNS
rebinding hostnames, and API requests without the page's random token. It uses
no external scripts, fonts, analytics, or browser storage. Like other local
tools, trusted processes running as your user can access the inbox; this is
not a multi-user authorization boundary. Session names and responses are
rendered as text, never executable HTML.

## Development

```sh
python3 -m unittest discover -s tests -v
node --check hq/static/app.js
node tests/test_ui.js
```

Tests cover SQLite persistence and migration, multiple independent items per
conversation, ownership changes and deferral, concurrent answers, per-session routing,
offline acknowledgement, timeout/cancellation, native steering decisions,
the HTTP origin boundary, and an end-to-end MCP human response.

Protocol references: [Codex app-server](https://learn.chatgpt.com/docs/app-server),
[Claude Code MCP](https://code.claude.com/docs/en/mcp),
[Claude channels](https://code.claude.com/docs/en/channels-reference),
[local MCP clients](https://modelcontextprotocol.io/docs/develop/connect-local-servers),
[ChatGPT MCP setup](https://developers.openai.com/plugins/build/app-quickstart).

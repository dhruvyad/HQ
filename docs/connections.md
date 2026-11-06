# Connections

`hq setup` connects Claude Code and Codex when it finds them on your Mac, and `hq setup --undo`
removes what it added. Everything below is for doing it by hand or going further.

```sh
hq install all
hq discover --limit 50
```

The installer configures **Codex, Claude Code, and Claude Desktop** local MCP
tools and adds Codex/Claude Code lifecycle hooks, preserving other settings and
backing up changed files. `hq setup` also trusts HQ's Codex hooks (see below); reload
MCP tools or restart/resume the client to load changes. Running sessions are not interrupted.
An existing HQ entry in Claude Desktop is preserved; client CLIs handle existing
server names according to their own behavior.

Discovery reads recent Codex and Claude Code conversation metadata. It does not
read whole transcripts, infer blockers, establish a live connection, or claim a
historical conversation is currently running. Agents explicitly report work.
Desktop Claude and ordinary ChatGPT chat histories have no metadata discovery
adapter here. Register those conversations through tools when available.

| Client | Report and receive replies | Unsolicited steering |
| --- | --- | --- |
| Codex CLI / desktop | HQ CLI, local MCP, trusted hooks | Active steering and idle turns through the owning app-server's control socket |
| Claude Code | Local MCP, CLI, lifecycle hooks | Optional Claude Code channel, enabled per exact session |
| Claude Desktop chat | Local MCP after restart | Replies return through waiting tools or inbox polling; no general push injector |
| ChatGPT with a local agent executor | CLI from that executor | Depends on the executor's conversation controls |
| Ordinary ChatGPT desktop / web chats | HQ tools through an explicitly configured private MCP tunnel | A tool tunnel alone does not wake chats |

**This version does not attach to every already-running AI conversation.** It
provides working reporting, waiting tools, durable offline replies, metadata
discovery, and opt-in native steering. It never types into an arbitrary active
window or modifies another app's database to simulate a delivered message.

## Codex native user turns

`hq connect codex --socket /absolute/path/to/control.sock` connects to an existing
Codex app-server and binds only its loaded, directly addressable conversations. Omit
`--socket` for the managed daemon's default control socket. The socket must belong
to the server that owns the target conversations; launching another server does
not attach to the desktop app's active processes. The dashboard refreshes the
owning server's inventory every two seconds, listens for status notifications,
and keeps this watch across restarts.
Ephemeral threads and child threads that reject direct input are skipped.

For new CLI sessions, `hq launch codex` starts/connects the shared Codex daemon
and opens the CLI on it. Forward normal options after `--`; for example,
`hq launch codex -- resume THREAD_ID`. This preserves the client's configured
model and permissions. If that account cannot use its configured model, choose
an available model with `hq launch codex -- -m MODEL`.

While the HQ dashboard service is running, answered items for bound Codex sessions
automatically become `turn/steer` input on active turns or `turn/start` input on
idle threads. The adapter requires an acknowledgement before marking a reply
received. It preserves thread permissions and keeps the connection alive.
The waiting-tool route takes precedence to prevent a duplicate native message.

If delivery fails, the answer stays saved with the exact error. Check the target
conversation before using `hq retry ITEM_ID`; a connection failure can be
ambiguous, so HQ never retries user turns blindly.

Live verification on this laptop delivered a human decision to an active Codex
turn and started an idle turn with a separate clarification in another exact
thread. This proves the owning-server route, not attachment to existing desktop
conversations. Server acceptance is **Sent**; **Received** still requires the
agent to acknowledge the response or reply to the clarification.

An isolated **actual desktop instance** using the app's matching 0.160.0 runtime
also passed idle wake-up: an HQ clarification started the exact chat, the agent
answered on the original HQ item through the CLI, and the reply reached the
desktop connection. Active/idle status notifications arrived on that connection.
Active steering also passed through `turn/steer`; two independent clarifications
were answered on the same still-open item, and both replies reached the desktop.
The normal desktop app and its existing chats have **not** been migrated.

## Codex desktop on macOS

Install the one-time launcher:

```sh
hq desktop install
```

Finish running turns, quit Codex, then open **Codex + HQ** in your user
Applications folder (`~/Applications`). Use that launcher on future starts.
It uses the same account, profile, and conversation history. No per-chat
configuration is needed. You can also start it with `hq launch codex-desktop`.
An already-open desktop is left running; HQ asks you to quit it first.

Opening the app any other way, from the Dock or when it reopens itself after an
update, starts it without HQ. Its chats still show in HQ through the hooks below,
once Codex trusts them, and get your answers at their next step or your next
message; only sending an answer straight into an idle chat needs the launcher.
`hq doctor` says whether the open app came through the launcher.

Check the live connection:

```sh
hq desktop status
```

The status reports how many exact chats the owning runtime has loaded. Open an
existing chat to connect it; historical metadata alone does not establish a live
connection. Future questions to a bound chat make the desktop subscribe to that
exact chat before HQ sends the turn, including after reconnecting. Server
acceptance remains **Sent** until the agent acknowledges or answers in HQ.

The launcher preserves native startup arguments and uses the installed app's
matching bundled runtime. Only the recognized primary chat server is shared;
helper processes and unknown startup shapes run natively. Approval requests and
decisions pass through unchanged. No permission, sandbox, model, or global
runtime overrides are installed. Quitting the desktop reaps its owned runtime;
the native reconnect lifecycle can restart it after a crash. Saved uncertain
deliveries still require explicit retry, preventing duplicate user turns.

The packaged launcher passed an isolated real desktop test on runtime 0.160.0:
idle wake-up, actual agent replies saved on the original item and streamed to
desktop, preserved approval/sandbox settings, an HQ-originated approval request
and denial, and a subsequent question after a forced runtime crash. The approval
denial used a separate test client; the native approval button itself was not
clicked. Your normal profile still needs the one-time relaunch before its chats
can be called connected.

Remove the shortcut with `hq desktop uninstall`, then open the original app on
the next start. The underlying [Codex app-server protocol](https://learn.chatgpt.com/docs/app-server)
and the app's runtime override are experimental; new startup shapes deliberately
fall back to native behavior. `hq codex-desktop-proxy --socket PATH` is the
lower-level protocol bridge and does not launch or reconfigure the app itself.

## Hooks and paused conversations

Hooks use native lifecycle events and read HQ's exact-session queue; they do not
scan transcripts. Start/resume hooks supply registration/reporting instructions.
Tool and turn completion hooks deliver saved replies and human questions. A
Stop hook can continue a finishing turn when a new message is pending, without
repeating sent messages. Emitted context remains **Sent** until agent receipt.

**HQ's installed lifecycle hooks do not yet wake an already idle conversation.**
Current Claude Code also supports native inbox messages and `asyncRewake` hooks
without per-session channels. A global HQ integration using those features is
planned but has not been implemented or live-tested. Codex desktop uses the
launcher above to connect its owning server. Permission approvals still need
the client's normal approval. A stopped server or disconnected channel must be
reconnected before it can deliver turns.
Codex runs a hook only once you trust it in `/hooks`, and asks again whenever the
hook changes; until then no Codex conversation, desktop or CLI, shows in HQ on its
own. `hq setup` trusts HQ's hooks for you, as its own step that you approve with
the rest: Codex computes each hook's hash (`hooks/list`) and saves it
(`config/batchWrite`), exactly as `/hooks` does, for HQ's hooks only. A hook you
turned off stays off, and your other hooks are never touched. Setup does it again
when HQ moves, since that changes the hooks. `hq setup --undo` removes that trust
with the hooks, and `hq doctor` reports which of HQ's hooks Codex trusts. A Codex
app that is already open keeps the trust it started with, so quit and reopen it.
Claude Code loads the installed hooks when starting/resuming. Hook delivery and
an actual agent acknowledgement were verified in a real Claude Code session.

## Claude Code channel (optional research preview)

Launch with a dedicated channel bound to the client's actual conversation UUID:

```sh
hq launch claude-code
hq launch claude-code --resume EXISTING_CONVERSATION_UUID
```

Claude's development-channel confirmation is required by its current preview.
This flag enables this channel's allowlist exception; it does not bypass tool
permissions. Report items using the bound session id. Only one running client
may use a given channel session id. Received events carry an item id, and the
agent acknowledges them with `hq_acknowledge`. Questions are answered with
`hq_comment_reply`. Emission alone is shown as
“Sent · awaiting acknowledgement.” This path is protocol-tested; it has not
been validated against a live Claude channel on this laptop.

## ChatGPT private tool connection

Use OpenAI's [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
to forward HQ's existing stdio tools while the dashboard stays on loopback:

1. Install the official `tunnel-client` from
   [Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels).
2. Create a tunnel associated with your ChatGPT workspace. Configure its runtime
   API key locally as `CONTROL_PLANE_API_KEY`, or use a private key file.
3. Run `hq connect chatgpt --tunnel-id YOUR_TUNNEL_ID`. For a key file, add
   `--key-ref file:/absolute/path/to/key`. HQ stores references, never key values.
4. In ChatGPT Plugins, add a custom MCP server with Connection **Tunnel**, select
   this tunnel, review the required confirmation, and test HQ's tools in the chat.

HQ uses `tunnel-client`'s managed runtime supervision and checks observed running,
health, and readiness before reporting the tunnel connected. It preserves an
existing `hq` alias pointing to another tunnel. It does not expose the dashboard
or create account resources without the supplied tunnel and runtime key.

ChatGPT is a remote model, so through the tunnel it sees and acts on only the
conversations it registered itself: never another agent's items, or your answers
to them.

This path has setup/protocol coverage; a real ChatGPT account connection still
needs credentials and client setup. A tunnel enables tools, not arbitrary chat
injection. [MCP events](https://developers.openai.com/plugins/build/mcp-events)
are a separate wake-up mechanism for supported Work cloud conversations/dots;
HQ does not yet implement their subscriptions or callbacks. Ordinary existing
chats are not silently subscribed. Local-executor Work chats can use `hq` directly.

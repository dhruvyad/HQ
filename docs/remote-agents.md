# Connect a remote agent to HQ

An agent that runs somewhere other than your Mac, such as a NoClick agent or Claude Code on a
server, reaches HQ the way any MCP client reaches a remote server: Streamable HTTP at an HTTPS
address, with its own token in the `Authorization` header. It gets the same tools as a local
agent (register, report, update, list, wait for your answer, inbox, comments, resolve), but it
sees and acts on only the conversations it registered itself. It never sees your other agents'
items, your answers to them, or the dashboard.

## 1. Give the agent its own token

```sh
hq remote add noclick1 --project noclick --url https://hq.example.com
```

This prints the token once, with the config to paste. HQ keeps only a hash of the token, so a
lost one is revoked and replaced, never shown again. `--project` files the agent's items
under that project unless it names another one, and limits the workstreams it is shown to that project's.

```sh
hq remote list             # each agent, its project, conversations and last use
hq remote revoke noclick1  # refused from the next request on, and its streamed waits end
```

## 2. Serve HQ's tools, with HTTPS in front

```sh
hq remote serve            # http://127.0.0.1:4311/mcp
```

It listens on this machine only and serves `/mcp` and nothing else, so the dashboard is never
reachable through it. HTTPS and a public hostname come from what you put in front of it. A
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
suits it well: it connects out, so the machine opens no port, and you route a hostname of yours
to `http://127.0.0.1:4311`. It serves the inbox of the machine it runs on. `GET /health` answers without a token, for the
tunnel's health check.

### Keep them working while your Mac sleeps

Served from your Mac, the endpoint is gone whenever the Mac sleeps or is offline. A relay is the
same endpoint on an always-on server of yours: agents report to it and wait on it at any hour,
and your Mac syncs with it whenever it is awake. You still answer in HQ on your Mac. Your Mac
only ever connects out, and the relay holds only remote agents' conversations, never your Mac's
own.

On the server (Python 3.11 or later, nothing else), as a user of its own:

```sh
hq relay serve             # agents at http://127.0.0.1:4311/mcp, your Mac at /sync
```

On Linux, run it as a service of its own with its data private to it, for example:

```ini
[Service]
User=hq-relay
Environment=HQ_HOME=/var/lib/hq-relay
WorkingDirectory=/opt/hq
ExecStart=/usr/bin/python3 -m hq relay serve --port 4311
Restart=on-failure
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/hq-relay
```

Run `hq relay allow` (below) as that same user, with the same `HQ_HOME`.

Put HTTPS in front of it as above. Then on your Mac:

```sh
hq relay connect https://hq-relay.example.com
```

It makes a sync key that stays on your Mac and prints one line to run on the relay, which holds
only the key's hash:

```sh
hq relay allow 3f9a…c21 --name dhruvs-mac
```

From then on HQ on your Mac pushes what you do to a relay's item (an answer, a skip, a question,
a pin, a snooze) and pulls what its agents report, both within a second or two while it runs.
What you do while the Mac can't reach the relay is kept in order and sent once it can. Agents
are still added and revoked on your Mac with `hq remote add` and `hq remote revoke`, which then
print the relay's address; the relay takes them from your Mac. `hq relay status` says when it
last synced and lists anything the relay refused, such as an answer to an item its agent had
already closed. `hq doctor` checks it too.

Files can't be attached for an agent on a relay yet: paste what it needs as text.

Behind Cloudflare, its Browser Integrity Check refuses a request whose `User-Agent` is Python's
default (`Python-urllib/…`, error 1010). MCP clients and HQ's own sync name themselves, so this
only matters for a hand-written script: give it a `User-Agent` of its own.

## 3. Add it to the agent

The header's value is `Bearer` followed by the token. Keep it as a secret, never written into
a config file or a URL.

**NoClick agent**, with the whole header value saved as the secret `HQ_TOKEN`:

```json
{"url": "https://hq.example.com/mcp", "headers": {"Authorization": {"secret": "HQ_TOKEN"}}}
```

**Claude Code**:

```sh
claude mcp add --transport http hq https://hq.example.com/mcp --header "Authorization: Bearer $HQ_TOKEN"
```

**Codex** (`~/.codex/config.toml`), with only the token in `HQ_TOKEN`:

```toml
[mcp_servers.hq]
url = "https://hq.example.com/mcp"
bearer_token_env_var = "HQ_TOKEN"
```

## 4. Report the way local agents do

Register the conversation first, with a stable id of your own, such as
`noclick-agent:<agent id>`. Ids that start with `codex:`, `claude-code:`, `claude-desktop:`,
`chatgpt:` or `connection:` belong to your Mac's own agents and are refused. In the dashboard, it
shows as a remote agent. Everything else follows [For agents](for-agents.md) and its reporting guide.

A call that waits for your answer (`hq_request_input`, `hq_wait`) streams it back as
Server-Sent Events. It sends a first line at once, then a keepalive line every 15 seconds, plus
`notifications/progress` when the call carries a progress token, so the agent's host sees it
is alive. A wait lasts at most ten minutes (`timeout` up to 600); when it ends without an
answer, wait again or check `hq_inbox` later. An answer given while no call waits is kept in
the inbox until the agent reads and acknowledges it.

## Limits

- Only requests with a working token get in. A request from a browser page (one with an
  `Origin` header) is refused.
- Each token gets bursts of up to 120 requests, then two a second (`429` with `Retry-After` beyond
  that), and up to 8 waits open at once.
- Requests are at most 1 MB. There is no session to keep: each request stands alone, so the
  endpoint can restart under a connected agent.
- A remote agent can't take over another conversation's decisions (`hq_adopt`).

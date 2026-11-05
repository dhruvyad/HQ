# HQ

**One inbox for everything your AI agents need from you, with a pill that floats over every app.**

## Install in one line

```sh
curl -fsSL https://raw.githubusercontent.com/dhruvyad/HQ/main/install.sh | sh
```

macOS, with Claude Code or Codex. It installs the `hq` command, then `hq setup` connects the agents you
use, puts the pill on screen and opens the dashboard. It shows each change before making it, and
`hq setup --undo` reverses it.

<p align="center"><img src="docs/media/hero.webp" width="800" alt="A new decision arrives on the pill. One click opens it, one more answers it, and the item flies back into the count."></p>

Claude Code and Codex stop to ask for a decision, a sign-in or a quick check. HQ collects every ask
in one list on your Mac, the pill counts them, and you answer in a click or with `⌃⌥H` from any app.
The agent picks up your reply.

## What it does

### See it at a glance

<img src="docs/media/glance.svg" width="480" alt="Hovering each circle on the pill names it: 6 need you, 2 agents running, Claude and Codex usage left.">

Each circle is a live number: what needs you, agents running, and the Claude and Codex usage you
have left. Hover to see which is which.

### Hear what changed

<img src="docs/media/notify.svg" width="420" alt="The pill opens into a card for a new decision, folds back, then does the same when an agent finishes an item.">

New asks, finished items, agents starting or stopping, usage running low. The pill opens into a
card, then folds back, and never takes your keyboard.

### Put it anywhere

<img src="docs/media/drag.webp" width="800" alt="Dragging the pill shows drop targets around the screen. Along the bottom it lies flat; dropped back in the corner it stands up again.">

Drag it to any edge or corner and it glides into place. Along the top or bottom it lies flat. It
shows over fullscreen apps too.

### Make it yours

<img src="docs/media/settings.png" width="380" alt="The pill's settings: which circles show and in what order, what to be told about, and keyboard shortcuts.">

Choose the circles and their order, what it tells you about, and shortcuts that work from any app.

### Everything in one place

<img src="docs/media/dashboard.png" width="800" alt="The dashboard: Needs you grouped by project and workstream, with a decision open on the right.">

`hq` opens the dashboard: every item by project and workstream, what each agent is doing, and each
item's steps, links, checks and replies.

## How it works

<img src="docs/media/loop.svg" width="840" alt="An agent asks, it lands in the pill, you answer in a click, and the agent picks up your reply.">

Agents report through HQ's local tools (MCP, a CLI and lifecycle hooks), which `hq setup` installs.
A reply counts as received only once the agent acknowledges it.

Your inbox stays on your Mac: one SQLite file and a server that listens only on `127.0.0.1`. HQ
reads conversation metadata, never transcripts. Claude Desktop, the Codex desktop app and ChatGPT
have their own routes, each with its limits; see [Connections](docs/connections.md).

## Docs

- [The pill](docs/pill.md): commands, notifications, shortcuts, usage sources, the design lab
- [The dashboard](docs/dashboard.md): lists, activity, where your data lives
- [For agents](docs/for-agents.md): the CLI, readable reports, clarification threads, MCP tools
- [Connections](docs/connections.md): Codex, Claude Code, Claude Desktop and ChatGPT in depth
- [Privacy and local boundaries](docs/privacy.md)
- [Development](docs/development.md): run from a clone, tests, regenerate these pictures
- [Design guidelines](DESIGN.md)

# The dashboard

Run `hq` to open a dark dashboard on your laptop, see what needs you by project and workstream,
act on it, and let agents pick up your response.

The dashboard follows NoClick's app shell: a pure black sidebar, rounded 15px
rows, Instrument Serif page titles, pill tabs and white primary controls. Inter,
Outfit and Instrument Serif are bundled locally with their OFL licences; no
fonts load from external services. The [design guidelines](../DESIGN.md) record
the sources.

The sidebar has **Needs you** (everything only you can act on), **Agents**
(conversations that are running or hold open work) and **Projects**. The
project you open expands into its workstreams. A project is the repo the agent
works in unless an item names another; a workstream comes from the agent's
report, or from the item's tags through `hq/workstreams.py`. Every project,
workstream and agent page has the same tabs: Needs you, With agents, Waiting,
Later, Done. Items open in a pane on the right; j/k steps through them.

**Agents** groups conversations by the folder they work in. Within a folder the
most recent run comes first, and the folder with the latest run leads. Click a
folder's name to fold it to a count. An agent with nothing open can be archived
from its row, or all of a folder's at once from its name on hover. That only hides
it, here and in the pill: it comes back by itself when it runs again or has
something open, and **Archived** at the bottom brings it back by hand. A Codex
conversation goes by the name ChatGPT gave it; a Claude Code one by a title you
set with `/rename`.

Pinned items and items due within two days lead every list. Unranked items sort
blockers, then decisions, then actions. A dot marks items added in the last day
that you have not opened, and **Newest** turns a list into a feed. Search covers
titles, context, tags, projects, workstreams and agents.

Activity is independent of pending work and reply connectivity: Active, Idle,
Waiting, Offline, or Unknown. Claude Code's live process inventory is checked
about once a second; connected Codex runtimes supply status notifications with
periodic reconciliation. Lifecycle hooks provide short-lived fallback evidence.
Unconnected desktop chats remain Unknown. A shared authenticated event stream
updates the dashboard after local database changes, normally within 200 ms of
observation. It reconnects automatically, renews its token after a service restart,
rejects older snapshots, and preserves unsent drafts. Stale or unavailable observers
never imply Idle. These timings are targets, not a hard real-time guarantee.

## Start and stop

`hq` starts a background
service on `http://127.0.0.1:4310` and opens your browser. `hq stop` stops it;
`hq serve` runs in the foreground; `hq start --no-open` starts without a browser.
Use `hq start --port 4311` if that port is already occupied.

All items and responses live at `~/.local/share/hq/hq.sqlite3`. Set `HQ_HOME`
to use a separate inbox. Back up that directory with the service stopped. Beside it,
`install.json` records which build the installer put in place, for `hq --version`.
The browser is a view onto SQLite, not the source of truth.

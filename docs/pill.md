# The pill

```sh
hq float
```

A small black pill floats over every app, Space and fullscreen window without taking
focus away from what you are doing. It starts at the top left, and runs side to side
when centered on the top or bottom edge. It is a capsule of circles, each one a live
number: what needs you, decisions waiting, due soon, agents running, or the Claude and
Codex usage you have left.

- **Hover** it: each circle says what it is.
- **Drag** it: drop targets appear, the nearest lights up, and it glides into place.
  Escape cancels. It remembers where you put it and follows your cursor between displays.
- **Click** it: the list of what needs you. Click elsewhere and the list closes.
- **Click a row**: the box expands into the full item, the same pane as the dashboard
  (steps, options, checks, replies, snooze, skip). Your draft survives if you look away.
- **Right-click** it, or use `…` in the list: choose which circles show and in what order,
  what it notifies you about, your keyboard shortcuts, open the dashboard, hide it for an
  hour, or quit.

When something changes, the capsule morphs into a card saying what happened, then folds
back: items added or removed, an agent starting or stopping, usage running low, or a reply
that failed to deliver. A burst becomes one card, hovering holds it open, and clicking it
opens that item.

Shortcuts work from any app and need no permissions. `⌃⌥H` opens Needs you and minimizes it
again, keeping your place and draft. `⌃⌥J` and `⌃⌥K` move through items, and `⌃⌥D` opens the
dashboard. All of them can be changed or cleared in the panel, and each can have several keys.
A key can also be modifiers alone, such as `⌃` with left `⌥` (sides count): it fires when you let
go, and only if no other key, click or scroll came in between, so `⌃⌥J` or `⌃`-click never set it off.
One left-hand modifier alone is refused, since it is tapped too often in passing.

Usage comes only from what each tool itself reports, and a circle with no source stays
empty rather than guessing. Codex works with nothing to set up. For Claude there are two
opt-in routes:

- `hq usage install` adds a status line, the only documented source. While any status line
  is set, Claude Code hides most of its footer hints. `hq usage uninstall` undoes it.
- `hq usage login` reads Claude Code's saved login from the Keychain (macOS asks first) and
  asks the unofficial usage endpoint that community tools use, so the footer stays as it
  is. The token stays in memory, goes only to Anthropic, and is never refreshed by HQ. It
  could stop working without notice. `hq usage logout` turns it off.

### Several accounts

```sh
hq accounts                       # each tool's accounts, which one is signed in, and usage left
hq accounts add claude            # sign in to another account in your browser; nothing switches
hq accounts use claude bob@example.com
hq accounts remove codex side@example.com
```

Clicking a usage circle shows the same. Adding an account runs the tool's own sign-in in a
throwaway folder, never against your real one, so your current login is untouched. HQ keeps
each account's login in your Keychain, under its own items named `HQ account <tool> <key>`,
and only there.

Switching works the way `/login` does, and it always goes through:

- **Claude:** HQ replaces the account's own part of Claude Code's Keychain login
  (`claudeAiOauth` and the keys `/login` itself replaces) and `oauthAccount` in
  `~/.claude.json`, and clears the caches `/login` clears. MCP-server logins, plugin secrets
  and everything else in that item stay exactly as they were. Every open Claude session
  moves to the new account at its next message.
- **Codex:** HQ replaces `~/.codex/auth.json`. Open Codex sessions keep the account they
  started with, so HQ says how many are open and asks you to restart them. Codex set to keep
  its login in the Keychain (`cli_auth_credentials_store`) can't be switched yet.

These three are the only files or items of another app that HQ ever writes, and only when
you switch. Before switching away, HQ saves the outgoing account's latest login, so you can
switch back. If that login was being renewed at that moment, the account is marked to sign
in again in case switching back fails. A write that fails is put back, so nothing is left
half switched.

For an account that isn't signed in, HQ reads usage with that account's saved login about
every 30 to 60 minutes while the pill or dashboard is open, never two accounts in a row. The
signed-in account's login is its own tool's to renew, never HQ's. A saved Claude login is
held by HQ alone, so once its access token runs out HQ renews it the way Claude Code renews
its own, asking for nothing beyond what it already holds, and keeps the new one in its own
Keychain items. The sign-in itself still ends when Claude ends it, about a month after you
signed in, and the account then says it needs a sign-in. A switch waits for a renewal under
way, so it never takes a login about to be replaced. Codex logins are never renewed: a saved
one is read only while its token is still valid. When HQ can't read an account, it shows the
last numbers it read, with their time, except that a limit whose reset time has passed shows
as full again. Logins go only to Anthropic or OpenAI, and never into logs, the database or
anything HQ shows. They reach the Keychain on stdin, with one exception: when MCP and plugin
logins make Claude Code's own Keychain entry too large for that, HQ writes the entry the way
Claude Code itself then does, as an argument, briefly visible to programs running as you.

`hq float stop` quits it, `hq float status` reports on it, and `hq float install` starts it
at login (`hq float uninstall` undoes that). It needs macOS and Xcode's command line tools
(`xcode-select --install`), because it compiles one small Swift file the first time it runs.
That file only moves the window; the panel is an ordinary local page at `/float`, inside
the same localhost, origin and token boundary as the dashboard. `hq update` restarts the pill
if it is running, and it compiles that file again only when it changed. `hq doctor` says
whether the pill is running, whether it is built for this version, and how your displays are
arranged.

## Design lab

```sh
hq float lab
hq float lab --item <item-id>
```

Refine the panel in batches rather than one change at a time. This opens the real panel
beside a bench of presets and design axes (how the circles look, where it can dock, how far
before a click becomes a drag, what happens after you answer, and so on). Flipping an axis
changes the real window immediately, so drag, focus and always-on-top are never simulated.
Leave a note on anything, then **Send picks** returns the whole round at once: it prints
them, saves them to `float-lab-picks.json` in HQ's data folder, and with `--item` replies to that
item so they reach the owning agent.

The lab runs on throwaway sample data with its own temporary server, so nothing you click
reaches a real agent or your real inbox. For the next round, edit `AXES` in
`hq/static/float-lab.js` and the matching `feel` default in `hq/static/float.js`.

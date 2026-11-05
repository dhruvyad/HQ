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
dashboard. All of them can be changed or cleared in the panel.

Usage comes only from what each tool itself reports, and a circle with no source stays
empty rather than guessing. Codex works with nothing to set up. For Claude there are two
opt-in routes:

- `hq usage install` adds a status line, the only documented source. While any status line
  is set, Claude Code hides most of its footer hints. `hq usage uninstall` undoes it.
- `hq usage login` reads Claude Code's saved login from the Keychain (macOS asks first) and
  asks the unofficial usage endpoint that community tools use, so the footer stays as it
  is. The token stays in memory, goes only to Anthropic, and is never refreshed by HQ. It
  could stop working without notice. `hq usage logout` turns it off.

`hq float stop` quits it, `hq float status` reports on it, and `hq float install` starts it
at login (`hq float uninstall` undoes that). It needs macOS and Xcode's command line tools
(`xcode-select --install`), because it compiles one small Swift file the first time it runs.
That file only moves the window; the panel is an ordinary local page at `/float`, inside
the same localhost, origin and token boundary as the dashboard.

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

# Development

**Python 3.11+. Zero runtime dependencies. SQLite. No cloud storage.**

## Run from a clone

```sh
git clone https://github.com/dhruvyad/HQ
cd HQ
./bin/hq
```

Optional installation: `python3 -m pip install .` (prefer a virtual environment).
The source launcher can also be linked onto your PATH, and `./install.sh` from a clone installs
that clone with `uv` instead of downloading one.

## Versions and updates

`hq --version` names the build, such as `hq 0.1.0 (abc1234, installed 2026-11-06)`. The
installer finds the commit first (`git ls-remote`, or GitHub's API without git), installs the
archive of that exact commit, and records `{commit, ref, source, dirty, installed_at, hq, env}`
in `install.json` in HQ's data folder (`HQ_HOME` if set). From a clone it records the clone's
commit, branch, and whether it has uncommitted changes. `./bin/hq --version` asks the clone's
git instead, and says "build not recorded" when neither is there.

`hq update` downloads the published installer for the recorded branch (`HQ_REF` picks another),
runs it with `HQ_NO_SETUP=1`, prints the old and new commit, and restarts the server and the
pill if they were running. It never opens the inbox. An install from a clone reinstalls that
clone as it is now, so pull there first; `./bin/hq update` itself refuses and says to use git.
Agent conversations already open keep the previous MCP tools until they restart.

`hq doctor` (or `hq doctor --json`) checks the install, the inbox schema, the server, both
agents' MCP entries and hooks, the pill, the locale, PATH and saved accounts. It only reads:
the inbox read-only, the agents' config files for HQ's entries, never a login or the Keychain.
When you add a migration, bump `SCHEMA` in `hq/store.py`; the doctor compares the inbox with a
throwaway one made by the current code.

## Tests

```sh
python3 -m unittest discover -s tests -v
node --check hq/static/app.js
node --check hq/static/float.js
node --check hq/static/float-lab.js
node tests/test_ui.js
swiftc -typecheck hq/FloatPanel.swift  # macOS only
```

Tests cover SQLite persistence and migration, multiple independent items per
conversation, ownership changes and deferral, concurrent answers, per-session routing,
offline acknowledgement, timeout/cancellation, native steering decisions,
the HTTP origin boundary, bundled font delivery, and an end-to-end MCP human response.
The installer runs piped to `sh` against fake `uv`, `git` and `curl` in an empty home, so the
tests never download or install anything; `hq update` and `hq doctor` run on temporary homes.

Protocol references: [Codex app-server](https://learn.chatgpt.com/docs/app-server),
[Claude Code MCP](https://code.claude.com/docs/en/mcp),
[Claude channels](https://code.claude.com/docs/en/channels-reference),
[local MCP clients](https://modelcontextprotocol.io/docs/develop/connect-local-servers),
[ChatGPT MCP setup](https://developers.openai.com/plugins/build/app-quickstart).

## The README's pictures

```sh
node docs/media/make/make.mjs              # all of them
node docs/media/make/make.mjs hero usage   # just these: hero, clips, usage, drag, settings, dashboard
```

Update them whenever the pill, the panel or the dashboard changes. Every picture shows the real
UI on made-up data (`docs/media/make/fixtures.py`). Each take starts its own HQ on a throwaway
inbox and a free port, in headless Chrome, so it never touches your inbox, your screen or a real
agent.

- **`hero.webp`, `drag.webp`**: recorded at 2x and about 60 frames a second, then evened out to 30
  and encoded as lossless animated WebP. Lossy frames are blended onto the one before, which
  leaves ghosts of old shapes behind after a fade; lossless costs the hero about a third more
  (4.7 MB against 3.4). The fake desktop is laid out in display points and zoomed
  2x as a whole, since a screencast only sends frames at the page's CSS size.
- **`glance.svg`, `notify.svg`**: the pill's own clips, drawn as animated SVG by `vector.mjs`. They
  stay sharp at any zoom and weigh a few kilobytes. What the page says (each circle's label, the
  card for a burst, the badge it leaves, the dot and the history) is read from the real page by
  `snapshot.js`, which draws any part of it as SVG from what the browser laid out. The pill's
  sizes and motion are constants in `vector.mjs`, measured from `/float`, with its springs taken
  from `float.js`, so a change there needs the matching change here.
- **`usage-accounts.svg`, `usage-switch.svg`, `usage-add.svg`**: the Usage view, drawn the same way
  by `usage.mjs` from every state the real panel goes through. They run against a throwaway HQ
  outside lab mode, with a fake HOME holding fake Claude and Codex login files, a PATH without
  either tool, and saved accounts as database rows only. Every write (switch, add, remove) is
  answered inside the page, since against the server it would reach the Keychain.
- **`settings.png`, `dashboard.png`**: 2x screenshots.
- **`loop.svg`**: drawn by hand.

Every picture has rounded corners, 12 CSS pixels at the width the README shows it, and is
transparent outside them, so it sits as well on GitHub's light page as on its dark one.

It needs Google Chrome (or `CHROME=/path/to/chrome`), `ffmpeg` with libwebp, and Python's
`fonttools` with `brotli` to embed small Inter and Instrument Serif subsets in the SVGs (or `uv`,
which fetches them).

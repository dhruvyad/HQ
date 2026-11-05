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

Protocol references: [Codex app-server](https://learn.chatgpt.com/docs/app-server),
[Claude Code MCP](https://code.claude.com/docs/en/mcp),
[Claude channels](https://code.claude.com/docs/en/channels-reference),
[local MCP clients](https://modelcontextprotocol.io/docs/develop/connect-local-servers),
[ChatGPT MCP setup](https://developers.openai.com/plugins/build/app-quickstart).

## The README's pictures

```sh
node docs/media/make/make.mjs              # all of them
node docs/media/make/make.mjs hero clips   # just these
```

Update them whenever the pill, the panel or the dashboard changes. Every picture shows the real
UI on made-up data (`docs/media/make/fixtures.py`). Each take starts its own HQ on a throwaway
inbox and a free port, in headless Chrome, so it never touches your inbox, your screen or a real
agent.

- **`hero.webp`, `drag.webp`**: recorded at 2x and about 60 frames a second, then evened out to 30
  and encoded as animated WebP: the hero lossy at q95, the drag lossless, because lossy blending
  leaves trails over its dimmed screen. The fake desktop is laid out in display points and zoomed
  2x as a whole, since a screencast only sends frames at the page's CSS size.
- **`glance.svg`, `notify.svg`**: the pill's own clips, drawn as animated SVG by `vector.mjs`. They
  stay sharp at any zoom and weigh a few kilobytes. The sizes, colours and baselines in it were
  measured from `/float`, and the motion is the panel's CSS, with its springs taken from
  `float.js`. They are not read from the page, so a change to the pill's look or motion needs the
  matching change there.
- **`settings.png`, `dashboard.png`**: 2x screenshots.
- **`loop.svg`**: drawn by hand.

Every picture has rounded corners, 12 CSS pixels at the width the README shows it, and is
transparent outside them, so it sits as well on GitHub's light page as on its dark one.

It needs Google Chrome (or `CHROME=/path/to/chrome`), `ffmpeg` with libwebp, and Python's
`fonttools` with `brotli` to embed a small Inter subset in the SVGs (or `uv`, which fetches them).

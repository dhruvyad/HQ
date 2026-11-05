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
node docs/media/make/make.mjs           # all of them
node docs/media/make/make.mjs hero drag # just these
```

They are recorded from the real UI on made-up data, in headless Chrome, so update them whenever the
pill, the panel or the dashboard changes. Each take starts its own HQ on a throwaway inbox and a
free port: it never touches your inbox, your screen or a real agent. It needs Google Chrome (or
`CHROME=/path/to/chrome`) and `ffmpeg`. The fixtures are in `docs/media/make/fixtures.py`; the
diagram, `docs/media/loop.svg`, is drawn by hand.

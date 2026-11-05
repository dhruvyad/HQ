# Privacy and local boundaries

**Python 3.11+. Zero runtime dependencies. SQLite. No cloud storage.**

The dashboard binds only to `127.0.0.1`. It rejects cross-origin requests, DNS
rebinding hostnames, and API requests without the page's random token. It uses
no external scripts, fonts or analytics. The browser keeps only view settings for
this origin: folded groups, sort order, and which items you have opened. Like other local
tools, trusted processes running as your user can access the inbox; this is
not a multi-user authorization boundary. Session names and responses are
rendered as text, never executable HTML.

What HQ reads, and when:

- **Your conversations:** only their metadata, such as names and folders. Discovery, hooks and
  activity checks never read transcripts. See [Connections](connections.md).
- **Usage left:** Codex reports it with nothing to set up. Claude usage is off until you opt in
  to one of two routes, described in [the pill](pill.md).
- **Replies:** HQ never says a reply was received until the agent acknowledges it, and pending
  replies survive disconnects.

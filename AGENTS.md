# HQ

Local-first agent inbox. Python 3.11+, SQLite, standard-library HTTP server,
vanilla HTML/CSS/JS. Keep runtime dependencies empty and the interface simple.

Run `python3 -m unittest discover -s tests -v` and
`node --check hq/static/app.js` before committing. Test response routing across
sessions, persistence, concurrent answers, and local HTTP boundaries.

Never claim a reply was received without an agent acknowledgement. Preserve
pending replies after disconnects. Never write to another app's session database.
Discovery reads metadata only. Never expose the dashboard to the network.
Keep all personal session data, responses, credentials, and databases out of Git.

Follow pull requests through CI on their latest head commit and fix failures.

When reporting work to HQ, follow `hq/AGENT_GUIDE.md`: action-first titles,
brief context/unlocks, recommendations, ordered steps, direct links, and copy
blocks. Human effort is separate from external waits. Report every independent
action as its own item and close the loop after acting on a reply.

Proactively populate HQ with every unfinished requirement, setup gap, and
unverified integration in this task. Keep agent work and concrete human actions
separate, and update the queue before finishing a turn. Metadata discovery or
passing protocol tests does not establish a live agent connection.

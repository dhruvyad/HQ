"""Metadata discovery is deliberately separate from a live connection."""
import json
import sqlite3
from pathlib import Path


def discover(store, limit=50):
    home = Path.home()
    added, warnings = [], []
    db_path = home / ".codex/state_5.sqlite"
    if db_path.exists():
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute("SELECT id,title,cwd FROM threads WHERE archived=0 AND source NOT LIKE '%subagent%' ORDER BY updated_at DESC LIMIT ?", (limit,))
                for row in rows:
                    id = "codex:" + row["id"]
                    store.register(id, (row["title"] or "").strip()[:500] or row["id"], "codex", row["id"], row["cwd"])
                    added.append(id)
        except sqlite3.Error as error:
            warnings.append(f"Codex discovery unavailable: {error}")
    index = home / ".claude/projects"
    if index.exists():
        # Index files contain conversation metadata, without reading transcripts.
        candidates = sorted(index.glob("*/sessions-index.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        entries = []
        for path in candidates:
            try:
                entries.extend(json.loads(path.read_text()).get("entries", []))
            except (OSError, ValueError):
                continue
        for row in sorted(entries, key=lambda r: r.get("modified", ""), reverse=True)[:limit]:
            external_id = row.get("sessionId")
            if external_id:
                id = "claude-code:" + external_id
                name = (row.get("summary") or row.get("firstPrompt") or "Claude conversation").strip()[:500]
                store.register(id, name or external_id,
                               "claude-code", external_id, row.get("projectPath"))
                added.append(id)
    return {"discovered": len(added), "session_ids": added, "warnings": warnings,
            "message": "Imported recent conversation metadata. Discovery does not establish a live connection or infer blockers."}

"""Made-up agents and items for the README's pictures, so they never show anyone's real work.

    python3 fixtures.py seed | add | resolve PREFIX

Only runs against a throwaway inbox: HQ_HOME must be a folder make.mjs created.
"""
import os
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
if "hq-media-" not in os.environ.get("HQ_HOME", ""):
    sys.exit("Refusing to touch an inbox make.mjs did not create")
from hq.store import Store  # noqa: E402

WHY = {"matters": {"lead": "Unblocks the next release", "points": ["Two teams are waiting on it"]},
       "needs_you": {"lead": "This is your call", "points": ["Nobody else has the context"]}}
SESSIONS = {
    "release": ("claude-code:demo-release", "Ship the spring release", "claude-code", "/Users/you/code/harbor", "active"),
    "cluster": ("codex:demo-cluster", "Move reports to the new cluster", "codex", "/Users/you/code/lantern", "active"),
    "onboarding": ("claude-code:demo-onboarding", "Rewrite the onboarding emails", "claude-code", "/Users/you/code/harbor", "idle"),
    "export": ("codex:demo-export", "Fix the billing export totals", "codex", "/Users/you/code/ledger", "waiting"),
    "docs": ("claude-code:demo-docs", "Update the API reference", "claude-code", "/Users/you/code/lantern", "idle"),
}
HARBOR, LANTERN, LEDGER = {"project": "Harbor"}, {"project": "Lantern"}, {"project": "Ledger"}


def items():
    today = date.today().isoformat()
    return [
        # (session, minutes old, title, fields)
        ("release", 6, "Choose the launch date for the spring release",
         {"kind": "question", "owner": "human", "minutes": 2, "rank": 40, **WHY, **HARBOR, "workstream": "Release",
          "options": [{"label": "Tuesday the 12th", "note": "Gives support a full week before the holidays.", "recommended": True},
                      {"label": "Thursday the 14th", "note": "Two more days for the docs, less room after launch."},
                      {"label": "After the holidays", "note": "Safest, but the partner demo slips with it."}]}),
        ("cluster", 35, "Approve the staging certificate so deploys can resume",
         {"kind": "blocker", "owner": "human", "minutes": 1, "rank": 20, **WHY, **LANTERN, "workstream": "Infrastructure",
          "steps": [{"text": "Open the certificate request", "link": {"label": "example.com/certs", "url": "https://example.com/certs"}},
                    "Approve it for staging only"]}),
        ("onboarding", 14, "Send one message to the demo bot, then check what it asks",
         {"owner": "human", "minutes": 3, "rank": 210, **WHY, **HARBOR, "workstream": "Onboarding",
          "steps": [{"text": "Open the demo", "link": {"label": "example.com/demo", "url": "https://example.com/demo"}},
                    {"text": "Send this message", "copy": "Set up a weekly report and send it to the team channel"},
                    {"text": "Check what it asks you", "checks": ["A list of channels to pick from",
                                                                   "No question about the report's format",
                                                                   "A preview before anything is sent"]}]}),
        ("cluster", 150, "Renew the example.com domain before it lapses",
         {"owner": "human", "minutes": 4, "rank": 200, "due": today, **WHY, **LANTERN, "workstream": "Infrastructure",
          "steps": [{"text": "Open the registrar", "link": {"label": "registrar", "url": "https://example.com/registrar"}},
                    "Renew for one year"]}),
        ("export", 95, "Check the export totals against last month's invoice",
         {"owner": "human", "minutes": 5, "rank": 230, **WHY, **LEDGER, "workstream": "Billing",
          "steps": [{"text": "Open both files", "link": {"label": "example.com/exports", "url": "https://example.com/exports"}},
                    {"text": "Compare the totals", "checks": ["Totals match to the cent", "No customer appears twice"]}]}),
        ("docs", 60 * 26, "Pick who gets the invite to the partner preview",
         {"kind": "question", "owner": "human", "minutes": 1, "rank": 120, **WHY, **HARBOR, "workstream": "Onboarding",
          "options": [{"label": "Everyone on the waitlist", "note": "More feedback, more support load."},
                      {"label": "Only paying partners", "note": "Smaller, and they asked first.", "recommended": True}]}),
        # With the agent or someone else, so they never count as needing you.
        ("release", 8, "Retry the failed import",
         {"owner": "agent", **HARBOR, "workstream": "Release", "detail": "Running again with the corrected file."}),
        ("cluster", 40, "Wait for the cluster quota increase",
         {"owner": "external", "waiting_on": "Cloud provider", **LANTERN, "workstream": "Infrastructure",
          "detail": "Requested 40 more cores. Usually approved within a day."}),
    ]


def seed(store):
    for sid, name, provider, cwd, _ in SESSIONS.values():
        store.register(sid, name, provider, cwd=cwd)
    now = time.time()
    for key, age, title, fields in items():
        fields = dict(fields)
        kind, owner = fields.pop("kind", "todo"), fields.pop("owner")
        detail, options = fields.pop("detail", ""), fields.pop("options", None)
        item = store.add(SESSIONS[key][0], title, detail, kind, options=options, owner=owner,
                         readable=owner == "human", source="demo", **fields)
        with store.db() as db:
            db.execute("UPDATE items SET created=?,updated=? WHERE id=?", (now - age * 60, now - age * 60, item["id"]))
    done = store.add(SESSIONS["docs"][0], "Confirm the backup restore finished", "", "todo", owner="human", minutes=2,
                     steps=["Open the restore log", "Look for the final success line"], readable=True, source="demo",
                     **WHY, **LANTERN)
    store.reply(done["id"], "Looks right")
    # Running for a while already, so nothing reads as having just started.
    for sid, _, _, _, state in SESSIONS.values():
        store.observe_activity(sid, state, "demo", lease=3600, observed=now - 900)


def add(store):
    """A new decision arrives, as an agent would report it."""
    store.add(SESSIONS["cluster"][0], "Name the new reports page", "", "question", owner="human", minutes=1, rank=30,
              readable=True, source="demo", project="Lantern", workstream="Reports",
              options=[{"label": "Reports", "note": "Plain, and it's what people already call it.", "recommended": True},
                       {"label": "Insights", "note": "Broader, but vaguer about what's inside."}],
              matters={"lead": "The page ships with the next release", "points": ["The name goes in the sidebar and the emails"]},
              needs_you={"lead": "It's a product call", "points": ["Both names test about the same"]})


def resolve(store, prefix):
    """An agent finishes an item on its own."""
    with store.db() as db:
        row = db.execute("SELECT id FROM items WHERE title LIKE ? AND status='open'", (prefix + "%",)).fetchone()
    store.resolve(row["id"], "Done")


if __name__ == "__main__":
    command, store = sys.argv[1], Store()
    if command == "seed":
        seed(store)
    elif command == "add":
        add(store)
    elif command == "resolve":
        resolve(store, sys.argv[2])
    else:
        sys.exit(__doc__)

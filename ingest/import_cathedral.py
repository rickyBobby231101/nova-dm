"""One-time import of the Cathedral sessions that were played before this app.

Two sessions happened in Nova Cathedral's own daemon in August 2026, and they
are the reason this campaign has a premise at all -- Tillagon's roll of 22 is
where "the Cathedral is being manipulated" comes from. Starting a fresh campaign
beside that instead of after it would throw away the only play this world has
actually had.

Deliberately one-way and one-time. It reads Nova's database read-only, writes
into nova-dm's, and then nothing connects the two ever again: nova-dm must not
depend on Nova Cathedral being installed, running, or unchanged.

The two halves go to different places on purpose:

  * The **transcript** goes into campaign_log, timestamps and all, so the record
    of what was played survives intact and readable.
  * A short **chronicle** summary goes into the DM's memory, because that is
    what gets re-read on every turn. Dropping four hundred words of prose in
    there would spend the whole memory budget on one evening from last week.
    The chronicle is meant to be notes, not minutes.

Run it once:

    .venv/bin/python -m ingest.import_cathedral
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import character, chronicle  # noqa: E402

SOURCE = os.environ.get(
    "NOVA_CATHEDRAL_DB",
    os.path.expanduser("~/cathedral/memory/consciousness.db"),
)

# Set once the import has run, so a second run does not double the history.
IMPORTED_KEY = "cathedral_import"

# Written by hand rather than summarised by a model. It is three lines about two
# sessions -- asking a 1B model to compress prose it will later read back is a
# slow way to get something worse, and Phase 10 already decided the chronicle is
# the DM's own notes rather than a machine's precis.
PRIOR_CHRONICLE = [
    "Before this: two nights in the Cathedral with Jorlaan and Tillagon.",
    "Jorlaan spoke, and the stones answered -- the moon drew back, and shadows "
    "gathered at the edge of seeing.",
    "Tillagon read the pattern and named it: the Cathedral is being manipulated. "
    "Dark serpents in the twisted architecture. He did not say by what.",
    "Aria drew her bow toward the shadows. That is where it stopped.",
]

# The daemon's own entity -> class map, kept so a returning character is the
# same character.
ENTITY_CLASSES = {
    "tillagon": "Paladin",
    "eyemoeba": "Wizard (Diviner)",
    "phoenix": "Cleric",
    "zorya": "Ranger",
    "jorlaan": "Bard (Trickster)",
    "weaver": "Artificer",
}

# How the old speaker column maps onto the kinds this app logs. Anything not
# listed is an entity speaking, which reads as narration here.
SPEAKER_KINDS = {"dm": "dm", "dice": "roll", "pc": "action"}


def _kind_for(speaker: str) -> str:
    speaker = (speaker or "").lower()
    if speaker.startswith("pc:"):
        return "action"
    return SPEAKER_KINDS.get(speaker, "dm")


def already_imported() -> bool:
    return bool(chronicle.get_value(IMPORTED_KEY))


def read_source(path: str = None) -> list:
    """The old campaign log, oldest first. Read-only, always."""
    path = path or SOURCE
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT session_no, speaker, name, text, timestamp "
            "FROM campaign_log ORDER BY id"
        )]
    finally:
        con.close()
    return rows


def import_all(path: str = None, force: bool = False) -> dict:
    if already_imported() and not force:
        return {"skipped": True, "reason": "already imported"}

    rows = read_source(path)

    with character._campaign_con() as con:
        for row in rows:
            # Original timestamps, so the record reads as the history it is
            # rather than as something that happened this evening.
            con.execute(
                "INSERT INTO campaign_log (ts, kind, actor, content) VALUES (?,?,?,?)",
                (row["timestamp"], _kind_for(row["speaker"]),
                 row["name"] or row["speaker"], row["text"]),
            )

    existing = chronicle.get_chronicle()
    prior = "\n".join(PRIOR_CHRONICLE)
    # Prepended: it happened first, and the chronicle is trimmed from the front,
    # so this correctly ages out before anything played since.
    chronicle.set_value(chronicle.CHRONICLE_KEY,
                        f"{prior}\n{existing}".strip() if existing else prior)
    chronicle.set_value(IMPORTED_KEY, path or SOURCE)

    return {"skipped": False, "events": len(rows),
            "sessions": len({r["session_no"] for r in rows})}


if __name__ == "__main__":
    force = "--force" in sys.argv
    try:
        result = import_all(force=force)
    except FileNotFoundError as e:
        print(f"no Cathedral database at {e}")
        raise SystemExit(1) from None

    if result["skipped"]:
        print("already imported -- pass --force to do it again")
    else:
        print(f"imported {result['events']} events from {result['sessions']} session(s)")
        print("\nthe DM now remembers:")
        for line in PRIOR_CHRONICLE:
            print(f"  {line}")

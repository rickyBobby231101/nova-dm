"""Phase 10: what the DM remembers between turns.

campaign_log has been written to since Phase 2 and read back by nothing. Every
DM turn was built from the character sheets and the current board and nothing
else, so the DM opened each turn with no idea what the party had just done,
where they were, or what they were trying to achieve. It could describe a room
it had described differently a minute earlier and never notice.

Memory here is two layers, because they answer different questions:

  * The **log tail** -- the last handful of things that actually happened, in
    order. This is short-term continuity: what was just said and done.
  * The **chronicle** -- the DM's own running account of the campaign, plus
    where the party currently is. This is what survives once the tail has
    scrolled past, and the DM writes it itself through the set_scene tool
    rather than anything here summarizing on its behalf.

Both are bounded on purpose. The DM may be a 4B model on a 2012 laptop, and the
spec's budget is roughly 2k tokens for the whole turn -- so this module trims to
a character budget and drops the oldest entries first, rather than letting a
long session quietly grow the prompt until the local model falls over.
"""
from datetime import datetime

from . import character

SCENE_KEY = "scene"
CHRONICLE_KEY = "chronicle"
# Owned by engine.campaign, read here. Named in both places rather than imported,
# because campaign imports this module and the cycle is not worth the tidiness.
PREMISE_KEY = "premise"

# What the DM needs to recall is the story, not the arithmetic. Rolls, damage
# and healing are deliberately left out: the board and the sheets already carry
# their outcome, and at ~2k tokens a turn they would crowd out the narration
# that gives them meaning.
NARRATIVE_KINDS = ("action", "dm", "encounter", "level_up", "xp")

DEFAULT_LIMIT = 20
# Trimmed from 3000 to pay for the premise now sitting above it. The log tail is
# the cheapest thing here to shorten: the chronicle already carries what mattered
# from the events that scroll off, which is the whole reason it exists.
DEFAULT_BUDGET = 2000


def get_value(key: str, default: str = "") -> str:
    with character._campaign_con() as con:
        row = con.execute("SELECT value FROM game_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_value(key: str, value: str) -> None:
    with character._campaign_con() as con:
        con.execute(
            "INSERT INTO game_state (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, datetime.now().isoformat(timespec="seconds")),
        )


def get_scene() -> str:
    return get_value(SCENE_KEY)


def get_chronicle() -> str:
    return get_value(CHRONICLE_KEY)


def set_scene(location: str, summary: str = None) -> dict:
    """Record where the party is, and optionally fold a line into the chronicle.

    The chronicle is appended to rather than replaced: a DM that rewrites its
    own history every scene loses the campaign. Trimming happens on the way out
    in context_block, not here, so nothing is ever actually destroyed -- the
    full account stays in game_state for anyone reading it later.
    """
    location = (location or "").strip()
    if location:
        set_value(SCENE_KEY, location)

    summary = (summary or "").strip()
    if summary:
        existing = get_chronicle()
        set_value(CHRONICLE_KEY, f"{existing}\n{summary}".strip() if existing else summary)

    return {"scene": get_scene(), "chronicle": get_chronicle()}


def recent(limit: int = DEFAULT_LIMIT, kinds=NARRATIVE_KINDS) -> list:
    """The last `limit` narrative entries, oldest first so they read as a story."""
    placeholders = ",".join("?" for _ in kinds)
    with character._campaign_con() as con:
        rows = con.execute(
            f"SELECT kind, actor, content FROM campaign_log WHERE kind IN ({placeholders}) "
            "ORDER BY id DESC LIMIT ?",
            (*kinds, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def _trim(lines: list, budget: int) -> list:
    """Drop from the front until the block fits. The newest lines are the ones
    the DM most needs, so age is what gets sacrificed."""
    kept, used = [], 0
    for line in reversed(lines):
        used += len(line) + 1
        if used > budget and kept:
            break
        kept.append(line)
    return list(reversed(kept))


def context_block(limit: int = DEFAULT_LIMIT, budget: int = DEFAULT_BUDGET) -> str:
    """The memory half of the DM's prompt, or empty on a brand new campaign."""
    sections = []

    # First, and never trimmed. The premise is not history -- it is the standing
    # situation, as true on turn two hundred as on turn one, and trimming it away
    # like an old log line would quietly return the DM to knowing nothing.
    premise = get_value(PREMISE_KEY)
    if premise:
        sections.append("The situation:\n" + premise)

    chronicle = get_chronicle()
    if chronicle:
        # Given last, and trimmed from the front, so the most recent history is
        # what survives a long campaign.
        sections.append("The story so far:\n" + "\n".join(
            _trim(chronicle.splitlines(), budget // 2)
        ))

    scene = get_scene()
    if scene:
        sections.append(f"Where the party is now: {scene}")

    entries = recent(limit)
    if entries:
        lines = [f"- {e['actor']}: {e['content']}" if e["kind"] == "action"
                 else f"- {e['content']}" for e in entries]
        sections.append("Just happened, oldest first:\n" + "\n".join(_trim(lines, budget)))

    return "\n\n".join(sections)

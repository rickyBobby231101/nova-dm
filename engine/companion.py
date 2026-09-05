#!/usr/bin/env python3
"""
Automated party members — the Cathedral's entities acting when nobody plays them.

Daniel, 2026-09-05: "all the entities should be playable or automated parts of
the party ... even then if they are not in campaign personally when I am then
it is automated."

An entity is automated by where it sits, not by a flag: anything parked on the
Cathedral seat is unclaimed, so the table plays it. The moment someone hits
PLAY AS, the same character stops being automated and starts being theirs. One
rule, no state to fall out of sync.

**A companion turn is a real turn.** It decides an action and then goes through
`dm.handle_player_action` exactly as a player's does — same context, same
narration, same log, same broadcast. The only difference is who wrote the
sentence. Building a parallel path would have meant a second place for turn
logic to drift, and this project has already paid for that lesson twice.

Cost is the constraint. Deciding an action is one extra model call before the
turn's own call, on a box where a hundred extra tokens costs about eight
seconds. So the decision prompt is deliberately tiny: the entity's own line
from the cast, its sheet in one line, the scene, and the last few beats. It
asks for a single sentence and enforces it.
"""

import re

from engine import campaign, character, chronicle, llm

# How much of the recent table the companion sees before deciding. Small on
# purpose — it needs the last beat it is reacting to, not the campaign.
RECENT_BEATS = 4

# One sentence. A companion that monologues steals the turn from the humans.
MAX_WORDS = 30


def is_automated(char: dict) -> bool:
    """True when nobody is playing this character."""
    seat = character.npc_seat()
    return bool(seat) and char.get("player_id") == seat["id"]


def automated_party() -> list:
    """Entities the table is currently playing itself."""
    return character.list_npcs()


def _persona_line(name: str) -> str:
    """The entity's own line from the campaign cast.

    Reuses what the DM already reads every turn rather than writing a second
    description that would drift from it.
    """
    cast = campaign.get_cast() or ""
    for line in cast.splitlines():
        if line.strip().startswith(f"- **{name}**"):
            return re.sub(r"\*\*|^- ", "", line).strip()
    return name


def _sheet_line(char: dict) -> str:
    return (f"{char['name']}, level {char['level']} {char['race']} {char['class']}, "
            f"{char['current_hp']}/{char['max_hp']} HP, AC {char['ac']}")


def build_prompt(char: dict, recent: list) -> str:
    """What the entity is asked, to decide one action."""
    beats = "\n".join(f"- {b.get('content', b.get('text', ''))}" for b in recent[-RECENT_BEATS:]) or "- (the scene is quiet)"
    return (
        f"You are {_persona_line(char['name'])}\n"
        f"Sheet: {_sheet_line(char)}\n\n"
        f"Where the party is: {chronicle.get_scene()}\n\n"
        f"Just happened:\n{beats}\n\n"
        f"What do you do next? Answer in ONE sentence, under {MAX_WORDS} words, "
        f"in your own voice, as an action at the table. No narration of "
        f"outcomes — say what you attempt, not what happens."
    )


def trim(text: str) -> str:
    """One sentence, within budget.

    Small local models pad and then keep going. Taking the first sentence is
    cheaper and more reliable than asking harder for brevity.
    """
    text = (text or "").strip().strip('"')
    text = re.split(r"(?<=[.!?])\s", text)[0].strip() if text else ""
    words = text.split()
    if len(words) > MAX_WORDS:
        text = " ".join(words[:MAX_WORDS]).rstrip(",;:") + "…"
    return text


def decide(char: dict, recent: list = None, ask=None) -> str:
    """Ask the entity what it does. Returns "" if it has nothing to add.

    `ask` is injectable so the decision can be tested without a model, and so
    the caller controls which provider pays for it.
    """
    recent = recent if recent is not None else chronicle.recent(RECENT_BEATS)
    ask = ask or llm.simple_ask
    try:
        out = ask(build_prompt(char, recent))
    except Exception:
        # A companion that cannot think must not stop the table.
        return ""
    return trim(out)

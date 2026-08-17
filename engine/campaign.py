"""Phase 12: the campaign has a premise.

A fresh campaign gave the DM character sheets, "No encounter is active", and the
player's sentence. Nothing else -- no setting, no situation, nobody to meet. A
small local model handed that much and no more does the only thing it can: it
pattern-matches on the action text. Observed in a real session, it returned the
*identical* narration for two different actions, because from where it sat those
two turns looked the same.

So a campaign is seeded from a file. Four pieces, kept apart because they are
spent in different places:

  * **premise** and **scene** are situation. Both passes need them -- deciding
    what to roll depends on what is going on -- so they ride in the per-turn
    context alongside the chronicle.
  * **cast** and **adversary** are colour. Only the narration pass can act on
    them, and on the two-pass local path the context is sent twice while the
    narration system prompt is sent once. Putting them there is half price.

The file is Markdown rather than a table because the person editing it is
writing prose about their own mythology, not filling in a form. Sections are
`## name` headers; anything not recognised is ignored, so notes can live in the
file without reaching the model.
"""
import re
import sys
from pathlib import Path

from . import chronicle

# game_state keys. `scene` is deliberately the one chronicle already owns: the
# seed sets the opening scene and the DM's set_scene tool moves it from there.
PREMISE_KEY = "premise"
CAST_KEY = "cast"
ADVERSARY_KEY = "adversary"
SEED_KEY = "campaign_seed"  # which file was loaded, for `is_seeded` and debugging

SECTIONS = ("premise", "scene", "cast", "adversary")


def parse_seed(text: str) -> dict:
    """Pull the `## section` blocks out of a seed file.

    Everything before the first recognised section is preamble -- the title, and
    any instructions to the human editing it -- and is dropped rather than sent
    to the model.
    """
    found = {}
    current = None
    for line in (text or "").splitlines():
        header = re.match(r"^##\s+(.+?)\s*$", line)
        if header:
            name = header.group(1).strip().lower()
            current = name if name in SECTIONS else None
            if current:
                found[current] = []
            continue
        if current:
            found[current].append(line)

    return {k: "\n".join(v).strip() for k, v in found.items() if "".join(v).strip()}


def load_seed(path) -> dict:
    """Read a seed file into the campaign's memory.

    Only writes what the file actually contains, so a seed can be edited down to
    one section and reloaded without wiping the rest.
    """
    path = Path(path)
    parsed = parse_seed(path.read_text())

    written = {}
    for key, state_key in (
        ("premise", PREMISE_KEY),
        ("cast", CAST_KEY),
        ("adversary", ADVERSARY_KEY),
        ("scene", chronicle.SCENE_KEY),
    ):
        if parsed.get(key):
            chronicle.set_value(state_key, parsed[key])
            written[key] = parsed[key]

    if written:
        chronicle.set_value(SEED_KEY, str(path))
    return written


def get_premise() -> str:
    return chronicle.get_value(PREMISE_KEY)


def get_cast() -> str:
    return chronicle.get_value(CAST_KEY)


def get_adversary() -> str:
    return chronicle.get_value(ADVERSARY_KEY)


def is_seeded() -> bool:
    return bool(get_premise())


def flavour_block() -> str:
    """Cast and adversary, for the narration prompt. Empty when unseeded."""
    parts = []
    cast = get_cast()
    if cast:
        parts.append("Who is in this world:\n" + cast)
    adversary = get_adversary()
    if adversary:
        parts.append("What opposes them:\n" + adversary)
    return "\n\n".join(parts)


def clear() -> None:
    """Forget the world but keep the story. Used when swapping campaigns; the
    chronicle and the log are the party's history and are not this module's to
    throw away."""
    for key in (PREMISE_KEY, CAST_KEY, ADVERSARY_KEY, SEED_KEY):
        chronicle.set_value(key, "")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m engine.campaign <seed.md>")
        raise SystemExit(2)

    loaded = load_seed(sys.argv[1])
    if not loaded:
        print(f"nothing loaded -- no {'/'.join(SECTIONS)} sections in {sys.argv[1]}")
        raise SystemExit(1)

    for name, body in loaded.items():
        print(f"  {name:<10} {len(body):>5} chars (~{len(body)//4} tokens)")
    print(f"\nseeded from {sys.argv[1]}")

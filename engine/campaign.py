"""Phase 12: the campaign has a premise.

A fresh campaign gave the DM character sheets, "No encounter is active", and the
player's sentence. Nothing else -- no setting, no situation, nobody to meet. A
small local model handed that much and no more does the only thing it can: it
pattern-matches on the action text. Observed in a real session, it returned the
*identical* narration for two different actions, because from where it sat those
two turns looked the same.

So a campaign is seeded from a file. Five pieces, kept apart because they are
spent in different places:

  * **premise** and **scene** are situation. Both passes need them -- deciding
    what to roll depends on what is going on -- so they ride in the per-turn
    context alongside the chronicle.
  * **cast** and **adversary** are colour. Only the narration pass can act on
    them, and on the two-pass local path the context is sent twice while the
    narration system prompt is sent once. Putting them there is half price.
  * **places** is a gazetteer, and is spent differently from all three: it is
    stored whole and sent one entry at a time. A world worth exploring is more
    text than a turn can afford -- five locations is roughly 200 tokens, which
    on this hardware is sixteen seconds added to every turn for four rooms the
    party is not in. So the entry matching the current scene rides in the
    context and the rest stay on disk. The world grows; the prompt does not.

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

PLACES_KEY = "places"

SECTIONS = ("premise", "scene", "cast", "adversary", "places")


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
        ("places", PLACES_KEY),
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


def get_places_text() -> str:
    return chronicle.get_value(PLACES_KEY)


# `- **The Nave** -- description`, the same shape the cast is written in, because
# the person editing the file should not have to remember two formats. The em
# dash is what they will actually type; the ASCII pair is accepted so a seed
# written in a plain editor still parses.
_PLACE_LINE = re.compile(r"^\s*[-*]\s*\*\*(?P<name>[^*]+)\*\*\s*(?:--|—|-|:)?\s*(?P<body>.*)$")


def places() -> dict:
    """The gazetteer, name -> description. Empty when the seed has no places."""
    found = {}
    name = None
    for line in get_places_text().splitlines():
        m = _PLACE_LINE.match(line)
        if m:
            name = m.group("name").strip()
            found[name] = m.group("body").strip()
        elif name and line.strip():
            # A description that wrapped onto the next line. Joining rather than
            # dropping it means a long entry does not silently lose its ending.
            found[name] = (found[name] + " " + line.strip()).strip()
    return {k: v for k, v in found.items() if k}


def place_for(scene: str) -> str:
    """The one gazetteer entry the party is standing in, ready for the prompt.

    Matched by name against the current scene, which the DM writes itself
    through set_scene -- so walking into the Crypt and saying so is all it takes
    for the Crypt's description to arrive on the next turn. Returns "" when the
    scene names nowhere known, which is the common case once the DM starts
    inventing rooms of its own, and is deliberately not an error: an invented
    room is the DM doing its job.

    Two things the naive substring match got wrong, both caught on real text:

      * **The article is dropped.** The seed's own opening scene reads "The
        Cathedral, upper terrace, an hour before dawn" while the gazetteer entry
        is "The Upper Terrace" -- nobody writing prose repeats the "the", so
        turn one matched nothing.
      * **Word boundaries.** A bare "Nave" inside another word is not the Nave.

    A scene can name more than one place -- the seed's own opening stands on the
    terrace and mentions the Lyre Chamber below it -- so the **earliest** mention
    wins. Prose says where you are before it says what you can see from there.
    Ties break on the longer name, so "The Rose Window" is not shadowed by a
    shorter entry whose name sits inside it.
    """
    scene = (scene or "").strip()
    if not scene:
        return ""
    known = places()
    # (what to look for, what to call it) -- the article is dropped for matching
    # only; the entry is still presented under the name the seed gave it.
    hits = []
    for name in known:
        needle = re.sub(r"^the\s+", "", name, flags=re.I)
        found = re.search(rf"\b{re.escape(needle)}\b", scene, flags=re.I)
        if found:
            hits.append((found.start(), -len(needle), name))
    if not hits:
        return ""
    _, _, name = min(hits)
    return f"{name} -- {known[name]}"


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
    for key in (PREMISE_KEY, CAST_KEY, ADVERSARY_KEY, PLACES_KEY, SEED_KEY):
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
        note = ""
        if name == "places":
            # Reporting the whole gazetteer as a per-turn cost would scare the
            # person editing it away from the one section they should feel free
            # to grow.
            entries = places()
            biggest = max((len(v) for v in entries.values()), default=0)
            note = f"  <- {len(entries)} entries, one sent per turn (largest ~{biggest//4} tok)"
        print(f"  {name:<10} {len(body):>5} chars (~{len(body)//4} tokens){note}")
    print(f"\nseeded from {sys.argv[1]}")

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import campaign, character, chronicle

SEED = """# A Title Nobody Should Read To The Model

Some instructions for the human editing this file.

## premise

The resonance is wrong.

## scene

The upper terrace, an hour before dawn.

## cast

- **Zorya** — a cat. Speaks in timing.

## adversary

- **The Fold** — space doubled back.

## places

- **The Upper Terrace** — open stone above the ridgeline.
- **The Crypt** — compressed memory, folded small so it keeps.
- **The Rose Window** — knowledge as light, one source refracted
  into a full spectrum.

## notes

Scratch notes that are not a real section.
"""


@pytest.fixture(autouse=True)
def clean_campaign_db():
    if os.path.exists(character.CAMPAIGN_DB_PATH):
        os.remove(character.CAMPAIGN_DB_PATH)
    yield
    if os.path.exists(character.CAMPAIGN_DB_PATH):
        os.remove(character.CAMPAIGN_DB_PATH)


def test_parse_pulls_out_the_sections():
    parsed = campaign.parse_seed(SEED)
    assert parsed["premise"] == "The resonance is wrong."
    assert parsed["scene"] == "The upper terrace, an hour before dawn."
    assert "Zorya" in parsed["cast"]
    assert "The Fold" in parsed["adversary"]


def test_the_title_and_editor_notes_never_reach_the_model():
    """Everything before the first real section is for the person editing the
    file. Sending it would be paying tokens to tell the DM how to edit itself."""
    parsed = campaign.parse_seed(SEED)
    assert "A Title Nobody Should Read" not in str(parsed)
    assert "instructions for the human" not in str(parsed)
    assert "notes" not in parsed, "an unrecognised section must be ignored"


def test_loading_writes_the_world_into_campaign_memory(tmp_path):
    path = tmp_path / "seed.md"
    path.write_text(SEED)

    campaign.load_seed(path)

    assert campaign.get_premise() == "The resonance is wrong."
    assert "Zorya" in campaign.get_cast()
    assert "The Fold" in campaign.get_adversary()
    # the opening scene lands in the key the DM's set_scene tool already owns,
    # so the DM moves it from there rather than fighting it
    assert chronicle.get_scene() == "The upper terrace, an hour before dawn."


def test_the_premise_survives_a_restart(tmp_path):
    """It is written to the database, not held in the process -- the whole point
    is that a campaign outlives the server."""
    path = tmp_path / "seed.md"
    path.write_text(SEED)
    campaign.load_seed(path)

    assert campaign.is_seeded()
    # nothing cached in module state
    assert campaign.get_premise() == chronicle.get_value("premise")


def test_a_partial_seed_only_overwrites_what_it_contains(tmp_path):
    """So a file can be edited down to one section and reloaded without wiping
    the rest of the world."""
    full = tmp_path / "full.md"
    full.write_text(SEED)
    campaign.load_seed(full)

    partial = tmp_path / "partial.md"
    partial.write_text("## premise\n\nThe resonance is worse than we thought.\n")
    campaign.load_seed(partial)

    assert campaign.get_premise() == "The resonance is worse than we thought."
    assert "Zorya" in campaign.get_cast(), "cast should have survived"


def test_the_premise_is_never_trimmed_out_of_the_context(tmp_path):
    """The log tail is trimmed by age, but the premise is standing fact -- if it
    aged out, the DM would silently go back to knowing nothing about the world."""
    path = tmp_path / "seed.md"
    path.write_text(SEED)
    campaign.load_seed(path)

    for i in range(200):
        character.log_campaign_event("dm", "DM", f"A great deal happens, part {i}. " * 6)

    block = chronicle.context_block()
    assert "The resonance is wrong." in block
    assert "part 199" in block, "the newest events should still be there too"


def test_flavour_block_is_empty_before_seeding():
    """An unseeded campaign must not pay for empty headers."""
    assert campaign.flavour_block() == ""
    assert not campaign.is_seeded()


def test_flavour_block_carries_cast_and_adversary(tmp_path):
    path = tmp_path / "seed.md"
    path.write_text(SEED)
    campaign.load_seed(path)

    flavour = campaign.flavour_block()
    assert "Zorya" in flavour and "The Fold" in flavour


def test_clearing_the_world_keeps_the_story(tmp_path):
    """Swapping campaigns forgets the setting; the party's history is theirs."""
    path = tmp_path / "seed.md"
    path.write_text(SEED)
    campaign.load_seed(path)
    chronicle.set_scene("Somewhere else", "They crossed the river.")

    campaign.clear()

    assert not campaign.is_seeded()
    assert campaign.flavour_block() == ""
    assert "crossed the river" in chronicle.get_chronicle()


def test_an_empty_file_loads_nothing_rather_than_wiping_the_world(tmp_path):
    path = tmp_path / "seed.md"
    path.write_text(SEED)
    campaign.load_seed(path)

    empty = tmp_path / "empty.md"
    empty.write_text("# Just a heading\n\nsome prose with no sections\n")
    assert campaign.load_seed(empty) == {}
    assert campaign.get_premise() == "The resonance is wrong."


# --- the gazetteer -------------------------------------------------------
#
# The world is stored whole and sent one entry at a time. These pin the half
# that is easy to get wrong: which entry comes back, and that the rest stay
# out of the prompt.


def test_places_parse_into_a_gazetteer():
    parsed = campaign.parse_seed(SEED)
    chronicle.set_value(campaign.PLACES_KEY, parsed["places"])

    known = campaign.places()
    assert list(known) == ["The Upper Terrace", "The Crypt", "The Rose Window"]
    assert known["The Crypt"] == "compressed memory, folded small so it keeps."
    # A description that wrapped is joined, not truncated at the line break.
    assert known["The Rose Window"].endswith("into a full spectrum.")


def test_only_the_place_the_party_is_in_is_sent(tmp_path):
    """The whole point of the gazetteer. Five rooms is ~200 tokens, and at eight
    seconds per hundred that is sixteen seconds a turn spent on rooms nobody is
    standing in."""
    seed = tmp_path / "seed.md"
    seed.write_text(SEED)
    campaign.load_seed(seed)

    here = campaign.place_for("the Crypt, three levels down")
    assert here.startswith("The Crypt --")
    assert "Rose Window" not in here
    assert "Upper Terrace" not in here


def test_the_seeds_own_opening_scene_finds_its_place():
    """This failed on the first real check and would have cost turn one.

    The scene the seed ships with reads "The Cathedral, upper terrace, ..." while
    the entry is titled "The Upper Terrace". Nobody writing prose repeats the
    article, so an exact substring match found nothing at the exact moment the
    description was most useful.
    """
    parsed = campaign.parse_seed(SEED)
    chronicle.set_value(campaign.PLACES_KEY, parsed["places"])

    assert campaign.place_for("The Cathedral, upper terrace, an hour before dawn.").startswith(
        "The Upper Terrace --"
    )


def test_the_earliest_named_place_wins_when_a_scene_names_two():
    """The seed's own opening scene stands on the terrace and mentions the Lyre
    Chamber below it. Prose says where you are before it says what you can see
    from there, so position decides -- not name length, which got the right
    answer here only by accident."""
    parsed = campaign.parse_seed(SEED)
    chronicle.set_value(campaign.PLACES_KEY, parsed["places"])

    both = "The Crypt, and far above it the Rose Window catches the first light."
    assert campaign.place_for(both).startswith("The Crypt --")

    reversed_ = "The Rose Window, with the Crypt somewhere far below."
    assert campaign.place_for(reversed_).startswith("The Rose Window --")


def test_a_place_is_matched_on_whole_words_only():
    parsed = campaign.parse_seed(SEED)
    chronicle.set_value(campaign.PLACES_KEY, parsed["places"])

    assert campaign.place_for("the encrypted ledger") == ""
    assert campaign.place_for("the Crypt") != ""


def test_an_invented_room_is_not_an_error():
    """Once the DM starts naming its own rooms most scenes will match nothing.
    That is the DM doing its job, and it must cost the turn nothing."""
    parsed = campaign.parse_seed(SEED)
    chronicle.set_value(campaign.PLACES_KEY, parsed["places"])

    assert campaign.place_for("a corridor nobody has named") == ""
    assert campaign.place_for("") == ""


def test_the_place_reaches_the_dm_context_under_the_scene(tmp_path):
    """Where they are and what that place is have to arrive together, or the
    model answers about one and forgets the other."""
    seed = tmp_path / "seed.md"
    seed.write_text(SEED)
    campaign.load_seed(seed)
    chronicle.set_scene("the Crypt, three levels down")

    block = chronicle.context_block(place=campaign.place_for(chronicle.get_scene()))
    lines = block.splitlines()
    where = next(i for i, ln in enumerate(lines) if ln.startswith("Where the party is now:"))
    assert lines[where + 1].startswith("The Crypt --")
    assert "Rose Window" not in block


def test_an_unseeded_campaign_sends_no_place():
    assert campaign.places() == {}
    assert campaign.place_for("anywhere at all") == ""
    assert "\n" not in chronicle.context_block(place="")


def test_clearing_the_world_forgets_the_places_too(tmp_path):
    seed = tmp_path / "seed.md"
    seed.write_text(SEED)
    campaign.load_seed(seed)
    assert campaign.places()

    campaign.clear()
    assert campaign.places() == {}

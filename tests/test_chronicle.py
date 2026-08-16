"""Phase 10: the DM's memory between turns.

These need only the campaign DB -- no SRD, no characters -- so they drive
campaign_log and game_state directly.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import character, chronicle


@pytest.fixture(autouse=True)
def clean_campaign_db():
    if os.path.exists(character.CAMPAIGN_DB_PATH):
        os.remove(character.CAMPAIGN_DB_PATH)
    yield
    if os.path.exists(character.CAMPAIGN_DB_PATH):
        os.remove(character.CAMPAIGN_DB_PATH)


def test_values_round_trip_and_overwrite():
    assert chronicle.get_value("nothing-here") == ""
    assert chronicle.get_value("nothing-here", "fallback") == "fallback"

    chronicle.set_value("k", "first")
    assert chronicle.get_value("k") == "first"
    chronicle.set_value("k", "second")
    assert chronicle.get_value("k") == "second"


def test_set_scene_records_where_the_party_is():
    chronicle.set_scene("Goblin warren, east tunnel")
    assert chronicle.get_scene() == "Goblin warren, east tunnel"


def test_the_chronicle_is_appended_to_not_replaced():
    """A DM that rewrites its own history every scene loses the campaign."""
    chronicle.set_scene("Trailhead", "The party took the job from the reeve.")
    chronicle.set_scene("Goblin warren", "They found the warren by dusk.")

    story = chronicle.get_chronicle()
    assert "took the job from the reeve" in story
    assert "found the warren by dusk" in story
    assert chronicle.get_scene() == "Goblin warren"


def test_a_scene_with_no_summary_moves_the_party_without_writing_history():
    chronicle.set_scene("Trailhead", "The party took the job.")
    chronicle.set_scene("The old bridge")

    assert chronicle.get_scene() == "The old bridge"
    assert chronicle.get_chronicle() == "The party took the job."


def test_recent_returns_the_story_oldest_first():
    character.log_campaign_event("action", "Thorin", "Thorin: I kick the door")
    character.log_campaign_event("dm", "DM", "The door bursts inward.")

    entries = chronicle.recent()
    assert [e["content"] for e in entries] == [
        "Thorin: I kick the door", "The door bursts inward."
    ]


def test_recent_leaves_out_the_arithmetic():
    """The board and the sheets already carry rolls and HP. At ~2k tokens a turn
    they would crowd out the narration that gives them meaning."""
    character.log_campaign_event("dm", "DM", "The goblin lunges.")
    character.log_campaign_event("roll", "Thorin", "Thorin rolls DEX: 14")
    character.log_campaign_event("damage", "Thorin", "Thorin takes 4 damage")
    character.log_campaign_event("heal", "Mira", "Mira heals 3")

    contents = [e["content"] for e in chronicle.recent()]
    assert contents == ["The goblin lunges."]


def test_recent_keeps_xp_and_levelling_because_they_are_story():
    character.log_campaign_event("xp", "Thorin", "Thorin gains 50 XP")
    character.log_campaign_event("level_up", "Thorin", "Thorin reaches level 2!")

    assert len(chronicle.recent()) == 2


def test_recent_returns_only_the_last_n():
    for i in range(30):
        character.log_campaign_event("dm", "DM", f"beat {i}")

    entries = chronicle.recent(limit=5)
    assert [e["content"] for e in entries] == [f"beat {i}" for i in range(25, 30)]


def test_context_block_is_empty_on_a_fresh_campaign():
    assert chronicle.context_block() == ""


def test_context_block_carries_all_three_layers():
    chronicle.set_scene("Goblin warren, east tunnel", "The party freed the miners.")
    character.log_campaign_event("action", "Thorin", "Thorin: I listen at the door")
    character.log_campaign_event("dm", "DM", "You hear scraping beyond it.")

    block = chronicle.context_block()

    assert "The story so far:" in block
    assert "freed the miners" in block
    assert "Where the party is now: Goblin warren, east tunnel" in block
    assert "I listen at the door" in block
    assert "You hear scraping beyond it." in block
    # oldest first, so it reads forwards
    assert block.index("I listen") < block.index("scraping")


def test_context_block_drops_the_oldest_when_over_budget():
    """A long session must not quietly grow the prompt until the local model
    falls over -- age is what gets sacrificed, not recency."""
    for i in range(40):
        character.log_campaign_event("dm", "DM", f"beat {i} " + "x" * 100)

    block = chronicle.context_block(limit=40, budget=600)

    assert len(block) < 1200
    assert "beat 39" in block
    assert "beat 0" not in block


def test_a_single_oversized_entry_still_survives():
    # Never return an empty tail just because one line blew the budget.
    character.log_campaign_event("dm", "DM", "y" * 5000)
    block = chronicle.context_block(budget=100)
    assert "yyyy" in block

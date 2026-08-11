import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import character

pytestmark = pytest.mark.skipif(
    not os.path.exists(character.SRD_DB_PATH),
    reason="srd.sqlite not present -- run ingest/ingest_srd.py first"
)


@pytest.fixture(autouse=True)
def clean_campaign_db():
    if os.path.exists(character.CAMPAIGN_DB_PATH):
        os.remove(character.CAMPAIGN_DB_PATH)
    yield
    if os.path.exists(character.CAMPAIGN_DB_PATH):
        os.remove(character.CAMPAIGN_DB_PATH)


def test_create_character_applies_racial_bonus_and_level1_hp():
    char = character.create_character(
        player_id=None, name="Thorin", race="dwarf", class_="fighter",
        ability_scores={"str": 16, "dex": 12, "con": 14, "int": 10, "wis": 10, "cha": 8}
    )
    assert char["name"] == "Thorin"
    assert char["level"] == 1
    assert char["con"] == 16  # dwarf gets +2 CON per SRD
    # fighter hit_die=10, con_mod(16)=3 -> max_hp = 10 + 3 = 13
    assert char["max_hp"] == 13
    assert char["current_hp"] == 13


def test_award_xp_triggers_level_up_with_correct_hp_and_proficiency():
    char = character.create_character(
        player_id=None, name="Thorin", race="human", class_="fighter",
        ability_scores={"str": 16, "dex": 12, "con": 14, "int": 10, "wis": 10, "cha": 8}
    )
    # human gets +1 to all -> CON 15, con_mod=2 -> level 1 HP = 10+2 = 12
    assert char["max_hp"] == 12
    assert character.rules.xp_to_level(char["xp"]) == 1

    result = character.award_xp(char["id"], 300)
    assert result["old_level"] == 1
    assert result["new_level"] == 2
    assert len(result["level_ups"]) == 1

    updated = character.get_character(char["id"])
    assert updated["level"] == 2
    assert updated["xp"] == 300
    # HP should have increased by the level-2 average-method gain (>0)
    assert updated["max_hp"] > 12
    assert updated["current_hp"] > 12  # current_hp rises with max_hp on level-up
    assert updated["proficiency_bonus"] == 2  # still +2 at level 2


def test_damage_and_heal_clamp_correctly():
    char = character.create_character(
        player_id=None, name="Mira", race="human", class_="cleric",
        ability_scores={"str": 10, "dex": 12, "con": 13, "int": 10, "wis": 15, "cha": 10}
    )
    max_hp = char["max_hp"]

    dmg = character.apply_damage(char["id"], max_hp + 50)
    assert dmg["current_hp"] == 0
    assert dmg["unconscious"] is True

    heal = character.apply_heal(char["id"], 9999)
    assert heal["current_hp"] == max_hp  # clamped to max, not overhealed

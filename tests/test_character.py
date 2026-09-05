import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import character, rules

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


# ---------------------------------------------------------------------------
# Ability score improvements
# ---------------------------------------------------------------------------

def _leveller(dex=12, con=13):
    player = character.create_player("Chazel")
    return character.create_character(
        player_id=player["id"], name="Ferrick", race="human", class_="fighter",
        ability_scores={"str": 15, "dex": dex, "con": con, "int": 10, "wis": 10, "cha": 10},
    )


def test_no_improvement_before_level_four():
    char = _leveller()
    for level in (2, 3):
        character.apply_level_up(char["id"], level)
    assert character.get_character(char["id"])["pending_asi"] == 0


def test_level_four_grants_two_points():
    char = _leveller()
    for level in (2, 3, 4):
        result = character.apply_level_up(char["id"], level)
    assert result["asi_points"] == 2
    assert character.get_character(char["id"])["pending_asi"] == 2


def test_a_jump_past_an_improvement_still_grants_it():
    """Enough XP at once carries a character up two levels. An improvement
    stepped over is one nobody ever gets."""
    char = _leveller()
    result = character.apply_level_up(char["id"], 5)  # 1 -> 5, crossing 4
    assert result["asi_points"] == 2


def test_a_jump_across_two_improvements_grants_both():
    char = _leveller()
    result = character.apply_level_up(char["id"], 9)  # crosses 4 and 8
    assert result["asi_points"] == 4


def test_spending_raises_the_score_and_the_points_go_down():
    char = _leveller()
    character.apply_level_up(char["id"], 4)
    before = character.get_character(char["id"])["str"]

    result = character.raise_ability(char["id"], "str", 2)

    assert result["to"] == before + 2
    assert character.get_character(char["id"])["pending_asi"] == 0


def test_points_can_be_split_across_two_abilities():
    """5e allows +1/+1, so the count is points rather than improvements."""
    char = _leveller()
    character.apply_level_up(char["id"], 4)
    wis_before = character.get_character(char["id"])["wis"]

    character.raise_ability(char["id"], "str", 1)
    assert character.get_character(char["id"])["pending_asi"] == 1
    character.raise_ability(char["id"], "wis", 1)

    after = character.get_character(char["id"])
    assert after["pending_asi"] == 0
    assert after["wis"] == wis_before + 1


def test_raising_constitution_pays_hit_points_for_every_level_already_taken():
    """5e applies the new modifier retroactively, not just going forward."""
    # Racial bonuses land before this runs, so pick the score off the sheet and
    # find one that actually crosses a modifier boundary.
    char = _leveller(con=13)
    character.apply_level_up(char["id"], 4)
    sheet = character.get_character(char["id"])
    if sheet["con"] % 2 == 0:  # even scores are the ones that cross on +1
        character.raise_ability(char["id"], "con", 1)
        sheet = character.get_character(char["id"])
    before_hp, before_mod = sheet["max_hp"], rules.ability_mod(sheet["con"])

    result = character.raise_ability(char["id"], "con", 1)

    after = character.get_character(char["id"])
    crossed = rules.ability_mod(after["con"]) - before_mod
    assert result["hp_gain"] == crossed * 4, "one modifier point at each of four levels"
    assert after["max_hp"] == before_hp + result["hp_gain"]


def test_raising_constitution_without_crossing_a_modifier_pays_nothing():
    """Odd scores gain nothing until the next point pairs with them."""
    char = _leveller(con=13)
    character.apply_level_up(char["id"], 4)
    sheet = character.get_character(char["id"])
    if sheet["con"] % 2:  # make it even, so the next point does not cross
        character.raise_ability(char["id"], "con", 1)

    assert character.raise_ability(char["id"], "con", 1)["hp_gain"] == 0


def test_raising_dexterity_moves_armour_class():
    char = _leveller(dex=13)
    character.apply_level_up(char["id"], 4)
    sheet = character.get_character(char["id"])
    if sheet["dex"] % 2 == 0:  # make it odd, so the next point crosses a modifier
        character.raise_ability(char["id"], "dex", 1)
        sheet = character.get_character(char["id"])

    result = character.raise_ability(char["id"], "dex", 1)

    assert result["ac"] == rules.unarmored_ac(character.get_character(char["id"])["dex"])
    assert result["ac"] == sheet["ac"] + 1


def test_armour_class_reflects_dexterity_from_the_start():
    """It was a flat 10, which left a nimble character no harder to hit."""
    char = _leveller(dex=16)
    assert char["ac"] == rules.unarmored_ac(char["dex"])
    assert char["ac"] > 10


def test_a_score_stops_at_twenty():
    char = _leveller()
    character.apply_level_up(char["id"], 9)  # 4 points
    with character._campaign_con() as con:
        con.execute("UPDATE characters SET str=19 WHERE id=?", (char["id"],))

    result = character.raise_ability(char["id"], "str", 2)

    assert result["to"] == 20
    assert result["granted"] == 1, "only the point that fit was spent"
    assert character.get_character(char["id"])["pending_asi"] == 3


def test_spending_what_you_do_not_have_is_refused():
    char = _leveller()
    with pytest.raises(ValueError, match="point"):
        character.raise_ability(char["id"], "str", 1)


def test_a_maxed_score_is_refused_rather_than_silently_wasting_the_point():
    char = _leveller()
    character.apply_level_up(char["id"], 4)
    with character._campaign_con() as con:
        con.execute("UPDATE characters SET str=20 WHERE id=?", (char["id"],))

    with pytest.raises(ValueError, match="already at 20"):
        character.raise_ability(char["id"], "str", 1)
    assert character.get_character(char["id"])["pending_asi"] == 2


def test_an_unknown_ability_is_refused():
    char = _leveller()
    character.apply_level_up(char["id"], 4)
    with pytest.raises(ValueError, match="not an ability"):
        character.raise_ability(char["id"], "luck", 1)


def test_an_unspent_improvement_survives_until_it_is_used():
    """A player who levels up mid-session and logs off must find it waiting."""
    char = _leveller()
    character.apply_level_up(char["id"], 4)
    assert character.get_character(char["id"])["pending_asi"] == 2
    character.apply_level_up(char["id"], 5)
    assert character.get_character(char["id"])["pending_asi"] == 2, "not lost, not doubled"


# ---------------------------------------------------------------------------
# Hit points cannot leave their own range
# ---------------------------------------------------------------------------

def test_negative_damage_does_not_heal():
    """Observed live: the DM called apply_damage(-3) and the engine put a
    character on 12/9 HP. Healing has its own tool, and it caps."""
    char = _leveller()
    before = character.get_character(char["id"])["current_hp"]

    result = character.apply_damage(char["id"], -3)

    assert result["current_hp"] == before
    assert result["current_hp"] <= character.get_character(char["id"])["max_hp"]


def test_damage_never_pushes_hit_points_above_maximum():
    char = _leveller()
    with character._campaign_con() as con:  # a sheet already out of range
        con.execute("UPDATE characters SET current_hp=max_hp+5 WHERE id=?", (char["id"],))

    result = character.apply_damage(char["id"], 0)

    sheet = character.get_character(char["id"])
    assert result["current_hp"] == sheet["max_hp"], "the stored value is corrected, not preserved"


def test_damage_still_stops_at_zero():
    char = _leveller()
    result = character.apply_damage(char["id"], 9999)
    assert result["current_hp"] == 0 and result["unconscious"]


def test_ordinary_damage_is_unaffected():
    char = _leveller()
    before = character.get_character(char["id"])["current_hp"]
    assert character.apply_damage(char["id"], 3)["current_hp"] == before - 3


# ── The Cathedral's entities are NPCs until somebody plays them ─────────────

def _seat_entities():
    """Two entities parked on the Cathedral seat, as the seeder makes them."""
    seat = character.create_player(character.NPC_SEAT_NAME)
    a = character.create_character(seat["id"], "Tillagon", "dragonborn", "paladin",
                                   dict(str=16, dex=10, con=16, int=11, wis=13, cha=15))
    b = character.create_character(seat["id"], "Zorya", "half-elf", "ranger",
                                   dict(str=10, dex=17, con=12, int=12, wis=16, cha=11))
    return seat, a, b


def test_unclaimed_entities_are_npcs():
    seat, a, b = _seat_entities()
    assert {c["name"] for c in character.list_npcs()} == {"Tillagon", "Zorya"}


def test_claiming_moves_an_entity_to_the_player():
    seat, a, b = _seat_entities()
    me = character.create_player("Chazel")

    r = character.claim_character(a["id"], me["id"])
    assert r["ok"] is True
    assert {c["name"] for c in character.list_characters_for_player(me["id"])} == {"Tillagon"}
    assert {c["name"] for c in character.list_npcs()} == {"Zorya"}


def test_an_entity_someone_is_playing_cannot_be_taken():
    """The same failure as the seat cookie, in a different costume: one player
    must never end up holding another's character."""
    seat, a, b = _seat_entities()
    first = character.create_player("Chazel")
    second = character.create_player("Jordan")

    character.claim_character(a["id"], first["id"])
    r = character.claim_character(a["id"], second["id"])

    assert "error" in r and "already being played" in r["error"]
    assert character.get_character(a["id"])["player_id"] == first["id"]


def test_releasing_returns_it_to_the_cathedral():
    seat, a, b = _seat_entities()
    me = character.create_player("Chazel")
    character.claim_character(a["id"], me["id"])

    r = character.release_character(a["id"], me["id"])
    assert r["ok"] is True
    assert {c["name"] for c in character.list_npcs()} == {"Tillagon", "Zorya"}


def test_you_cannot_release_a_character_you_made_yourself():
    """The Cathedral only takes back what is the Cathedral's."""
    _seat_entities()
    me = character.create_player("Chazel")
    mine = character.create_character(me["id"], "Mira", "elf", "druid",
                                      dict(str=10, dex=14, con=12, int=11, wis=15, cha=10))
    assert "error" in character.release_character(mine["id"], me["id"])


def test_you_cannot_release_someone_elses_entity():
    seat, a, b = _seat_entities()
    first = character.create_player("Chazel")
    second = character.create_player("Jordan")
    character.claim_character(a["id"], first["id"])
    assert "error" in character.release_character(a["id"], second["id"])


def test_claiming_preserves_the_sheet():
    """An entity picked up mid-campaign keeps whatever has happened to it."""
    seat, a, b = _seat_entities()
    me = character.create_player("Chazel")
    before = character.get_character(a["id"])
    character.claim_character(a["id"], me["id"])
    after = character.get_character(a["id"])
    for field in ("name", "race", "class", "level", "max_hp", "ac"):
        assert before[field] == after[field]


def test_a_name_is_found_however_it_is_typed():
    """Friends must get their own characters back without anyone's help.
    A phone that autocapitalises, or does not, must reach the same seat."""
    me = character.create_player("Chazel")
    for typed in ("Chazel", "chazel", "CHAZEL", "  Chazel  "):
        found = character.find_player_by_name(typed)
        assert found and found["id"] == me["id"], f"{typed!r} did not find the seat"

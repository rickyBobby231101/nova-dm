import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import character, encounter

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


def _make_character(name="Thorin", dex=12):
    return character.create_character(
        player_id=None, name=name, race="dwarf", class_="fighter",
        ability_scores={"str": 16, "dex": dex, "con": 14, "int": 10, "wis": 10, "cha": 8},
    )


def _monsters(state):
    return [c for c in state["combatants"] if c["kind"] == "monster"]


def _pcs(state):
    return [c for c in state["combatants"] if c["kind"] == "character"]


def test_start_encounter_enrolls_party_and_monsters():
    _make_character()
    state = encounter.start_encounter("Goblin ambush", [{"slug": "goblin", "count": 3}])

    assert state["name"] == "Goblin ambush"
    assert state["round"] == 1
    assert len(_monsters(state)) == 3
    assert len(_pcs(state)) == 1
    # sorted(): the board comes back in initiative order, not insertion order
    assert sorted(m["name"] for m in _monsters(state)) == ["Goblin 1", "Goblin 2", "Goblin 3"]


def test_character_ids_selects_who_is_in_the_fight():
    """The campaign DB keeps every character ever rolled -- an old one must not be
    dragged into a new fight just because it exists."""
    here = _make_character("Present")
    _make_character("Retired")

    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}],
                                      character_ids=[here["id"]])

    assert [c["name"] for c in _pcs(state)] == ["Present"]


def test_omitting_character_ids_enrolls_everyone():
    _make_character("A")
    _make_character("B")
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    assert sorted(c["name"] for c in _pcs(state)) == ["A", "B"]


def test_single_monster_is_not_numbered():
    _make_character()
    state = encounter.start_encounter("Duel", [{"slug": "orc", "count": 1}])
    assert [m["name"] for m in _monsters(state)] == ["Orc"]


def test_monster_hp_is_rolled_within_its_dice_range():
    """Goblin is 2d6, so 2..12 -- a fixed 7 would mean we used the average instead
    of rolling, which is the thing this project refuses to do."""
    _make_character()
    seen = set()
    for _ in range(8):
        state = encounter.start_encounter("hp", [{"slug": "goblin", "count": 4}])
        for m in _monsters(state):
            assert 2 <= m["max_hp"] <= 12
            assert m["current_hp"] == m["max_hp"]
            seen.add(m["max_hp"])
    assert len(seen) > 1, "every goblin had identical HP -- HP is not being rolled"


def test_initiative_is_sorted_descending():
    _make_character("Quick", dex=20)
    _make_character("Slow", dex=6)
    state = encounter.start_encounter("order", [{"slug": "goblin", "count": 2}])

    inits = [c["initiative"] for c in state["combatants"]]
    assert inits == sorted(inits, reverse=True)


def test_dex_breaks_an_initiative_tie():
    """Tested against _order directly: a tie is rare enough that rolling for one
    would make this test pass by luck rather than by the tiebreak working."""
    rows = [
        {"id": 1, "initiative": 14, "dex": 10},
        {"id": 2, "initiative": 14, "dex": 18},
        {"id": 3, "initiative": 20, "dex": 8},
        {"id": 4, "initiative": 14, "dex": 18},
    ]
    assert [r["id"] for r in encounter._order(rows)] == [3, 2, 4, 1]


def test_damage_to_a_player_lands_in_characters_not_combatants():
    """The source-of-truth rule: a PC's HP lives in `characters`. If it were copied
    into `combatants` the two would drift the first time anything hit."""
    char = _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    pc = _pcs(state)[0]

    encounter.damage_combatant(pc["id"], 5)

    assert character.get_character(char["id"])["current_hp"] == char["max_hp"] - 5
    with character._campaign_con() as con:
        row = con.execute("SELECT current_hp FROM combatants WHERE id=?", (pc["id"],)).fetchone()
    assert row["current_hp"] is None, "PC hit points must not be stored on the combatant row"

    assert encounter.get_combatant(pc["id"])["current_hp"] == char["max_hp"] - 5


def test_damage_to_a_monster_lands_in_combatants():
    char = _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin = _monsters(state)[0]

    result = encounter.damage_combatant(goblin["id"], 2)

    assert result["current_hp"] == goblin["max_hp"] - 2
    assert character.get_character(char["id"])["current_hp"] == char["max_hp"]


def test_damage_floors_at_zero_and_marks_down():
    _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin = _monsters(state)[0]

    result = encounter.damage_combatant(goblin["id"], 999)

    assert result["current_hp"] == 0
    assert result["is_down"] is True
    assert encounter.get_combatant(goblin["id"])["is_down"] is True


def test_heal_is_capped_at_max_hp():
    _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin = _monsters(state)[0]

    encounter.damage_combatant(goblin["id"], 3)
    result = encounter.heal_combatant(goblin["id"], 999)

    assert result["current_hp"] == goblin["max_hp"]


def test_advance_turn_wraps_and_increments_round():
    _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    count = len(state["combatants"])

    for _ in range(count - 1):
        state = encounter.advance_turn()
    assert state["round"] == 1
    assert state["turn_index"] == count - 1

    state = encounter.advance_turn()
    assert state["turn_index"] == 0
    assert state["round"] == 2


def test_monster_attack_rolls_against_real_ac_and_applies_damage():
    char = _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin, pc = _monsters(state)[0], _pcs(state)[0]

    result = encounter.monster_attack(goblin["id"], "Scimitar", pc["id"])

    assert result["target_ac"] == char["ac"]
    assert 1 <= result["d20"] <= 20
    assert result["attack_total"] == result["d20"] + result["attack_bonus"]
    if result["hit"]:
        assert result["damage"] >= 1
        assert character.get_character(char["id"])["current_hp"] == char["max_hp"] - result["damage"]
    else:
        assert result["damage"] == 0
        assert character.get_character(char["id"])["current_hp"] == char["max_hp"]


def test_monster_attack_handles_flat_damage():
    """A badger's bite is the literal string "1", not a dice expression -- parsing
    it as dice would raise."""
    _make_character()
    state = encounter.start_encounter("critters", [{"slug": "badger", "count": 1}])
    badger, pc = _monsters(state)[0], _pcs(state)[0]

    result = encounter.monster_attack(badger["id"], "Bite", pc["id"])

    assert "error" not in result
    assert result["damage"] in (0, 1)


def test_monster_attack_handles_versatile_choose_damage():
    """A guard's spear encodes its only damage as a choose-one entry. Skipping
    those would leave the guard unable to attack at all."""
    _make_character()
    state = encounter.start_encounter("watch", [{"slug": "guard", "count": 1}])
    guard, pc = _monsters(state)[0], _pcs(state)[0]

    assert encounter.usable_actions(encounter.get_srd_monster("guard"))
    result = encounter.monster_attack(guard["id"], "Spear", pc["id"])

    assert "error" not in result
    if result["hit"]:
        assert result["damage"] >= 2  # 1d6+1


def test_monster_with_no_attack_action_errors_rather_than_inventing_one():
    _make_character()
    state = encounter.start_encounter("croak", [{"slug": "frog", "count": 1}])
    frog, pc = _monsters(state)[0], _pcs(state)[0]

    result = encounter.monster_attack(frog["id"], "Bite", pc["id"])

    assert "error" in result and "no attack action" in result["error"]


def test_starting_an_encounter_ends_the_previous_one():
    _make_character()
    encounter.start_encounter("first", [{"slug": "goblin", "count": 1}])
    state = encounter.start_encounter("second", [{"slug": "orc", "count": 1}])

    assert state["name"] == "second"
    assert [m["name"] for m in _monsters(state)] == ["Orc"]


def test_end_encounter_clears_the_board():
    _make_character()
    encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])

    assert encounter.end_encounter() is True
    assert encounter.get_state() is None
    assert encounter.end_encounter() is False


def test_monster_search_filters_by_name_and_cr():
    by_name = encounter.list_srd_monsters(query="goblin")
    assert any(m["slug"] == "goblin" for m in by_name)

    low = encounter.list_srd_monsters(max_cr=0.25)
    assert low and all(m["cr"] <= 0.25 for m in low)

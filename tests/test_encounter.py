import json
import os
import sys
import threading

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


def test_condition_on_a_player_lands_in_characters_not_combatants():
    """Same source-of-truth split as hit points: a player's conditions outlast the
    fight, a monster's don't exist outside it."""
    char = _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    pc = _pcs(state)[0]

    result = encounter.apply_condition(pc["id"], "poisoned")

    assert "error" not in result
    stored = json.loads(character.get_character(char["id"])["conditions_json"])
    assert stored == [{"name": "poisoned"}]
    with character._campaign_con() as con:
        row = con.execute("SELECT conditions_json FROM combatants WHERE id=?", (pc["id"],)).fetchone()
    assert json.loads(row["conditions_json"]) == []


def test_condition_on_a_monster_lands_in_combatants():
    char = _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin = _monsters(state)[0]

    encounter.apply_condition(goblin["id"], "prone")

    assert encounter.get_combatant(goblin["id"])["conditions"] == [{"name": "prone"}]
    assert json.loads(character.get_character(char["id"])["conditions_json"]) == []


def test_conditions_survive_into_the_broadcast_state():
    _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin = _monsters(state)[0]

    encounter.apply_condition(goblin["id"], "stunned")
    refreshed = next(c for c in encounter.get_state()["combatants"] if c["id"] == goblin["id"])

    assert refreshed["conditions"] == [{"name": "stunned"}]
    assert refreshed["can_act"] is False


def test_removing_a_condition_clears_it():
    _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin = _monsters(state)[0]
    encounter.apply_condition(goblin["id"], "prone")

    encounter.remove_condition(goblin["id"], "prone")

    assert encounter.get_combatant(goblin["id"])["conditions"] == []


def test_a_monster_immune_to_a_condition_refuses_it():
    """1620 of the 3207 creatures publish condition immunities -- recording one
    anyway would quietly change the dice for the rest of the fight."""
    _make_character()
    state = encounter.start_encounter("deep", [{"slug": "aboleth-nihilith", "count": 1}])
    beast = _monsters(state)[0]

    result = encounter.apply_condition(beast["id"], "charmed")

    assert "error" in result and "immune to charmed" in result["error"]
    assert encounter.get_combatant(beast["id"])["conditions"] == []


def test_a_monster_without_that_immunity_accepts_it():
    _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin = _monsters(state)[0]

    assert "error" not in encounter.apply_condition(goblin["id"], "charmed")


def test_exhaustion_level_round_trips_through_storage():
    _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin = _monsters(state)[0]

    encounter.apply_condition(goblin["id"], "exhaustion", 4)

    assert encounter.get_combatant(goblin["id"])["conditions"] == [
        {"name": "exhaustion", "level": 4}
    ]


def _advance_to_round(target_round):
    """Turn over the whole initiative order until the given round is reached."""
    state = encounter.get_state()
    while state["round"] < target_round:
        state = encounter.advance_turn()
    return state


def test_a_timed_condition_expires_when_its_rounds_run_out():
    _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin = _monsters(state)[0]

    encounter.apply_condition(goblin["id"], "prone", duration_rounds=2)
    assert encounter.get_combatant(goblin["id"])["conditions"][0]["name"] == "prone"

    _advance_to_round(2)
    assert encounter.get_combatant(goblin["id"])["conditions"], "should survive its second round"

    _advance_to_round(3)
    assert encounter.get_combatant(goblin["id"])["conditions"] == []


def test_expiry_is_announced_so_the_table_sees_it_end():
    _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin = _monsters(state)[0]
    encounter.apply_condition(goblin["id"], "stunned", duration_rounds=1)

    state = encounter.get_state()
    while state["round"] < 2:
        state = encounter.advance_turn()

    assert any("no longer stunned" in e["text"] for e in state["expired_conditions"])


def test_an_undated_condition_is_never_swept_away():
    _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin = _monsters(state)[0]
    encounter.apply_condition(goblin["id"], "poisoned")

    _advance_to_round(6)

    assert encounter.get_combatant(goblin["id"])["conditions"] == [{"name": "poisoned"}]


def test_state_reports_rounds_remaining():
    _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin = _monsters(state)[0]
    encounter.apply_condition(goblin["id"], "restrained", duration_rounds=3)

    assert encounter.get_combatant(goblin["id"])["conditions"][0]["remaining"] == 3
    _advance_to_round(2)
    assert encounter.get_combatant(goblin["id"])["conditions"][0]["remaining"] == 2


def test_timed_conditions_are_cleared_when_the_fight_ends():
    """Rounds only exist inside an encounter, so a duration counted in them has
    nothing left to count -- it would otherwise sit on a character forever."""
    char = _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    pc = _pcs(state)[0]
    encounter.apply_condition(pc["id"], "restrained", duration_rounds=5)
    encounter.apply_condition(pc["id"], "poisoned")

    encounter.end_encounter()

    stored = json.loads(character.get_character(char["id"])["conditions_json"])
    assert stored == [{"name": "poisoned"}], "only the open-ended condition should survive"


def test_a_new_fight_does_not_inherit_a_stale_countdown():
    """Round numbers restart, so a duration from the last fight would expire at
    the wrong time -- or never."""
    char = _make_character()
    state = encounter.start_encounter("first", [{"slug": "goblin", "count": 1}])
    pc = _pcs(state)[0]
    encounter.apply_condition(pc["id"], "restrained", duration_rounds=9)

    encounter.start_encounter("second", [{"slug": "goblin", "count": 1}])

    assert json.loads(character.get_character(char["id"])["conditions_json"]) == []


def test_expiry_actually_restores_the_dice():
    """The point of the countdown: when prone runs out, attacks stop having
    advantage."""
    _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin, pc = _monsters(state)[0], _pcs(state)[0]
    encounter.apply_condition(pc["id"], "prone", duration_rounds=1)

    assert encounter.monster_attack(goblin["id"], "Scimitar", pc["id"])["adv"] == "advantage"

    _advance_to_round(2)
    character.apply_heal(pc["character_id"], 999)

    assert encounter.monster_attack(goblin["id"], "Scimitar", pc["id"])["adv"] is None


def test_concurrent_changes_do_not_clobber_each_other():
    """Each socket event runs on its own thread, so two changes landing together
    used to lose one: applying two conditions in quick succession dropped the
    first, and simultaneous damage lost hit points the same way."""
    _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin = _monsters(state)[0]
    encounter.heal_combatant(goblin["id"], 999)
    starting_hp = encounter.get_combatant(goblin["id"])["current_hp"]

    names = ["prone", "poisoned", "blinded", "restrained", "charmed", "deafened"]
    threads = [threading.Thread(target=encounter.apply_condition, args=(goblin["id"], n))
               for n in names]
    threads += [threading.Thread(target=encounter.damage_combatant, args=(goblin["id"], 1))
                for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = encounter.get_combatant(goblin["id"])
    assert {c["name"] for c in final["conditions"]} == set(names)
    assert final["current_hp"] == max(0, starting_hp - 6)


def test_monster_attack_takes_advantage_against_a_prone_target():
    _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin, pc = _monsters(state)[0], _pcs(state)[0]
    encounter.apply_condition(pc["id"], "prone")

    result = encounter.monster_attack(goblin["id"], "Scimitar", pc["id"])

    assert result["adv"] == "advantage"
    assert len(result["d20_rolls"]) == 2
    assert result["d20"] == max(result["d20_rolls"])
    assert "advantage" in result["text"]


def test_monster_attack_suffers_disadvantage_while_poisoned():
    _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin, pc = _monsters(state)[0], _pcs(state)[0]
    encounter.apply_condition(goblin["id"], "poisoned")

    result = encounter.monster_attack(goblin["id"], "Scimitar", pc["id"])

    assert result["adv"] == "disadvantage"
    assert result["d20"] == min(result["d20_rolls"])


def test_opposing_conditions_cancel_back_to_a_straight_roll():
    _make_character()
    state = encounter.start_encounter("fight", [{"slug": "goblin", "count": 1}])
    goblin, pc = _monsters(state)[0], _pcs(state)[0]
    encounter.apply_condition(goblin["id"], "poisoned")
    encounter.apply_condition(pc["id"], "prone")

    result = encounter.monster_attack(goblin["id"], "Scimitar", pc["id"])

    assert result["adv"] is None
    assert len(result["d20_rolls"]) == 1


def test_conditions_reference_data_is_available():
    listed = encounter.list_conditions()
    assert len(listed) == 15
    poisoned = next(c for c in listed if c["slug"] == "poisoned")
    assert poisoned["description"], "conditions need their SRD text for the DM screen"


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


def test_monster_attack_handles_fixed_damage():
    """A badger's bite is a flat 1 with no dice at all -- rolling it as a dice
    expression would raise, and dropping it would leave the badger unarmed."""
    _make_character()
    state = encounter.start_encounter("critters", [{"slug": "badger", "count": 1}])
    badger, pc = _monsters(state)[0], _pcs(state)[0]

    result = encounter.monster_attack(badger["id"], "Bite", pc["id"])

    assert "error" not in result
    assert result["damage"] in (0, 1)


def test_attack_parsed_from_prose_is_usable():
    """Whole Open5e sources (Tome of Beasts 3, Black Flag, menagerie) publish
    attacks only as English prose. The ingest parses those into the same shape, so
    the engine can't tell the difference."""
    _make_character()
    state = encounter.start_encounter("tob", [{"slug": "aboleth_bf", "count": 1}])
    beast, pc = _monsters(state)[0], _pcs(state)[0]

    actions = encounter.usable_actions(encounter.get_srd_monster("aboleth_bf"))
    assert actions and actions[0]["attack_bonus"] > 0

    result = encounter.monster_attack(beast["id"], actions[0]["name"], pc["id"])
    assert "error" not in result
    assert 1 <= result["d20"] <= 20


def test_creature_without_hit_dice_falls_back_to_printed_hp():
    """Black Flag creatures publish no hit dice at all, only a flat hit point
    total -- they must still come to the table with real HP."""
    _make_character()
    data = encounter.get_srd_monster("aboleth_bf")
    assert data["hp_expr"] is None, "fixture assumes this creature has no hit dice"

    state = encounter.start_encounter("bf", [{"slug": "aboleth_bf", "count": 1}])
    beast = _monsters(state)[0]

    assert beast["max_hp"] == data["fallback_hp"]


def test_search_labels_the_source_and_puts_core_statblocks_first():
    """The same creature ships in up to four books. Without the source the builder
    shows identical-looking duplicates, and a DM searching 'badger' wants the
    familiar one, not Black Flag's."""
    results = encounter.list_srd_monsters(query="badger")
    badgers = [m for m in results if m["name"] == "Badger"]

    assert len(badgers) > 1, "fixture assumes this creature is published more than once"
    assert all(m["source"] for m in badgers)
    assert badgers[0]["source"] == encounter.CORE_SOURCE


def test_catalog_covers_more_than_the_srd():
    """The point of the Open5e move: the creature list is no longer 334 SRD
    statblocks."""
    all_monsters = encounter.list_srd_monsters()
    assert len(all_monsters) > 3000


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

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import character, dm, encounter, llm, voice

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


def _make_character(name="Thorin"):
    return character.create_character(
        player_id=None, name=name, race="dwarf", class_="fighter",
        ability_scores={"str": 16, "dex": 12, "con": 14, "int": 10, "wis": 10, "cha": 8},
    )


@pytest.fixture(autouse=True)
def clean_llm_state(monkeypatch):
    """Phase 8 put the model behind engine.llm, whose demotion set is global.
    Keep turns in this file from inheriting each other's, and from reading
    whatever keys happen to be in the developer's environment."""
    llm.reset_availability()
    for var in ("NOVA_DM_LLM_PROVIDER", "NOVA_DM_LLM_CHAIN", "NOVA_DM_LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    yield
    llm.reset_availability()


def _install_voice(monkeypatch, run):
    """Seat a scripted voice in the DM's chair.

    These go through the real llm.run_turn rather than patching it out, so what
    is under test is the whole path dm relies on -- announce, execute, narrate,
    and the error handling around it. `run` receives (execute, emit).
    """
    voice_cls = type("ScriptedVoice", (), {
        "name": "test",
        "__init__": lambda self, model=None: None,
        "available": lambda self: True,
        "run_turn": (
            lambda self, system, user_message, tools, execute, emit: run(execute, emit)
        ),
    })
    monkeypatch.setitem(llm.PROVIDERS, "test", voice_cls)
    monkeypatch.setenv("NOVA_DM_LLM_CHAIN", "test")


def _emitted(socketio, event="campaign_event"):
    return [c.args[1]["text"] for c in socketio.emit.call_args_list if c.args[0] == event]


def test_execute_tool_roll_check_logs_and_broadcasts():
    char = _make_character()
    socketio = MagicMock()

    result = dm._execute_tool(
        "roll_check",
        {"character_id": char["id"], "ability": "str", "proficient": True, "advantage": "none"},
        socketio,
    )

    assert 1 <= result["d20"] <= 20
    socketio.emit.assert_called_once()
    event_name, payload = socketio.emit.call_args[0]
    assert event_name == "campaign_event"
    assert "Thorin rolls STR" in payload["text"]

    with character._campaign_con() as con:
        rows = con.execute("SELECT * FROM campaign_log WHERE kind='roll'").fetchall()
    assert len(rows) == 1


def test_execute_tool_apply_damage_reduces_hp_and_logs():
    char = _make_character()
    socketio = MagicMock()

    result = dm._execute_tool("apply_damage", {"character_id": char["id"], "amount": 5}, socketio)

    assert result["current_hp"] == char["max_hp"] - 5
    updated = character.get_character(char["id"])
    assert updated["current_hp"] == char["max_hp"] - 5

    with character._campaign_con() as con:
        rows = con.execute("SELECT * FROM campaign_log WHERE kind='damage'").fetchall()
    assert len(rows) == 1


def test_execute_tool_unknown_tool_returns_error():
    socketio = MagicMock()
    result = dm._execute_tool("nonexistent_tool", {}, socketio)
    assert "error" in result


def test_handle_player_action_runs_tool_loop_then_narrates(monkeypatch):
    char = _make_character()
    socketio = MagicMock()

    def run(execute, emit):
        execute("roll_check", {"character_id": char["id"], "ability": "str",
                               "proficient": True, "advantage": "none"})
        emit("You swing and the blow lands true.")

    _install_voice(monkeypatch, run)
    dm.handle_player_action(char["id"], "I attack the goblin", socketio)

    with character._campaign_con() as con:
        kinds = [r["kind"] for r in con.execute("SELECT kind FROM campaign_log ORDER BY id").fetchall()]
    assert kinds == ["action", "roll", "dm"]

    emitted_texts = _emitted(socketio)
    assert any("I attack the goblin" in t for t in emitted_texts)
    assert any("swing" in t for t in emitted_texts)

    assert ("turn_complete", {"character_id": char["id"]}) == (
        socketio.emit.call_args_list[-1].args[0], socketio.emit.call_args_list[-1].args[1]
    )


def test_narration_is_tagged_with_the_voice_that_spoke_it(monkeypatch):
    """A gemma turn reads differently from a hosted one, so the feed labels who
    is talking rather than presenting every voice as the same DM."""
    char = _make_character()
    socketio = MagicMock()

    _install_voice(monkeypatch, lambda execute, emit: emit("The hall falls silent."))
    dm.handle_player_action(char["id"], "I listen", socketio)

    announced = [c.args[1]["provider"] for c in socketio.emit.call_args_list
                 if c.args[0] == "dm_provider"]
    assert announced == ["test"]

    dm_events = [c.args[1] for c in socketio.emit.call_args_list
                 if c.args[0] == "campaign_event" and c.args[1]["kind"] == "dm"]
    assert dm_events[0]["provider"] == "test"


def test_set_scene_tool_records_the_party_position_without_narrating_it():
    _make_character()
    socketio = MagicMock()

    result = dm._execute_tool(
        "set_scene",
        {"location": "Goblin warren, east tunnel", "summary": "The party freed the miners."},
        socketio,
    )

    assert result["scene"] == "Goblin warren, east tunnel"
    assert "freed the miners" in result["chronicle"]
    assert [c.args[0] for c in socketio.emit.call_args_list] == ["scene_update"]

    # The DM's notebook is not narration -- the players hear about the place
    # from the prose, not from a log line.
    with character._campaign_con() as con:
        kinds = [r["kind"] for r in con.execute("SELECT kind FROM campaign_log").fetchall()]
    assert kinds == []


def test_the_dm_is_told_what_it_wrote_down_last_turn():
    """The whole point of Phase 10: the turn opens with the story, not just the
    numbers."""
    _make_character()
    socketio = MagicMock()
    dm._execute_tool("set_scene", {"location": "The old bridge",
                                   "summary": "They struck a deal with the troll."}, socketio)
    character.log_campaign_event("action", "Thorin", "Thorin: I test the ropes")

    context = dm._build_context(character.list_active_characters())

    assert "struck a deal with the troll" in context
    assert "Where the party is now: The old bridge" in context
    assert "I test the ropes" in context
    # memory comes before the numbers
    assert context.index("The old bridge") < context.index("Characters at the table:")


def test_context_on_a_fresh_campaign_is_unchanged():
    _make_character()
    context = dm._build_context(character.list_active_characters())
    assert context.startswith("Characters at the table:")


def test_award_xp_tool_defaults_to_the_whole_party():
    a = _make_character("Thorin")
    b = _make_character("Mira")
    socketio = MagicMock()

    result = dm._execute_tool("award_xp", {"amount": 50, "reason": "freeing the miners"}, socketio)

    assert sorted(r["character_id"] for r in result["awarded"]) == sorted([a["id"], b["id"]])
    assert character.get_character(a["id"])["xp"] == 50
    assert character.get_character(b["id"])["xp"] == 50
    # An award the table never hears about may as well not have happened.
    assert any("gains 50 XP" in t and "freeing the miners" in t for t in _emitted(socketio))


def test_award_xp_tool_can_name_who_earned_it():
    a = _make_character("Thorin")
    b = _make_character("Mira")
    socketio = MagicMock()

    dm._execute_tool("award_xp", {"amount": 50, "character_ids": [a["id"]]}, socketio)

    assert character.get_character(a["id"])["xp"] == 50
    assert character.get_character(b["id"])["xp"] == 0


def test_crossing_a_threshold_levels_the_character_and_says_so():
    char = _make_character()
    socketio = MagicMock()

    dm._execute_tool("award_xp", {"amount": 300}, socketio)

    updated = character.get_character(char["id"])
    assert updated["level"] == 2
    assert updated["proficiency_bonus"] == 2
    assert updated["max_hp"] > char["max_hp"]

    assert any("reaches level 2" in t for t in _emitted(socketio))
    # The sheet has to move on its own -- the player is looking at it.
    events = [c.args[0] for c in socketio.emit.call_args_list]
    assert "level_up" in events and "sheet_update" in events


def test_ending_a_won_fight_pays_out_its_xp():
    char = _make_character()
    socketio = MagicMock()
    state = dm._execute_tool(
        "start_encounter", {"name": "fight", "monsters": [{"slug": "goblin", "count": 1}]}, socketio
    )
    goblin = next(c for c in state["combatants"] if c["kind"] == "monster")
    encounter.damage_combatant(goblin["id"], goblin["max_hp"])

    result = dm._execute_tool("end_encounter", {}, socketio)

    assert result == {"ended": True, "xp_each": 50}
    assert character.get_character(char["id"])["xp"] == 50
    assert any("defeating Goblin" in t for t in _emitted(socketio))


def test_start_encounter_tool_builds_the_board_and_broadcasts():
    _make_character()
    socketio = MagicMock()

    state = dm._execute_tool(
        "start_encounter",
        {"name": "Goblin ambush", "monsters": [{"slug": "goblin", "count": 2}]},
        socketio,
    )

    assert len(state["combatants"]) == 3  # 2 goblins + the PC
    events = [c.args[0] for c in socketio.emit.call_args_list]
    assert "encounter_update" in events


def test_monster_attack_tool_reports_a_real_roll():
    char = _make_character()
    socketio = MagicMock()
    state = dm._execute_tool(
        "start_encounter", {"name": "fight", "monsters": [{"slug": "goblin", "count": 1}]}, socketio
    )
    goblin = next(c for c in state["combatants"] if c["kind"] == "monster")
    pc = next(c for c in state["combatants"] if c["kind"] == "character")

    result = dm._execute_tool(
        "monster_attack",
        {"combatant_id": goblin["id"], "action_name": "Scimitar", "target_id": pc["id"]},
        socketio,
    )

    assert 1 <= result["d20"] <= 20
    assert result["target_ac"] == char["ac"]


def test_advance_turn_and_end_encounter_tools():
    _make_character()
    socketio = MagicMock()
    dm._execute_tool("start_encounter", {"name": "fight", "monsters": [{"slug": "goblin", "count": 1}]}, socketio)

    state = dm._execute_tool("advance_turn", {}, socketio)
    assert state["turn_index"] == 1

    # The goblin is still standing, so ending the fight here pays out nothing.
    assert dm._execute_tool("end_encounter", {}, socketio) == {"ended": True, "xp_each": 0}
    assert encounter.get_state() is None


def test_condition_tools_apply_and_clear():
    _make_character()
    socketio = MagicMock()
    state = dm._execute_tool(
        "start_encounter", {"name": "fight", "monsters": [{"slug": "goblin", "count": 1}]}, socketio
    )
    goblin = next(c for c in state["combatants"] if c["kind"] == "monster")

    applied = dm._execute_tool(
        "apply_condition", {"combatant_id": goblin["id"], "condition": "prone"}, socketio
    )
    assert applied["conditions"] == [{"name": "prone"}]

    cleared = dm._execute_tool(
        "remove_condition", {"combatant_id": goblin["id"], "condition": "prone"}, socketio
    )
    assert cleared["conditions"] == []


def test_condition_tool_reports_immunity_rather_than_lying():
    _make_character()
    socketio = MagicMock()
    state = dm._execute_tool(
        "start_encounter", {"name": "deep", "monsters": [{"slug": "aboleth-nihilith", "count": 1}]},
        socketio,
    )
    beast = next(c for c in state["combatants"] if c["kind"] == "monster")

    result = dm._execute_tool(
        "apply_condition", {"combatant_id": beast["id"], "condition": "charmed"}, socketio
    )

    assert "error" in result and "immune" in result["error"]


def test_context_lists_conditions_and_says_advantage_is_automatic():
    """The DM has to know the state of the board, and must not re-apply advantage
    on top of what the engine already did."""
    _make_character()
    socketio = MagicMock()
    state = dm._execute_tool(
        "start_encounter", {"name": "fight", "monsters": [{"slug": "goblin", "count": 1}]}, socketio
    )
    goblin = next(c for c in state["combatants"] if c["kind"] == "monster")
    dm._execute_tool("apply_condition", {"combatant_id": goblin["id"], "condition": "stunned"},
                     socketio)

    context = dm._build_context(character.list_active_characters())

    assert "conditions: stunned" in context
    assert "cannot act" in context
    assert "applied by the engine" in context


def test_roll_check_tool_merges_conditions_with_the_requested_advantage():
    """Poisoned imposes disadvantage; an explicitly requested advantage cancels it
    rather than one silently winning."""
    char = _make_character()
    socketio = MagicMock()
    with character._campaign_con() as con:
        con.execute("UPDATE characters SET conditions_json=? WHERE id=?",
                    ('[{"name": "poisoned"}]', char["id"]))

    result = dm._execute_tool(
        "roll_check",
        {"character_id": char["id"], "ability": "str", "proficient": False, "advantage": "advantage"},
        socketio,
    )

    assert result["adv"] is None
    assert len(result["d20_rolls"]) == 1


def test_context_gives_the_dm_the_real_board():
    """The DM must narrate from the board, so the board has to be in its context."""
    _make_character()
    socketio = MagicMock()
    dm._execute_tool("start_encounter", {"name": "Goblin ambush", "monsters": [{"slug": "goblin", "count": 1}]}, socketio)

    context = dm._build_context(character.list_active_characters())

    assert "Goblin ambush" in context
    assert "combatant_id=" in context
    assert ">>" in context  # current turn marker


def test_context_says_so_when_no_encounter_is_running():
    _make_character()
    assert "No encounter is active" in dm._build_context(character.list_active_characters())


def test_handle_player_action_speaks_the_narration(monkeypatch):
    char = _make_character()
    socketio = MagicMock()

    _install_voice(monkeypatch,
                   lambda execute, emit: emit("The torchlight gutters as you step through."))

    spoken = []
    with patch.object(voice, "speak", side_effect=spoken.append):
        dm.handle_player_action(char["id"], "I open the door", socketio)

    assert spoken == ["The torchlight gutters as you step through."]


def test_turn_survives_a_broken_speaker(monkeypatch):
    """A dead sound card must not cost the player their turn -- the text still
    lands and the submit button is still released."""
    char = _make_character()
    socketio = MagicMock()

    _install_voice(monkeypatch,
                   lambda execute, emit: emit("The torchlight gutters as you step through."))

    with patch.object(voice, "speak", side_effect=RuntimeError("no audio device")):
        dm.handle_player_action(char["id"], "I open the door", socketio)

    assert any("torchlight" in t for t in _emitted(socketio))
    assert socketio.emit.call_args_list[-1].args[0] == "turn_complete"


def test_handle_player_action_surfaces_an_overlong_turn(monkeypatch):
    """The iteration cap itself lives in engine.llm and is tested there; what dm
    owes the table is that the message arrives and the button comes back."""
    char = _make_character()
    socketio = MagicMock()

    def overrun(execute, emit):
        raise llm.TurnExhausted("the DM pauses to gather their thoughts -- try again")

    _install_voice(monkeypatch, overrun)
    dm.handle_player_action(char["id"], "I keep trying forever", socketio)

    assert any("gather their thoughts" in t for t in _emitted(socketio))
    assert socketio.emit.call_args_list[-1].args[0] == "turn_complete"


def test_handle_player_action_surfaces_a_dead_voice_instead_of_hanging(monkeypatch):
    """Phase 8 changed the wording -- the table is told no voice is available
    rather than that one vendor is unreachable -- but the underlying cause must
    still reach the players, and the turn must still end."""
    char = _make_character()
    socketio = MagicMock()

    def die(execute, emit):
        raise llm.ProviderUnavailable("credit balance is too low")

    _install_voice(monkeypatch, die)
    dm.handle_player_action(char["id"], "I attack the goblin", socketio)

    emitted_texts = _emitted(socketio)
    assert any("credit balance is too low" in t for t in emitted_texts)
    assert any("no DM voice is available" in t for t in emitted_texts)

    # A plumbing failure is table chatter: never logged as narration, never spoken.
    with character._campaign_con() as con:
        kinds = [r["kind"] for r in con.execute("SELECT kind FROM campaign_log ORDER BY id").fetchall()]
    assert "dm" not in kinds

    # even on the error path the player's button must be released
    assert socketio.emit.call_args_list[-1].args[0] == "turn_complete"


# ---------------------------------------------------------------------------
# Situational tool sets -- prompt size is what a turn costs on this hardware
# ---------------------------------------------------------------------------

def test_encounter_tools_are_withheld_when_nobody_is_fighting():
    """They all take a combatant id, which does not exist outside a fight, so
    sending them buys nothing and is re-read on every iteration."""
    out = {t["name"] for t in dm.tools_for(in_combat=False)}
    assert not (out & dm.ENCOUNTER_ONLY)
    # but a fight has to be startable from outside one
    assert "start_encounter" in out
    assert {"roll_check", "roll_dice", "apply_damage", "set_scene"} <= out


def test_every_tool_is_available_in_a_fight():
    assert dm.tools_for(in_combat=True) == dm.TOOLS


def test_withholding_them_actually_shrinks_the_prompt():
    import json
    peace = len(json.dumps(dm.tools_for(in_combat=False)))
    war = len(json.dumps(dm.tools_for(in_combat=True)))
    assert peace < war
    # worth having: a fifth of the tool budget, re-read on every call
    assert (war - peace) / war > 0.15

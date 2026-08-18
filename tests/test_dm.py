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

    # emitted with the right character -- not necessarily last, since the
    # table's busy flag is cleared after the turn returns
    assert ("turn_complete", {"character_id": char["id"]}) in [
        (c.args[0], c.args[1]) for c in socketio.emit.call_args_list if len(c.args) > 1
    ]


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
    # emitted, not necessarily last -- the table's busy flag is cleared after it
    assert "turn_complete" in [c.args[0] for c in socketio.emit.call_args_list]


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
    # emitted, not necessarily last -- the table's busy flag is cleared after it
    assert "turn_complete" in [c.args[0] for c in socketio.emit.call_args_list]


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
    # emitted, not necessarily last -- the table's busy flag is cleared after it
    assert "turn_complete" in [c.args[0] for c in socketio.emit.call_args_list]


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


def test_the_prompt_is_split_without_losing_a_rule():
    """Every rule still reaches the pass it governs -- the split is a routing
    change, not a trim of the rules themselves."""
    assert dm.SYSTEM_PROMPT == dm._IDENTITY + dm._MECHANICS + dm._PROSE
    assert dm.PLANNING_PROMPT == dm._IDENTITY + dm._MECHANICS
    assert dm.NARRATION_PROMPT == dm._IDENTITY + dm._PROSE

    # planning is told what to call and nothing about style
    assert "roll_check" in dm.PLANNING_PROMPT
    assert "second person" not in dm.PLANNING_PROMPT
    # narration is told how to write and nothing about calling
    assert "second person" in dm.NARRATION_PROMPT
    assert "start_encounter" not in dm.NARRATION_PROMPT
    # both still know who they are
    assert "Dungeon Master" in dm.PLANNING_PROMPT and "Dungeon Master" in dm.NARRATION_PROMPT


def test_the_narration_pass_is_much_cheaper_than_the_planning_pass():
    """The whole reason a slower, better model can afford to write the prose."""
    assert len(dm.NARRATION_PROMPT) < len(dm.PLANNING_PROMPT) * 0.75


def test_the_seeded_world_reaches_the_narration_pass_but_not_the_planning_pass(monkeypatch):
    """Deciding which dice to roll does not depend on how Zorya talks, and on
    the two-pass path the narration prompt is sent once while the context is
    sent twice -- so colour belongs here and nowhere else."""
    monkeypatch.setattr(dm.campaign, "flavour_block",
                        lambda: "Who is in this world:\n- Zorya - a cat.")

    suffix = dm._flavour_suffix()

    assert "Zorya" in suffix
    assert "Zorya" not in dm.PLANNING_PROMPT
    assert "Zorya" in dm.NARRATION_PROMPT + suffix


def test_an_unseeded_campaign_pays_nothing_for_flavour(monkeypatch):
    monkeypatch.setattr(dm.campaign, "flavour_block", lambda: "")
    assert dm._flavour_suffix() == ""


def test_players_are_not_shown_the_database_column_name():
    """The column is int_ because int is a builtin. Players were seeing
    'Chazel rolls INT_: 20+0=20'."""
    from engine import rules
    assert rules.ability_label("int_") == "INT"
    assert rules.ability_label("int") == "INT"
    assert rules.ability_label("dex") == "DEX"


# ---------------------------------------------------------------------------
# Handing the die to the player
# ---------------------------------------------------------------------------

def test_a_client_cannot_send_its_own_number(monkeypatch):
    """The prompt says *when* to roll, never what was rolled -- a client that
    could report its own result would report a twenty every time."""
    import app as app_module
    from engine import handoff

    rolled = []  # (an unknown token is refused -- see test_handoff.py)
    monkeypatch.setattr(handoff, "answer", lambda token: rolled.append(token) or True)

    # everything a client is allowed to say about a roll
    app_module.on_player_roll({"token": "abc", "d20": 20, "total": 99})

    assert rolled == ["abc"], "only the token is read; the numbers are ignored"


def test_asking_can_be_turned_off(monkeypatch):
    """It roughly doubles the wall time of a turn that needs a roll, so it has
    to be one env var to put back."""
    monkeypatch.setattr(dm.handoff, "ASK", False)
    emitted = []
    socketio = type("S", (), {"emit": lambda self, *a, **k: emitted.append(a)})()

    dm._await_player({"id": 1, "name": "Ferrick"}, {"ability": "dex"}, None, socketio)

    assert emitted == [], "no prompt should be sent when asking is off"


def test_a_prompt_says_what_is_being_rolled(monkeypatch):
    monkeypatch.setattr(dm.handoff, "ASK", True)
    monkeypatch.setattr(dm.handoff, "wait", lambda pending, timeout=None: True)
    emitted = []
    socketio = type("S", (), {"emit": lambda self, name, payload=None, **k:
                              emitted.append((name, payload))})()

    dm._await_player({"id": 7, "name": "Ferrick"},
                     {"ability": "dex", "proficient": True}, "advantage", socketio)

    prompt = dict(emitted)["roll_prompt"]
    assert prompt["character_id"] == 7
    assert prompt["ability"] == "DEX"
    assert prompt["proficient"] is True
    assert prompt["advantage"] == "advantage"
    assert prompt["token"]


def test_nobody_taking_the_die_is_said_out_loud(monkeypatch):
    """A roll the player did not make should look different from one they did."""
    monkeypatch.setattr(dm.handoff, "ASK", True)
    monkeypatch.setattr(dm.handoff, "wait", lambda pending, timeout=None: False)
    emitted = []
    socketio = type("S", (), {"emit": lambda self, name, payload=None, **k:
                              emitted.append((name, payload))})()

    dm._await_player({"id": 1, "name": "Ferrick"}, {"ability": "wis"}, None, socketio)

    said = [p["text"] for n, p in emitted if n == "campaign_event"]
    assert any("didn't pick up the die" in t for t in said)


def test_the_prompt_is_always_cleared(monkeypatch):
    """Otherwise a timed-out prompt sits on every phone forever."""
    monkeypatch.setattr(dm.handoff, "ASK", True)
    for answered in (True, False):
        monkeypatch.setattr(dm.handoff, "wait", lambda pending, timeout=None, a=answered: a)
        emitted = []

        def record(self, name, payload=None, _sink=emitted, **kwargs):
            _sink.append(name)

        socketio = type("S", (), {"emit": record})()
        dm._await_player({"id": 1, "name": "F"}, {"ability": "str"}, None, socketio)
        assert "roll_prompt_done" in emitted


# ---------------------------------------------------------------------------
# One turn at a time
# ---------------------------------------------------------------------------

def test_two_players_acting_at_once_are_serialized(monkeypatch):
    """This box is saturated by a single turn, so two at once do not run twice
    as fast -- each runs at half speed and both wait longer than if they had
    queued. They also interleave in the feed, which reads as the DM losing the
    thread."""
    import threading
    import time

    overlapped = []
    inside = threading.Event()

    def slow_turn(actor, character_id, action_text, socketio):
        if inside.is_set():
            overlapped.append(True)
        inside.set()
        time.sleep(0.2)
        inside.clear()

    monkeypatch.setattr(dm, "_run_turn", slow_turn)
    monkeypatch.setattr(dm.character, "get_character", lambda cid: {"id": cid, "name": "P"})
    socketio = type("S", (), {"emit": lambda self, *a, **k: None})()

    threads = [threading.Thread(target=dm.handle_player_action, args=(i, "act", socketio))
               for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlapped == [], "turns must not run concurrently"


def test_the_table_is_told_when_it_is_busy(monkeypatch):
    """A second player watching a dead button should know why."""
    monkeypatch.setattr(dm, "_run_turn", lambda *a: None)
    monkeypatch.setattr(dm.character, "get_character", lambda cid: {"id": cid, "name": "Ferrick"})
    sent = []
    socketio = type("S", (), {"emit": lambda self, name, payload=None, **k:
                              sent.append((name, payload))})()

    dm.handle_player_action(1, "I listen.", socketio)

    states = [p for n, p in sent if n == "dm_state"]
    assert states[0]["busy"] is True and states[0]["acting"] == "Ferrick"
    assert states[-1]["busy"] is False, "and told when it is free again"


def test_the_table_is_freed_even_if_the_turn_explodes(monkeypatch):
    """A crash mid-turn must not leave every player locked out forever."""
    def boom(*a):
        raise RuntimeError("the DM fell over")

    monkeypatch.setattr(dm, "_run_turn", boom)
    monkeypatch.setattr(dm.character, "get_character", lambda cid: {"id": cid, "name": "P"})
    sent = []
    socketio = type("S", (), {"emit": lambda self, name, payload=None, **k:
                              sent.append((name, payload))})()

    with pytest.raises(RuntimeError):
        dm.handle_player_action(1, "act", socketio)

    assert not dm.is_busy()
    assert [p for n, p in sent if n == "dm_state"][-1]["busy"] is False

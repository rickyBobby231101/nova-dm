import os
import sys
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import character, dm, encounter, voice

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


class _Block:
    def __init__(self, type_, **kwargs):
        self.type = type_
        for k, v in kwargs.items():
            setattr(self, k, v)


class _Response:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


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


def test_handle_player_action_runs_tool_loop_then_narrates():
    char = _make_character()
    socketio = MagicMock()

    tool_use_block = _Block(
        "tool_use", id="tu_1", name="roll_check",
        input={"character_id": char["id"], "ability": "str", "proficient": True, "advantage": "none"},
    )
    first_response = _Response(content=[tool_use_block], stop_reason="tool_use")
    second_response = _Response(
        content=[_Block("text", text="You swing and the blow lands true.")],
        stop_reason="end_turn",
    )

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [first_response, second_response]

    with patch.object(dm, "_get_client", return_value=fake_client):
        dm.handle_player_action(char["id"], "I attack the goblin", socketio)

    assert fake_client.messages.create.call_count == 2

    with character._campaign_con() as con:
        kinds = [r["kind"] for r in con.execute("SELECT kind FROM campaign_log ORDER BY id").fetchall()]
    assert kinds == ["action", "roll", "dm"]

    emitted_texts = [call.args[1]["text"] for call in socketio.emit.call_args_list
                     if call.args[0] == "campaign_event"]
    assert any("I attack the goblin" in t for t in emitted_texts)
    assert any("swing" in t for t in emitted_texts)

    assert ("turn_complete", {"character_id": char["id"]}) == (
        socketio.emit.call_args_list[-1].args[0], socketio.emit.call_args_list[-1].args[1]
    )


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

    assert dm._execute_tool("end_encounter", {}, socketio) == {"ended": True}
    assert encounter.get_state() is None


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


def test_handle_player_action_speaks_the_narration():
    char = _make_character()
    socketio = MagicMock()

    response = _Response(
        content=[_Block("text", text="The torchlight gutters as you step through.")],
        stop_reason="end_turn",
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response

    spoken = []
    with patch.object(dm, "_get_client", return_value=fake_client), \
         patch.object(voice, "speak", side_effect=spoken.append):
        dm.handle_player_action(char["id"], "I open the door", socketio)

    assert spoken == ["The torchlight gutters as you step through."]


def test_turn_survives_a_broken_speaker():
    """A dead sound card must not cost the player their turn -- the text still
    lands and the submit button is still released."""
    char = _make_character()
    socketio = MagicMock()

    response = _Response(
        content=[_Block("text", text="The torchlight gutters as you step through.")],
        stop_reason="end_turn",
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response

    with patch.object(dm, "_get_client", return_value=fake_client), \
         patch.object(voice, "speak", side_effect=RuntimeError("no audio device")):
        dm.handle_player_action(char["id"], "I open the door", socketio)

    emitted_texts = [call.args[1]["text"] for call in socketio.emit.call_args_list
                     if call.args[0] == "campaign_event"]
    assert any("torchlight" in t for t in emitted_texts)
    assert socketio.emit.call_args_list[-1].args[0] == "turn_complete"


def test_handle_player_action_gives_up_after_max_iterations():
    char = _make_character()
    socketio = MagicMock()

    looping_block = _Block(
        "tool_use", id="tu_x", name="roll_dice", input={"expr": "1d6"},
    )
    looping_response = _Response(content=[looping_block], stop_reason="tool_use")

    fake_client = MagicMock()
    fake_client.messages.create.return_value = looping_response

    with patch.object(dm, "_get_client", return_value=fake_client):
        dm.handle_player_action(char["id"], "I keep trying forever", socketio)

    assert fake_client.messages.create.call_count == dm.MAX_TOOL_ITERATIONS
    emitted_texts = [call.args[1]["text"] for call in socketio.emit.call_args_list
                     if call.args[0] == "campaign_event"]
    assert any("gather their thoughts" in t for t in emitted_texts)


def test_handle_player_action_surfaces_api_error_instead_of_hanging():
    char = _make_character()
    socketio = MagicMock()

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = anthropic.APIConnectionError(
        message="credit balance is too low", request=req
    )

    with patch.object(dm, "_get_client", return_value=fake_client):
        dm.handle_player_action(char["id"], "I attack the goblin", socketio)

    assert fake_client.messages.create.call_count == 1
    emitted_texts = [call.args[1]["text"] for call in socketio.emit.call_args_list
                     if call.args[0] == "campaign_event"]
    assert any("credit balance is too low" in t for t in emitted_texts)
    assert any("unreachable" in t for t in emitted_texts)

    # even on the error path the player's button must be released
    assert socketio.emit.call_args_list[-1].args[0] == "turn_complete"

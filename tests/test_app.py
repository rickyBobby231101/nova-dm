import os
import sys

import pytest
from flask_socketio import SocketIOTestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
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


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _join(client, name="Chazel"):
    return client.post("/join", data={"name": name})


def _create_character(client, name="Thorin"):
    return client.post("/characters", data={
        "name": name, "race": "dwarf", "class": "fighter",
        "str": "16", "dex": "12", "con": "14", "int": "10", "wis": "10", "cha": "8",
    })


def test_join_sets_cookie_and_redirects(client):
    resp = _join(client)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/characters")
    assert "session_token" in resp.headers.get("Set-Cookie", "")


def test_characters_page_requires_join_first(client):
    resp = client.get("/characters")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/") or resp.headers["Location"].endswith("/join")


def test_character_creation_end_to_end(client):
    _join(client)
    resp = _create_character(client)
    assert resp.status_code == 302
    assert "/play?character_id=" in resp.headers["Location"]

    char_id = int(resp.headers["Location"].rsplit("=", 1)[1])
    api_resp = client.get(f"/api/character/{char_id}")
    assert api_resp.status_code == 200
    data = api_resp.get_json()
    assert data["name"] == "Thorin"
    assert data["race"] == "dwarf"
    assert data["con"] == 16  # dwarf +2 CON applied
    assert data["max_hp"] > 0


def test_play_page_rejects_other_players_character(client):
    _join(client, name="Chazel")
    resp = _create_character(client)
    char_id = int(resp.headers["Location"].rsplit("=", 1)[1])

    # a second player, different cookie jar (fresh client), should not see it
    other = app_module.app.test_client()
    other.post("/join", data={"name": "Someone Else"})
    resp2 = other.get(f"/play?character_id={char_id}")
    assert resp2.status_code == 302
    assert resp2.headers["Location"].endswith("/characters")


def test_roll_request_broadcasts_roll_result_and_logs_it():
    _join_resp = app_module.app.test_client()
    _join_resp.post("/join", data={"name": "Chazel"})
    create_resp = _join_resp.post("/characters", data={
        "name": "Mira", "race": "human", "class": "cleric",
        "str": "10", "dex": "12", "con": "13", "int": "10", "wis": "15", "cha": "10",
    })
    char_id = int(create_resp.headers["Location"].rsplit("=", 1)[1])

    socket_client = SocketIOTestClient(app_module.app, app_module.socketio)
    assert socket_client.is_connected()
    socket_client.emit("roll_request", {"character_id": char_id, "ability": "wis", "proficient": True})
    received = socket_client.get_received()

    # connect also delivers voice_state, so pick the event out by name rather than
    # assuming a roll is the only thing a client ever hears
    rolls = [r for r in received if r["name"] == "roll_result"]
    assert len(rolls) == 1
    result = rolls[0]["args"][0]
    assert result["character"] == "Mira"
    assert "d20" in result and 1 <= result["d20"] <= 20
    assert "total" in result

    with app_module.character._campaign_con() as con:
        rows = con.execute("SELECT * FROM campaign_log WHERE kind='roll'").fetchall()
    assert len(rows) == 1
    assert "Mira" in rows[0]["content"]


def test_dm_screen_renders():
    resp = app_module.app.test_client().get("/dm")
    assert resp.status_code == 200
    assert b"DM Screen" in resp.data


def test_dm_can_run_a_fight_over_sockets():
    """The human DM's controls and the players' view are the same round trip: one
    encounter_update payload broadcast to the room."""
    player = app_module.app.test_client()
    player.post("/join", data={"name": "Chazel"})
    create = player.post("/characters", data={
        "name": "Mira", "race": "human", "class": "cleric",
        "str": "10", "dex": "12", "con": "13", "int": "10", "wis": "15", "cha": "10",
    })
    char_id = int(create.headers["Location"].rsplit("=", 1)[1])

    dm_client = SocketIOTestClient(app_module.app, app_module.socketio)
    player_client = SocketIOTestClient(app_module.app, app_module.socketio)
    dm_client.get_received()
    player_client.get_received()

    dm_client.emit("dm_start_encounter", {"name": "Ambush", "monsters": [{"slug": "goblin", "count": 2}]})

    updates = [r for r in player_client.get_received() if r["name"] == "encounter_update"]
    assert updates, "the player's device never heard the fight start"
    state = updates[-1]["args"][0]["encounter"]
    assert state["name"] == "Ambush"
    assert len(state["combatants"]) == 3

    goblin = next(c for c in state["combatants"] if c["kind"] == "monster")
    dm_client.emit("dm_damage", {"combatant_id": goblin["id"], "amount": 2})
    after = [r for r in dm_client.get_received() if r["name"] == "encounter_update"][-1]
    hit = next(c for c in after["args"][0]["encounter"]["combatants"] if c["id"] == goblin["id"])
    assert hit["current_hp"] == goblin["max_hp"] - 2

    pc = next(c for c in state["combatants"] if c["kind"] == "character")
    dm_client.emit("dm_damage", {"combatant_id": pc["id"], "amount": 3})
    char = app_module.character.get_character(char_id)
    assert char["current_hp"] == char["max_hp"] - 3

    dm_client.emit("dm_next_turn")
    turned = [r for r in dm_client.get_received() if r["name"] == "encounter_update"][-1]
    assert turned["args"][0]["encounter"]["turn_index"] == 1

    dm_client.emit("dm_end_encounter")
    ended = [r for r in dm_client.get_received() if r["name"] == "encounter_update"][-1]
    assert ended["args"][0]["encounter"] is None


def test_monster_search_returns_srd_matches():
    client = SocketIOTestClient(app_module.app, app_module.socketio)
    client.get_received()
    client.emit("monster_search", {"query": "goblin"})
    results = [r for r in client.get_received() if r["name"] == "monster_results"][-1]
    assert any(m["slug"] == "goblin" for m in results["args"][0]["monsters"])


def test_set_voice_is_room_wide():
    """The speaker is the server box, so muting from one phone has to mute the
    room and update every other device's toggle."""
    try:
        one = SocketIOTestClient(app_module.app, app_module.socketio)
        two = SocketIOTestClient(app_module.app, app_module.socketio)
        one.get_received()
        two.get_received()

        one.emit("set_voice", {"enabled": False})

        assert app_module.voice.is_enabled() is False
        for client in (one, two):
            states = [r for r in client.get_received() if r["name"] == "voice_state"]
            assert states and states[-1]["args"][0]["enabled"] is False
    finally:
        app_module.voice.set_enabled(True)

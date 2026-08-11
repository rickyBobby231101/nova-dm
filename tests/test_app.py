import os
import sys

import pytest
from flask_socketio import SocketIOTestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import character
import app as app_module

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

    assert len(received) == 1
    result = received[0]["args"][0]
    assert result["character"] == "Mira"
    assert "d20" in result and 1 <= result["d20"] <= 20
    assert "total" in result

    with app_module.character._campaign_con() as con:
        rows = con.execute("SELECT * FROM campaign_log WHERE kind='roll'").fetchall()
    assert len(rows) == 1
    assert "Mira" in rows[0]["content"]

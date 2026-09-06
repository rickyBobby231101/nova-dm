import io
import os
import sys

import pytest
from flask_socketio import SocketIOTestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
from engine import auth, character

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
    return client.post("/join", data={"name": name, "join_code": auth.join_code()})


def _become_dm(client):
    """Kept as a no-op so older tests still read.

    There is no second door as of 2026-09-05: being at the table is being the
    DM. The helper stays rather than being deleted from a dozen call sites, and
    says so here so nobody concludes a password still exists."""
    return None


def _socket_client():
    """A socket only connects for a browser that has given the join code."""
    client = app_module.app.test_client()
    _join(client)
    return SocketIOTestClient(app_module.app, app_module.socketio, flask_test_client=client)


def _dm_socket_client():
    """...and dm_* events additionally need the DM password."""
    client = app_module.app.test_client()
    _join(client)
    _become_dm(client)
    return SocketIOTestClient(app_module.app, app_module.socketio, flask_test_client=client)


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
    other.post("/join", data={"name": "Someone Else", "join_code": auth.join_code()})
    resp2 = other.get(f"/play?character_id={char_id}")
    assert resp2.status_code == 302
    assert resp2.headers["Location"].endswith("/characters")


def test_roll_request_broadcasts_roll_result_and_logs_it():
    _join_resp = app_module.app.test_client()
    _join_resp.post("/join", data={"name": "Chazel", "join_code": auth.join_code()})
    create_resp = _join_resp.post("/characters", data={
        "name": "Mira", "race": "human", "class": "cleric",
        "str": "10", "dex": "12", "con": "13", "int": "10", "wis": "15", "cha": "10",
    })
    char_id = int(create_resp.headers["Location"].rsplit("=", 1)[1])

    socket_client = _socket_client()
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


def _join_and_create(name="Chazel", char_name="Mira", race="dwarf"):
    client = app_module.app.test_client()
    client.post("/join", data={"name": name, "join_code": auth.join_code()})
    resp = client.post("/characters", data={
        "name": char_name, "race": race, "class": "cleric",
        "str": "10", "dex": "12", "con": "13", "int": "10", "wis": "15", "cha": "10",
    })
    return client, int(resp.headers["Location"].rsplit("=", 1)[1])


def test_export_route_offers_a_download():
    client, char_id = _join_and_create()

    resp = client.get(f"/character/{char_id}/export")

    assert resp.status_code == 200
    assert "attachment" in resp.headers["Content-Disposition"]
    assert "mira.nova-dm.json" in resp.headers["Content-Disposition"]
    payload = resp.get_json()
    assert payload["format"] == "nova-dm.character"
    assert payload["character"]["name"] == "Mira"


def test_export_refuses_someone_elses_character():
    _, char_id = _join_and_create(name="Chazel", char_name="Mira")
    other = app_module.app.test_client()
    other.post("/join", data={"name": "Someone Else", "join_code": auth.join_code()})

    resp = other.get(f"/character/{char_id}/export")

    assert resp.status_code == 302
    assert "/characters" in resp.headers["Location"]


def test_pasted_export_imports_and_lands_in_play():
    client, char_id = _join_and_create(char_name="Mira", race="dwarf")
    blob = client.get(f"/character/{char_id}/export").get_data(as_text=True)

    importer = app_module.app.test_client()
    importer.post("/join", data={"name": "Second Table", "join_code": auth.join_code()})
    resp = importer.post("/characters/import", data={"pasted": blob})

    assert resp.status_code == 302
    new_id = int(resp.headers["Location"].rsplit("=", 1)[1])
    assert new_id != char_id

    original = app_module.character.get_character(char_id)
    restored = app_module.character.get_character(new_id)
    assert restored["con"] == original["con"], "racial bonus was re-applied on import"
    assert restored["name"] == original["name"]


def test_uploaded_file_imports():
    client, char_id = _join_and_create(char_name="Mira")
    blob = client.get(f"/character/{char_id}/export").get_data()

    importer = app_module.app.test_client()
    importer.post("/join", data={"name": "Third Table", "join_code": auth.join_code()})
    resp = importer.post(
        "/characters/import",
        data={"file": (io.BytesIO(blob), "mira.nova-dm.json")},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 302
    assert "/play" in resp.headers["Location"]


def test_bad_paste_shows_a_message_instead_of_a_500():
    client, _ = _join_and_create()

    resp = client.post("/characters/import", data={"pasted": "{not json"})

    assert resp.status_code == 400
    assert b"isn&#39;t valid JSON" in resp.data or b"isn't valid JSON" in resp.data
    assert b"IMPORT A CHARACTER" in resp.data, "should re-render the page, not a bare error"


def test_the_table_view_renders():
    client = app_module.app.test_client()
    _join(client)
    resp = client.get("/dm")
    assert resp.status_code == 200
    assert b"The Table" in resp.data


# --------------------------------------------------------------------------
# Phase 11: the door, and the second door behind it
# --------------------------------------------------------------------------

def test_join_needs_the_code(client):
    resp = client.post("/join", data={"name": "Gatecrasher", "join_code": "WRONG1"})

    assert resp.status_code == 403
    assert b"join code isn" in resp.data
    assert "session_token" not in resp.headers.get("Set-Cookie", "")


def test_join_code_is_forgiving_about_case_and_spacing(client):
    code = auth.join_code()
    resp = client.post("/join", data={"name": "Chazel",
                                      "join_code": f" {code.lower()[:3]}-{code.lower()[3:]} "})

    assert resp.status_code == 302, "a code read aloud and retyped should still work"


def test_campaign_pages_are_closed_before_the_code(client):
    """Not merely unlinked -- actually closed."""
    for path in ["/characters", "/play?character_id=1", "/api/encounter",
                 "/narration/" + "a" * 32 + ".wav"]:
        resp = client.get(path)
        assert resp.status_code == 302, f"{path} was reachable without the join code"


def test_socket_refuses_a_browser_that_never_joined():
    """An unauthenticated socket must not sit in the room collecting broadcasts."""
    stranger = SocketIOTestClient(app_module.app, app_module.socketio,
                                  flask_test_client=app_module.app.test_client())
    assert not stranger.is_connected()


def test_the_table_can_drive_the_dm_events():
    """As of 2026-09-05 the join code is the only door.

    There used to be a second password on the manual controls. Daniel: "we
    don't need a password." The threat model is a private tailnet among
    invited friends, and a second credential only meant the one person who
    could unstick a stalled turn had to be at the laptop that knew it.

    So anyone at the table can now drive them — which is the point: someone
    else keeps the game moving while Daniel plays rather than runs it."""
    player = _socket_client()  # join code only
    player.get_received()

    player.emit("dm_award_xp", {"amount": 10})

    assert "dm_denied" not in [r["name"] for r in player.get_received()]


def test_the_dm_screen_no_longer_challenges(client):
    """The second password is gone; the screen opens for anyone at the table."""
    _join(client)
    resp = client.get("/dm")

    assert resp.status_code == 200
    assert b"Encounter Builder" in resp.data


def test_a_stranger_still_cannot_drive_the_dm_events():
    """The join code remains the boundary that matters.

    Removing the second password widened who at the table may drive the game.
    It did not open it to anyone who never joined — a socket that never gave
    the join code is refused at connect and never reaches these events at all."""
    stranger = SocketIOTestClient(app_module.app, app_module.socketio,
                                  flask_test_client=app_module.app.test_client())
    assert not stranger.is_connected(), "an unjoined socket reached the table"


def test_dm_can_run_a_fight_over_sockets():
    """The human DM's controls and the players' view are the same round trip: one
    encounter_update payload broadcast to the room."""
    player = app_module.app.test_client()
    player.post("/join", data={"name": "Chazel", "join_code": auth.join_code()})
    create = player.post("/characters", data={
        "name": "Mira", "race": "human", "class": "cleric",
        "str": "10", "dex": "12", "con": "13", "int": "10", "wis": "15", "cha": "10",
    })
    char_id = int(create.headers["Location"].rsplit("=", 1)[1])

    dm_client = _dm_socket_client()
    player_client = _socket_client()
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


def test_dm_can_award_xp_over_sockets_and_the_sheet_follows():
    """The human DM's grant travels the same path as the AI DM's tool, and the
    player's device is told to move its sheet without a refresh."""
    player = app_module.app.test_client()
    player.post("/join", data={"name": "Chazel", "join_code": auth.join_code()})
    create = player.post("/characters", data={
        "name": "Mira", "race": "human", "class": "cleric",
        "str": "10", "dex": "12", "con": "13", "int": "10", "wis": "15", "cha": "10",
    })
    char_id = int(create.headers["Location"].rsplit("=", 1)[1])

    dm_client = _dm_socket_client()
    player_client = _socket_client()
    dm_client.get_received()
    player_client.get_received()

    dm_client.emit("dm_award_xp", {"amount": 300, "character_ids": [char_id],
                                   "reason": "freeing the miners"})

    char = app_module.character.get_character(char_id)
    assert char["xp"] == 300
    assert char["level"] == 2

    received = player_client.get_received()
    names = [r["name"] for r in received]
    assert "level_up" in names
    sheet = [r for r in received if r["name"] == "sheet_update"][-1]["args"][0]["character"]
    assert sheet["id"] == char_id and sheet["level"] == 2

    texts = [r["args"][0]["text"] for r in received if r["name"] == "campaign_event"]
    assert any("freeing the miners" in t for t in texts)
    assert any("reaches level 2" in t for t in texts)


def test_dm_award_xp_ignores_an_empty_amount():
    app_module.app.test_client().post("/join", data={"name": "Chazel", "join_code": auth.join_code()})
    dm_client = _dm_socket_client()
    dm_client.get_received()

    dm_client.emit("dm_award_xp", {"amount": 0})
    assert not [r for r in dm_client.get_received() if r["name"] == "campaign_event"]


def test_dm_can_apply_and_clear_a_condition_over_sockets():
    player = app_module.app.test_client()
    player.post("/join", data={"name": "Chazel", "join_code": auth.join_code()})
    create = player.post("/characters", data={
        "name": "Mira", "race": "human", "class": "cleric",
        "str": "10", "dex": "12", "con": "13", "int": "10", "wis": "15", "cha": "10",
    })
    char_id = int(create.headers["Location"].rsplit("=", 1)[1])

    dm_client = _dm_socket_client()
    dm_client.get_received()
    dm_client.emit("dm_start_encounter",
                   {"name": "Ambush", "monsters": [{"slug": "goblin", "count": 1}],
                    "character_ids": [char_id]})
    state = [r for r in dm_client.get_received() if r["name"] == "encounter_update"][-1]
    pc = next(c for c in state["args"][0]["encounter"]["combatants"] if c["kind"] == "character")

    dm_client.emit("dm_condition", {"apply": True, "combatant_id": pc["id"], "condition": "prone"})
    after = [r for r in dm_client.get_received() if r["name"] == "encounter_update"][-1]
    updated = next(c for c in after["args"][0]["encounter"]["combatants"] if c["id"] == pc["id"])
    assert updated["conditions"] == [{"name": "prone"}]

    dm_client.emit("dm_condition", {"apply": False, "combatant_id": pc["id"], "condition": "prone"})
    cleared = [r for r in dm_client.get_received() if r["name"] == "encounter_update"][-1]
    updated = next(c for c in cleared["args"][0]["encounter"]["combatants"] if c["id"] == pc["id"])
    assert updated["conditions"] == []


def test_a_timed_condition_counts_down_and_is_announced_when_it_ends():
    dm_client = _dm_socket_client()
    dm_client.get_received()
    dm_client.emit("dm_start_encounter",
                   {"name": "Ambush", "monsters": [{"slug": "goblin", "count": 1}],
                    "character_ids": []})
    state = [r for r in dm_client.get_received() if r["name"] == "encounter_update"][-1]
    goblin = state["args"][0]["encounter"]["combatants"][0]

    dm_client.emit("dm_condition", {"apply": True, "combatant_id": goblin["id"],
                                    "condition": "prone", "duration_rounds": 1})
    after = [r for r in dm_client.get_received() if r["name"] == "encounter_update"][-1]
    chip = after["args"][0]["encounter"]["combatants"][0]["conditions"][0]
    assert chip["remaining"] == 1

    # one combatant, so a single turn wraps the round
    dm_client.emit("dm_next_turn")
    received = dm_client.get_received()
    texts = [r["args"][0]["text"] for r in received if r["name"] == "campaign_event"]
    assert any("no longer prone" in t for t in texts)

    final = [r for r in received if r["name"] == "encounter_update"][-1]
    assert final["args"][0]["encounter"]["combatants"][0]["conditions"] == []


def test_immunity_refusal_is_announced_to_the_table():
    dm_client = _dm_socket_client()
    dm_client.get_received()
    dm_client.emit("dm_start_encounter",
                   {"name": "Deep", "monsters": [{"slug": "aboleth-nihilith", "count": 1}],
                    "character_ids": []})
    state = [r for r in dm_client.get_received() if r["name"] == "encounter_update"][-1]
    beast = state["args"][0]["encounter"]["combatants"][0]

    dm_client.emit("dm_condition",
                   {"apply": True, "combatant_id": beast["id"], "condition": "charmed"})

    texts = [r["args"][0]["text"] for r in dm_client.get_received()
             if r["name"] == "campaign_event"]
    assert any("immune to charmed" in t for t in texts)


def test_a_poisoned_character_rolls_checks_at_disadvantage():
    """The player doesn't have to remember -- the server applies it."""
    player = app_module.app.test_client()
    player.post("/join", data={"name": "Chazel", "join_code": auth.join_code()})
    create = player.post("/characters", data={
        "name": "Mira", "race": "human", "class": "cleric",
        "str": "10", "dex": "12", "con": "13", "int": "10", "wis": "15", "cha": "10",
    })
    char_id = int(create.headers["Location"].rsplit("=", 1)[1])
    with app_module.character._campaign_con() as con:
        con.execute("UPDATE characters SET conditions_json=? WHERE id=?",
                    ('[{"name": "poisoned"}]', char_id))

    client = _socket_client()
    client.get_received()
    client.emit("roll_request", {"character_id": char_id, "ability": "wis", "proficient": True})

    result = [r for r in client.get_received() if r["name"] == "roll_result"][-1]["args"][0]
    assert result["adv"] == "disadvantage"
    assert result["d20"] == min(result["d20_rolls"])


def test_monster_search_returns_srd_matches():
    client = _socket_client()
    client.get_received()
    client.emit("monster_search", {"query": "goblin"})
    results = [r for r in client.get_received() if r["name"] == "monster_results"][-1]
    assert any(m["slug"] == "goblin" for m in results["args"][0]["monsters"])


def test_set_voice_is_room_wide():
    """The speaker is the server box, so muting from one phone has to mute the
    room and update every other device's toggle."""
    try:
        one = _socket_client()
        two = _socket_client()
        one.get_received()
        two.get_received()

        one.emit("set_voice", {"enabled": False})

        assert app_module.voice.is_enabled() is False
        for client in (one, two):
            states = [r for r in client.get_received() if r["name"] == "voice_state"]
            assert states and states[-1]["args"][0]["enabled"] is False
    finally:
        app_module.voice.set_enabled(True)


# ---------------------------------------------------------------------------
# The startup warning about whatever else wants Ollama
# ---------------------------------------------------------------------------

def test_a_stale_model_in_ollama_is_called_out(monkeypatch):
    """A resident model that is not ours must be evicted before a turn can
    start, and swapping 3.5GB out for 1.4GB is minutes. Better said up front
    than discovered mid-game."""
    monkeypatch.setattr(app_module.requests, "get",
                        lambda *a, **k: type("R", (), {
                            "json": lambda self: {"models": [{"name": "qwen3:4b"}]}})())

    warnings = app_module._ollama_rivals()

    assert len(warnings) == 1
    assert "qwen3:4b" in warnings[0] and "llama3.2:1b" in warnings[0]
    assert "ollama stop qwen3:4b" in warnings[0]


def test_nothing_is_said_when_the_right_model_is_loaded(monkeypatch):
    monkeypatch.setattr(app_module.requests, "get",
                        lambda *a, **k: type("R", (), {
                            "json": lambda self: {"models": [{"name": "llama3.2:1b"}]}})())
    assert app_module._ollama_rivals() == []


def test_nothing_is_said_when_ollama_holds_nothing(monkeypatch):
    monkeypatch.setattr(app_module.requests, "get",
                        lambda *a, **k: type("R", (), {"json": lambda self: {"models": []}})())
    assert app_module._ollama_rivals() == []


def test_an_unreachable_ollama_is_not_fatal(monkeypatch):
    """Checking for a stale model must never be why the game fails to start."""
    def boom(*a, **k):
        raise OSError("no ollama")
    monkeypatch.setattr(app_module.requests, "get", boom)
    assert app_module._ollama_rivals() == []


# ---------------------------------------------------------------------------
# Portraits
# ---------------------------------------------------------------------------

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_a_player_can_give_their_character_a_face():
    client, char_id = _join_and_create()

    resp = client.post(f"/character/{char_id}/avatar",
                       data={"image": (io.BytesIO(_PNG), "me.png")},
                       content_type="multipart/form-data")

    assert resp.status_code == 302
    stored = app_module.character.get_character(char_id)["avatar"]
    assert stored
    assert client.get(f"/avatar/{stored}").status_code == 200


def test_nobody_can_put_a_face_on_someone_elses_character():
    _, char_id = _join_and_create(name="Chazel")
    other = app_module.app.test_client()
    other.post("/join", data={"name": "Someone Else", "join_code": auth.join_code()})

    resp = other.post(f"/character/{char_id}/avatar",
                      data={"image": (io.BytesIO(_PNG), "mine.png")},
                      content_type="multipart/form-data")

    assert resp.status_code == 302 and "/characters" in resp.headers["Location"]
    assert app_module.character.get_character(char_id)["avatar"] is None


def test_a_file_that_is_not_an_image_says_so_instead_of_500ing():
    client, char_id = _join_and_create()

    resp = client.post(f"/character/{char_id}/avatar",
                       data={"image": (io.BytesIO(b"not an image at all"), "x.png")},
                       content_type="multipart/form-data")

    assert resp.status_code == 400
    assert b"doesn&#39;t look like an image" in resp.data or b"look like an image" in resp.data


def test_portraits_are_behind_the_join_code(client):
    """The story is for the table, and so are their faces."""
    resp = client.get("/avatar/" + "a" * 32 + ".png")
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Reconnecting mid-session
# ---------------------------------------------------------------------------

def test_a_reconnecting_device_is_given_the_story_it_missed():
    """A phone that sleeps drops the socket, and everything narrated meanwhile
    is broadcast to a room it is no longer in. Without this it reconnects to a
    blank feed and the session looks like it never happened."""
    app_module.character.log_campaign_event("action", "Chazel", "Chazel: I listen at the door.")
    app_module.character.log_campaign_event("dm", "DM", "The silence answers.")

    client = _socket_client()
    events = {r["name"]: r["args"][0] for r in client.get_received()}

    assert "feed_history" in events
    texts = [e["text"] for e in events["feed_history"]["entries"]]
    assert "The silence answers." in texts
    assert texts.index("Chazel: I listen at the door.") < texts.index("The silence answers."), \
        "oldest first, so the feed reads in order"


def test_the_backfill_leaves_out_the_imported_cathedral_sessions():
    """Those are history, not something this table just watched happen."""
    app_module.character.log_campaign_event("archive", "Nova", "A night from a fortnight ago.")
    app_module.character.log_campaign_event("dm", "DM", "Tonight's narration.")

    client = _socket_client()
    events = {r["name"]: r["args"][0] for r in client.get_received()}

    texts = [e["text"] for e in events["feed_history"]["entries"]]
    assert "Tonight's narration." in texts
    assert "A night from a fortnight ago." not in texts


def test_a_brand_new_campaign_backfills_nothing_rather_than_erroring():
    client = _socket_client()
    events = {r["name"]: r["args"][0] for r in client.get_received()}
    assert events["feed_history"]["entries"] == []


# ---------------------------------------------------------------------------
# The table, watched
# ---------------------------------------------------------------------------

def test_anyone_with_the_join_code_can_watch_the_table():
    """Nova runs the game now, so there is no privileged human whose screen
    this is."""
    client = app_module.app.test_client()
    _join(client)

    resp = client.get("/dm")

    assert resp.status_code == 200
    assert b"The Table" in resp.data


def test_everyone_at_the_table_is_offered_the_controls():
    """The escape hatch is now open to whoever is present, not to whoever knows
    a second secret. The hatch itself stays: it is the only way to move the
    game when the model stalls, and deleting a working escape hatch because it
    is not the happy path is how an evening ends early."""
    client = app_module.app.test_client()
    _join(client)

    resp = client.get("/dm")

    assert b"Encounter Builder" in resp.data


def test_the_manual_override_is_still_there():
    """The password went; the escape hatch did not. It is the only way to move
    the game when the model stalls, and deleting a working escape hatch because
    it is not the happy path is how an evening ends early."""
    client = app_module.app.test_client()
    _join(client)
    _become_dm(client)

    resp = client.get("/dm")

    assert b"Encounter Builder" in resp.data
    assert b"manual override" in resp.data


def test_watching_the_table_still_needs_the_join_code(client):
    assert client.get("/dm").status_code == 302


def test_the_table_shows_who_is_actually_connected():
    """A character sheet exists whether or not anyone has it open, so counting
    characters showed six players in an empty room."""
    from engine import presence
    presence.clear()

    watcher = _socket_client()
    events = {r["name"]: r["args"][0] for r in watcher.get_received()}

    assert "who_is_here" in events
    assert len(events["who_is_here"]["here"]) == 1
    assert presence.count() == 1


def test_leaving_removes_you_from_the_table():
    from engine import presence
    presence.clear()

    watcher = _socket_client()
    assert presence.count() == 1
    watcher.disconnect()

    assert presence.count() == 0


def test_connect_replays_turn_state_so_a_late_device_is_not_stuck():
    """The bug this pins cost a live session.

    `dm_state` is broadcast once when a turn starts and once when it ends, and
    the client disables its submit button until it hears the end. A device that
    is disconnected at that moment -- a slept phone, or any page open across a
    server restart, which kills the `finally` that sends it -- never hears it,
    and the button stays dead forever. Connect has to replay the state rather
    than assume the client was listening.
    """
    from engine import dm

    # Nobody is acting: a device arriving now must be told the table is free,
    # which is what unsticks a client left disabled by a restart.
    idle = _socket_client()
    state = [r for r in idle.get_received() if r["name"] == "dm_state"]
    assert state, "connect must send dm_state at all"
    assert state[-1]["args"][0] == {"busy": False, "acting": None}

    # Mid-turn, the same connect must say so -- and name who, or the note reads
    # "someone is queued" to a table that can see whose turn it is.
    dm._turn_lock.acquire()
    dm._acting = "Mira"
    try:
        latecomer = _socket_client()
        mid = [r for r in latecomer.get_received() if r["name"] == "dm_state"][-1]
        assert mid["args"][0] == {"busy": True, "acting": "Mira"}
    finally:
        dm._acting = None
        dm._turn_lock.release()


def test_rejoining_returns_you_to_the_same_seat():
    """Backing out to the join screen used to cost you your party.

    `do_join` called create_player() on every submit, so each visit minted a
    fresh player row owning nothing while the characters stayed behind on the
    row before it. Fifteen rows had accumulated on the real table, twelve of
    them empty, and the player's own druid was invisible to them.
    """
    first = app_module.app.test_client()
    _join(first)
    first.post("/characters", data={"name": "Mira", "race": "elf", "class": "druid",
                                    "str": 10, "dex": 14, "con": 12,
                                    "int": 11, "wis": 15, "cha": 10},
               follow_redirects=True)

    # A second device -- or the same one after backing out and clearing cookies.
    again = app_module.app.test_client()
    page = again.post("/join", data={"name": "Chazel", "join_code": auth.join_code()},
                      follow_redirects=True)
    assert b"Mira" in page.data, "the same name must come back to the same characters"


def test_a_different_name_gets_its_own_seat():
    """Reusing a seat by name must not hand one player another's party."""
    a = app_module.app.test_client()
    _join(a)
    a.post("/characters", data={"name": "Mira", "race": "elf", "class": "druid",
                                "str": 10, "dex": 14, "con": 12,
                                "int": 11, "wis": 15, "cha": 10},
           follow_redirects=True)

    b = app_module.app.test_client()
    page = b.post("/join", data={"name": "Jorlaan", "join_code": auth.join_code()},
                  follow_redirects=True)
    assert b"Mira" not in page.data


def test_an_empty_import_says_what_to_do(client):
    """Pressing IMPORT with nothing chosen is the commonest way to press it.

    It used to fall through to the JSON parser and come back "That isn't valid
    JSON (Expecting value, line 1)", which reads like the player's file is
    broken when they have not picked one yet.
    """
    _join(client)
    page = client.post("/characters/import", data={"pasted": "   "})
    assert page.status_code == 400
    body = page.data.decode()
    assert "valid JSON" not in body
    assert "Choose a .nova-dm.json file" in body


def test_pages_are_never_cached(client):
    """A phone reported the page not reloading. The server was sending no cache
    headers at all, so the browser was free to keep serving a stale character
    list -- which looks exactly like losing your party."""
    _join(client)
    page = client.get("/characters")
    assert "no-store" in page.headers.get("Cache-Control", "")


def test_a_stale_cookie_on_an_empty_seat_does_not_outrank_the_name():
    """Observed on Daniel's phone, 2026-09-05.

    The browser still held an August cookie for one of the empty "Chazel"
    rows. `do_join` read `_current_player() or find_player_by_name(name)`, so
    the cookie short-circuited the lookup and the page said "No characters
    yet" while his elf druid sat on another seat under the same name.

    The cookie was right about who he was and wrong about which chair he had
    ended up in. find_player_by_name already prefers the seat that actually
    holds characters; it was simply never consulted once a cookie existed.
    """
    # A seat that owns a character.
    owner = app_module.app.test_client()
    _join(owner)
    owner.post("/characters", data={"name": "Mira", "race": "elf", "class": "druid",
                                    "str": 10, "dex": 14, "con": 12,
                                    "int": 11, "wis": 15, "cha": 10},
               follow_redirects=True)

    # A stale browser: same name, its own cookie, no characters. It gets one by
    # joining under a name nobody has used, then re-joining as "Chazel".
    stale = app_module.app.test_client()
    stale.post("/join", data={"name": "Ghost", "join_code": auth.join_code()},
               follow_redirects=True)
    page = stale.post("/join", data={"name": "Chazel"}, follow_redirects=True)

    assert b"No characters yet" not in page.data, (
        "an empty cookie seat still outranks the name — the player cannot "
        "reach their own characters from that browser"
    )
    assert b"Mira" in page.data


def test_a_cookie_on_a_seat_with_characters_is_never_moved():
    """The correction must not evict someone who is already correctly seated.

    Two players sharing a name is the case that would break: if the rule moved
    any cookie to the name-matched seat, the second would be handed the first's
    party. It only ever moves off a seat that holds nothing.
    """
    first = app_module.app.test_client()
    _join(first)
    first.post("/characters", data={"name": "Mira", "race": "elf", "class": "druid",
                                    "str": 10, "dex": 14, "con": 12,
                                    "int": 11, "wis": 15, "cha": 10},
               follow_redirects=True)

    page = first.post("/join", data={"name": "Chazel"}, follow_redirects=True)
    assert b"Mira" in page.data, "a correctly seated player was moved"

"""nova-dm web app: player join -> character creation -> live play. Phase 2 --
no DM/LLM pipeline yet (that's Phase 3); the action box in player.html exists
but isn't wired to anything. This phase proves the multiplayer plumbing works
via a real live-sync round trip: roll a check, every connected device sees it."""
import json
import socket as _socket

from flask import Flask, jsonify, make_response, redirect, render_template, request, url_for
from flask_socketio import SocketIO, emit, join_room

from engine import character, conditions, dice, dm, encounter, portable, voice

app = Flask(__name__)
app.secret_key = "nova-dm-lan-only"  # LAN-only, no real auth in scope -- see plan
socketio = SocketIO(app, async_mode="threading")

CAMPAIGN_ROOM = "campaign"  # one shared campaign for now -- multi-campaign is out of scope


def _current_player():
    token = request.cookies.get("session_token")
    if not token:
        return None
    return character.get_player_by_token(token)


@app.route("/")
def join():
    if _current_player():
        return redirect(url_for("characters"))
    return render_template("join.html")


@app.route("/join", methods=["POST"])
def do_join():
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("join"))
    player = character.create_player(name)
    resp = make_response(redirect(url_for("characters")))
    resp.set_cookie("session_token", player["session_token"], max_age=60 * 60 * 24 * 30)
    return resp


@app.route("/characters", methods=["GET", "POST"])
def characters():
    player = _current_player()
    if not player:
        return redirect(url_for("join"))

    if request.method == "POST":
        ability_scores = {k: int(request.form.get(k, 10))
                          for k in ["str", "dex", "con", "int", "wis", "cha"]}
        char = character.create_character(
            player_id=player["id"],
            name=request.form.get("name", "Adventurer").strip(),
            race=request.form.get("race"),
            class_=request.form.get("class"),
            ability_scores=ability_scores,
        )
        return redirect(url_for("play", character_id=char["id"]))

    return _render_characters(player)


def _render_characters(player, import_error: str = None, status: int = 200):
    chars = character.list_characters_for_player(player["id"])
    html = render_template("characters.html", player=player, characters=chars,
                           classes=character.list_srd_classes(),
                           races=character.list_srd_races(), import_error=import_error)
    return (html, status) if import_error else html


@app.route("/character/<int:character_id>/export")
def export_character(character_id):
    player = _current_player()
    if not player:
        return redirect(url_for("join"))
    char = character.get_character(character_id)
    if not char or char["player_id"] != player["id"]:
        return redirect(url_for("characters"))

    payload = portable.export_character(character_id)
    resp = make_response(json.dumps(payload, indent=2))
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Content-Disposition"] = f'attachment; filename="{portable.filename_for(char)}"'
    return resp


@app.route("/characters/import", methods=["POST"])
def import_character():
    player = _current_player()
    if not player:
        return redirect(url_for("join"))

    upload = request.files.get("file")
    blob = upload.read().decode("utf-8", "replace") if upload and upload.filename else ""
    blob = blob or request.form.get("pasted", "")

    try:
        char = portable.import_character(portable.parse(blob), player["id"])
    except portable.PortableError as e:
        # A bad paste is a normal thing for a player to do -- say what's wrong on
        # the page rather than handing them a 500.
        return _render_characters(player, import_error=str(e), status=400)
    except UnicodeDecodeError:
        return _render_characters(player, import_error="That file isn't text.", status=400)

    return redirect(url_for("play", character_id=char["id"]))


@app.route("/play")
def play():
    player = _current_player()
    if not player:
        return redirect(url_for("join"))
    character_id = request.args.get("character_id", type=int)
    char = character.get_character(character_id) if character_id else None
    if not char or char["player_id"] != player["id"]:
        return redirect(url_for("characters"))
    return render_template("player.html", player=player, character=char)


@app.route("/dm")
def dm_screen():
    # Open on the LAN, same as everything else here -- whoever is running the game
    # opens this page. No auth, deliberately: see the note on app.secret_key.
    return render_template("dm.html", encounter=encounter.get_state(),
                          characters=character.list_active_characters(),
                          conditions=encounter.list_conditions())


@app.route("/api/encounter")
def api_encounter():
    return jsonify({"encounter": encounter.get_state()})


@app.route("/api/character/<int:character_id>")
def api_character(character_id):
    char = character.get_character(character_id)
    if not char:
        return jsonify({"error": "not found"}), 404
    return jsonify(char)


@socketio.on("connect")
def on_connect():
    join_room(CAMPAIGN_ROOM)
    # A device joining mid-session needs the current state of the shared speaker
    # and whatever fight is already underway.
    emit("voice_state", {"enabled": voice.is_enabled(), "available": voice.available()})
    emit("encounter_update", {"encounter": encounter.get_state()})


@socketio.on("roll_request")
def on_roll_request(data):
    character_id = data.get("character_id")
    ability = data.get("ability", "str")
    proficient = bool(data.get("proficient", False))
    char = character.get_character(character_id)
    if not char:
        emit("roll_result", {"error": "unknown character"})
        return
    # A poisoned character rolls at disadvantage whether or not they remember to.
    adv = conditions.check_advantage(json.loads(char["conditions_json"] or "[]"))
    result = dice.roll_check(char, ability, proficient=proficient, adv=adv)
    character.log_campaign_event(
        "roll", char["name"],
        f"{char['name']} rolls {ability}: {result['d20']}+{result['modifier']}={result['total']}"
    )
    socketio.emit("roll_result", {"character": char["name"], **result}, room=CAMPAIGN_ROOM)


@socketio.on("submit_action")
def on_submit_action(data):
    character_id = data.get("character_id")
    action_text = (data.get("action_text") or "").strip()
    if not action_text:
        return
    dm.handle_player_action(character_id, action_text, socketio)


def _broadcast_encounter():
    """One full-state payload after any change, from either driver -- the human at
    /dm or the AI DM's tools. Small enough to send whole, and it keeps every device
    correct regardless of which one moved."""
    socketio.emit("encounter_update", {"encounter": encounter.get_state()}, room=CAMPAIGN_ROOM)


@socketio.on("monster_search")
def on_monster_search(data):
    monsters = encounter.list_srd_monsters(
        query=(data.get("query") or "").strip() or None,
        max_cr=data.get("max_cr"),
    )
    emit("monster_results", {"monsters": monsters[:60]})


@socketio.on("dm_start_encounter")
def on_dm_start_encounter(data):
    state = encounter.start_encounter(
        data.get("name") or "Encounter",
        data.get("monsters") or [],
        character_ids=data.get("character_ids"),
    )
    order = ", ".join(f"{c['name']} ({c['initiative']})" for c in state["combatants"])
    text = f"Encounter: {state['name']}. Initiative -- {order}"
    character.log_campaign_event("encounter", "DM", text)
    socketio.emit("campaign_event", {"text": text, "kind": "encounter"}, room=CAMPAIGN_ROOM)
    _broadcast_encounter()


@socketio.on("dm_damage")
def on_dm_damage(data):
    result = encounter.damage_combatant(data.get("combatant_id"), data.get("amount", 0))
    if "error" not in result:
        socketio.emit("campaign_event", {"text": result["text"], "kind": "hp"}, room=CAMPAIGN_ROOM)
    _broadcast_encounter()


@socketio.on("dm_heal")
def on_dm_heal(data):
    result = encounter.heal_combatant(data.get("combatant_id"), data.get("amount", 0))
    if "error" not in result:
        socketio.emit("campaign_event", {"text": result["text"], "kind": "hp"}, room=CAMPAIGN_ROOM)
    _broadcast_encounter()


@socketio.on("dm_condition")
def on_dm_condition(data):
    if data.get("apply"):
        result = encounter.apply_condition(
            data.get("combatant_id"), data.get("condition"),
            data.get("level"), data.get("duration_rounds"),
            data.get("until_turn_of"), data.get("until_boundary") or "end",
        )
    else:
        result = encounter.remove_condition(data.get("combatant_id"), data.get("condition"))
    if "error" in result:
        # Immunity refusals are worth showing the table, not swallowing.
        socketio.emit("campaign_event", {"text": result["error"], "kind": "condition"},
                      room=CAMPAIGN_ROOM)
    else:
        socketio.emit("campaign_event", {"text": result["text"], "kind": "condition"},
                      room=CAMPAIGN_ROOM)
    _broadcast_encounter()


@socketio.on("dm_next_turn")
def on_dm_next_turn():
    state = encounter.advance_turn()
    if state:
        # Anything that ran out this round is announced, so effects visibly end
        # rather than just stopping.
        for ended in state.get("expired_conditions") or []:
            socketio.emit("campaign_event", {"text": ended["text"], "kind": "condition"},
                          room=CAMPAIGN_ROOM)
        text = f"Round {state['round']} -- {state['current']['name']}'s turn."
        character.log_campaign_event("encounter", "DM", text)
        socketio.emit("campaign_event", {"text": text, "kind": "encounter"}, room=CAMPAIGN_ROOM)
    _broadcast_encounter()


@socketio.on("dm_end_encounter")
def on_dm_end_encounter():
    if encounter.end_encounter():
        socketio.emit("campaign_event", {"text": "The encounter ends.", "kind": "encounter"},
                      room=CAMPAIGN_ROOM)
    _broadcast_encounter()


@socketio.on("set_voice")
def on_set_voice(data):
    # The speaker is the server box, not the phone in your hand -- so the mute is
    # room-wide, and every device's toggle has to follow it.
    voice.set_enabled(bool(data.get("enabled")))
    socketio.emit(
        "voice_state",
        {"enabled": voice.is_enabled(), "available": voice.available()},
        room=CAMPAIGN_ROOM,
    )


def _lan_ip():
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packet actually sent, just picks the local route
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


if __name__ == "__main__":
    ip = _lan_ip()
    print("nova-dm running: http://localhost:5050" +
          (f"  (LAN: http://{ip}:5050)" if ip else ""))
    socketio.run(app, host="0.0.0.0", port=5050, allow_unsafe_werkzeug=True)

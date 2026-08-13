"""nova-dm web app: player join -> character creation -> live play. Phase 2 --
no DM/LLM pipeline yet (that's Phase 3); the action box in player.html exists
but isn't wired to anything. This phase proves the multiplayer plumbing works
via a real live-sync round trip: roll a check, every connected device sees it."""
import socket as _socket

from flask import Flask, jsonify, make_response, redirect, render_template, request, url_for
from flask_socketio import SocketIO, emit, join_room

from engine import character, dice, dm, voice

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

    chars = character.list_characters_for_player(player["id"])
    return render_template("characters.html", player=player, characters=chars,
                          classes=character.list_srd_classes(), races=character.list_srd_races())


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


@app.route("/api/character/<int:character_id>")
def api_character(character_id):
    char = character.get_character(character_id)
    if not char:
        return jsonify({"error": "not found"}), 404
    return jsonify(char)


@socketio.on("connect")
def on_connect():
    join_room(CAMPAIGN_ROOM)
    # A device joining mid-session needs the current state of the shared speaker.
    emit("voice_state", {"enabled": voice.is_enabled(), "available": voice.available()})


@socketio.on("roll_request")
def on_roll_request(data):
    character_id = data.get("character_id")
    ability = data.get("ability", "str")
    proficient = bool(data.get("proficient", False))
    char = character.get_character(character_id)
    if not char:
        emit("roll_result", {"error": "unknown character"})
        return
    result = dice.roll_check(char, ability, proficient=proficient)
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

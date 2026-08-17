"""nova-dm web app: player join -> character creation -> live play. Phase 2 --
no DM/LLM pipeline yet (that's Phase 3); the action box in player.html exists
but isn't wired to anything. This phase proves the multiplayer plumbing works
via a real live-sync round trip: roll a check, every connected device sees it."""
import functools
import json
import os
import shutil
import socket as _socket
import subprocess

import requests
from flask import (
    Flask,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_socketio import SocketIO, emit, join_room

from engine import (
    auth,
    character,
    chronicle,
    conditions,
    dice,
    dm,
    encounter,
    llm,
    portable,
    voice,
)

app = Flask(__name__)
# Persisted outside the repo and generated on first run. The old hardcoded key
# was fine while nothing but the living room could reach this; it is not fine
# now that the session cookie is the only thing saying who the DM is.
app.secret_key = auth.secret_key()
socketio = SocketIO(app, async_mode="threading")

OLLAMA_HOST = llm.OLLAMA_HOST

CAMPAIGN_ROOM = "campaign"  # one shared campaign for now -- multi-campaign is out of scope


def _announce_narration(clip):
    """Tell every connected device a clip is ready to play.

    Called from engine.voice's worker thread, not a request context, which is why
    it uses socketio.emit with an explicit room rather than flask_socketio.emit.
    """
    socketio.emit("narration", clip, room=CAMPAIGN_ROOM)


voice.set_broadcast(_announce_narration)


def _at_the_table() -> bool:
    """Has this browser given the join code? Held in the signed session, so it
    cannot be set by anyone who does not know the secret key."""
    return bool(session.get("at_the_table"))


def _is_dm() -> bool:
    return bool(session.get("is_dm"))


def _current_player():
    if not _at_the_table():
        return None
    token = request.cookies.get("session_token")
    if not token:
        return None
    return character.get_player_by_token(token)


def requires_table(view):
    """Everything about the campaign is behind the join code."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not _at_the_table():
            return redirect(url_for("join"))
        return view(*args, **kwargs)
    return wrapper


@app.route("/")
def join():
    if _current_player():
        return redirect(url_for("characters"))
    return render_template("join.html", need_code=not _at_the_table())


@app.route("/join", methods=["POST"])
def do_join():
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("join"))

    # An already-seated browser doesn't re-enter the code to make a second
    # character; a new one always does.
    if not _at_the_table():
        if not auth.check_join_code(request.form.get("join_code", "")):
            return render_template("join.html", need_code=True,
                                   error="That join code isn't right.",
                                   name=name), 403
        session["at_the_table"] = True
        session.permanent = True

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


@app.route("/dm", methods=["GET", "POST"])
def dm_screen():
    """The DM's chair is behind its own password.

    Separate from the join code on purpose: sitting at the table shouldn't hand
    someone the board. A player who knows the join code still can't start
    encounters, deal damage, or hand out levels.
    """
    if request.method == "POST" and not _is_dm():
        if not auth.check_dm_password(request.form.get("dm_password", "")):
            return render_template("dm_login.html", error="Wrong password."), 403
        session["is_dm"] = True
        session.permanent = True
        return redirect(url_for("dm_screen"))

    if not _is_dm():
        return render_template("dm_login.html")

    return render_template("dm.html", encounter=encounter.get_state(),
                          characters=character.list_active_characters(),
                          conditions=encounter.list_conditions(),
                          scene=chronicle.get_scene())


@app.route("/narration/<clip_id>.wav")
@requires_table
def narration_clip(clip_id):
    """Serve one synthesized clip to whichever devices were told about it.

    voice.clip_path refuses anything that isn't one of its own hex names, so a
    crafted id can't walk out of the clip directory. Behind the join code too --
    narration is the story, and the story is for the table.
    """
    path = voice.clip_path(clip_id)
    if path is None:
        return jsonify({"error": "not found"}), 404
    return send_file(path, mimetype="audio/wav", conditional=True)


@app.route("/api/encounter")
@requires_table
def api_encounter():
    return jsonify({"encounter": encounter.get_state()})


@app.route("/api/character/<int:character_id>")
@requires_table
def api_character(character_id):
    char = character.get_character(character_id)
    if not char:
        return jsonify({"error": "not found"}), 404
    return jsonify(char)


def dm_only(handler):
    """Guard for the events that run the game rather than play in it.

    Gating /dm alone would have been theatre: the page is just buttons, and every
    button is a socket event any connected client can emit. This is where the
    DM's authority actually lives.
    """
    @functools.wraps(handler)
    def wrapper(*args, **kwargs):
        if not _is_dm():
            emit("dm_denied", {"event": handler.__name__})
            return None
        return handler(*args, **kwargs)
    return wrapper


@socketio.on("connect")
def on_connect():
    # Refusing the connection outright, rather than letting it sit in the room
    # ignored: an unauthenticated socket should not receive campaign broadcasts.
    if not _at_the_table():
        return False

    join_room(CAMPAIGN_ROOM)
    # A device joining mid-session needs the current state of the shared speaker
    # and whatever fight is already underway.
    emit("voice_state", {"enabled": voice.is_enabled(), "available": voice.available(),
                         "sink": voice.sink()})
    emit("encounter_update", {"encounter": encounter.get_state()})
    return None


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
@dm_only
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
@dm_only
def on_dm_damage(data):
    result = encounter.damage_combatant(data.get("combatant_id"), data.get("amount", 0))
    if "error" not in result:
        socketio.emit("campaign_event", {"text": result["text"], "kind": "hp"}, room=CAMPAIGN_ROOM)
    _broadcast_encounter()


@socketio.on("dm_heal")
@dm_only
def on_dm_heal(data):
    result = encounter.heal_combatant(data.get("combatant_id"), data.get("amount", 0))
    if "error" not in result:
        socketio.emit("campaign_event", {"text": result["text"], "kind": "hp"}, room=CAMPAIGN_ROOM)
    _broadcast_encounter()


@socketio.on("dm_condition")
@dm_only
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
@dm_only
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
@dm_only
def on_dm_end_encounter():
    # Counted before the fight closes -- ending it clears the board this reads.
    award = encounter.victory_xp()
    if encounter.end_encounter():
        socketio.emit("campaign_event", {"text": "The encounter ends.", "kind": "encounter"},
                      room=CAMPAIGN_ROOM)
        if award["per_character"]:
            dm.award_xp(award["character_ids"], award["per_character"], socketio,
                        reason=dm._victory_reason(award["defeated"]))
    _broadcast_encounter()


@socketio.on("dm_award_xp")
@dm_only
def on_dm_award_xp(data):
    """The human DM's manual grant. Goes through the same path as the AI DM's
    tool, so a level-up earned this way is announced identically."""
    amount = int(data.get("amount") or 0)
    if not amount:
        return
    ids = data.get("character_ids") or [c["id"] for c in character.list_active_characters()]
    dm.award_xp(ids, amount, socketio, reason=data.get("reason"))


@socketio.on("set_voice")
def on_set_voice(data):
    # Room-wide: this silences the DM for everyone, which is a table decision.
    # Muting only your own device is separate and lives in the browser, so one
    # player stepping away doesn't take the narration from everyone else.
    voice.set_enabled(bool(data.get("enabled")))
    socketio.emit(
        "voice_state",
        {"enabled": voice.is_enabled(), "available": voice.available(), "sink": voice.sink()},
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


def _ollama_rivals():
    """Warn when Ollama is holding a model other than the DM's.

    This box runs Ollama with OLLAMA_MAX_LOADED_MODELS=1, so a resident model
    that is not ours has to be evicted and ours loaded before a turn can start
    -- and evicting a 3.5GB model to load a 1.4GB one is minutes, not seconds.

    Deliberately about *models* rather than about other applications. The first
    version of this named nova-cathedral, on the theory that another app sharing
    Ollama was the problem. That was wrong: Nova asks for the same llama3.2:1b
    the DM does, requests to Ollama run in parallel, and Nova idles at 0.2% of a
    core. Measured with it running, a turn was 83.5s; with it stopped, 93.4s.
    The slow turn that started the theory had a stale qwen3 sitting in Ollama
    from an experiment -- a resident wrong model, which is what this now checks.
    """
    try:
        response = requests.get(f"{OLLAMA_HOST}/api/ps", timeout=3)
        loaded = [m["name"] for m in response.json().get("models", [])]
    except Exception:
        return []

    wanted = llm.PROVIDERS[llm.chain()[0]].default_model if llm.chain() else None
    strangers = [m for m in loaded if m != wanted]
    if not strangers or not wanted:
        return []
    return [
        f"\n  ! Ollama is holding {', '.join(strangers)} but the DM wants {wanted}.\n"
        f"    Only one model stays resident, so the first turn will pay to swap\n"
        f"    them over. To do it now instead of mid-game:\n"
        f"        ollama stop {strangers[0]}"
    ]


def _tailscale_address():
    """This machine's address on the tailnet, if it is on one.

    How a remote player actually reaches the game: the LAN address above is
    meaningless to someone in another house, and the tailnet address works from
    anywhere without forwarding a port or putting the game on the public
    internet. Returns the MagicDNS name when there is one -- it is far easier to
    read out than 100.x.y.z -- and falls back to the raw address.
    """
    if not shutil.which("tailscale"):
        return None
    try:
        status = subprocess.run(["tailscale", "status", "--json"],
                                capture_output=True, timeout=5, text=True)
        if status.returncode == 0:
            self_node = json.loads(status.stdout).get("Self") or {}
            name = (self_node.get("DNSName") or "").rstrip(".")
            if name:
                return name
            ips = self_node.get("TailscaleIPs") or []
            if ips:
                return ips[0]
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


if __name__ == "__main__":
    # 0.0.0.0 keeps same-room play working over wifi. Set NOVA_DM_BIND to the
    # tailnet address to serve *only* the tailnet -- worth doing if this laptop
    # is ever on a network whose other occupants aren't invited.
    host = os.environ.get("NOVA_DM_BIND", "0.0.0.0")
    port = int(os.environ.get("NOVA_DM_PORT", "5050"))

    lan = _lan_ip()
    tailnet = _tailscale_address()

    print(f"nova-dm running on {host}:{port}")
    print(f"  here:    http://localhost:{port}")
    if lan:
        print(f"  wifi:    http://{lan}:{port}          (same house)")
    if tailnet:
        print(f"  tailnet: http://{tailnet}:{port}   <- send this to remote players")
    else:
        print("  tailnet: not connected -- run 'tailscale up' for remote players")

    # The codes are printed every start, not just the first: they live in a file
    # nobody is going to go looking for, and the DM needs to read the join code
    # out loud at the top of a session.
    print(f"  join code:   {auth.join_code()}      <- share this with the players")
    print(f"  DM password: {auth.dm_password()}  <- keep this")
    print(f"  (stored in {auth.SECRETS_PATH})")

    for warning in _ollama_rivals():
        print(warning)

    socketio.run(app, host=host, port=port, allow_unsafe_werkzeug=True)

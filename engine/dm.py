"""Phase 3: the DM pipeline. Claude narrates and adjudicates, but per the same
rule as dice.py -- it never invents a die roll, a check result, or an HP change
itself. Every one of those goes through a tool call into engine.dice /
engine.character, exactly like the live rolls in Phase 2, so the numbers stay real."""
import json

import anthropic

from . import character, dice, voice

MODEL = "claude-opus-5"
MAX_TOOL_ITERATIONS = 6

SYSTEM_PROMPT = """You are the Dungeon Master for a live D&D 5e (SRD) tabletop session, with this app as the shared table. Players describe what their characters do in free text; you narrate outcomes and run the game.

Rules you must follow:
- You never invent a die roll, an ability check result, a damage number, or an HP total. For every check, save, or damage roll, call the matching tool (roll_check, roll_dice, apply_damage, apply_heal) and narrate from its actual result. This app's dice are real randomness -- rolling yourself in prose would defeat the point of a physical-feeling tabletop game.
- Call roll_check when an action's outcome is uncertain and covered by an ability score (attacks, skill checks, saves). Decide ability, proficiency, and advantage/disadvantage from the fiction and the character's sheet.
- Call roll_dice for damage and other raw dice expressions (e.g. weapon damage, healing dice) once you know a hit or effect landed.
- Call apply_damage / apply_heal to make HP changes real -- don't just narrate a character surviving a hit without recording the damage.
- Keep narration tight: a paragraph or two, second person, evocative but not padded. This is live play at a table, not a novel.
- Multiple characters may be present at the table. Address the acting character's character_id for tools; you may involve other listed characters narratively.
"""

TOOLS = [
    {
        "name": "roll_check",
        "description": "Roll a d20 ability check/save for a character, applying their modifier and (if proficient) proficiency bonus. Use for any uncertain action -- attacks, skill checks, saving throws.",
        "input_schema": {
            "type": "object",
            "properties": {
                "character_id": {"type": "integer"},
                "ability": {"type": "string", "enum": ["str", "dex", "con", "int_", "wis", "cha"]},
                "proficient": {"type": "boolean", "description": "Whether the character is proficient in this check/save."},
                "advantage": {"type": "string", "enum": ["none", "advantage", "disadvantage"]},
            },
            "required": ["character_id", "ability", "proficient", "advantage"],
        },
    },
    {
        "name": "roll_dice",
        "description": "Roll a raw dice expression like '2d6+3', e.g. for weapon damage or healing. Never compute or guess this yourself.",
        "input_schema": {
            "type": "object",
            "properties": {"expr": {"type": "string", "description": "Dice expression, e.g. '1d8+2'."}},
            "required": ["expr"],
        },
    },
    {
        "name": "apply_damage",
        "description": "Apply damage to a character's HP (absorbs temp HP first). Call this whenever a character actually takes damage.",
        "input_schema": {
            "type": "object",
            "properties": {"character_id": {"type": "integer"}, "amount": {"type": "integer"}},
            "required": ["character_id", "amount"],
        },
    },
    {
        "name": "apply_heal",
        "description": "Restore HP to a character, capped at max HP. Call this whenever a character is actually healed.",
        "input_schema": {
            "type": "object",
            "properties": {"character_id": {"type": "integer"}, "amount": {"type": "integer"}},
            "required": ["character_id", "amount"],
        },
    },
]

def _clean_error_message(e: anthropic.APIError) -> str:
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return error["message"]
    return e.message


_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _build_context(characters_at_table: list) -> str:
    lines = ["Characters at the table:"]
    for c in characters_at_table:
        lines.append(
            f"- id={c['id']} {c['name']}, {c['race']} {c['class']}, level {c['level']}, "
            f"HP {c['current_hp']}/{c['max_hp']}, AC {c['ac']}, "
            f"STR {c['str']} DEX {c['dex']} CON {c['con']} INT {c['int_']} WIS {c['wis']} CHA {c['cha']}, "
            f"proficiency bonus +{c['proficiency_bonus']}"
        )
    return "\n".join(lines)


def _execute_tool(name: str, tool_input: dict, socketio) -> dict:
    if name == "roll_check":
        char = character.get_character(tool_input["character_id"])
        if not char:
            return {"error": "unknown character"}
        adv = tool_input.get("advantage", "none")
        result = dice.roll_check(
            char, tool_input["ability"],
            proficient=tool_input.get("proficient", False),
            adv=adv if adv != "none" else None,
        )
        advtag = f" ({result['adv']})" if result["adv"] else ""
        text = f"{char['name']} rolls {tool_input['ability'].upper()}{advtag}: {result['d20']}+{result['modifier']}={result['total']}"
        character.log_campaign_event("roll", char["name"], text)
        socketio.emit("campaign_event", {"text": text, "kind": "roll"}, room="campaign")
        return result

    if name == "roll_dice":
        result = dice.roll(tool_input["expr"])
        text = f"Rolling {tool_input['expr']}: {'+'.join(str(r) for r in result['rolls'])}" + (
            f"+{result['modifier']}" if result["modifier"] else ""
        ) + f" = {result['total']}"
        character.log_campaign_event("roll", "DM", text)
        socketio.emit("campaign_event", {"text": text, "kind": "roll"}, room="campaign")
        return result

    if name == "apply_damage":
        result = character.apply_damage(tool_input["character_id"], tool_input["amount"])
        char = character.get_character(tool_input["character_id"])
        text = f"{char['name']} takes {tool_input['amount']} damage -> {result['current_hp']}/{char['max_hp']} HP"
        character.log_campaign_event("damage", char["name"], text)
        socketio.emit("campaign_event", {"text": text, "kind": "hp"}, room="campaign")
        return result

    if name == "apply_heal":
        result = character.apply_heal(tool_input["character_id"], tool_input["amount"])
        char = character.get_character(tool_input["character_id"])
        text = f"{char['name']} heals {tool_input['amount']} -> {result['current_hp']}/{char['max_hp']} HP"
        character.log_campaign_event("heal", char["name"], text)
        socketio.emit("campaign_event", {"text": text, "kind": "hp"}, room="campaign")
        return result

    return {"error": f"unknown tool: {name}"}


def handle_player_action(character_id: int, action_text: str, socketio):
    actor = character.get_character(character_id)
    if not actor:
        return

    action_line = f"{actor['name']}: {action_text}"
    character.log_campaign_event("action", actor["name"], action_line)
    socketio.emit("campaign_event", {"text": action_line, "kind": "action"}, room="campaign")

    context = _build_context(character.list_active_characters())
    messages = [{"role": "user", "content": f"{context}\n\n{actor['name']} does: {action_text}"}]

    client = _get_client()
    try:
        for _ in range(MAX_TOOL_ITERATIONS):
            try:
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=1024,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=messages,
                )
            except anthropic.APIError as e:
                socketio.emit(
                    "campaign_event",
                    {"text": f"(the DM is unreachable right now: {_clean_error_message(e)})", "kind": "dm"},
                    room="campaign",
                )
                return

            narration = "\n".join(b.text for b in response.content if b.type == "text").strip()
            if narration:
                character.log_campaign_event("dm", "DM", narration)
                socketio.emit("campaign_event", {"text": narration, "kind": "dm"}, room="campaign")
                # Queued and spoken on a worker thread -- synthesis takes seconds,
                # and nothing about the turn should wait on the speaker.
                try:
                    voice.speak(narration)
                except Exception:
                    pass

            if response.stop_reason != "tool_use":
                break

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = _execute_tool(block.name, block.input, socketio)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })
            messages.append({"role": "user", "content": tool_results})
        else:
            socketio.emit(
                "campaign_event",
                {"text": "(the DM pauses to gather their thoughts -- try again)", "kind": "dm"},
                room="campaign",
            )
    finally:
        # The feed is broadcast to the whole room, so the acting player's client
        # can't tell which narration was theirs -- this says whose turn just ended.
        socketio.emit("turn_complete", {"character_id": character_id}, room="campaign")

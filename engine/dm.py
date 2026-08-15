"""Phase 3: the DM pipeline. Claude narrates and adjudicates, but per the same
rule as dice.py -- it never invents a die roll, a check result, or an HP change
itself. Every one of those goes through a tool call into engine.dice /
engine.character, exactly like the live rolls in Phase 2, so the numbers stay real."""
import json

import anthropic

from . import character, conditions, dice, encounter, voice

MODEL = "claude-opus-5"
# A combat round legitimately spends several calls (attack, damage, advance turn),
# so the Phase 3 cap of 6 would bail out mid-fight.
MAX_TOOL_ITERATIONS = 12

SYSTEM_PROMPT = """You are the Dungeon Master for a live D&D 5e (SRD) tabletop session, with this app as the shared table. Players describe what their characters do in free text; you narrate outcomes and run the game.

Rules you must follow:
- You never invent a die roll, an ability check result, a damage number, or an HP total. For every check, save, or damage roll, call the matching tool (roll_check, roll_dice, apply_damage, apply_heal) and narrate from its actual result. This app's dice are real randomness -- rolling yourself in prose would defeat the point of a physical-feeling tabletop game.
- Call roll_check when an action's outcome is uncertain and covered by an ability score (attacks, skill checks, saves). Decide ability, proficiency, and advantage/disadvantage from the fiction and the character's sheet.
- Call roll_dice for damage and other raw dice expressions (e.g. weapon damage, healing dice) once you know a hit or effect landed.
- Call apply_damage / apply_heal to make HP changes real -- don't just narrate a character surviving a hit without recording the damage.
- Keep narration tight: a paragraph or two, second person, evocative but not padded. This is live play at a table, not a novel.
- Multiple characters may be present at the table. Address the acting character's character_id for tools; you may involve other listed characters narratively.

Combat:
- When a fight starts, call start_encounter with the SRD monster slugs and counts. That rolls initiative and hit points for real; don't describe a fight as "begun" without it.
- Once an encounter is active, its initiative order and everyone's current HP are given to you below. Narrate from that board -- don't contradict it or track HP in your head.
- Monsters attack via monster_attack, which rolls to-hit against the target's real AC and applies real damage. Never decide yourself whether a monster's attack hit.
- Use advance_turn when a combatant's turn ends, and end_encounter when the fight is over.
- damage_combatant and heal_combatant work on anyone in the encounter, monster or player; apply_damage and apply_heal remain for players outside of combat.
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
    {
        "name": "start_encounter",
        "description": "Begin a combat encounter with SRD monsters. Rolls each monster's hit points and everyone's initiative for real. Use SRD slugs like 'goblin', 'orc', 'dire-wolf'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short name for the fight, e.g. 'Goblin ambush'."},
                "monsters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "slug": {"type": "string"},
                            "count": {"type": "integer"},
                        },
                        "required": ["slug", "count"],
                    },
                },
                "character_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Which characters are in this fight. Use the ids of the characters listed at the table; omit only if everyone listed is involved.",
                },
            },
            "required": ["name", "monsters"],
        },
    },
    {
        "name": "monster_attack",
        "description": "Roll a monster's attack against another combatant. Rolls to-hit against the target's real AC and applies real damage on a hit. Never decide a monster's hit or damage yourself.",
        "input_schema": {
            "type": "object",
            "properties": {
                "combatant_id": {"type": "integer", "description": "The attacking monster's combatant id."},
                "action_name": {"type": "string", "description": "The action's name, e.g. 'Scimitar'."},
                "target_id": {"type": "integer", "description": "The target's combatant id."},
            },
            "required": ["combatant_id", "action_name", "target_id"],
        },
    },
    {
        "name": "damage_combatant",
        "description": "Apply damage to anyone in the encounter, monster or player, by combatant id.",
        "input_schema": {
            "type": "object",
            "properties": {"combatant_id": {"type": "integer"}, "amount": {"type": "integer"}},
            "required": ["combatant_id", "amount"],
        },
    },
    {
        "name": "heal_combatant",
        "description": "Heal anyone in the encounter, monster or player, by combatant id.",
        "input_schema": {
            "type": "object",
            "properties": {"combatant_id": {"type": "integer"}, "amount": {"type": "integer"}},
            "required": ["combatant_id", "amount"],
        },
    },
    {
        "name": "apply_condition",
        "description": "Put an SRD condition on a combatant (poisoned, prone, restrained, stunned, frightened, blinded, charmed, grappled, incapacitated, invisible, paralyzed, petrified, deafened, unconscious, exhaustion). The engine applies the resulting advantage or disadvantage to later rolls by itself.",
        "input_schema": {
            "type": "object",
            "properties": {
                "combatant_id": {"type": "integer"},
                "condition": {"type": "string"},
                "level": {"type": "integer", "description": "Exhaustion tier 1-6; omit for other conditions."},
                "duration_rounds": {
                    "type": "integer",
                    "description": "How many rounds it lasts, counting the current one. The engine clears it automatically when it runs out. Omit for something that lasts until it is removed.",
                },
                "until_turn_of": {
                    "type": "integer",
                    "description": "Combatant id whose next turn ends this condition -- use for 'until the end of your next turn' and similar. Takes precedence over duration_rounds.",
                },
                "until_boundary": {
                    "type": "string",
                    "enum": ["start", "end"],
                    "description": "Whether it ends at the start or the end of that turn. Defaults to end.",
                },
            },
            "required": ["combatant_id", "condition"],
        },
    },
    {
        "name": "remove_condition",
        "description": "Clear a condition from a combatant when it ends.",
        "input_schema": {
            "type": "object",
            "properties": {
                "combatant_id": {"type": "integer"},
                "condition": {"type": "string"},
            },
            "required": ["combatant_id", "condition"],
        },
    },
    {
        "name": "advance_turn",
        "description": "End the current combatant's turn and move to the next in initiative order.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "end_encounter",
        "description": "End the active encounter when the fight is over.",
        "input_schema": {"type": "object", "properties": {}},
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

    state = encounter.get_state()
    if state:
        lines.append(
            f"\nActive encounter: {state['name']} -- round {state['round']}. Initiative order "
            f"(current turn marked >>):"
        )
        for i, c in enumerate(state["combatants"]):
            marker = ">>" if i == state["turn_index"] else "  "
            down = " (down)" if c["is_down"] else ""
            active = conditions.describe(c.get("conditions"))
            tag = f", conditions: {active}" if active else ""
            if not c.get("can_act", True):
                tag += " -- cannot act"
            lines.append(
                f"{marker} combatant_id={c['id']} {c['name']} [{c['kind']}] init {c['initiative']}, "
                f"HP {c['current_hp']}/{c['max_hp']}, AC {c['ac']}{down}{tag}"
            )
        lines.append(
            "Advantage and disadvantage from these conditions are applied by the engine "
            "automatically -- narrate the effect, but do not adjust the roll yourself."
        )
    else:
        lines.append("\nNo encounter is active.")
    return "\n".join(lines)


def _emit(socketio, text: str, kind: str):
    character.log_campaign_event(kind, "DM", text)
    socketio.emit("campaign_event", {"text": text, "kind": kind}, room="campaign")


def _broadcast_encounter(socketio):
    """Every client renders the whole board from one payload, so any change the DM
    makes -- and any the human DM screen makes -- lands the same way."""
    socketio.emit("encounter_update", {"encounter": encounter.get_state()}, room="campaign")


def _execute_tool(name: str, tool_input: dict, socketio) -> dict:
    if name == "roll_check":
        char = character.get_character(tool_input["character_id"])
        if not char:
            return {"error": "unknown character"}
        adv = tool_input.get("advantage", "none")
        # Whatever the DM asked for is combined with what the character's own
        # conditions impose -- one cancels the other per the SRD.
        adv = conditions.combine(
            adv if adv != "none" else None,
            conditions.check_advantage(json.loads(char["conditions_json"] or "[]")),
        )
        result = dice.roll_check(
            char, tool_input["ability"],
            proficient=tool_input.get("proficient", False),
            adv=adv,
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

    if name == "start_encounter":
        state = encounter.start_encounter(
            tool_input.get("name"),
            tool_input.get("monsters") or [],
            character_ids=tool_input.get("character_ids"),
        )
        if not state:
            return {"error": "could not start encounter"}
        order = ", ".join(f"{c['name']} ({c['initiative']})" for c in state["combatants"])
        _emit(socketio, f"Encounter: {state['name']}. Initiative -- {order}", "encounter")
        _broadcast_encounter(socketio)
        return state

    if name == "monster_attack":
        result = encounter.monster_attack(
            tool_input["combatant_id"], tool_input.get("action_name"), tool_input["target_id"]
        )
        if "error" in result:
            return result
        _emit(socketio, result["text"], "attack")
        _broadcast_encounter(socketio)
        return result

    if name in ("damage_combatant", "heal_combatant"):
        fn = encounter.damage_combatant if name == "damage_combatant" else encounter.heal_combatant
        result = fn(tool_input["combatant_id"], tool_input["amount"])
        if "error" in result:
            return result
        _emit(socketio, result["text"], "hp")
        _broadcast_encounter(socketio)
        return result

    if name in ("apply_condition", "remove_condition"):
        if name == "apply_condition":
            result = encounter.apply_condition(
                tool_input["combatant_id"], tool_input.get("condition"),
                tool_input.get("level"), tool_input.get("duration_rounds"),
                tool_input.get("until_turn_of"), tool_input.get("until_boundary") or "end",
            )
        else:
            result = encounter.remove_condition(
                tool_input["combatant_id"], tool_input.get("condition")
            )
        if "error" in result:
            return result
        _emit(socketio, result["text"], "condition")
        _broadcast_encounter(socketio)
        return result

    if name == "advance_turn":
        state = encounter.advance_turn()
        if not state:
            return {"error": "no active encounter"}
        current = state["current"]
        for ended in state.get("expired_conditions") or []:
            _emit(socketio, ended["text"], "condition")
        _emit(socketio, f"Round {state['round']} -- {current['name']}'s turn.", "encounter")
        _broadcast_encounter(socketio)
        return state

    if name == "end_encounter":
        ended = encounter.end_encounter()
        if ended:
            _emit(socketio, "The encounter ends.", "encounter")
            _broadcast_encounter(socketio)
        return {"ended": ended}

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

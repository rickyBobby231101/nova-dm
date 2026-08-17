"""Phase 3: the DM pipeline. The model narrates and adjudicates, but per the same
rule as dice.py -- it never invents a die roll, a check result, or an HP change
itself. Every one of those goes through a tool call into engine.dice /
engine.character, exactly like the live rolls in Phase 2, so the numbers stay real.

Phase 8 moved the choice of model behind engine.llm, so this file no longer knows
or cares which voice is in the DM's chair. What it still owns is the part that
matters: the tools, the board context, and the engine that actually rolls."""
import json
import logging

from . import character, chronicle, conditions, dice, encounter, llm, voice

log = logging.getLogger(__name__)

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

Memory:
- What you are told about the story so far, where the party is, and what just happened is your own record from earlier turns. Treat it as true and stay consistent with it -- do not re-describe a place differently or forget what the party already did.
- Call set_scene when the party moves somewhere new or something happens that later turns must know about. Nothing else carries the story forward: recent events scroll out of view, and what you did not write down is gone.
- Keep those summary lines short and factual. They are notes to yourself, not narration, and the players never see them.

Advancement:
- Ending a fight pays out the XP for every monster the party actually put down, split among them. That is automatic -- never call award_xp for a fight, and never announce an XP total yourself.
- Use award_xp for what combat does not cover: a quest completed, a rescue, a problem solved by talking. Say what it was for and let the engine report the number.
- Leveling is the engine's job too. If it announces a level-up, weave it into the story; never tell a player they have levelled unless the engine says so.
"""

# Tools that need a fight already on the board: every one of them takes a
# combatant id, which only exists inside an encounter.
ENCOUNTER_ONLY = {
    "end_encounter",
    "advance_turn",
    "monster_attack",
    "damage_combatant",
    "heal_combatant",
}


def tools_for(in_combat: bool):
    """Only the tools this situation can actually use.

    The whole list is re-sent on every iteration of the tool loop, and on this
    machine reading the prompt is what a turn actually costs -- measured at 12.4
    tokens/sec, so carrying the encounter tools through a conversation nobody is
    fighting in spends around thirty seconds per call to offer the model six
    things it cannot legally call. start_encounter deliberately stays: that is
    how a fight begins.
    """
    if in_combat:
        return TOOLS
    return [t for t in TOOLS if t["name"] not in ENCOUNTER_ONLY]


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
        "name": "set_scene",
        "description": "Record where the party is and, optionally, one line about what just changed in the story. Call this whenever the party moves somewhere new or something happens that later turns need to know about. This is your own memory: it is read back to you at the start of every turn, and it is the only thing that survives once recent events scroll out of view.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "Where the party is, e.g. 'Goblin warren, east tunnel'."},
                "summary": {"type": "string", "description": "One line for the record, e.g. 'The party freed the captive miners and made an enemy of the Iron Ring.'"},
            },
            "required": ["location"],
        },
    },
    {
        "name": "award_xp",
        "description": "Award experience points for a quest finished, a problem solved without a fight, or a milestone reached. Combat XP is paid out automatically when an encounter ends, so never use this for defeating monsters. Omit character_ids to award the whole party.",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "integer", "description": "XP each named character receives."},
                "character_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Who earned it. Omit for the whole party.",
                },
                "reason": {"type": "string", "description": "Short phrase, e.g. 'freeing the miners'."},
            },
            "required": ["amount"],
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

def _victory_reason(defeated: list) -> str:
    names = [d["name"] for d in defeated]
    if len(names) > 3:
        return f"defeating {len(names)} foes"
    return "defeating " + ", ".join(names) if names else "victory"


def award_xp(character_ids: list, amount: int, socketio, reason: str = None) -> list:
    """Award XP and tell the table, including any level-up it set off.

    Shared by all three things that can grant it -- the AI DM's tool, the human
    DM's screen, and the end of a won fight -- so a level-up reads the same
    however it was earned, and no caller can award XP without announcing it.

    The sheet_update that follows each award is what makes leveling visible
    without a refresh: level, HP and proficiency all move at once, and the
    player is looking at their own sheet when it happens.
    """
    results = []
    for character_id in character_ids:
        char = character.get_character(character_id)
        if not char:
            continue

        result = character.award_xp(character_id, amount)
        text = f"{char['name']} gains {amount} XP ({result['xp']} total)"
        if reason:
            text += f" -- {reason}"
        character.log_campaign_event("xp", char["name"], text)
        _emit(socketio, text, "xp")

        for level_up in result["level_ups"]:
            announcement = (
                f"{char['name']} reaches level {level_up['level']}! "
                f"+{level_up['hp_gain']} HP, proficiency +{level_up['proficiency_bonus']}"
            )
            character.log_campaign_event("level_up", char["name"], announcement)
            _emit(socketio, announcement, "level")
            socketio.emit("level_up", {"character_id": character_id, **level_up},
                          room="campaign")

        socketio.emit("sheet_update", {"character": character.get_character(character_id)},
                      room="campaign")
        results.append(result)
    return results


def _build_context(characters_at_table: list) -> str:
    # Memory first: the DM should read what has been happening before it reads
    # the current numbers, the same way a person picks a game back up.
    memory = chronicle.context_block()
    lines = [memory, "\nCharacters at the table:"] if memory else ["Characters at the table:"]
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

    if name == "set_scene":
        result = chronicle.set_scene(tool_input.get("location"), tool_input.get("summary"))
        # Not announced to the table: this is the DM's notebook, not narration.
        # The players hear about the new place from the prose, not from a log line.
        socketio.emit("scene_update", {"scene": result["scene"]}, room="campaign")
        return result

    if name == "award_xp":
        # No character_ids means the whole party -- the common case by far, and
        # asking a small local model to list every id correctly is a worse bet
        # than defaulting.
        ids = tool_input.get("character_ids") or [
            c["id"] for c in character.list_active_characters()
        ]
        results = award_xp(ids, int(tool_input["amount"]), socketio,
                           reason=tool_input.get("reason"))
        return {"awarded": [{"character_id": r["character_id"], "xp": r["xp"],
                             "level": r["new_level"]} for r in results]}

    if name == "end_encounter":
        # Counted before the fight is closed out -- ending it clears the board
        # this reads.
        award = encounter.victory_xp()
        ended = encounter.end_encounter()
        xp_each = 0
        if ended:
            _emit(socketio, "The encounter ends.", "encounter")
            _broadcast_encounter(socketio)
            if award["per_character"]:
                xp_each = award["per_character"]
                award_xp(award["character_ids"], xp_each, socketio,
                         reason=_victory_reason(award["defeated"]))
        return {"ended": ended, "xp_each": xp_each}

    return {"error": f"unknown tool: {name}"}


def handle_player_action(character_id: int, action_text: str, socketio):
    actor = character.get_character(character_id)
    if not actor:
        return

    action_line = f"{actor['name']}: {action_text}"
    character.log_campaign_event("action", actor["name"], action_line)
    socketio.emit("campaign_event", {"text": action_line, "kind": "action"}, room="campaign")

    context = _build_context(character.list_active_characters())
    prompt = f"{context}\n\n{actor['name']} does: {action_text}"

    # Which voice answered is part of the table's state, not a debug detail --
    # a Gemma turn reads differently from a Claude one and the DM screen says so.
    speaking = {"provider": None}

    def announce(name: str):
        speaking["provider"] = name
        socketio.emit("dm_provider", {"provider": name}, room="campaign")

    def narrate(text: str):
        character.log_campaign_event("dm", "DM", text)
        socketio.emit(
            "campaign_event",
            {"text": text, "kind": "dm", "provider": speaking["provider"]},
            room="campaign",
        )
        # Queued and spoken on a worker thread -- synthesis takes seconds,
        # and nothing about the turn should wait on the speaker. A mute DM must
        # never cost the table its narration, so this still swallows -- but it
        # says so, because the symptom of a swallowed failure here is silence,
        # which looks exactly like a feature that was never wired up.
        try:
            voice.speak(text)
        except Exception:
            log.exception("could not queue narration for the speaker")

    # A fight on the board decides which tools are worth paying to send.
    # get_state already filters to status='active', so a board at all means combat.
    in_combat = encounter.get_state() is not None

    try:
        outcome = llm.run_turn(
            SYSTEM_PROMPT,
            prompt,
            tools_for(in_combat),
            lambda name, tool_input: _execute_tool(name, tool_input, socketio),
            narrate,
            on_provider=announce,
            # Who acted is not in doubt -- we were handed the character before
            # the model was asked anything. Supplying it spares a small model
            # the clerical work of echoing an id back, which it gets wrong often
            # enough to lose the roll entirely.
            defaults={"character_id": actor["id"]},
        )
        if outcome.error:
            # Shown at the table but deliberately not logged as DM narration and
            # not spoken -- the speaker is for the story, not for plumbing.
            socketio.emit(
                "campaign_event",
                {"text": f"({outcome.error})", "kind": "dm"},
                room="campaign",
            )
    finally:
        # The feed is broadcast to the whole room, so the acting player's client
        # can't tell which narration was theirs -- this says whose turn just ended.
        socketio.emit("turn_complete", {"character_id": character_id}, room="campaign")

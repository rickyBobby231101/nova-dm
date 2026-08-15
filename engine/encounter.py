"""Phase 5: combat. Encounters, initiative, monsters and their HP.

Same rule as everywhere else in this engine (see dice.py, dm.py): nothing here
invents a number. Monster stats come out of the SRD, every roll -- initiative,
hit points, to-hit, damage -- goes through engine.dice.

The source-of-truth rule that keeps this consistent: a player character's HP
lives in `characters` and nowhere else. Combatant rows for PCs leave max_hp and
current_hp NULL and read through to the character, and damage to a PC routes to
engine.character.apply_damage. Anything else gives a PC two HP values that drift
apart the first time one of them is touched.
"""
import json
from datetime import datetime

from . import character, dice, rules
from . import conditions as cond

# Open5e's title for the WotC SRD statblocks, as stored in monsters.source.
CORE_SOURCE = "5e Core Rules"


def _roll_expr(expr) -> dict | None:
    """Roll a normalized expression. Some creatures deal fixed damage with no dice
    ("Hit: 1 piercing damage"), which is a constant rather than a roll."""
    if not expr:
        return None
    expr = str(expr).strip()
    if expr.isdigit():
        return {"expr": expr, "rolls": [], "modifier": int(expr), "total": int(expr)}
    try:
        return dice.roll(expr)
    except ValueError:
        return None


def _monster_ac(data: dict) -> int:
    ac = data.get("ac", data.get("armor_class"))
    return int(ac) if isinstance(ac, int) else 10


def usable_actions(data: dict) -> list:
    """Attacks the engine can actually roll. ingest_srd.normalize_attacks already
    reduced every book's format to one shape, so nothing here has to know whether
    a creature came from the SRD, Tome of Beasts or Black Flag. A creature with an
    empty list genuinely has no rollable attack (77 of 3207) and says so rather
    than having one invented for it."""
    return list(data.get("attacks") or [])


def list_srd_monsters(query: str = None, max_cr: float = None) -> list:
    sql = 'SELECT "index" AS slug, name, cr, xp, source FROM monsters WHERE 1=1'
    params = []
    if query:
        sql += " AND name LIKE ?"
        params.append(f"%{query}%")
    if max_cr is not None:
        sql += " AND cr <= ?"
        params.append(max_cr)
    # The core statblocks first: the same creature ships in up to four books, and
    # a DM searching "badger" almost always wants the familiar one. Matched by
    # exact title because "Black Flag SRD" also contains the word SRD.
    sql += f" ORDER BY (source = '{CORE_SOURCE}') DESC, name, cr"
    with character._srd_con() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def list_conditions() -> list:
    with character._srd_con() as con:
        rows = con.execute(
            'SELECT "index" AS slug, name, description FROM conditions ORDER BY name'
        ).fetchall()
    return [dict(r) for r in rows]


def get_srd_monster(slug: str) -> dict | None:
    with character._srd_con() as con:
        row = con.execute('SELECT raw_json FROM monsters WHERE "index"=?', (slug,)).fetchone()
    return json.loads(row["raw_json"]) if row else None


def _roll_initiative(dex: int) -> int:
    return dice.roll("1d20")["total"] + rules.ability_mod(dex)


def start_encounter(name: str, specs: list, character_ids: list = None) -> dict:
    """specs: [{"slug": "goblin", "count": 3}]. Ends any encounter still running --
    one fight at a time, matching the one-shared-campaign scope.

    character_ids picks who is actually at the table. It matters: the campaign DB
    keeps every character anyone has ever rolled, so defaulting to all of them drags
    abandoned and duplicate characters into every fight. The DM screen sends an
    explicit list; None keeps the old enroll-everyone behaviour.
    """
    end_encounter()

    with character._campaign_con() as con:
        cur = con.execute(
            "INSERT INTO encounters (name, status, round, turn_index, created_at) VALUES (?,?,?,?,?)",
            (name or "Encounter", "active", 1, 0, datetime.now().isoformat(timespec="seconds")),
        )
        encounter_id = cur.lastrowid

        for spec in specs or []:
            data = get_srd_monster(spec.get("slug"))
            if not data:
                continue
            count = max(1, int(spec.get("count", 1)))
            for n in range(count):
                # hp_expr is Open5e's hit_dice, which already carries the CON bonus
                # ("10d12+50"). Black Flag creatures publish no dice at all, so they
                # fall back to their flat printed hit points.
                rolled = _roll_expr(data.get("hp_expr"))
                hp = rolled["total"] if rolled else int(
                    data.get("fallback_hp") or data.get("hit_points") or 1
                )
                hp = max(1, hp)
                label = data["name"] if count == 1 else f"{data['name']} {n + 1}"
                con.execute(
                    "INSERT INTO combatants (encounter_id, kind, monster_slug, name, max_hp,"
                    " current_hp, ac, initiative, dex) VALUES (?,?,?,?,?,?,?,?,?)",
                    (encounter_id, "monster", spec["slug"], label, hp, hp,
                     _monster_ac(data), _roll_initiative(data.get("dexterity", 10)),
                     data.get("dexterity", 10)),
                )

        party = character.list_active_characters()
        if character_ids is not None:
            wanted = {int(i) for i in character_ids}
            party = [c for c in party if c["id"] in wanted]
        for char in party:
            con.execute(
                "INSERT INTO combatants (encounter_id, kind, character_id, name, ac, initiative, dex)"
                " VALUES (?,?,?,?,?,?,?)",
                (encounter_id, "character", char["id"], char["name"], char["ac"],
                 _roll_initiative(char["dex"]), char["dex"]),
            )

    character.log_campaign_event("encounter", "DM", f"Encounter begins: {name}")
    return get_state()


def _order(rows: list) -> list:
    """Initiative descending, DEX as the tiebreak, id last so the order is stable."""
    return sorted(rows, key=lambda r: (-r["initiative"], -r["dex"], r["id"]))


def get_state() -> dict | None:
    """The whole board, as every client renders it. PC hit points are read through
    to `characters` rather than stored here."""
    with character._campaign_con() as con:
        enc = con.execute(
            "SELECT * FROM encounters WHERE status='active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not enc:
            return None
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM combatants WHERE encounter_id=?", (enc["id"],)
        ).fetchall()]

    combatants = []
    for row in _order(rows):
        entry = {
            "id": row["id"], "kind": row["kind"], "name": row["name"],
            "ac": row["ac"], "initiative": row["initiative"],
            "monster_slug": row["monster_slug"], "character_id": row["character_id"],
        }
        if row["kind"] == "character":
            char = character.get_character(row["character_id"])
            if not char:
                continue
            entry["current_hp"], entry["max_hp"] = char["current_hp"], char["max_hp"]
            entry["conditions"] = cond.normalize(json.loads(char["conditions_json"] or "[]"))
        else:
            entry["current_hp"], entry["max_hp"] = row["current_hp"], row["max_hp"]
            entry["conditions"] = cond.normalize(json.loads(row["conditions_json"] or "[]"))
        entry["is_down"] = entry["current_hp"] <= 0
        entry["can_act"] = cond.can_act(entry["conditions"])
        combatants.append(entry)

    turn_index = enc["turn_index"] % len(combatants) if combatants else 0
    return {
        "id": enc["id"], "name": enc["name"], "round": enc["round"],
        "turn_index": turn_index, "combatants": combatants,
        "current": combatants[turn_index] if combatants else None,
    }


def get_combatant(combatant_id: int) -> dict | None:
    state = get_state()
    if not state:
        return None
    return next((c for c in state["combatants"] if c["id"] == combatant_id), None)


def damage_combatant(combatant_id: int, amount: int) -> dict:
    target = get_combatant(combatant_id)
    if not target:
        return {"error": "unknown combatant"}
    amount = max(0, int(amount))

    if target["kind"] == "character":
        result = character.apply_damage(target["character_id"], amount)
        current = result["current_hp"]
    else:
        current = max(0, target["current_hp"] - amount)
        with character._campaign_con() as con:
            con.execute(
                "UPDATE combatants SET current_hp=?, is_down=? WHERE id=?",
                (current, 1 if current <= 0 else 0, combatant_id),
            )

    text = f"{target['name']} takes {amount} damage -> {current}/{target['max_hp']} HP"
    if current <= 0:
        text += " (down)"
    character.log_campaign_event("damage", target["name"], text)
    return {"combatant_id": combatant_id, "name": target["name"], "amount": amount,
            "current_hp": current, "max_hp": target["max_hp"], "is_down": current <= 0,
            "text": text}


def heal_combatant(combatant_id: int, amount: int) -> dict:
    target = get_combatant(combatant_id)
    if not target:
        return {"error": "unknown combatant"}
    amount = max(0, int(amount))

    if target["kind"] == "character":
        result = character.apply_heal(target["character_id"], amount)
        current = result["current_hp"]
    else:
        current = min(target["max_hp"], target["current_hp"] + amount)
        with character._campaign_con() as con:
            con.execute(
                "UPDATE combatants SET current_hp=?, is_down=0 WHERE id=?", (current, combatant_id)
            )

    text = f"{target['name']} heals {amount} -> {current}/{target['max_hp']} HP"
    character.log_campaign_event("heal", target["name"], text)
    return {"combatant_id": combatant_id, "name": target["name"], "amount": amount,
            "current_hp": current, "max_hp": target["max_hp"], "text": text}


def _immunities(slug: str) -> set:
    """Open5e stores these as a comma-separated string ('charmed, frightened').
    1620 of the 3207 creatures have them, so they're worth honouring."""
    data = get_srd_monster(slug) or {}
    raw = data.get("condition_immunities") or ""
    if isinstance(raw, list):
        parts = [str(x) for x in raw]
    else:
        parts = str(raw).split(",")
    return {p.strip().lower() for p in parts if p.strip()}


def _write_conditions(target: dict, value: list):
    payload = json.dumps(value)
    with character._campaign_con() as con:
        if target["kind"] == "character":
            con.execute("UPDATE characters SET conditions_json=? WHERE id=?",
                        (payload, target["character_id"]))
        else:
            con.execute("UPDATE combatants SET conditions_json=? WHERE id=?",
                        (payload, target["id"]))


def apply_condition(combatant_id: int, name: str, level=None) -> dict:
    target = get_combatant(combatant_id)
    if not target:
        return {"error": "unknown combatant"}
    name = str(name or "").strip().lower()
    if not name:
        return {"error": "no condition given"}

    if target["kind"] == "monster" and name in _immunities(target["monster_slug"]):
        # Recording a condition the creature is immune to would quietly change the
        # dice for the rest of the fight, so refuse rather than store it.
        return {"error": f"{target['name']} is immune to {name}"}

    updated = cond.add(target["conditions"], name, level)
    _write_conditions(target, updated)

    label = cond.describe([c for c in updated if c["name"] == name])
    text = f"{target['name']} is {label}"
    character.log_campaign_event("condition", target["name"], text)
    return {"combatant_id": combatant_id, "name": target["name"], "condition": name,
            "conditions": updated, "text": text}


def remove_condition(combatant_id: int, name: str) -> dict:
    target = get_combatant(combatant_id)
    if not target:
        return {"error": "unknown combatant"}
    name = str(name or "").strip().lower()

    updated = cond.remove(target["conditions"], name)
    _write_conditions(target, updated)

    text = f"{target['name']} is no longer {name}"
    character.log_campaign_event("condition", target["name"], text)
    return {"combatant_id": combatant_id, "name": target["name"], "condition": name,
            "conditions": updated, "text": text}


def monster_attack(combatant_id: int, action_name: str, target_id: int) -> dict:
    """Roll a monster's attack against another combatant. The to-hit bonus and
    damage dice come from the SRD; the d20 and the damage roll come from
    engine.dice."""
    attacker = get_combatant(combatant_id)
    target = get_combatant(target_id)
    if not attacker or attacker["kind"] != "monster":
        return {"error": "unknown monster combatant"}
    if not target:
        return {"error": "unknown target"}

    data = get_srd_monster(attacker["monster_slug"])
    actions = usable_actions(data or {})
    if not actions:
        return {"error": f"{attacker['name']} has no attack action in the SRD"}

    action = next((a for a in actions if a["name"].lower() == (action_name or "").lower()), actions[0])

    # Conditions on either side change the roll -- the engine applies this, so
    # nobody has to remember that prone grants advantage mid-fight.
    adv = cond.attack_advantage(attacker["conditions"], target["conditions"])
    rolled = dice.roll_d20(adv)
    d20 = rolled["d20"]
    total = d20 + action["attack_bonus"]
    hit = d20 == 20 or (d20 != 1 and total >= target["ac"])
    adv_tag = f" ({adv})" if adv else ""

    result = {
        "attacker": attacker["name"], "action": action["name"], "target": target["name"],
        "d20": d20, "d20_rolls": rolled["d20_rolls"], "adv": adv,
        "attack_bonus": action["attack_bonus"], "attack_total": total,
        "target_ac": target["ac"], "hit": hit, "critical": d20 == 20, "damage": 0,
    }

    if not hit:
        result["text"] = (f"{attacker['name']} attacks {target['name']} with {action['name']}"
                          f"{adv_tag}: {d20}+{action['attack_bonus']}={total} "
                          f"vs AC {target['ac']} -- miss")
        character.log_campaign_event("attack", attacker["name"], result["text"])
        return result

    rolled = _roll_expr(action["damage_dice"])
    damage = max(0, rolled["total"]) if rolled else 0
    result["damage"] = damage
    result["text"] = (f"{attacker['name']} hits {target['name']} with {action['name']}{adv_tag}: "
                      f"{d20}+{action['attack_bonus']}={total} vs AC {target['ac']} for {damage}")
    character.log_campaign_event("attack", attacker["name"], result["text"])

    applied = damage_combatant(target_id, damage)
    result["target_current_hp"] = applied.get("current_hp")
    result["target_is_down"] = applied.get("is_down")
    return result


def advance_turn() -> dict | None:
    state = get_state()
    if not state or not state["combatants"]:
        return None
    next_index = state["turn_index"] + 1
    round_ = state["round"] + 1 if next_index >= len(state["combatants"]) else state["round"]
    with character._campaign_con() as con:
        con.execute(
            "UPDATE encounters SET turn_index=?, round=? WHERE id=?",
            (next_index % len(state["combatants"]), round_, state["id"]),
        )
    return get_state()


def end_encounter() -> bool:
    with character._campaign_con() as con:
        cur = con.execute("UPDATE encounters SET status='ended' WHERE status='active'")
        ended = cur.rowcount > 0
    if ended:
        character.log_campaign_event("encounter", "DM", "The encounter ends.")
    return ended

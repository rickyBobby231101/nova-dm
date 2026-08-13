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


# SRD damage entries come in three shapes, found by checking all 334 monsters:
# a normal dice string ("1d6+2"), a flat number ("1" -- a badger's bite), and an
# entry with no damage_dice at all, which is a "choose one" set of options. Those
# are not decoration: a guard's spear encodes its ONLY damage that way (1d6+1 one
# handed / 1d8+1 two handed), so dropping them leaves ten SRD monsters -- guard,
# druid, merfolk, werewolf -- unable to attack. Take the first option, which is
# both the one-handed damage for versatile weapons and the bonus damage rider on
# a djinni's scimitar.
def _damage_dice_of(entry: dict) -> str | None:
    if entry.get("damage_dice"):
        return entry["damage_dice"]
    options = (entry.get("from") or {}).get("options") or []
    for option in options:
        if option.get("damage_dice"):
            return option["damage_dice"]
    return None


def _damage_amount(damage_dice) -> dict | None:
    if not damage_dice:
        return None
    expr = str(damage_dice).strip()
    if expr.isdigit():
        return {"expr": expr, "rolls": [], "modifier": int(expr), "total": int(expr)}
    try:
        return dice.roll(expr)
    except ValueError:
        return None


def _monster_ac(data: dict) -> int:
    ac = data.get("armor_class")
    if isinstance(ac, list) and ac:
        return int(ac[0].get("value", 10))
    if isinstance(ac, int):
        return ac
    return 10


def usable_actions(data: dict) -> list:
    """Attack actions with both a to-hit bonus and real damage. Multiattack and
    other narrative-only entries are excluded -- 5 SRD monsters have nothing left
    after this, and that's a legitimate 'it has no attack' answer."""
    out = []
    for action in data.get("actions") or []:
        if action.get("attack_bonus") is None:
            continue
        if not any(_damage_dice_of(d) for d in action.get("damage") or []):
            continue
        out.append(action)
    return out


def list_srd_monsters(query: str = None, max_cr: float = None) -> list:
    sql = 'SELECT "index" AS slug, name, cr, xp FROM monsters WHERE 1=1'
    params = []
    if query:
        sql += " AND name LIKE ?"
        params.append(f"%{query}%")
    if max_cr is not None:
        sql += " AND cr <= ?"
        params.append(max_cr)
    sql += " ORDER BY cr, name"
    with character._srd_con() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


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
                # hit_points_roll carries the CON bonus (orc: 2d8+6); hit_dice does not.
                hp_expr = data.get("hit_points_roll") or data.get("hit_dice")
                rolled = _damage_amount(hp_expr)
                hp = rolled["total"] if rolled else int(data.get("hit_points", 1))
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
        else:
            entry["current_hp"], entry["max_hp"] = row["current_hp"], row["max_hp"]
        entry["is_down"] = entry["current_hp"] <= 0
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
    attack_roll = dice.roll("1d20")
    d20 = attack_roll["rolls"][0]
    total = d20 + action["attack_bonus"]
    hit = d20 == 20 or (d20 != 1 and total >= target["ac"])

    result = {
        "attacker": attacker["name"], "action": action["name"], "target": target["name"],
        "d20": d20, "attack_bonus": action["attack_bonus"], "attack_total": total,
        "target_ac": target["ac"], "hit": hit, "critical": d20 == 20, "damage": 0,
    }

    if not hit:
        result["text"] = (f"{attacker['name']} attacks {target['name']} with {action['name']}: "
                          f"{d20}+{action['attack_bonus']}={total} vs AC {target['ac']} -- miss")
        character.log_campaign_event("attack", attacker["name"], result["text"])
        return result

    damage = 0
    for entry in action.get("damage") or []:
        rolled = _damage_amount(_damage_dice_of(entry))
        if rolled:
            damage += rolled["total"]
    result["damage"] = damage
    result["text"] = (f"{attacker['name']} hits {target['name']} with {action['name']}: "
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

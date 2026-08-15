"""Phase 6: portable characters. nova-dm's own open JSON format, so a character
can be backed up, handed to a friend, moved between machines, or restored after a
wiped campaign.sqlite.

The format is deliberately ours and documented here rather than borrowed from a
closed platform -- same reasoning that moved the monster catalog to Open5e.

Import is a RESTORE, not a re-creation: it writes the stored values straight in
rather than going through character.create_character, which applies racial
ability bonuses to the base scores it is handed. Rebuilding a dwarf through that
path would add its +2 CON again on every round trip.
"""
import json
from datetime import datetime

from . import character, rules

FORMAT = "nova-dm.character"
VERSION = 1

ABILITY_COLS = ["str", "dex", "con", "int_", "wis", "cha"]


class PortableError(ValueError):
    """Something is wrong with the file, phrased for a player to read."""


def export_character(character_id: int) -> dict:
    char = character.get_character(character_id)
    if not char:
        raise PortableError("That character no longer exists.")

    with character._campaign_con() as con:
        features = [r["feature_slug"] for r in con.execute(
            "SELECT feature_slug FROM character_features WHERE character_id=? ORDER BY id",
            (character_id,),
        ).fetchall()]

    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        # id and player_id are left out on purpose: identity and ownership belong
        # to whichever table imports the file, not to the file itself.
        "character": {
            "name": char["name"],
            "race": char["race"],
            "class": char["class"],
            "level": char["level"],
            "xp": char["xp"],
            "abilities": {col: char[col] for col in ABILITY_COLS},
            "hp": {"max": char["max_hp"], "current": char["current_hp"], "temp": char["temp_hp"]},
            "ac": char["ac"],
            "speed": char["speed"],
            "gold": char["gold"],
            "inspiration": char["inspiration"],
            "conditions": json.loads(char["conditions_json"] or "[]"),
            "notes": char["notes"],
        },
        "features": features,
    }


def parse(blob: str) -> dict:
    """Turn pasted or uploaded text into a payload, or explain why it isn't one."""
    if not (blob or "").strip():
        raise PortableError("Nothing to import -- paste a character file or choose one.")
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError as e:
        raise PortableError(f"That isn't valid JSON ({e.msg}, line {e.lineno}).") from e
    if not isinstance(payload, dict):
        raise PortableError("That file doesn't look like a nova-dm character.")
    return payload


def _validate(payload: dict) -> dict:
    if payload.get("format") != FORMAT:
        raise PortableError("That file isn't a nova-dm character export.")
    version = payload.get("version")
    if not isinstance(version, int) or version > VERSION:
        raise PortableError(
            f"That file was written by a newer nova-dm (format version {version}); "
            f"this one reads up to version {VERSION}."
        )

    data = payload.get("character")
    if not isinstance(data, dict):
        raise PortableError("That file has no character in it.")
    for field in ("name", "race", "class"):
        if not str(data.get(field) or "").strip():
            raise PortableError(f"The character is missing its {field}.")
    return data


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def import_character(payload: dict, player_id) -> dict:
    """Write a character from an export payload. Unknown keys are ignored so a
    file from a later version still loads."""
    data = _validate(payload)

    abilities = data.get("abilities") or {}
    scores = {col: _int(abilities.get(col), 10) for col in ABILITY_COLS}

    hp = data.get("hp") or {}
    max_hp = max(1, _int(hp.get("max"), 1))
    # A file claiming more current than max would break the HP bar's arithmetic.
    current_hp = min(max_hp, _int(hp.get("current"), max_hp))

    level = max(1, _int(data.get("level"), 1))
    # Purely derived from level, so recompute rather than trust the file -- this
    # also quietly repairs a hand-edited one. Everything else is taken as given:
    # a LAN game with no auth model doesn't get anti-cheat here either.
    proficiency_bonus = rules.proficiency_bonus(level)

    conditions = data.get("conditions")
    conditions = conditions if isinstance(conditions, list) else []

    with character._campaign_con() as con:
        cur = con.execute(
            "INSERT INTO characters (player_id, name, race, class, level, xp,"
            " str, dex, con, int_, wis, cha, max_hp, current_hp, temp_hp, ac, speed,"
            " proficiency_bonus, gold, inspiration, conditions_json, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (player_id, str(data["name"]).strip(), data["race"], data["class"], level,
             _int(data.get("xp")), *[scores[c] for c in ABILITY_COLS],
             max_hp, current_hp, _int(hp.get("temp")), _int(data.get("ac"), 10),
             _int(data.get("speed"), 30), proficiency_bonus, _int(data.get("gold")),
             _int(data.get("inspiration")), json.dumps(conditions), data.get("notes")),
        )
        char_id = cur.lastrowid
        for slug in payload.get("features") or []:
            con.execute(
                "INSERT INTO character_features (character_id, feature_slug, source_level)"
                " VALUES (?,?,?)",
                (char_id, str(slug), level),
            )

    character.log_campaign_event("import", data["name"], f"{data['name']} joins the campaign.")
    return character.get_character(char_id)


def filename_for(char: dict) -> str:
    slug = "".join(c.lower() if c.isalnum() else "-" for c in char["name"]).strip("-") or "character"
    return f"{slug}.nova-dm.json"

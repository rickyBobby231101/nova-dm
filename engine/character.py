"""Character creation and progression. Reads reference data from db/srd.sqlite,
writes live state to db/campaign.sqlite. All derived stats (HP, proficiency,
level) are computed via engine.rules, never hand-entered."""
import json
import os
import sqlite3

from . import rules

DB_DIR = os.path.join(os.path.dirname(__file__), "..", "db")
SRD_DB_PATH = os.path.join(DB_DIR, "srd.sqlite")
CAMPAIGN_DB_PATH = os.path.join(DB_DIR, "campaign.sqlite")

ABILITIES = ["str", "dex", "con", "int_", "wis", "cha"]
_SRD_TO_COL = {"str": "str", "dex": "dex", "con": "con", "int": "int_", "wis": "wis", "cha": "cha"}

CAMPAIGN_SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    session_token TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER,
    name TEXT NOT NULL,
    race TEXT NOT NULL,
    class TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 1,
    xp INTEGER NOT NULL DEFAULT 0,
    str INTEGER NOT NULL, dex INTEGER NOT NULL, con INTEGER NOT NULL,
    int_ INTEGER NOT NULL, wis INTEGER NOT NULL, cha INTEGER NOT NULL,
    max_hp INTEGER NOT NULL, current_hp INTEGER NOT NULL, temp_hp INTEGER NOT NULL DEFAULT 0,
    ac INTEGER NOT NULL DEFAULT 10,
    speed INTEGER NOT NULL DEFAULT 30,
    proficiency_bonus INTEGER NOT NULL DEFAULT 2,
    gold INTEGER NOT NULL DEFAULT 0,
    inspiration INTEGER NOT NULL DEFAULT 0,
    conditions_json TEXT NOT NULL DEFAULT '[]',
    notes TEXT
);
CREATE TABLE IF NOT EXISTS character_inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL,
    item_slug TEXT NOT NULL,
    qty INTEGER NOT NULL DEFAULT 1,
    equipped INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS character_spells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL,
    spell_slug TEXT NOT NULL,
    prepared INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS character_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL,
    feature_slug TEXT NOT NULL,
    source_level INTEGER NOT NULL
);
"""


def _connect(path):
    con = sqlite3.connect(path, timeout=15)
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    return con


def _campaign_con():
    con = _connect(CAMPAIGN_DB_PATH)
    con.executescript(CAMPAIGN_SCHEMA)
    return con


def _srd_con():
    return _connect(SRD_DB_PATH)


def get_character(character_id: int) -> dict:
    with _campaign_con() as con:
        row = con.execute("SELECT * FROM characters WHERE id=?", (character_id,)).fetchone()
    return dict(row) if row else None


def create_character(player_id, name: str, race: str, class_: str, ability_scores: dict) -> dict:
    """ability_scores: dict with keys str/dex/con/int/wis/cha (pre-racial-bonus base scores)."""
    scores = {_SRD_TO_COL[k]: v for k, v in ability_scores.items()}

    with _srd_con() as srd:
        race_row = srd.execute('SELECT raw_json FROM races WHERE "index"=?', (race,)).fetchone()
        class_row = srd.execute('SELECT raw_json, hit_die FROM classes WHERE "index"=?', (class_,)).fetchone()
        level1_row = srd.execute(
            "SELECT raw_json FROM class_levels WHERE class_index=? AND level=1", (class_,)
        ).fetchone()
    if not race_row or not class_row:
        raise ValueError(f"unknown race/class: {race}/{class_}")

    race_data = json.loads(race_row["raw_json"])
    for bonus in race_data.get("ability_bonuses", []):
        col = _SRD_TO_COL.get(bonus["ability_score"]["index"])
        if col:
            scores[col] = scores.get(col, 10) + bonus["bonus"]

    hit_die = class_row["hit_die"]
    con_mod = rules.ability_mod(scores["con"])
    max_hp = hit_die + con_mod  # level 1: max hit die roll, not average
    speed = race_data.get("speed", 30)

    with _campaign_con() as con:
        cur = con.execute(
            "INSERT INTO characters (player_id, name, race, class, level, xp, "
            "str, dex, con, int_, wis, cha, max_hp, current_hp, temp_hp, ac, speed, "
            "proficiency_bonus, gold) VALUES (?,?,?,?,1,0,?,?,?,?,?,?,?,?,0,10,?,2,0)",
            (player_id, name, race, class_,
             scores.get("str", 10), scores.get("dex", 10), scores.get("con", 10),
             scores.get("int_", 10), scores.get("wis", 10), scores.get("cha", 10),
             max_hp, max_hp, speed)
        )
        char_id = cur.lastrowid
        if level1_row:
            level1 = json.loads(level1_row["raw_json"])
            for feat in level1.get("features", []):
                con.execute(
                    "INSERT INTO character_features (character_id, feature_slug, source_level) "
                    "VALUES (?,?,1)",
                    (char_id, feat["index"])
                )
    return get_character(char_id)


def award_xp(character_id: int, amount: int) -> dict:
    char = get_character(character_id)
    if not char:
        raise ValueError(f"unknown character: {character_id}")
    old_level = rules.xp_to_level(char["xp"])
    new_xp = char["xp"] + amount
    new_level = rules.xp_to_level(new_xp)

    with _campaign_con() as con:
        con.execute("UPDATE characters SET xp=? WHERE id=?", (new_xp, character_id))

    level_ups = []
    for lvl in range(old_level + 1, new_level + 1):
        level_ups.append(apply_level_up(character_id, target_level=lvl))
    return {"character_id": character_id, "xp": new_xp, "old_level": old_level,
            "new_level": new_level, "level_ups": level_ups}


def apply_level_up(character_id: int, target_level: int = None) -> dict:
    char = get_character(character_id)
    target_level = target_level or (char["level"] + 1)

    with _srd_con() as srd:
        class_row = srd.execute(
            'SELECT hit_die FROM classes WHERE "index"=?', (char["class"],)
        ).fetchone()
        level_row = srd.execute(
            "SELECT raw_json FROM class_levels WHERE class_index=? AND level=?",
            (char["class"], target_level)
        ).fetchone()

    con_mod = rules.ability_mod(char["con"])
    hp_gain = rules.hp_on_levelup(class_row["hit_die"], con_mod)
    new_max_hp = char["max_hp"] + hp_gain
    new_prof = rules.proficiency_bonus(target_level)

    features_added = []
    with _campaign_con() as con:
        con.execute(
            "UPDATE characters SET level=?, max_hp=?, current_hp=current_hp+?, "
            "proficiency_bonus=? WHERE id=?",
            (target_level, new_max_hp, hp_gain, new_prof, character_id)
        )
        if level_row:
            level_data = json.loads(level_row["raw_json"])
            for feat in level_data.get("features", []):
                con.execute(
                    "INSERT INTO character_features (character_id, feature_slug, source_level) "
                    "VALUES (?,?,?)",
                    (character_id, feat["index"], target_level)
                )
                features_added.append(feat["index"])

    return {"character_id": character_id, "level": target_level, "hp_gain": hp_gain,
            "new_max_hp": new_max_hp, "proficiency_bonus": new_prof, "features_added": features_added}


def apply_damage(character_id: int, amount: int) -> dict:
    char = get_character(character_id)
    remaining = amount
    temp_hp = char["temp_hp"]
    if temp_hp > 0:
        absorbed = min(temp_hp, remaining)
        temp_hp -= absorbed
        remaining -= absorbed
    current_hp = max(0, char["current_hp"] - remaining)
    with _campaign_con() as con:
        con.execute("UPDATE characters SET current_hp=?, temp_hp=? WHERE id=?",
                    (current_hp, temp_hp, character_id))
    return {"character_id": character_id, "current_hp": current_hp, "temp_hp": temp_hp,
            "unconscious": current_hp == 0}


def apply_heal(character_id: int, amount: int) -> dict:
    char = get_character(character_id)
    current_hp = min(char["max_hp"], char["current_hp"] + amount)
    with _campaign_con() as con:
        con.execute("UPDATE characters SET current_hp=? WHERE id=?", (current_hp, character_id))
    return {"character_id": character_id, "current_hp": current_hp, "max_hp": char["max_hp"]}

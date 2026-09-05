"""Character creation and progression. Reads reference data from db/srd.sqlite,
writes live state to db/campaign.sqlite. All derived stats (HP, proficiency,
level) are computed via engine.rules, never hand-entered."""
import json
import os
import secrets
import sqlite3
from datetime import datetime

from . import rules

DB_DIR = os.path.join(os.path.dirname(__file__), "..", "db")
SRD_DB_PATH = os.path.join(DB_DIR, "srd.sqlite")

# Overridable so the tests can be pointed somewhere disposable. They delete this
# file between cases, and for a long time that file was the real campaign -- the
# one holding everyone's characters. Running the suite while the game was up
# corrupted it, and running it while the game was down silently destroyed it.
# Losing a player's character to a test run is not a risk worth carrying for the
# convenience of a hardcoded path.
CAMPAIGN_DB_PATH = os.environ.get(
    "NOVA_DM_CAMPAIGN_DB", os.path.join(DB_DIR, "campaign.sqlite")
)

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
CREATE TABLE IF NOT EXISTS campaign_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT,
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS encounters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    round INTEGER NOT NULL DEFAULT 1,
    turn_index INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
-- max_hp/current_hp are for monsters only. A player character's HP lives in
-- `characters` and nowhere else -- see engine/encounter.py. Two copies of a PC's
-- HP would drift apart the moment either side took a hit.
CREATE TABLE IF NOT EXISTS combatants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    encounter_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    character_id INTEGER,
    monster_slug TEXT,
    name TEXT NOT NULL,
    max_hp INTEGER,
    current_hp INTEGER,
    ac INTEGER NOT NULL DEFAULT 10,
    initiative INTEGER NOT NULL DEFAULT 0,
    dex INTEGER NOT NULL DEFAULT 10,
    is_down INTEGER NOT NULL DEFAULT 0,
    -- monsters only, for the same reason as max_hp above: a player's conditions
    -- live on the character and outlast the fight, a monster's don't outlast it.
    conditions_json TEXT NOT NULL DEFAULT '[]'
);
-- Where the campaign is and what has happened, in the DM's own words. Keyed
-- rather than columned because what a campaign needs to remember will grow and
-- none of it is worth a migration -- see engine/chronicle.py.
CREATE TABLE IF NOT EXISTS game_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _connect(path):
    con = sqlite3.connect(path, timeout=15)
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    return con


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS silently
# does nothing to a table that already exists, so a new column needs an ALTER --
# and it has to be safe to run on every connect, like the schema above it.
ADDED_COLUMNS = {
    # Improvements earned but not yet spent. On the character rather than in
    # game_state because it belongs to one character, and because an unspent
    # improvement has to survive until its player is next at the table.
    "characters": [
        ("pending_asi", "INTEGER NOT NULL DEFAULT 0"),
        # The stored filename of an uploaded portrait, or NULL for the initials
        # fallback. Just the id -- the bytes live under engine.avatar's
        # directory, because a database is a poor place to keep two megabytes
        # that never need querying.
        ("avatar", "TEXT"),
    ],
}


def _migrate(con):
    for table, columns in ADDED_COLUMNS.items():
        existing = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for name, spec in columns:
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")


def _campaign_con():
    con = _connect(CAMPAIGN_DB_PATH)
    con.executescript(CAMPAIGN_SCHEMA)
    _migrate(con)
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

    # AC was a flat 10 here, which left a DEX 17 rogue no harder to hit than a
    # sack of flour. Armour still isn't modelled, so unarmored AC is the honest
    # number -- and it has to be derived rather than stored blind, because
    # raising DEX later has to move it.
    ac = rules.unarmored_ac(scores.get("dex", 10))

    with _campaign_con() as con:
        cur = con.execute(
            "INSERT INTO characters (player_id, name, race, class, level, xp, "
            "str, dex, con, int_, wis, cha, max_hp, current_hp, temp_hp, ac, speed, "
            "proficiency_bonus, gold) VALUES (?,?,?,?,1,0,?,?,?,?,?,?,?,?,0,?,?,2,0)",
            (player_id, name, race, class_,
             scores.get("str", 10), scores.get("dex", 10), scores.get("con", 10),
             scores.get("int_", 10), scores.get("wis", 10), scores.get("cha", 10),
             max_hp, max_hp, ac, speed)
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

    # Counted across the whole jump rather than for the destination alone: enough
    # XP at once can carry a character up two levels, and an improvement stepped
    # over is one nobody ever gets.
    asi_points = len(rules.asi_levels_crossed(char["level"], target_level)) * rules.ASI_POINTS

    features_added = []
    with _campaign_con() as con:
        con.execute(
            "UPDATE characters SET level=?, max_hp=?, current_hp=current_hp+?, "
            "proficiency_bonus=?, pending_asi=pending_asi+? WHERE id=?",
            (target_level, new_max_hp, hp_gain, new_prof, asi_points, character_id)
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
            "new_max_hp": new_max_hp, "proficiency_bonus": new_prof,
            "features_added": features_added, "asi_points": asi_points,
            "pending_asi": get_character(character_id)["pending_asi"]}


def raise_ability(character_id: int, ability: str, amount: int = 1) -> dict:
    """Spend part of an earned improvement on one ability score.

    pending_asi counts points, not improvements -- two per improvement -- because
    5e lets them be split across two abilities. Spending them one at a time is
    the same operation as spending both at once, and a player who walks away
    mid-choice finds the remainder still waiting.
    """
    char = get_character(character_id)
    if not char:
        raise ValueError(f"unknown character: {character_id}")

    col = _SRD_TO_COL.get(ability, ability)
    if col not in ABILITIES:
        raise ValueError(f"not an ability: {ability}")

    amount = int(amount)
    if amount < 1:
        raise ValueError("an improvement raises a score, it does not lower one")
    if char["pending_asi"] < amount:
        raise ValueError(
            f"{char['name']} has {char['pending_asi']} improvement point(s), not {amount}")

    old = char[col]
    new = min(old + amount, rules.ABILITY_CAP)
    if new == old:
        raise ValueError(f"{ability.upper()} is already at {rules.ABILITY_CAP}")
    granted = new - old

    # A raised CON is worth hit points for every level already taken, not just
    # the ones still to come -- 5e applies the new modifier retroactively.
    hp_gain = 0
    if col == "con":
        hp_gain = (rules.ability_mod(new) - rules.ability_mod(old)) * char["level"]

    new_ac = rules.unarmored_ac(new) if col == "dex" else char["ac"]

    with _campaign_con() as con:
        con.execute(
            f"UPDATE characters SET {col}=?, max_hp=max_hp+?, current_hp=current_hp+?, "
            "ac=?, pending_asi=pending_asi-? WHERE id=?",
            (new, hp_gain, hp_gain, new_ac, granted, character_id)
        )

    after = get_character(character_id)
    return {"character_id": character_id, "ability": rules.ability_label(col),
            "from": old, "to": new, "granted": granted, "hp_gain": hp_gain,
            "ac": after["ac"], "pending_asi": after["pending_asi"],
            "name": char["name"]}


def apply_damage(character_id: int, amount: int) -> dict:
    char = get_character(character_id)
    # Damage below zero is not healing, it is a mistake. Observed live: the DM
    # called apply_damage(-3) and the engine obediently put a character on
    # 12/9 HP -- above their own maximum, which nothing else in the game can
    # produce. Healing has its own tool, and it caps.
    remaining = max(0, amount)
    temp_hp = char["temp_hp"]
    if temp_hp > 0:
        absorbed = min(temp_hp, remaining)
        temp_hp -= absorbed
        remaining -= absorbed
    # Clamped at both ends. The floor was always here; the ceiling is what was
    # missing, and a stored value above max_hp outlives the turn that made it.
    current_hp = min(char["max_hp"], max(0, char["current_hp"] - remaining))
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


# ── players / sessions ───────────────────────────────────────────────────────

def create_player(name: str) -> dict:
    token = secrets.token_hex(16)
    with _campaign_con() as con:
        cur = con.execute(
            "INSERT INTO players (name, session_token, created_at) VALUES (?,?,?)",
            (name.strip(), token, datetime.now().isoformat())
        )
        player_id = cur.lastrowid
    return {"id": player_id, "name": name.strip(), "session_token": token}


def find_player_by_name(name: str) -> dict:
    """The seat this name already had, if any.

    A phone that cleared its cookies, or a player who backed out to the join
    screen and came in again, is the same person walking back to the same
    chair -- and their characters are on the row they left behind. Prefers the
    seat that actually has characters, then the most recent.

    Identity here is a name behind a join code, which is the right strength for
    a game among friends on a private tailnet: everyone at the table was
    invited by the host. It is deliberately not an account system.
    """
    name = (name or "").strip()
    if not name:
        return None
    with _campaign_con() as con:
        row = con.execute(
            "SELECT p.* FROM players p "
            "WHERE lower(p.name) = lower(?) "
            "ORDER BY (SELECT COUNT(*) FROM characters c WHERE c.player_id = p.id) DESC, "
            "         p.id DESC LIMIT 1", (name,)
        ).fetchone()
    return dict(row) if row else None


def get_player_by_token(token: str) -> dict:
    with _campaign_con() as con:
        row = con.execute("SELECT * FROM players WHERE session_token=?", (token,)).fetchone()
    return dict(row) if row else None


# The Cathedral's own entities sit on this seat. Nobody joins as "The
# Cathedral", so anything parked here is an NPC the DM voices — until a player
# claims it, at which point it becomes theirs and stops being an NPC. One pool,
# one rule, no separate npc flag to keep in sync with reality.
NPC_SEAT_NAME = "The Cathedral"


def npc_seat():
    """The seat holding unclaimed entities, or None if it was never created."""
    return find_player_by_name(NPC_SEAT_NAME)


def list_npcs() -> list:
    """Entities nobody is playing yet."""
    seat = npc_seat()
    return list_characters_for_player(seat["id"]) if seat else []


def claim_character(character_id: int, player_id: int) -> dict:
    """A player takes over an unclaimed entity.

    Only ever moves a character *off* the NPC seat. A character already owned
    by a player is somebody's, and moving it would hand one person another's
    character — the same failure the seat cookie fix guards against, in a
    different costume.
    """
    seat = npc_seat()
    if not seat:
        return {"error": "there is no Cathedral seat"}

    char = get_character(character_id)
    if not char:
        return {"error": f"no character {character_id}"}
    if char["player_id"] != seat["id"]:
        return {"error": f"{char['name']} is already being played"}

    with _campaign_con() as con:
        con.execute("UPDATE characters SET player_id=? WHERE id=?",
                    (player_id, character_id))
    return {"ok": True, "character_id": character_id, "name": char["name"]}


def release_character(character_id: int, player_id: int) -> dict:
    """Hand a claimed entity back to the Cathedral.

    Only works on an entity — a character the player made themselves is not
    the Cathedral's to take back.
    """
    seat = npc_seat()
    if not seat:
        return {"error": "there is no Cathedral seat"}
    char = get_character(character_id)
    if not char or char["player_id"] != player_id:
        return {"error": "not yours to release"}
    if char["name"] not in {c["name"] for c in _cathedral_entity_names()}:
        return {"error": f"{char['name']} is not a Cathedral entity"}

    with _campaign_con() as con:
        con.execute("UPDATE characters SET player_id=? WHERE id=?",
                    (seat["id"], character_id))
    return {"ok": True, "released": char["name"]}


def _cathedral_entity_names() -> list:
    """Names that belong to the Cathedral, whoever currently holds them."""
    return [{"name": n} for n in
            ("Tillagon", "Eyemoeba", "Phoenix", "Zorya", "Jorlaan",
             "The Weaver", "Lucid")]


def list_characters_for_player(player_id: int) -> list:
    with _campaign_con() as con:
        rows = con.execute("SELECT * FROM characters WHERE player_id=?", (player_id,)).fetchall()
    return [dict(r) for r in rows]


def list_active_characters() -> list:
    """All characters in the (single, shared) campaign -- used to give the DM table context."""
    with _campaign_con() as con:
        rows = con.execute("SELECT * FROM characters").fetchall()
    return [dict(r) for r in rows]


# ── SRD lookups for character creation ───────────────────────────────────────

def list_srd_classes() -> list:
    with _srd_con() as srd:
        rows = srd.execute('SELECT "index", name FROM classes ORDER BY name').fetchall()
    return [dict(r) for r in rows]


def list_srd_races() -> list:
    with _srd_con() as srd:
        rows = srd.execute('SELECT "index", name FROM races ORDER BY name').fetchall()
    return [dict(r) for r in rows]


# ── campaign log — the permanent tape ────────────────────────────────────────

def log_campaign_event(kind: str, actor: str, content: str):
    with _campaign_con() as con:
        con.execute(
            "INSERT INTO campaign_log (ts, kind, actor, content) VALUES (?,?,?,?)",
            (datetime.now().isoformat(), kind, actor, content)
        )

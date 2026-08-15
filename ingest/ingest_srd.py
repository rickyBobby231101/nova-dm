"""One-time reference data -> SQLite loader. Idempotent -- safe to re-run.

Two sources, each where it's actually better:

- Monsters: Open5e. It carries 3207 creatures (SRD plus Kobold Press's Tome of
  Beasts, Creature Codex, Black Flag and others) against dnd5eapi's 334. Open5e
  was rejected for this in Phase 1 as slow and timeout-prone; re-tested 2026-08-13
  it answers in well under a second, so that objection no longer holds.
- Classes, races, spells, equipment, features: still dnd5eapi. Its class level
  progression is structured rows (level, proficiency bonus, features), which
  engine.character relies on for leveling. Open5e ships the same progression as a
  markdown table in a string field, so switching those would mean parsing markdown
  to get back what we already have properly typed.

Monsters are normalized at ingest rather than at query time -- see
normalize_attacks below. Open5e's creatures come in three different attack shapes
depending on which book they're from, and the engine should not have to know that.

Usage: python3 ingest_srd.py
"""
import json
import os
import re
import sqlite3
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from engine import rules  # noqa: E402  (path set above)

BASE = "https://www.dnd5eapi.co/api/2014"
OPEN5E_MONSTERS = "https://api.open5e.com/v1/monsters/?limit=500"
OPEN5E_CONDITIONS = "https://api.open5e.com/v1/conditions/?limit=100"
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "srd.sqlite")

HIT_RE = re.compile(r"([+-]\d+)\s+to hit")
DMG_RE = re.compile(r"\((\d+d\d+(?:\s*[+-]\s*\d+)?)\)")
# Small creatures deal fixed damage with no dice at all -- a badger's bite is
# "Hit: 1 piercing damage". That's 40 statblocks, mostly the low-CR animals a DM
# actually reaches for, so they get their attack rather than being left unarmed.
FLAT_RE = re.compile(r"Hit:\s*(\d+)\s+\w+\s+damage", re.I)
DICE_RE = re.compile(r"^\d+d\d+([+-]\d+)?$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS classes (
    "index" TEXT PRIMARY KEY, name TEXT, hit_die INTEGER, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS class_levels (
    class_index TEXT, level INTEGER, prof_bonus INTEGER, raw_json TEXT,
    PRIMARY KEY (class_index, level)
);
CREATE TABLE IF NOT EXISTS races (
    "index" TEXT PRIMARY KEY, name TEXT, speed INTEGER, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS spells (
    "index" TEXT PRIMARY KEY, name TEXT, level INTEGER, school TEXT, raw_json TEXT
);
-- source: Open5e carries the same creature from several books (badger, badger-a5e,
-- badger_bf), so the builder has to show which one a row came from or the DM sees
-- three identical "Awakened Shrub" entries.
CREATE TABLE IF NOT EXISTS monsters (
    "index" TEXT PRIMARY KEY, name TEXT, cr REAL, xp INTEGER, source TEXT, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS equipment (
    "index" TEXT PRIMARY KEY, name TEXT, category TEXT, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS conditions (
    "index" TEXT PRIMARY KEY, name TEXT, description TEXT, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS features (
    "index" TEXT PRIMARY KEY, name TEXT, class_index TEXT, level INTEGER, raw_json TEXT
);
"""


def get(url: str, retries: int = 2, backoff: float = 3.0, timeout: float = 10) -> dict:
    full = url if url.startswith("http") else f"https://www.dnd5eapi.co{url}"
    for attempt in range(retries + 1):
        try:
            r = requests.get(full, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries:
                raise
            print(f"  retry ({e}) ...")
            time.sleep(backoff)


def fetch_all(endpoint: str) -> list:
    data = get(f"{BASE}/{endpoint}")
    return data["results"]


def ingest_classes(con):
    items = fetch_all("classes")
    print(f"classes: {len(items)}")
    for item in items:
        detail = get(item["url"])
        con.execute(
            'INSERT OR REPLACE INTO classes ("index", name, hit_die, raw_json) VALUES (?,?,?,?)',
            (detail["index"], detail["name"], detail.get("hit_die"), json.dumps(detail))
        )
        levels = get(f"{item['url']}/levels")
        for lvl in levels:
            con.execute(
                "INSERT OR REPLACE INTO class_levels (class_index, level, prof_bonus, raw_json) "
                "VALUES (?,?,?,?)",
                (detail["index"], lvl["level"], lvl.get("prof_bonus"), json.dumps(lvl))
            )
        for feat_ref in [f for lvl in levels for f in lvl.get("features", [])]:
            feat = get(feat_ref["url"])
            con.execute(
                'INSERT OR REPLACE INTO features ("index", name, class_index, level, raw_json) '
                "VALUES (?,?,?,?,?)",
                (feat["index"], feat["name"], detail["index"], feat.get("level"), json.dumps(feat))
            )
        con.commit()
        print(f"  {detail['name']}: {len(levels)} levels")


def _clean_dice(expr) -> str | None:
    """Reduce a damage expression to something engine.dice can actually roll.
    A few statblocks write compound damage like '2d10+2d10'; take the leading
    rollable term rather than dropping the attack entirely."""
    expr = str(expr or "").replace(" ", "")
    if DICE_RE.match(expr):
        return expr
    if expr.isdigit():   # fixed damage, e.g. a badger's 1
        return expr
    terms = [t for t in re.findall(r"\d+d\d+|[+-]?\d+", expr) if "d" in t]
    return terms[0] if terms and DICE_RE.match(terms[0]) else None


def normalize_attacks(monster: dict) -> list:
    """One attack shape for the engine, whatever book the creature came from.

    Open5e's older books give structured attack_bonus/damage_dice/damage_bonus.
    Newer ones (menagerie, tob-2023, tob3, blackflag -- 1751 creatures) give only
    prose: "Melee Weapon Attack: +8 to hit, 10 ft., one target, 18 (3d8+5)
    slashing damage." Parsing that back out recovers 97.7% of them; the rest have
    no rollable attack and are left with none, which the engine reports honestly
    rather than inventing a number for.
    """
    attacks = []
    for action in monster.get("actions") or []:
        if not isinstance(action, dict):
            continue
        bonus, expr = action.get("attack_bonus"), action.get("damage_dice")
        if bonus is not None and expr:
            expr = str(expr).replace(" ", "")
            # damage_bonus is a separate field; don't double count when the dice
            # expression already carries its own modifier.
            if action.get("damage_bonus") and not re.search(r"[+-]\d+$", expr):
                expr = f"{expr}+{action['damage_bonus']}"
        else:
            desc = action.get("desc") or ""
            hit = HIT_RE.search(desc)
            dmg = DMG_RE.search(desc) or FLAT_RE.search(desc)
            if not (hit and dmg):
                continue
            bonus, expr = int(hit.group(1)), dmg.group(1)
        expr = _clean_dice(expr)
        if expr is None:
            continue
        attacks.append({
            "name": action.get("name") or "Attack",
            "attack_bonus": int(bonus),
            "damage_dice": expr,
        })
    return attacks


def ingest_monsters(con):
    """Open5e, paginated. Stores the creature as published plus a normalized
    `attacks` list and `hp_expr`, so engine.encounter reads one shape."""
    url, monsters = OPEN5E_MONSTERS, []
    while url:
        # 500-creature pages take several seconds each -- the default 10s timeout
        # is too tight for these, though fine for dnd5eapi's small responses.
        page = get(url, timeout=60)
        monsters.extend(page["results"])
        url = page.get("next")
        print(f"  fetched {len(monsters)}", end="\r")
    print(f"monsters: {len(monsters)}")

    # Not just a refresh: any dnd5eapi rows left behind carry the old format, which
    # the engine no longer reads -- they'd survive as silently attackless monsters.
    con.execute("DELETE FROM monsters")

    armed = 0
    for m in monsters:
        attacks = normalize_attacks(m)
        armed += 1 if attacks else 0
        # Open5e's hit_dice already includes the CON bonus ("10d12+50"), unlike its
        # own hit_points average. Black Flag creatures ship no hit_dice at all, so
        # they fall back to flat published hit points.
        hp_expr = _clean_dice(m.get("hit_dice"))
        record = dict(m, attacks=attacks, hp_expr=hp_expr,
                      ac=m.get("armor_class"), fallback_hp=m.get("hit_points"))
        con.execute(
            'INSERT OR REPLACE INTO monsters ("index", name, cr, xp, source, raw_json)'
            " VALUES (?,?,?,?,?,?)",
            (m["slug"], m["name"], m.get("cr"), rules.xp_for_cr(m.get("cr")),
             m.get("document__title") or m.get("document__slug"), json.dumps(record)),
        )
    con.commit()
    print(f"  {armed}/{len(monsters)} have a rollable attack")


def ingest_conditions(con):
    """The 15 SRD conditions with their rules text, from the same open source as
    the monsters -- the DM screen shows these so nobody has to remember what
    'restrained' does."""
    results = get(OPEN5E_CONDITIONS, timeout=30)["results"]
    print(f"conditions: {len(results)}")
    for c in results:
        con.execute(
            'INSERT OR REPLACE INTO conditions ("index", name, description, raw_json)'
            " VALUES (?,?,?,?)",
            (c["slug"], c["name"], c.get("desc") or "", json.dumps(c)),
        )
    con.commit()


def ingest_simple(con, endpoint: str, table: str, extra_cols):
    items = fetch_all(endpoint)
    print(f"{table}: {len(items)}")
    for item in items:
        detail = get(item["url"])
        cols = ["index", "name"] + list(extra_cols.keys())
        vals = [detail["index"], detail["name"]] + [extra_cols[c](detail) for c in extra_cols]
        placeholders = ",".join("?" * (len(cols) + 1))
        col_sql = ",".join(f'"{c}"' for c in cols)
        con.execute(
            f"INSERT OR REPLACE INTO {table} ({col_sql}, raw_json) VALUES ({placeholders})",
            vals + [json.dumps(detail)]
        )
    con.commit()


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)

    ingest_classes(con)
    ingest_simple(con, "races", "races", {"speed": lambda d: d.get("speed")})
    ingest_simple(con, "spells", "spells",
                  {"level": lambda d: d.get("level"), "school": lambda d: d.get("school", {}).get("name")})
    ingest_monsters(con)
    ingest_conditions(con)
    ingest_simple(con, "equipment", "equipment",
                  {"category": lambda d: d.get("equipment_category", {}).get("name")})

    counts = {}
    for table in ["classes", "class_levels", "races", "spells", "monsters", "equipment", "features"]:
        counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    con.close()
    print("\nFinal counts:", counts)


if __name__ == "__main__":
    sys.exit(main())

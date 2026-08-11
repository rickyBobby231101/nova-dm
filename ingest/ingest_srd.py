"""One-time SRD -> SQLite loader. Source: dnd5eapi.co (chosen over Open5e after
live testing showed it's fast/consistent here while Open5e was slow and
occasionally timed out entirely). Idempotent -- safe to re-run.

Usage: python3 ingest_srd.py
"""
import json
import os
import sqlite3
import sys
import time

import requests

BASE = "https://www.dnd5eapi.co/api/2014"
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "srd.sqlite")

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
CREATE TABLE IF NOT EXISTS monsters (
    "index" TEXT PRIMARY KEY, name TEXT, cr REAL, xp INTEGER, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS equipment (
    "index" TEXT PRIMARY KEY, name TEXT, category TEXT, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS features (
    "index" TEXT PRIMARY KEY, name TEXT, class_index TEXT, level INTEGER, raw_json TEXT
);
"""


def get(url: str, retries: int = 2, backoff: float = 3.0) -> dict:
    full = url if url.startswith("http") else f"https://www.dnd5eapi.co{url}"
    for attempt in range(retries + 1):
        try:
            r = requests.get(full, timeout=10)
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
    ingest_simple(con, "monsters", "monsters",
                  {"cr": lambda d: d.get("challenge_rating"), "xp": lambda d: d.get("xp")})
    ingest_simple(con, "equipment", "equipment",
                  {"category": lambda d: d.get("equipment_category", {}).get("name")})

    counts = {}
    for table in ["classes", "class_levels", "races", "spells", "monsters", "equipment", "features"]:
        counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    con.close()
    print("\nFinal counts:", counts)


if __name__ == "__main__":
    sys.exit(main())

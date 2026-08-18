import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import character, chronicle
from ingest import import_cathedral

OLD_SCHEMA = """
CREATE TABLE campaign_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_no INTEGER, speaker TEXT, name TEXT, text TEXT, timestamp TEXT
);
"""

ROWS = [
    (1, "dm", "Nova — Dungeon Master", "The moon drew back from the Cathedral.",
     "2026-08-10T22:19:52"),
    (1, "dice", "🎲", "Jorlaan (Bard (Trickster), Lv2): 15+2=17 — success",
     "2026-08-10T22:19:52"),
    (2, "tillagon", "Tillagon", "The Cathedral is being manipulated.",
     "2026-08-10T22:27:11"),
    (3, "pc:1", "Aria (Ranger, Lv2)", "I draw my bow and step toward the shadows.",
     "2026-08-11T14:20:57"),
]


@pytest.fixture(autouse=True)
def clean(tmp_path):
    if os.path.exists(character.CAMPAIGN_DB_PATH):
        os.remove(character.CAMPAIGN_DB_PATH)
    yield
    if os.path.exists(character.CAMPAIGN_DB_PATH):
        os.remove(character.CAMPAIGN_DB_PATH)


@pytest.fixture
def old_db(tmp_path):
    path = tmp_path / "consciousness.db"
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    con.executemany(
        "INSERT INTO campaign_log (session_no, speaker, name, text, timestamp) "
        "VALUES (?,?,?,?,?)", ROWS)
    con.commit()
    con.close()
    return str(path)


def test_the_transcript_comes_across_intact(old_db):
    """The record of what was actually played is worth keeping in full."""
    result = import_cathedral.import_all(old_db)

    assert result["events"] == len(ROWS)
    assert result["sessions"] == 3
    with character._campaign_con() as con:
        texts = [r["content"] for r in con.execute("SELECT content FROM campaign_log")]
    assert "The Cathedral is being manipulated." in texts


def test_original_timestamps_are_kept(old_db):
    """It should read as history, not as something that happened this evening."""
    import_cathedral.import_all(old_db)
    with character._campaign_con() as con:
        stamps = [r["ts"] for r in con.execute("SELECT ts FROM campaign_log ORDER BY id")]
    assert stamps[0].startswith("2026-08-10")


def test_speakers_become_the_kinds_this_app_understands(old_db):
    import_cathedral.import_all(old_db)
    with character._campaign_con() as con:
        kinds = {r["content"]: r["kind"] for r in
                 con.execute("SELECT content, kind FROM campaign_log")}
    assert kinds["The moon drew back from the Cathedral."] == "dm"
    assert kinds["I draw my bow and step toward the shadows."] == "action"
    assert kinds["Jorlaan (Bard (Trickster), Lv2): 15+2=17 — success"] == "roll"
    # an entity speaking is narration here, not a player action
    assert kinds["The Cathedral is being manipulated."] == "dm"


def test_the_dm_is_left_short_notes_rather_than_four_hundred_words(old_db):
    """The chronicle is re-read on every turn. Dumping the transcript in there
    would spend the whole memory budget on one evening from last week."""
    import_cathedral.import_all(old_db)

    written = chronicle.get_chronicle()
    assert "manipulated" in written
    assert len(written) < 600, "notes, not minutes"
    assert "The moon drew back from the Cathedral." not in written


def test_the_prior_history_sorts_before_anything_played_since(old_db):
    """The chronicle trims from the front, so the oldest must be first or it
    would outlive things that happened after it."""
    chronicle.set_scene("The Lyre Chamber", "They went below and found it silent.")

    import_cathedral.import_all(old_db)

    written = chronicle.get_chronicle()
    assert written.index("Before this") < written.index("They went below")


def test_running_it_twice_does_not_double_the_history(old_db):
    import_cathedral.import_all(old_db)
    again = import_cathedral.import_all(old_db)

    assert again["skipped"]
    with character._campaign_con() as con:
        count = con.execute("SELECT COUNT(*) FROM campaign_log").fetchone()[0]
    assert count == len(ROWS)


def test_force_overrides_the_guard(old_db):
    import_cathedral.import_all(old_db)
    assert not import_cathedral.import_all(old_db, force=True)["skipped"]


def test_a_missing_cathedral_is_a_clear_error_not_a_traceback(tmp_path):
    with pytest.raises(FileNotFoundError):
        import_cathedral.import_all(str(tmp_path / "nope.db"))


def test_the_source_is_only_ever_read(old_db):
    """nova-dm must not be able to change Nova Cathedral's data."""
    before = open(old_db, "rb").read()
    import_cathedral.import_all(old_db)
    assert open(old_db, "rb").read() == before


def test_the_class_map_matches_the_daemons(old_db):
    """A returning character should be the same character."""
    assert import_cathedral.ENTITY_CLASSES["tillagon"] == "Paladin"
    assert import_cathedral.ENTITY_CLASSES["zorya"] == "Ranger"
    assert import_cathedral.ENTITY_CLASSES["jorlaan"] == "Bard (Trickster)"

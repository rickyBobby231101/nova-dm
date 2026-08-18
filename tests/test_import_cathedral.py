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


def test_imported_events_do_not_pose_as_things_that_just_happened(old_db):
    """They get fresh row ids and recent() orders by id, so logging them as
    ordinary events told the DM a fortnight-old session happened moments ago --
    and spent the tail budget saying so."""
    import_cathedral.import_all(old_db)

    with character._campaign_con() as con:
        kinds = {r["kind"] for r in con.execute("SELECT kind FROM campaign_log")}
    assert kinds == {import_cathedral.ARCHIVE_KIND}
    assert import_cathedral.ARCHIVE_KIND not in chronicle.NARRATIVE_KINDS

    tail = chronicle.context_block()
    assert "I draw my bow and step toward the shadows." not in tail


def test_the_dm_is_left_one_line_rather_than_a_transcript(old_db):
    """The chronicle is re-read on both passes of every turn. Four lines of it
    cost about 40s a turn and llama3.2 made no use of them -- asked what
    Tillagon meant, it answered with weather. So: the hook, not the scenery."""
    import_cathedral.import_all(old_db)

    written = chronicle.get_chronicle()
    assert "manipulated" in written, "the hook has to survive the trim"
    assert len(written) < 200, "one line, not minutes"
    assert "The moon drew back from the Cathedral." not in written


def test_the_full_transcript_is_still_there_for_anyone_who_wants_it(old_db):
    """Trimming what the DM re-reads must not throw away what was played."""
    import_cathedral.import_all(old_db)

    with character._campaign_con() as con:
        texts = [r["content"] for r in con.execute("SELECT content FROM campaign_log")]
    assert "The moon drew back from the Cathedral." in texts


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

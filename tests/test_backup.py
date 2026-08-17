import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import backup, character, chronicle


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Snapshots go somewhere disposable, and never near the real ones."""
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
    if os.path.exists(character.CAMPAIGN_DB_PATH):
        os.remove(character.CAMPAIGN_DB_PATH)
    yield
    if os.path.exists(character.CAMPAIGN_DB_PATH):
        os.remove(character.CAMPAIGN_DB_PATH)


def _a_campaign():
    player = character.create_player("Chazel")
    return character.create_character(
        player_id=player["id"], name="Ferrick", race="halfling", class_="rogue",
        ability_scores={"str": 9, "dex": 17, "con": 12, "int": 13, "wis": 11, "cha": 14},
    )


# ---------------------------------------------------------------------------
# The guarantee that matters: a test run cannot touch the real campaign
# ---------------------------------------------------------------------------

def test_the_suite_is_not_pointed_at_the_real_campaign():
    """This is the bug that cost a player two characters: the suite deletes the
    campaign database between cases, and that used to be the live one."""
    real = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "db", "campaign.sqlite"))
    assert os.path.abspath(character.CAMPAIGN_DB_PATH) != real


def test_the_real_campaign_database_is_never_opened(tmp_path):
    _a_campaign()
    real = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "db", "campaign.sqlite"))
    # whatever the game's own file contains, this run did not write it
    assert os.path.abspath(character.CAMPAIGN_DB_PATH).startswith(
        os.path.abspath(os.environ["NOVA_DM_CAMPAIGN_DB"]).rsplit("/", 1)[0])
    assert real not in character.CAMPAIGN_DB_PATH


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def test_nothing_to_back_up_is_not_an_error():
    """First run, before anyone has played."""
    assert backup.snapshot("startup") is None


def test_a_snapshot_captures_the_characters():
    _a_campaign()

    path = backup.snapshot("startup")

    assert path is not None and path.exists()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    names = [r[0] for r in con.execute("SELECT name FROM characters")]
    assert names == ["Ferrick"]


def test_a_snapshot_of_an_open_database_is_still_readable():
    """The campaign is normally open, in WAL mode, with a live server attached.
    Copying those bytes gets a torn read -- SQLite's backup API does not."""
    _a_campaign()
    with character._campaign_con() as con:  # hold it open across the snapshot
        con.execute("SELECT 1")
        path = backup.snapshot("while-open")

    restored = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    assert restored.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 1


def test_a_failed_snapshot_never_raises(monkeypatch):
    """A backup that fails must not be the reason the game won't start."""
    _a_campaign()
    monkeypatch.setattr(backup.sqlite3, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")))
    assert backup.snapshot("startup") is None


def test_old_snapshots_are_pruned(monkeypatch):
    monkeypatch.setattr(backup, "KEEP", 3)
    _a_campaign()
    for i in range(6):
        backup.snapshot(f"run{i}")
        os.utime(sorted(backup.BACKUP_DIR.glob("*.sqlite"))[-1], (i * 100, i * 100))

    assert len(list(backup.BACKUP_DIR.glob("campaign-*.sqlite"))) <= backup.KEEP


def test_a_reason_cannot_escape_into_the_filename():
    _a_campaign()
    path = backup.snapshot("../../etc/passwd")
    assert path.parent == backup.BACKUP_DIR
    assert "/" not in path.name.replace(str(backup.BACKUP_DIR), "")


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def test_restore_brings_a_lost_character_back():
    """The whole point. Somebody rolled a character; something deleted it."""
    _a_campaign()
    snap = backup.snapshot("before-the-mistake")

    os.remove(character.CAMPAIGN_DB_PATH)  # the mistake
    assert character.list_active_characters() == []

    backup.restore(snap)

    assert [c["name"] for c in character.list_active_characters()] == ["Ferrick"]


def test_restore_saves_what_it_is_about_to_overwrite():
    """Restoring the wrong snapshot is exactly when you need the thing you just
    replaced."""
    _a_campaign()
    snap = backup.snapshot("first")
    before = len(list(backup.BACKUP_DIR.glob("campaign-*.sqlite")))

    backup.restore(snap)

    after = list(backup.BACKUP_DIR.glob("campaign-*.sqlite"))
    assert len(after) == before + 1
    assert any("before-restore" in p.name for p in after)


def test_restore_survives_a_stale_write_ahead_log():
    """A log left over from the database being replaced must not break the
    restore -- deleting it by hand does, because connections are still open."""
    _a_campaign()
    snap = backup.snapshot("first")
    open(character.CAMPAIGN_DB_PATH + "-wal", "wb").write(b"stale")

    backup.restore(snap)

    assert [c["name"] for c in character.list_active_characters()] == ["Ferrick"]


def test_restore_refuses_a_snapshot_that_is_not_there():
    with pytest.raises(FileNotFoundError):
        backup.restore("/nonexistent/campaign.sqlite")


def test_listing_says_what_each_snapshot_holds():
    """A backup you cannot identify is one you will not dare restore."""
    _a_campaign()
    chronicle.set_scene("The Lyre Chamber", "They went below.")
    backup.snapshot("startup")

    listed = backup.list_snapshots()

    assert len(listed) == 1
    assert any("Ferrick" in c for c in listed[0]["characters"])

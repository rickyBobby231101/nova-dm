"""Snapshots of the campaign, because a campaign is not reproducible.

Code can be rewritten and the SRD can be re-ingested. A character somebody rolled
on their phone, and the story that happened to it, cannot -- there is no source
to rebuild it from. That asymmetry is the whole reason this module exists.

It was written the day a test run deleted two characters. The database sat at a
hardcoded path that the suite removed between cases, so the loss took one command
and left nothing behind.

Snapshots use SQLite's own backup API rather than copying the file. A campaign
database is usually open, in WAL mode, with a live server attached to it; copying
those bytes gets a torn read and a snapshot that is worse than none, because it
looks like a backup right up until the moment it is needed.
"""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from . import character

BACKUP_DIR = Path(os.environ.get(
    "NOVA_DM_BACKUP_DIR",
    Path(character.DB_DIR).resolve() / "backups",
))

# Enough to undo a bad session or a bad idea. They are tens of kilobytes.
KEEP = 20


def snapshot(reason: str = "manual") -> Path | None:
    """Copy the campaign database somewhere safe. Returns the path, or None if
    there is no campaign yet.

    Never raises. A failed backup must not stop the game from starting -- that
    would turn a safety net into a new way to lose the evening.
    """
    source = Path(character.CAMPAIGN_DB_PATH)
    if not source.exists():
        return None

    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_reason = "".join(c for c in reason if c.isalnum() or c in "-_")[:24] or "manual"
        target = BACKUP_DIR / f"campaign-{stamp}-{safe_reason}.sqlite"

        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, \
                sqlite3.connect(target) as dst:
            src.backup(dst)

        _prune()
        return target
    except Exception:
        return None


def _prune():
    snaps = sorted(BACKUP_DIR.glob("campaign-*.sqlite"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    for stale in snaps[KEEP:]:
        try:
            stale.unlink()
        except OSError:
            pass


def list_snapshots() -> list:
    """Newest first, with what each one actually contains -- a backup you cannot
    identify is one you will not dare restore."""
    if not BACKUP_DIR.exists():
        return []

    out = []
    for path in sorted(BACKUP_DIR.glob("campaign-*.sqlite"),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        entry = {"path": path, "when": datetime.fromtimestamp(path.stat().st_mtime),
                 "characters": [], "events": 0}
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            entry["characters"] = [
                f"{r['name']} ({r['race']} {r['class']} {r['level']})"
                for r in con.execute("SELECT name,race,class,level FROM characters")
            ]
            entry["events"] = con.execute("SELECT COUNT(*) FROM campaign_log").fetchone()[0]
            con.close()
        except Exception:
            pass
        out.append(entry)
    return out


def restore(path) -> Path:
    """Put a snapshot back, after snapshotting what is there now.

    The current campaign is backed up first even though it is being replaced:
    restoring the wrong file is exactly the moment you most need the thing you
    just overwrote.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    snapshot("before-restore")
    target = Path(character.CAMPAIGN_DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Write through a connection and let SQLite retire the old write-ahead log
    # itself. Deleting the -wal by hand looks tidier and is not safe: this
    # process keeps campaign connections open (a `with` on a sqlite3 connection
    # manages the transaction, not the handle), and pulling the log out from
    # under them fails with a bare "disk I/O error" that names nothing.
    try:
        _copy_into(path, target)
    except sqlite3.Error:
        # The database being replaced is unopenable -- typically a write-ahead
        # log damaged by whatever went wrong in the first place. That is not a
        # reason to refuse the restore; it is the reason someone is restoring.
        # Clear the wreckage and write fresh. Only on this path, because doing
        # it unconditionally breaks the healthy case: connections stay open in
        # this process, and pulling the log out from under them fails.
        for suffix in ("", "-wal", "-shm"):
            stale = Path(str(target) + suffix)
            if stale.exists():
                stale.unlink()
        _copy_into(path, target)

    return target


def _copy_into(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(str(target), timeout=15)
    try:
        src.backup(dst)
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dst.commit()
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "restore":
        if len(sys.argv) != 3:
            print("usage: python -m engine.backup restore <snapshot.sqlite>")
            raise SystemExit(2)
        print(f"restored {restore(sys.argv[2])}")
    elif len(sys.argv) > 1 and sys.argv[1] == "list":
        snaps = list_snapshots()
        if not snaps:
            print("no snapshots yet")
        for s in snaps:
            who = ", ".join(s["characters"]) or "(no characters)"
            print(f"  {s['when']:%Y-%m-%d %H:%M}  {s['events']:>4} events  {who}")
            print(f"      {s['path']}")
    else:
        made = snapshot("manual")
        print(f"saved {made}" if made else "no campaign database to back up yet")

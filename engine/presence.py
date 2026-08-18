"""Who is actually at the table.

The app knew about characters and never about people. A character sheet exists
whether or not anyone has it open, so the spectator view could show six players
when the room was empty, or nobody when three phones were waiting on a turn.

Presence is deliberately not persisted. It is a fact about right now, and a
process restart means every socket reconnects anyway -- storing it would only
create a version of the truth that outlives the truth.
"""
import threading
from datetime import datetime

_lock = threading.Lock()
_sessions = {}  # socket id -> {"player", "character", "since"}


def arrive(sid: str, player: str = None, character: str = None) -> None:
    with _lock:
        _sessions[sid] = {
            "player": player or "someone",
            "character": character,
            "since": datetime.now(),
        }


def depart(sid: str) -> None:
    with _lock:
        _sessions.pop(sid, None)


def set_character(sid: str, character: str) -> None:
    """A device that was on the character list opens a sheet."""
    with _lock:
        if sid in _sessions:
            _sessions[sid]["character"] = character


def here() -> list:
    """Everyone connected, longest-present first so the list does not reshuffle
    every time somebody's phone reconnects."""
    with _lock:
        rows = [dict(v, sid=k) for k, v in _sessions.items()]
    rows.sort(key=lambda r: r["since"])
    return [{
        "player": r["player"],
        "character": r["character"],
        "minutes": max(0, int((datetime.now() - r["since"]).total_seconds() // 60)),
    } for r in rows]


def count() -> int:
    with _lock:
        return len(_sessions)


def clear() -> None:
    with _lock:
        _sessions.clear()

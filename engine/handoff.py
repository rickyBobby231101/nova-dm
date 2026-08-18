"""Waiting for a player to roll their own dice.

The engine has always rolled the moment the DM asked for a check, which is
correct and feels like watching. At a table the DM says "give me a Wisdom
check" and *you* pick up the die -- the pause is most of the pleasure.

So a check can be handed to the player instead. The turn genuinely stops: the
worker thread running it blocks until that player taps, and only then does the
narration pass see a number. That is why every wait has a deadline. A player who
walks away, closes the tab, or loses signal must not leave the table frozen and
everybody else waiting on a turn that will never finish -- so an unanswered
prompt rolls itself and the game goes on.

Kept apart from engine.dm because it is plumbing about threads and timeouts,
and dm.py is about the rules.
"""
import os
import secrets
import threading

# Long enough to notice a prompt on a phone that is face-down on a table, short
# enough that nobody blames the software when someone wanders off.
TIMEOUT = float(os.environ.get("NOVA_DM_ROLL_TIMEOUT", "90"))

# Whether a check is handed to the player at all. Off returns the old behaviour
# in one env var, which matters because it roughly doubles the wall time of a
# turn that needs a roll.
ASK = os.environ.get("NOVA_DM_ASK_TO_ROLL", "1") not in ("0", "false", "no")

_pending = {}
_lock = threading.Lock()


class Pending:
    __slots__ = ("token", "character_id", "event", "answered")

    def __init__(self, token, character_id):
        self.token = token
        self.character_id = character_id
        self.event = threading.Event()
        self.answered = False


def open_prompt(character_id: int) -> Pending:
    pending = Pending(secrets.token_hex(8), character_id)
    with _lock:
        _pending[pending.token] = pending
    return pending


def answer(token: str) -> bool:
    """A player tapped. Returns False for a token that is unknown or already
    used -- a double tap must not roll twice."""
    with _lock:
        pending = _pending.get(token)
        if pending is None or pending.answered:
            return False
        pending.answered = True
    pending.event.set()
    return True


def wait(pending: Pending, timeout: float = None) -> bool:
    """Block until the player answers. True if they did, False on timeout."""
    answered = pending.event.wait(TIMEOUT if timeout is None else timeout)
    with _lock:
        _pending.pop(pending.token, None)
    return answered


def cancel(pending: Pending) -> None:
    with _lock:
        _pending.pop(pending.token, None)


def outstanding() -> int:
    with _lock:
        return len(_pending)

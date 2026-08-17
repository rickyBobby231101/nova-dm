"""Phase 11: enough of a lock for a game that leaves the LAN.

Everything here was deliberately absent while nova-dm was a LAN app: the threat
model was "my friends, in my house, on my wifi", and a join form that asked for
nothing was the right amount of friction. Playing online changes who can reach
the door, not who is invited -- so this adds the smallest thing that keeps
strangers out, and nothing more.

Two secrets, because there are two different powers. The **join code** lets you
play: it is short, spoken aloud over a call, and typed on a phone. The **DM
password** lets you run the game -- start encounters, deal damage, award XP --
and is deliberately longer and separate, because handing someone a seat at the
table should not hand them the board.

They are stored in plaintext, on purpose. Daniel has to be able to read the join
code back to tell it to someone, which a hash cannot do, and the file sits in his
own home directory at mode 0600: anyone who can read it already has his account,
at which point the campaign database is theirs anyway. Hashing here would buy
nothing and cost the one thing the code is for.
"""
import json
import os
import secrets
import stat
from pathlib import Path

# No 0/O/1/I/5/S: these get read aloud over a voice call and typed on a phone
# keyboard, and a code nobody can dictate is a code nobody can join with.
ALPHABET = "ABCDEFGHJKMNPQRTUVWXYZ2346789"

JOIN_CODE_LENGTH = 6
DM_PASSWORD_LENGTH = 10

SECRETS_PATH = Path(
    os.environ.get("NOVA_DM_SECRETS", Path.home() / ".config" / "nova-dm" / "secrets.json")
)

_cache = None


def _generate(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def _fresh() -> dict:
    return {
        "secret_key": secrets.token_hex(32),
        "join_code": _generate(JOIN_CODE_LENGTH),
        "dm_password": _generate(DM_PASSWORD_LENGTH),
    }


def load() -> dict:
    """Read the secrets, creating them on first run.

    Kept outside the repo entirely: a secret in the working tree is one `git add
    .` away from being committed, and this file is the difference between a
    private game and an open one.
    """
    global _cache
    if _cache is not None:
        return _cache

    if SECRETS_PATH.exists():
        try:
            data = json.loads(SECRETS_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
    else:
        data = {}

    # Fill in anything missing rather than regenerating the lot -- rotating the
    # join code because the DM password was absent would lock out a live table.
    fresh = _fresh()
    changed = False
    for key, value in fresh.items():
        if not data.get(key):
            data[key] = value
            changed = True

    if changed:
        save(data)

    _cache = data
    return data


def save(data: dict):
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_PATH.write_text(json.dumps(data, indent=2))
    SECRETS_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600


def secret_key() -> str:
    return load()["secret_key"]


def join_code() -> str:
    return load()["join_code"]


def dm_password() -> str:
    return load()["dm_password"]


def _matches(supplied: str, actual: str) -> bool:
    """Case- and space-insensitive, but still constant time.

    Someone is typing this on a phone while being read it over a call, so
    demanding exact case would fail people who are entering the right code.
    The comparison is still compare_digest: the normalization decides what
    counts as equal, not how long it takes to find that out.
    """
    cleaned = (supplied or "").strip().replace(" ", "").replace("-", "").upper()
    return secrets.compare_digest(cleaned, actual.upper())


def check_join_code(supplied: str) -> bool:
    return _matches(supplied, join_code())


def check_dm_password(supplied: str) -> bool:
    return _matches(supplied, dm_password())


def rotate(which: str) -> str:
    """Issue a new join code or DM password. Returns the new value.

    Wanted the evening someone shares a screenshot of the join screen.
    """
    if which not in ("join_code", "dm_password"):
        raise ValueError(f"nothing called {which!r} to rotate")
    data = load()
    length = JOIN_CODE_LENGTH if which == "join_code" else DM_PASSWORD_LENGTH
    data[which] = _generate(length)
    save(data)
    return data[which]


def reset_cache():
    """Drop the in-process copy. For tests, and after editing the file by hand."""
    global _cache
    _cache = None

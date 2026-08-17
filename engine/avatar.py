"""Phase C: a face for each character.

Players asked for pictures. That is a small feature with a large attack surface,
because it is the first time this app accepts a file from a device it does not
control -- and it is reachable by anyone holding the join code, which is a thing
people read aloud over a call.

So the rules here are narrow on purpose:

  * The bytes decide the type, not the filename and not the browser's
    Content-Type. Both are supplied by whoever is uploading.
  * Stored under a name this module generates. An uploaded filename never
    reaches the filesystem, so it cannot walk out of the directory or overwrite
    somebody else's picture.
  * Capped in size, because the upload arrives before anyone has checked it.
  * Nothing leaves this machine. No image service, no CDN -- the same
    local-first stance as the voice.
"""
import os
import re
import secrets
from pathlib import Path

from . import character

AVATAR_DIR = Path(os.environ.get(
    "NOVA_DM_AVATAR_DIR",
    Path(character.DB_DIR).resolve() / "avatars",
))

# Comfortably more than a phone photo needs after the browser scales it, and
# far less than a slow laptop wants to serve to five devices at once.
MAX_BYTES = 2 * 1024 * 1024

# Magic numbers, checked against the actual bytes. A file claiming to be a PNG
# is not one; a file starting with the PNG signature is.
SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)

_ID_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif)$")


class AvatarError(ValueError):
    """Something a player did, phrased for a player to read."""


def _sniff(blob: bytes):
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp", "image/webp"
    for signature, ext, mime in SIGNATURES:
        if blob.startswith(signature):
            return ext, mime
    return None, None


def save(blob: bytes) -> str:
    """Store an uploaded image and return its id. Raises AvatarError for
    anything a player can fix by choosing a different file."""
    if not blob:
        raise AvatarError("That file was empty.")
    if len(blob) > MAX_BYTES:
        raise AvatarError(
            f"That image is {len(blob) // 1024 // 1024}MB. The limit is "
            f"{MAX_BYTES // 1024 // 1024}MB — try a smaller one.")

    ext, _ = _sniff(blob)
    if not ext:
        raise AvatarError("That doesn't look like an image. PNG, JPEG, GIF or WebP.")

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    avatar_id = f"{secrets.token_hex(16)}.{ext}"
    (AVATAR_DIR / avatar_id).write_bytes(blob)
    return avatar_id


def path(avatar_id: str):
    """Resolve an id to a file, or None.

    The id arrives in a URL, so it is untrusted: only names this module
    generates are served, which is what keeps a crafted id inside the directory.
    """
    if not avatar_id or not _ID_RE.match(avatar_id):
        return None
    found = AVATAR_DIR / avatar_id
    return found if found.exists() else None


def mimetype(avatar_id: str) -> str:
    ext = (avatar_id or "").rsplit(".", 1)[-1]
    return {"png": "image/png", "jpg": "image/jpeg",
            "gif": "image/gif", "webp": "image/webp"}.get(ext, "application/octet-stream")


def set_for_character(character_id: int, blob: bytes) -> str:
    """Give a character a face, and forget the one it had."""
    avatar_id = save(blob)
    previous = (character.get_character(character_id) or {}).get("avatar")

    with character._campaign_con() as con:
        con.execute("UPDATE characters SET avatar=? WHERE id=?", (avatar_id, character_id))

    # The old one is unreachable the moment the row changes; leaving it behind
    # would quietly fill the disk over a campaign's worth of second thoughts.
    if previous:
        stale = path(previous)
        if stale:
            try:
                stale.unlink()
            except OSError:
                pass

    return avatar_id


def initials(name: str) -> str:
    """What to show before anyone has uploaded anything.

    An empty circle reads as broken; two letters read as a character who simply
    hasn't picked a picture yet.
    """
    parts = [p for p in re.split(r"[\s_-]+", (name or "").strip()) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()

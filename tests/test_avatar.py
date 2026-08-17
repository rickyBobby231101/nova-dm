import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import avatar, character

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(avatar, "AVATAR_DIR", tmp_path / "avatars")
    if os.path.exists(character.CAMPAIGN_DB_PATH):
        os.remove(character.CAMPAIGN_DB_PATH)
    yield
    if os.path.exists(character.CAMPAIGN_DB_PATH):
        os.remove(character.CAMPAIGN_DB_PATH)


def _a_character():
    player = character.create_player("Chazel")
    return character.create_character(
        player_id=player["id"], name="Ferrick", race="halfling", class_="rogue",
        ability_scores={"str": 9, "dex": 17, "con": 12, "int": 13, "wis": 11, "cha": 14},
    )


def test_the_bytes_decide_the_format_not_the_filename():
    """Filename and Content-Type both come from whoever is uploading."""
    for blob, ext in [(PNG, "png"), (JPEG, "jpg"), (GIF, "gif"), (WEBP, "webp")]:
        assert avatar.save(blob).endswith("." + ext)


def test_something_that_is_not_an_image_is_refused():
    with pytest.raises(avatar.AvatarError, match="doesn't look like an image"):
        avatar.save(b"<?php echo 'hello'; ?>")
    with pytest.raises(avatar.AvatarError, match="empty"):
        avatar.save(b"")


def test_a_png_extension_on_a_script_does_not_help_it():
    """The check is on content, so a name can't smuggle anything past it."""
    with pytest.raises(avatar.AvatarError):
        avatar.save(b"#!/bin/sh\nrm -rf /\n")


def test_something_too_large_is_refused_with_a_size_a_person_can_act_on():
    with pytest.raises(avatar.AvatarError, match="limit is"):
        avatar.save(PNG + b"\x00" * avatar.MAX_BYTES)


def test_the_uploaded_name_never_reaches_the_filesystem():
    stored = avatar.save(PNG)
    assert "/" not in stored and ".." not in stored
    assert avatar.path(stored).parent == avatar.AVATAR_DIR


def test_a_crafted_id_cannot_walk_out_of_the_directory():
    """The id arrives in a URL, so it is untrusted."""
    assert avatar.path("../../../etc/passwd") is None
    assert avatar.path("../secrets.json") is None
    assert avatar.path("") is None
    assert avatar.path("not-a-real-id.png") is None


def test_an_id_that_is_well_formed_but_absent_is_not_served():
    assert avatar.path("a" * 32 + ".png") is None


def test_giving_a_character_a_face_records_it_on_the_sheet():
    char = _a_character()

    stored = avatar.set_for_character(char["id"], PNG)

    assert character.get_character(char["id"])["avatar"] == stored
    assert avatar.path(stored) is not None


def test_replacing_a_portrait_does_not_leave_the_old_one_behind():
    """A campaign's worth of second thoughts would otherwise fill the disk."""
    char = _a_character()
    first = avatar.set_for_character(char["id"], PNG)

    second = avatar.set_for_character(char["id"], JPEG)

    assert character.get_character(char["id"])["avatar"] == second
    assert avatar.path(first) is None, "the replaced image should be gone"


def test_a_character_starts_with_no_portrait():
    assert _a_character()["avatar"] is None


def test_initials_stand_in_until_someone_uploads_something():
    """An empty circle reads as broken; two letters read as a character who
    simply hasn't picked a picture."""
    assert avatar.initials("Ferrick Underbough") == "FU"
    assert avatar.initials("Chazel") == "CH"
    assert avatar.initials("The Weaver") == "TW"
    assert avatar.initials("") == "?"
    assert avatar.initials(None) == "?"

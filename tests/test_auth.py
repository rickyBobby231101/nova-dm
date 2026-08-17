import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import auth


@pytest.fixture(autouse=True)
def isolated_secrets(monkeypatch, tmp_path):
    """Never touch the real secrets file -- a test run must not rotate the code
    Daniel just read out to somebody."""
    monkeypatch.setattr(auth, "SECRETS_PATH", tmp_path / "nova-dm" / "secrets.json")
    auth.reset_cache()
    yield
    auth.reset_cache()


def test_secrets_are_created_on_first_use():
    code = auth.join_code()

    assert auth.SECRETS_PATH.exists()
    assert len(code) == auth.JOIN_CODE_LENGTH
    assert len(auth.dm_password()) == auth.DM_PASSWORD_LENGTH
    assert len(auth.secret_key()) == 64  # 32 bytes of hex


def test_the_file_is_not_world_readable():
    auth.join_code()
    mode = stat.S_IMODE(auth.SECRETS_PATH.stat().st_mode)
    assert mode == 0o600


def test_codes_avoid_characters_that_get_misread():
    """These are dictated over a call and typed on a phone."""
    for _ in range(40):
        auth.reset_cache()
        auth.SECRETS_PATH.unlink(missing_ok=True)
        assert not (set(auth.join_code()) & set("01OIl5S"))


def test_secrets_survive_a_restart():
    first = auth.join_code()
    key = auth.secret_key()

    auth.reset_cache()  # as if the process restarted

    assert auth.join_code() == first
    assert auth.secret_key() == key, "a changed key logs out every seated player"


def test_a_missing_field_is_filled_without_rotating_the_others():
    """Adding a secret later must not silently reissue the ones in use."""
    code = auth.join_code()
    data = json.loads(auth.SECRETS_PATH.read_text())
    del data["dm_password"]
    auth.SECRETS_PATH.write_text(json.dumps(data))
    auth.reset_cache()

    assert auth.dm_password()  # regenerated
    assert auth.join_code() == code  # untouched


def test_checks_accept_the_real_thing_and_refuse_the_rest():
    assert auth.check_join_code(auth.join_code())
    assert auth.check_dm_password(auth.dm_password())

    assert not auth.check_join_code("NOPE12")
    assert not auth.check_join_code("")
    assert not auth.check_join_code(None)
    # the two secrets are not interchangeable
    assert not auth.check_join_code(auth.dm_password())
    assert not auth.check_dm_password(auth.join_code())


def test_checks_forgive_how_a_human_retypes_a_code():
    code = auth.join_code()

    assert auth.check_join_code(code.lower())
    assert auth.check_join_code(f"  {code}  ")
    assert auth.check_join_code(f"{code[:3]}-{code[3:]}")
    assert auth.check_join_code(f"{code[:3]} {code[3:]}")


def test_rotate_changes_one_secret_and_leaves_the_rest():
    old_code = auth.join_code()
    old_dm = auth.dm_password()
    old_key = auth.secret_key()

    new_code = auth.rotate("join_code")

    assert new_code != old_code
    assert auth.check_join_code(new_code)
    assert not auth.check_join_code(old_code)
    assert auth.dm_password() == old_dm
    assert auth.secret_key() == old_key


def test_rotate_persists():
    new_code = auth.rotate("join_code")
    auth.reset_cache()
    assert auth.join_code() == new_code


def test_rotate_refuses_anything_it_does_not_own():
    with pytest.raises(ValueError):
        auth.rotate("secret_key")  # rotating this logs everyone out; not via here
    with pytest.raises(ValueError):
        auth.rotate("nonsense")


def test_a_corrupt_file_is_replaced_rather_than_crashing_the_app():
    """The alternative is a game that won't boot because a file got truncated."""
    auth.SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    auth.SECRETS_PATH.write_text("{ this is not json")
    auth.reset_cache()

    assert len(auth.join_code()) == auth.JOIN_CODE_LENGTH

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Point the campaign database somewhere disposable BEFORE anything under engine/
# is imported, because engine.character reads it at module level.
#
# This is load-bearing, not tidiness. The suite deletes the campaign database
# between cases, and that file used to be the real one -- the campaign holding
# everybody's characters. It cost a player two characters. A test run must not
# be able to reach the game's own data at all, so the safety is structural
# rather than a rule someone has to remember.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="nova-dm-tests-")
os.environ["NOVA_DM_CAMPAIGN_DB"] = os.path.join(_TEST_DB_DIR, "campaign.sqlite")

# Likewise for the secrets and the synthesized audio: neither belongs in a test
# run, and rotating the real join code mid-session would lock out a live table.
os.environ.setdefault("NOVA_DM_SECRETS", os.path.join(_TEST_DB_DIR, "secrets.json"))
os.environ.setdefault("NOVA_DM_AUDIO_DIR", os.path.join(_TEST_DB_DIR, "narration"))

from engine import character, voice  # noqa: E402  (must follow the env setup above)


def pytest_configure(config):
    """Refuse to run at all if the redirect did not take.

    A wrong path here is not a failing test -- it is data loss, and it would be
    discovered afterwards. Better to stop before the first case runs.
    """
    real = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "db", "campaign.sqlite"))
    in_use = os.path.abspath(character.CAMPAIGN_DB_PATH)
    if in_use == real:
        raise pytest.UsageError(
            f"tests would write to the REAL campaign database ({real}). "
            "engine.character.CAMPAIGN_DB_PATH must honour NOVA_DM_CAMPAIGN_DB."
        )


@pytest.fixture(autouse=True)
def silence_voice(request, monkeypatch):
    """No test may spawn piper or make a real sound.

    engine.voice.speak() hands narration to a background worker, so without this
    a dm/app test would synthesize for real *and* leak its queued work into
    whichever test happened to be running when the worker got to it. test_voice.py
    opts out -- it exercises the module's internals with subprocess patched out.
    """
    if request.node.fspath.basename == "test_voice.py":
        yield
        return
    monkeypatch.setattr(voice, "speak", lambda text: None)
    yield

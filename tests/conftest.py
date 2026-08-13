import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import voice


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

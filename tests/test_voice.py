import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import voice


@pytest.fixture(autouse=True)
def reset_voice_state():
    """_enabled is module-level state shared by the whole app -- don't let one
    test's mute leak into the next."""
    voice.set_enabled(True)
    yield
    voice.set_enabled(True)


@pytest.fixture
def no_audio(monkeypatch):
    """Record what would have been run instead of running it. Nothing in this file
    is ever allowed to actually make a sound."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(voice.subprocess, "run", fake_run)
    return calls


def test_prepare_text_collapses_whitespace_and_strips_markdown():
    assert voice.prepare_text("The  *goblin*\n\nlunges.") == "The goblin lunges."


def test_prepare_text_truncates_runaway_narration():
    assert len(voice.prepare_text("word " * 500)) == voice.MAX_CHARS


def test_prepare_text_handles_empty_and_none():
    assert voice.prepare_text("") == ""
    assert voice.prepare_text(None) == ""


def test_piper_speak_synthesizes_then_plays(monkeypatch, no_audio, tmp_path):
    model = tmp_path / f"{voice.VOICE}.onnx"
    model.write_bytes(b"fake-model")
    monkeypatch.setattr(voice, "VOICES_DIR", tmp_path)
    monkeypatch.setattr(voice.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert voice._piper_speak("The goblin lunges.") is True

    assert no_audio[0][0] == "piper"
    assert str(model) in no_audio[0]
    assert no_audio[1][0] == "aplay"


def test_piper_speak_declines_when_model_missing(monkeypatch, no_audio, tmp_path):
    monkeypatch.setattr(voice, "VOICES_DIR", tmp_path)  # empty dir, no .onnx
    monkeypatch.setattr(voice.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert voice._piper_speak("The goblin lunges.") is False
    assert no_audio == []


def test_piper_speak_cleans_up_its_temp_wav(monkeypatch, tmp_path):
    model = tmp_path / f"{voice.VOICE}.onnx"
    model.write_bytes(b"fake-model")
    monkeypatch.setattr(voice, "VOICES_DIR", tmp_path)
    monkeypatch.setattr(voice.shutil, "which", lambda name: f"/usr/bin/{name}")

    wav_paths = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "piper":
            wav_paths.append(cmd[cmd.index("--output_file") + 1])
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(voice.subprocess, "run", fake_run)
    voice._piper_speak("The goblin lunges.")

    assert wav_paths and not os.path.exists(wav_paths[0])


def test_speak_falls_back_to_espeak_when_piper_unavailable(monkeypatch, no_audio):
    monkeypatch.setattr(voice, "_piper_speak", lambda text: False)
    monkeypatch.setattr(voice.shutil, "which", lambda name: "/usr/bin/espeak-ng")

    voice.speak("The goblin lunges.")
    voice._queue.join()

    assert no_audio[0][0] == "espeak-ng"


def test_speak_is_silent_noop_when_nothing_is_installed(monkeypatch, no_audio):
    monkeypatch.setattr(voice.shutil, "which", lambda name: None)

    voice.speak("The goblin lunges.")  # must not raise
    voice._queue.join()

    assert no_audio == []


def test_speak_does_nothing_when_muted(monkeypatch, no_audio):
    monkeypatch.setattr(voice.shutil, "which", lambda name: "/usr/bin/espeak-ng")
    monkeypatch.setattr(voice, "_piper_speak", lambda text: False)

    voice.set_enabled(False)
    voice.speak("The goblin lunges.")
    voice._queue.join()

    assert no_audio == []


def test_speak_skips_empty_narration(monkeypatch, no_audio):
    monkeypatch.setattr(voice.shutil, "which", lambda name: "/usr/bin/espeak-ng")
    monkeypatch.setattr(voice, "_piper_speak", lambda text: False)

    voice.speak("   \n  ")
    voice._queue.join()

    assert no_audio == []


def test_queue_plays_narration_in_order(monkeypatch):
    """Two turns resolving close together must be heard in submission order, and
    never on top of each other -- the reason this module has a worker queue."""
    spoken = []
    monkeypatch.setattr(voice, "_piper_speak", lambda text: spoken.append(text) or True)

    voice.speak("First the goblin lunges.")
    voice.speak("Then the door slams shut.")
    voice._queue.join()

    assert spoken == ["First the goblin lunges.", "Then the door slams shut."]


def test_worker_survives_a_failing_synthesis(monkeypatch):
    """A crashed worker would silence the rest of the session, so an exception in
    one line must not kill the thread."""
    spoken = []

    def flaky(text):
        if "boom" in text:
            raise RuntimeError("audio device on fire")
        spoken.append(text)
        return True

    monkeypatch.setattr(voice, "_piper_speak", flaky)

    voice.speak("boom")
    voice.speak("The story continues.")
    voice._queue.join()

    assert spoken == ["The story continues."]


def test_available_reports_espeak_when_piper_model_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(voice, "VOICES_DIR", tmp_path)
    monkeypatch.setattr(voice.shutil, "which", lambda name: None if name == "piper" else "/usr/bin/x")
    assert voice.available() is True

    monkeypatch.setattr(voice.shutil, "which", lambda name: None)
    assert voice.available() is False

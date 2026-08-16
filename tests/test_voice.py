import os
import subprocess
import sys
from pathlib import Path

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
    voice.set_broadcast(None)


@pytest.fixture(autouse=True)
def clip_dir(monkeypatch, tmp_path):
    """Never write clips into the real temp dir -- a test run would leave audio
    behind and could prune a running session's clips."""
    monkeypatch.setattr(voice, "AUDIO_DIR", tmp_path / "clips")
    return tmp_path / "clips"


@pytest.fixture(autouse=True)
def no_real_kokoro(monkeypatch):
    """Kokoro's weights are really on this machine, so without this the suite
    would load 311MB of ONNX and synthesize for real. Point it at nothing;
    the tests that exercise Kokoro install their own fake."""
    monkeypatch.setattr(voice, "KOKORO_MODEL", Path("/nonexistent/kokoro.onnx"))
    monkeypatch.setattr(voice, "KOKORO_VOICES", Path("/nonexistent/voices.bin"))
    monkeypatch.setattr(voice, "_kokoro", None)
    monkeypatch.setattr(voice, "_kokoro_failed", False)


@pytest.fixture
def no_audio(monkeypatch):
    """Record what would have been run instead of running it. Nothing in this file
    is ever allowed to actually make a sound.

    piper's job is to leave a wav behind, so the fake creates one: _synthesize
    checks the file exists before trusting a zero exit code.
    """
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "piper" and "--output_file" in cmd:
            open(cmd[cmd.index("--output_file") + 1], "wb").write(b"RIFFfake")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(voice.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def piper_installed(monkeypatch, tmp_path):
    model = tmp_path / f"{voice.VOICE}.onnx"
    model.write_bytes(b"fake-model")
    monkeypatch.setattr(voice, "VOICES_DIR", tmp_path)
    monkeypatch.setattr(voice.shutil, "which", lambda name: f"/usr/bin/{name}")
    return model


def test_prepare_text_collapses_whitespace_and_strips_markdown():
    assert voice.prepare_text("The  *goblin*\n\nlunges.") == "The goblin lunges."


def test_prepare_text_truncates_runaway_narration():
    assert len(voice.prepare_text("word " * 500)) == voice.MAX_CHARS


def test_prepare_text_handles_empty_and_none():
    assert voice.prepare_text("") == ""
    assert voice.prepare_text(None) == ""


def test_synthesize_uses_piper_and_returns_the_clip(no_audio, piper_installed, clip_dir):
    path = voice._synthesize("The goblin lunges.")

    assert path is not None and path.exists() and path.parent == clip_dir
    assert no_audio[0][0] == "piper"
    assert str(piper_installed) in no_audio[0]


def test_synthesize_returns_none_when_no_engine_can_speak(monkeypatch, no_audio, tmp_path):
    monkeypatch.setattr(voice, "VOICES_DIR", tmp_path / "empty")
    (tmp_path / "empty").mkdir()
    monkeypatch.setattr(voice.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert voice._synthesize("The goblin lunges.") is None
    assert no_audio == []


def test_synthesize_leaves_no_wav_behind_when_it_fails(monkeypatch, piper_installed, clip_dir):
    """A half-written clip that nobody can play must not sit in the directory
    waiting to be served."""
    def failing_run(cmd, **kwargs):
        # piper exits non-zero *after* creating the file, which is the messy case
        open(cmd[cmd.index("--output_file") + 1], "wb").write(b"partial")
        return subprocess.CompletedProcess(cmd, 1, b"", b"boom")

    monkeypatch.setattr(voice.subprocess, "run", failing_run)

    assert voice._synthesize("The goblin lunges.") is None
    assert list(clip_dir.glob("*.wav")) == []


# --------------------------------------------------------------------------
# Kokoro: the default engine, with Piper behind it
# --------------------------------------------------------------------------

@pytest.fixture
def kokoro_installed(monkeypatch, tmp_path):
    """A stand-in Kokoro that writes a recognizable wav, so tests can tell which
    engine spoke without loading 311MB of real weights."""
    model_dir = tmp_path / "kokoro"
    model_dir.mkdir()
    (model_dir / "kokoro-v1.0.onnx").write_bytes(b"fake")
    (model_dir / "voices-v1.0.bin").write_bytes(b"fake")
    monkeypatch.setattr(voice, "KOKORO_MODEL", model_dir / "kokoro-v1.0.onnx")
    monkeypatch.setattr(voice, "KOKORO_VOICES", model_dir / "voices-v1.0.bin")

    calls = []

    class FakeKokoro:
        def create(self, text, voice=None, speed=None, lang=None):
            calls.append({"text": text, "voice": voice, "speed": speed})
            import numpy as np
            return np.zeros(2205, dtype="float32"), 22050

    monkeypatch.setattr(voice, "_kokoro", FakeKokoro())
    return calls


def test_kokoro_is_preferred_over_piper(no_audio, piper_installed, kokoro_installed, clip_dir):
    """The whole point of adding Kokoro is that it speaks by default."""
    path = voice._synthesize("The goblin lunges.")

    assert path is not None and path.exists()
    assert kokoro_installed[0]["text"] == "The goblin lunges."
    assert kokoro_installed[0]["voice"] == voice.KOKORO_VOICE
    assert no_audio == []  # piper was never reached


def test_piper_takes_over_when_kokoro_fails(monkeypatch, no_audio, piper_installed,
                                            kokoro_installed):
    """Kokoro is the nicer voice, not a dependency -- a table with a broken
    onnxruntime should still hear a DM."""
    monkeypatch.setattr(voice, "_synthesize_kokoro", lambda text, out: False)

    path = voice._synthesize("The goblin lunges.")

    assert path is not None
    assert no_audio[0][0] == "piper"


def test_engine_override_puts_piper_first(monkeypatch, no_audio, piper_installed,
                                          kokoro_installed):
    monkeypatch.setattr(voice, "TTS_ENGINE", "piper")

    voice._synthesize("The goblin lunges.")

    assert no_audio[0][0] == "piper"
    assert kokoro_installed == []


def test_kokoro_load_failure_is_remembered_not_retried(monkeypatch, tmp_path):
    """Re-importing a broken onnxruntime for every line would make each clip pay
    the same doomed cost before falling through to Piper anyway."""
    import builtins

    model_dir = tmp_path / "kokoro"
    model_dir.mkdir()
    (model_dir / "kokoro-v1.0.onnx").write_bytes(b"fake")
    (model_dir / "voices-v1.0.bin").write_bytes(b"fake")
    monkeypatch.setattr(voice, "KOKORO_MODEL", model_dir / "kokoro-v1.0.onnx")
    monkeypatch.setattr(voice, "KOKORO_VOICES", model_dir / "voices-v1.0.bin")
    monkeypatch.setattr(voice, "_kokoro", None)
    monkeypatch.setattr(voice, "_kokoro_failed", False)

    attempts = []
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "kokoro_onnx":
            attempts.append(name)
            raise ImportError("no onnxruntime here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert voice._kokoro_model() is None
    assert voice._kokoro_model() is None
    assert len(attempts) == 1


# --------------------------------------------------------------------------
# Phase 11: clips reach other devices
# --------------------------------------------------------------------------

def test_deliver_broadcasts_a_clip_and_keeps_the_file(monkeypatch, no_audio, piper_installed,
                                                      clip_dir):
    """A remote player can only hear narration that outlived the turn."""
    monkeypatch.setattr(voice, "SINK", "devices")
    sent = []
    voice.set_broadcast(sent.append)

    voice._deliver("The goblin lunges.")

    assert len(sent) == 1
    assert sent[0]["url"] == f"/narration/{sent[0]['id']}.wav"
    assert sent[0]["text"] == "The goblin lunges."
    assert (clip_dir / f"{sent[0]['id']}.wav").exists()
    # Nothing was played on this box: the sink is other people's devices.
    assert [c[0] for c in no_audio] == ["piper"]


def test_deliver_to_server_sink_plays_locally_and_broadcasts_nothing(
        monkeypatch, no_audio, piper_installed, clip_dir):
    monkeypatch.setattr(voice, "SINK", "server")
    sent = []
    voice.set_broadcast(sent.append)

    voice._deliver("The goblin lunges.")

    assert sent == []
    assert [c[0] for c in no_audio] == ["piper", "aplay"]
    assert list(clip_dir.glob("*.wav")) == []  # not published, so not kept


def test_deliver_to_both_sinks_does_each_once(monkeypatch, no_audio, piper_installed, clip_dir):
    monkeypatch.setattr(voice, "SINK", "both")
    sent = []
    voice.set_broadcast(sent.append)

    voice._deliver("The goblin lunges.")

    assert len(sent) == 1
    assert [c[0] for c in no_audio] == ["piper", "aplay"]
    assert (clip_dir / f"{sent[0]['id']}.wav").exists()


def test_espeak_fallback_is_server_only(monkeypatch, no_audio):
    """espeak writes straight to the sound card, so it cannot serve a remote
    player -- on a devices sink an unavailable piper means silence, not a
    fallback nobody can hear."""
    monkeypatch.setattr(voice.shutil, "which", lambda name: None if name == "piper" else "/usr/bin/x")

    monkeypatch.setattr(voice, "SINK", "devices")
    voice._deliver("The goblin lunges.")
    assert no_audio == []

    monkeypatch.setattr(voice, "SINK", "server")
    voice._deliver("The goblin lunges.")
    assert no_audio[0][0] == "espeak-ng"


def test_missing_broadcast_does_not_strand_a_clip(monkeypatch, no_audio, piper_installed,
                                                  clip_dir):
    """Before app.py registers a sink there is nobody to tell, so the clip is
    rubbish -- it must not accumulate."""
    monkeypatch.setattr(voice, "SINK", "devices")
    voice.set_broadcast(None)

    voice._deliver("The goblin lunges.")

    assert list(clip_dir.glob("*.wav")) == []


def test_prune_keeps_only_the_recent_clips(clip_dir):
    clip_dir.mkdir(parents=True)
    for i in range(voice.KEEP_CLIPS + 5):
        clip = clip_dir / f"{i:032x}.wav"
        clip.write_bytes(b"x")
        os.utime(clip, (i, i))  # oldest first, so pruning order is deterministic

    voice._prune()

    assert len(list(clip_dir.glob("*.wav"))) == voice.KEEP_CLIPS


def test_clip_path_refuses_anything_but_its_own_names(clip_dir):
    """The id arrives from a URL, so it is untrusted."""
    clip_dir.mkdir(parents=True)
    real = clip_dir / f"{'a' * 32}.wav"
    real.write_bytes(b"x")

    assert voice.clip_path("a" * 32) == real
    assert voice.clip_path("../../etc/passwd") is None
    assert voice.clip_path("b" * 32) is None  # well-formed but does not exist
    assert voice.clip_path("") is None
    assert voice.clip_path("short") is None


# --------------------------------------------------------------------------
# Queue behaviour (unchanged guarantees)
# --------------------------------------------------------------------------

def test_speak_is_silent_noop_when_nothing_is_installed(monkeypatch, no_audio):
    monkeypatch.setattr(voice.shutil, "which", lambda name: None)

    voice.speak("The goblin lunges.")  # must not raise
    voice._queue.join()

    assert no_audio == []


def test_speak_does_nothing_when_muted(monkeypatch, no_audio):
    monkeypatch.setattr(voice, "_deliver", lambda text: no_audio.append(["delivered"]))

    voice.set_enabled(False)
    voice.speak("The goblin lunges.")
    voice._queue.join()

    assert no_audio == []


def test_speak_skips_empty_narration(monkeypatch, no_audio):
    monkeypatch.setattr(voice, "_deliver", lambda text: no_audio.append(["delivered"]))

    voice.speak("   \n  ")
    voice._queue.join()

    assert no_audio == []


def test_queue_plays_narration_in_order(monkeypatch):
    """Two turns resolving close together must be heard in submission order, and
    never on top of each other -- the reason this module has a worker queue."""
    spoken = []
    monkeypatch.setattr(voice, "_deliver", spoken.append)

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

    monkeypatch.setattr(voice, "_deliver", flaky)

    voice.speak("boom")
    voice.speak("The story continues.")
    voice._queue.join()

    assert spoken == ["The story continues."]


def test_available_needs_piper_for_devices_but_espeak_will_do_for_the_room(monkeypatch, tmp_path):
    monkeypatch.setattr(voice, "VOICES_DIR", tmp_path)  # no model
    monkeypatch.setattr(voice.shutil, "which", lambda name: None if name == "piper" else "/usr/bin/x")

    monkeypatch.setattr(voice, "SINK", "server")
    assert voice.available() is True

    monkeypatch.setattr(voice, "SINK", "devices")
    assert voice.available() is False  # espeak cannot be sent to a phone

    monkeypatch.setattr(voice.shutil, "which", lambda name: None)
    monkeypatch.setattr(voice, "SINK", "server")
    assert voice.available() is False

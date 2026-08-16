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
    voice.set_broadcast(None)


@pytest.fixture(autouse=True)
def clip_dir(monkeypatch, tmp_path):
    """Never write clips into the real temp dir -- a test run would leave audio
    behind and could prune a running session's clips."""
    monkeypatch.setattr(voice, "AUDIO_DIR", tmp_path / "clips")
    return tmp_path / "clips"


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


def test_piper_speak_synthesizes_then_plays(no_audio, piper_installed):
    assert voice._piper_speak("The goblin lunges.") is True

    assert no_audio[0][0] == "piper"
    assert str(piper_installed) in no_audio[0]
    assert no_audio[1][0] == "aplay"


def test_piper_speak_declines_when_model_missing(monkeypatch, no_audio, tmp_path):
    monkeypatch.setattr(voice, "VOICES_DIR", tmp_path / "empty")
    (tmp_path / "empty").mkdir()
    monkeypatch.setattr(voice.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert voice._piper_speak("The goblin lunges.") is False
    assert no_audio == []


def test_piper_speak_cleans_up_its_wav(no_audio, piper_installed, clip_dir):
    """The server-speaker path keeps nothing: nobody is going to fetch it."""
    voice._piper_speak("The goblin lunges.")
    assert list(clip_dir.glob("*.wav")) == []


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

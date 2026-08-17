"""Phase 4, reworked in Phase 11: the DM speaks -- to every device, not just the room.

Phase 4 synthesized narration with Piper and played it through `aplay` on the
machine running the app: one shared speaker for one shared table. That was the
right call while everyone sat in the same room. Phase 11 moved the table online,
and a speaker in Daniel's house narrates to nobody when the other player is
somewhere else entirely.

So synthesis still happens here and is still serialized through one worker -- two
turns resolving close together must be heard in order and must never overlap --
but the audio is now written to a clip directory and announced to the room, and
each browser fetches and plays it. The ordering guarantee moved with it: clips
are announced in synthesis order and the clients play them one at a time.

The server speaker is kept as an option (NOVA_DM_VOICE_SINK=devices|server|both)
because when everyone *is* in one room, six phones playing the same line a few
hundred milliseconds apart is worse than one speaker was.

Two synthesizers sit behind one seam (_synthesize). Kokoro is the default because
Piper, at any of its voices, still reads synthetic and this is a DM speaking, not
a status line. Piper stays as the fallback and is far faster, so the choice is a
real trade rather than an upgrade: measured here, Piper runs ~0.25x real-time and
Kokoro ~1.3-2.7x depending on voice. Synthesis has always run in the worker and
never blocked a turn, which is the only reason the slower engine is affordable --
speak() still returns immediately.
"""
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
import wave
from pathlib import Path

VOICES_DIR = Path(os.environ.get("NOVA_DM_VOICES_DIR", Path.home() / "cathedral" / "models" / "voices"))
# Piper's voice, used when Kokoro isn't available. lessac reads flat and
# synthetic; amy has noticeably more warmth at the same "medium" cost.
VOICE = os.environ.get("NOVA_DM_VOICE", "en_US-amy-medium")

# Which synthesizer speaks. Kokoro sounds markedly more organic than Piper and
# is what the DM uses by default; Piper stays as the fast fallback. Measured on
# this box: Piper ~0.25x real-time, Kokoro ~1.3-2.7x depending on voice -- so
# Kokoro takes longer to say a line than the line lasts. That is affordable only
# because synthesis already runs in the worker and never blocks a turn.
TTS_ENGINE = os.environ.get("NOVA_DM_TTS", "kokoro").strip().lower()

KOKORO_DIR = Path(os.environ.get("NOVA_DM_KOKORO_DIR", Path.home() / "cathedral" / "models" / "kokoro"))
KOKORO_MODEL = KOKORO_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES = KOKORO_DIR / "voices-v1.0.bin"
KOKORO_VOICE = os.environ.get("NOVA_DM_KOKORO_VOICE", "am_michael")
KOKORO_SPEED = float(os.environ.get("NOVA_DM_KOKORO_SPEED", "1.0"))

# Where synthesized clips live until they're pruned. Deliberately outside the
# repo: these are ephemeral audio, not project files.
AUDIO_DIR = Path(os.environ.get("NOVA_DM_AUDIO_DIR", Path(tempfile.gettempdir()) / "nova-dm-narration"))

# "devices" = browsers play it (the online default), "server" = this box's
# speaker (one room, one speaker), "both" = both.
SINK = os.environ.get("NOVA_DM_VOICE_SINK", "devices").strip().lower()

MAX_CHARS = 800
SYNTH_TIMEOUT = 120  # generous: long narration is ~7s, but a wedged piper must not pin the worker
PLAY_TIMEOUT = 300
KEEP_CLIPS = 20  # a session's recent narration; older clips are pruned as new ones land

log = logging.getLogger(__name__)

_queue: queue.Queue = queue.Queue()
_worker = None
_worker_lock = threading.Lock()
_enabled = True
_broadcast = None
_kokoro = None
_kokoro_lock = threading.Lock()
_kokoro_failed = False


def set_broadcast(fn):
    """Register how a finished clip reaches the players.

    app.py passes a function that emits over SocketIO. Keeping it a callback is
    what lets this module stay ignorant of Flask -- importing the app here would
    be circular, and the queue worker is not in a request context anyway.
    """
    global _broadcast
    _broadcast = fn


def set_enabled(on: bool):
    """Room-wide mute, owned by the server so every device follows one state.
    Per-device mute is a client-side concern -- see player.html."""
    global _enabled
    _enabled = bool(on)


def is_enabled() -> bool:
    return _enabled


def sink() -> str:
    return SINK


def _sink_includes(target: str) -> bool:
    return SINK in (target, "both")


def prepare_text(text: str) -> str:
    """Strip the bits of prose that sound wrong read aloud, and cap the length so
    one runaway narration can't hold the speaker for minutes."""
    text = re.sub(r"[*_`#]", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_CHARS]


def voice_model() -> Path | None:
    model = VOICES_DIR / f"{VOICE}.onnx"
    return model if model.exists() else None


def clip_path(clip_id: str) -> Path | None:
    """Resolve a clip id to a file, refusing anything that isn't a plain clip.

    The id goes into a URL, so it is untrusted input: only hex names produced by
    _synthesize are served, which keeps path traversal out of the audio route.
    """
    if not clip_id or not re.fullmatch(r"[0-9a-f]{32}", clip_id):
        return None
    path = AUDIO_DIR / f"{clip_id}.wav"
    return path if path.exists() else None


def _prune():
    """Keep the clip directory from growing for the length of a campaign."""
    try:
        clips = sorted(AUDIO_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in clips[KEEP_CLIPS:]:
        try:
            stale.unlink()
        except OSError:
            pass


def kokoro_available() -> bool:
    return KOKORO_MODEL.exists() and KOKORO_VOICES.exists()


def _kokoro_model():
    """Load the Kokoro model once and keep it.

    Loading costs ~1.6s and 311MB of ONNX weights, which is fine once per
    process and absurd once per line. A failure is remembered rather than
    retried: if the weights are missing or onnxruntime won't import, every
    later clip should fall straight through to Piper instead of paying the
    import cost again for the same answer.
    """
    global _kokoro, _kokoro_failed
    if _kokoro is not None or _kokoro_failed:
        return _kokoro
    with _kokoro_lock:
        if _kokoro is None and not _kokoro_failed:
            try:
                from kokoro_onnx import Kokoro
                _kokoro = Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
            except Exception:
                _kokoro_failed = True
    return _kokoro


def _write_wav(path: Path, samples, sample_rate: int):
    """Kokoro hands back floats; browsers and aplay want 16-bit PCM."""
    import numpy as np
    with wave.open(str(path), "w") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes((np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes())


def _synthesize_kokoro(text: str, out: Path) -> bool:
    if not kokoro_available():
        return False
    model = _kokoro_model()
    if model is None:
        return False
    try:
        samples, sample_rate = model.create(
            text, voice=KOKORO_VOICE, speed=KOKORO_SPEED, lang="en-us"
        )
        _write_wav(out, samples, sample_rate)
        return out.exists()
    except Exception:
        # Any synthesis failure is a fallback, not a crash -- Piper is right
        # there. Logged because "the nicer voice quietly stopped being used" is
        # otherwise indistinguishable from nothing being wrong.
        log.exception("kokoro synthesis failed; falling back")
        return False


def _synthesize_piper(text: str, out: Path) -> bool:
    if not shutil.which("piper"):
        return False
    model = voice_model()
    if not model:
        return False
    try:
        proc = subprocess.run(
            ["piper", "--model", str(model), "--output_file", str(out)],
            input=text.encode(),
            capture_output=True,
            timeout=SYNTH_TIMEOUT,
        )
        return proc.returncode == 0 and out.exists()
    except (OSError, subprocess.SubprocessError):
        return False


def _synthesize(text: str) -> Path | None:
    """Render text to a wav in AUDIO_DIR, or None if no engine could.

    The engine choice lives behind this one seam so the queue, the broadcast,
    the routes and the client never learn which synthesizer spoke.
    """
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    out = AUDIO_DIR / f"{uuid.uuid4().hex}.wav"

    engines = [_synthesize_kokoro, _synthesize_piper]
    if TTS_ENGINE == "piper":
        engines.reverse()

    for engine in engines:
        try:
            if engine(text, out):
                return out
        except Exception:
            log.exception("%s raised", engine.__name__)
        out.unlink(missing_ok=True)

    log.warning("no synthesizer could speak (engine=%s, kokoro=%s, piper=%s)",
                TTS_ENGINE, kokoro_available(), bool(shutil.which("piper") and voice_model()))
    return None


def _play_local(path: Path) -> bool:
    """Play on this machine's speaker. Blocking on purpose: the worker queue's
    whole job is keeping narration in order."""
    if not shutil.which("aplay"):
        return False
    try:
        subprocess.run(["aplay", "-q", str(path)], capture_output=True, timeout=PLAY_TIMEOUT)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _espeak_speak(text: str) -> bool:
    """Last-resort voice, and server-only -- espeak writes straight to the sound
    card, so a remote player gets nothing from it. Better than a silent table
    when someone is actually sitting here."""
    if not shutil.which("espeak-ng"):
        return False
    try:
        subprocess.run(
            ["espeak-ng", "-s", "160", text],
            capture_output=True,
            timeout=PLAY_TIMEOUT,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _deliver(text: str):
    """One narration, to whichever sinks are configured."""
    path = _synthesize(text)

    if path is None:
        if _sink_includes("server"):
            _espeak_speak(text)
        return

    published = False
    try:
        if _sink_includes("devices") and _broadcast:
            # Announced before local playback: a remote player should not wait
            # out the length of the clip playing in someone else's room.
            _broadcast({"id": path.stem, "url": f"/narration/{path.stem}.wav", "text": text})
            published = True
            _prune()
        if _sink_includes("server"):
            _play_local(path)
    finally:
        if not published:
            path.unlink(missing_ok=True)


def _run_worker():
    while True:
        text = _queue.get()
        try:
            _deliver(text)
        except Exception:
            # A silent table is a bad turn; a crashed worker is a broken game --
            # so this still swallows. But it no longer swallows *quietly*: a DM
            # that has gone mute is the hardest kind of bug to chase when the
            # only evidence is the absence of a sound.
            log.exception("narration failed for: %.60s", text)
        finally:
            _queue.task_done()


def _ensure_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_worker, daemon=True)
            _worker.start()


def speak(text: str):
    """Queue narration to be spoken aloud. Returns immediately; never raises."""
    if not _enabled:
        return
    prepared = prepare_text(text)
    if not prepared:
        return
    _ensure_worker()
    _queue.put(prepared)


def available() -> bool:
    """Whether anything will actually be heard -- used by app.py to report status.

    Kokoro and Piper can both produce a clip a browser will fetch; espeak only
    counts when this box's own speaker is in play, because it writes to the
    sound card and has nothing to send.
    """
    if kokoro_available() or (shutil.which("piper") and voice_model()):
        return True
    return bool(_sink_includes("server") and shutil.which("espeak-ng"))

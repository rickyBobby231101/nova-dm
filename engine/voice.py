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

Measured on this machine: ~6.7s to synthesize ~27s of speech, so synthesis is far
too slow to sit inline in a turn -- speak() always returns immediately.
"""
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

VOICES_DIR = Path(os.environ.get("NOVA_DM_VOICES_DIR", Path.home() / "cathedral" / "models" / "voices"))
VOICE = os.environ.get("NOVA_DM_VOICE", "en_US-lessac-medium")

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

_queue: queue.Queue = queue.Queue()
_worker = None
_worker_lock = threading.Lock()
_enabled = True
_broadcast = None


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


def _synthesize(text: str) -> Path | None:
    """Render text to a wav in AUDIO_DIR. Returns None if Piper can't do it."""
    if not shutil.which("piper"):
        return None
    model = voice_model()
    if not model:
        return None

    out = None
    try:
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        out = AUDIO_DIR / f"{uuid.uuid4().hex}.wav"
        proc = subprocess.run(
            ["piper", "--model", str(model), "--output_file", str(out)],
            input=text.encode(),
            capture_output=True,
            timeout=SYNTH_TIMEOUT,
        )
        if proc.returncode != 0 or not out.exists():
            if out:
                out.unlink(missing_ok=True)
            return None
        return out
    except (OSError, subprocess.SubprocessError):
        if out:
            out.unlink(missing_ok=True)
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


def _piper_speak(text: str) -> bool:
    """Server-speaker path: synthesize, play here, keep nothing.

    Still used when the sink is server-only, and by the espeak fallback decision.
    """
    path = _synthesize(text)
    if path is None:
        return False
    try:
        return _play_local(path)
    finally:
        path.unlink(missing_ok=True)


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
            # A silent table is a bad turn; a crashed worker is a broken game.
            pass
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

    Piper is what can reach a browser; espeak only counts when this box's own
    speaker is in play, because it cannot produce a clip to send.
    """
    if shutil.which("piper") and voice_model():
        return True
    return bool(_sink_includes("server") and shutil.which("espeak-ng"))

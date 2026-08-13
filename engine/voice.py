"""Phase 4: the DM speaks. Narration is synthesized locally with Piper and played
out of the machine running the app -- that box is the table's speaker, so there's
one shared voice for the room rather than audio streamed to six phones.

Ported from the Piper -> temp WAV -> aplay pattern already proven in Nova
Cathedral's nova/modules/voice.py, minus everything nova-dm doesn't need (no STT,
no voice downloading, no pyttsx3). One difference on purpose: narration goes
through a single-consumer queue rather than a bare lock, because two turns
resolving close together must be heard in order and must never overlap.

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
from pathlib import Path

VOICES_DIR = Path(os.environ.get("NOVA_DM_VOICES_DIR", Path.home() / "cathedral" / "models" / "voices"))
VOICE = "en_US-lessac-medium"

MAX_CHARS = 800
SYNTH_TIMEOUT = 120  # generous: long narration is ~7s, but a wedged piper must not pin the worker
PLAY_TIMEOUT = 300

_queue: queue.Queue = queue.Queue()
_worker = None
_worker_lock = threading.Lock()
_enabled = True


def set_enabled(on: bool):
    """Room-wide mute. The speaker is across the room from the players, so the
    toggle has to be reachable from a phone -- app.py exposes it over SocketIO."""
    global _enabled
    _enabled = bool(on)


def is_enabled() -> bool:
    return _enabled


def prepare_text(text: str) -> str:
    """Strip the bits of prose that sound wrong read aloud, and cap the length so
    one runaway narration can't hold the speaker for minutes."""
    text = re.sub(r"[*_`#]", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_CHARS]


def voice_model() -> Path | None:
    model = VOICES_DIR / f"{VOICE}.onnx"
    return model if model.exists() else None


def _piper_speak(text: str) -> bool:
    """Synthesize with Piper and play it. Returns False if unavailable, so the
    caller can fall back."""
    if not shutil.which("piper") or not shutil.which("aplay"):
        return False
    model = voice_model()
    if not model:
        return False

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        proc = subprocess.run(
            ["piper", "--model", str(model), "--output_file", tmp_path],
            input=text.encode(),
            capture_output=True,
            timeout=SYNTH_TIMEOUT,
        )
        if proc.returncode != 0:
            return False
        # Blocking play: the queue's whole job is keeping narration in order.
        subprocess.run(["aplay", "-q", tmp_path], capture_output=True, timeout=PLAY_TIMEOUT)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


def _espeak_speak(text: str) -> bool:
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


def _run_worker():
    while True:
        text = _queue.get()
        try:
            if not _piper_speak(text):
                _espeak_speak(text)
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
    """Whether anything will actually be heard -- used by app.py to report status."""
    if shutil.which("piper") and shutil.which("aplay") and voice_model():
        return True
    return bool(shutil.which("espeak-ng"))

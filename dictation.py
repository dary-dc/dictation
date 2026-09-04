#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "sounddevice",
#     "soundfile",
#     "numpy",
#     "groq",
#     "deepgram-sdk",
#     "PySide6-Essentials",
# ]
# ///
"""Push-to-talk dictation for Wayland/GNOME.

Records your microphone for as long as you want, transcribes with Groq Whisper
(batch) or Deepgram Nova streaming (realtime partials), and drops the text on
your clipboard ready to paste.

Batch mode uses the Bridge ensemble when keys allow: Groq whisper + Deepgram
nova-3 transcribe the same WAV in parallel, and a Gemini judge merges the best
wording when they disagree (see bridge/ARCHITECTURE.md).

Usage:
    uv run dictation.py toggle    # start if idle, stop+transcribe if recording  (recommended)
    uv run dictation.py start     # begin recording (alias: record)
    uv run dictation.py stop      # stop recording, transcribe, copy to clipboard
    uv run dictation.py resend    # re-transcribe or re-copy the last recording
    uv run dictation.py doctor    # check that everything is set up correctly

Options (override config.toml):
    --notify {all,minimal,errors,none}   how chatty the desktop notifications are
    -q, --quiet                          no notifications at all (= --notify none)
    --debug                              verbose logs to stderr + ~/.local/state/dictation/debug.log

Bind a single key to `toggle`, or two keys to `start` and `stop`.

Configuration (first match wins for the API key):
    1. $GROQ_API_KEY environment variable
    2. ~/.config/dictation/config.toml  (recommended — GNOME shortcuts don't see your shell env)
    3. ./config.toml next to this script
See config.example.toml for the format.
"""

from __future__ import annotations

import argparse
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

SAMPLE_RATE = 16_000          # Whisper expects 16 kHz; mono keeps uploads tiny (~32 KB/s)
MIN_AUDIO_BYTES = 8_000       # ignore clips shorter than ~0.25 s (accidental double-press)
STOP_TIMEOUT = 6.0            # seconds to wait for the recorder to flush & exit
TRANSCRIBE_RETRIES = 2        # extra attempts on transient API/network errors
TRANSCRIBE_RETRY_DELAY = 1.5  # seconds between retries
RESEND_HINT = "Super+Shift+D to resend last recording."
DG_KEEPALIVE_SEC = 4.0        # Deepgram WebSocket keepalive interval
PARTIAL_NOTIFY_SEC = 0.8      # throttle live partial desktop notifications
JUDGE_TIMEOUT_SEC = 6.0       # cap on the Gemini ensemble-judge call
ENSEMBLE_GRACE_SEC = 6.0      # extra wait for stragglers once one engine has answered
ENSEMBLE_MIN_WAIT_SEC = 20.0  # floor on waiting for the *first* engine to answer
ENSEMBLE_MAX_WAIT_SEC = 90.0  # ceiling on the same, for very long recordings

# Whisper pads trailing silence with YouTube-ish boilerplate; strip it from a
# transcription's tail. Kept conservative — only phrases nobody dictates.
# Longer variants are listed in full because the guard below allows a marker
# only 8 characters of slack: "subtitles by" alone never matched the phrase it
# was added for, "Subtitles by Amara.org" being 22 characters long.
HALLUCINATED_TAILS = (
    "thank you for watching",
    "thanks for watching",
    "see you in the next video",
    "subtitles by",
    "amara org",
    "subtitles by amara org",
    "subtitles by the amara org community",
)

# Which notification categories each level is allowed to show.
#   start   = "recording began" cue
#   done    = "transcribed, ready to paste" cue
#   chatter = routine info ("transcribing…", "nothing recording", …)
#   problem = warnings and errors
NOTIFY_LEVELS = {
    "all": {"start", "done", "chatter", "problem"},
    "minimal": {"start", "done", "problem"},
    "errors": {"problem"},
    "none": set(),
}

LOG_MAX_BYTES = 1_000_000  # rotate ~/.local/state/dictation/debug.log at ~1 MB

# Runtime knobs, set once by configure() from config + CLI flags.
_notify_level = "all"
_debug = False


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def runtime_dir() -> Path:
    """A fast, auto-cleaned directory for the in-progress recording and PID file."""
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    d = Path(base) / "dictation"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_dir() -> Path:
    """Persistent per-user state (debug log, optional transcript history)."""
    d = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "dictation"
    d.mkdir(parents=True, exist_ok=True)
    return d


AUDIO_PATH = runtime_dir() / "recording.wav"
TRANSCRIPT_PATH = runtime_dir() / "transcript.txt"
PID_PATH = runtime_dir() / "recorder.pid"
MODE_PATH = runtime_dir() / "recorder.mode"  # "simple" | "full", set at start
OVERLAY_STATE = state_dir() / "overlay.json"  # last overlay window geometry
LAST_WAV_PATH = state_dir() / "last.wav"
LAST_TXT_PATH = state_dir() / "last.txt"
LAST_META_PATH = state_dir() / "last.json"


# --------------------------------------------------------------------------- #
# Config & runtime configuration
# --------------------------------------------------------------------------- #
def _read_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except Exception:
        return {}


def _clean_api_key(raw: object) -> str:
    key = str(raw or "").strip()
    if not key or "PASTE_YOUR_KEY" in key:
        return ""
    return key


def resolve_deepgram_key(cfg: dict) -> str:
    """Deepgram key from env, dictation config, or Bridge's config.toml."""
    key = _clean_api_key(os.environ.get("DEEPGRAM_API_KEY"))
    if key:
        return key
    key = _clean_api_key(cfg.get("deepgram_api_key"))
    if key:
        return key
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return _clean_api_key(_read_toml(config_home / "bridge" / "config.toml").get("deepgram_api_key"))


def resolve_gemini_key(cfg: dict) -> str:
    """Gemini key (ensemble judge) from env, config, Bridge config, or web-agent .env."""
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        key = _clean_api_key(os.environ.get(var))
        if key:
            return key
    key = _clean_api_key(cfg.get("gemini_api_key"))
    if key:
        return key
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    key = _clean_api_key(_read_toml(config_home / "bridge" / "config.toml").get("gemini_api_key"))
    if key:
        return key
    env_file = Path.home() / "Desktop/projects/web-agent/.env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("GEMINI_API_KEY="):
                return _clean_api_key(line.split("=", 1)[1].strip().strip("\"'"))
    except OSError:
        pass
    return ""


def load_config() -> dict:
    cfg: dict = {}
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    for candidate in (
        config_home / "dictation" / "config.toml",
        Path(__file__).resolve().parent / "config.toml",
    ):
        if candidate.is_file():
            cfg = _read_toml(candidate)
            break

    api_key = os.environ.get("GROQ_API_KEY") or cfg.get("api_key", "")
    if "PASTE_YOUR_KEY" in api_key:  # unedited example placeholder counts as "no key"
        api_key = ""

    backend = str(cfg.get("stt_backend", "groq")).lower()
    if backend not in ("groq", "deepgram"):
        backend = "groq"

    default_model = "nova-3" if backend == "deepgram" else "whisper-large-v3-turbo"
    model = str(cfg.get("model", default_model))
    if backend == "deepgram" and model.startswith("whisper"):
        model = "nova-3"

    keyterms = [str(t).strip() for t in cfg.get("keyterms", []) if str(t).strip()][:40]

    return {
        "stt_backend": backend,
        "api_key": api_key,
        "deepgram_api_key": resolve_deepgram_key(cfg),
        "model": model,
        "language": cfg.get("language", "en"),
        "prompt": cfg.get("prompt", ""),
        "keyterms": keyterms,
        # Frameless always-on-top window with live transcription while you
        # speak (groq backend). Falls back to plain recording if Qt is broken.
        "overlay": bool(cfg.get("overlay", True)),
        "overlay_font_size": int(cfg.get("overlay_font_size", 11)),
        # Lab mode: keep the WAV + a per-engine comparison record for each
        # dictation (last 20) under ~/.local/state/dictation/lab, including a
        # single-pass baseline for quality comparison. Review: dictation.py lab
        "lab": bool(cfg.get("lab", True)),
        # Bridge-style ensemble for the groq (batch) backend: two engines over
        # the same WAV, Gemini judge on disagreement. Fails open per key.
        "ensemble": bool(cfg.get("ensemble", True)),
        "ensemble_model": str(cfg.get("ensemble_model", "whisper-large-v3")),
        "judge_model": str(cfg.get("judge_model", "gemini-flash-lite-latest")),
        "gemini_api_key": resolve_gemini_key(cfg),
        "save_history": bool(cfg.get("save_history", False)),
        "save_last_recording": bool(cfg.get("save_last_recording", True)),
        "notifications": str(cfg.get("notifications", "all")).lower(),
        "debug": bool(cfg.get("debug", False)),
    }


def configure(level: str, debug: bool) -> None:
    """Set the global notification level and debug flag (CLI flags > config)."""
    global _notify_level, _debug
    if level in NOTIFY_LEVELS:
        _notify_level = level
    _debug = debug


def log(msg: str) -> None:
    """Timestamped line to the rolling log file; also to stderr under --debug.

    The file is written whether or not --debug is set. A keyboard-driven tool
    fails when nobody is watching a terminal, and --debug is never already on
    at the moment it matters — so the log has to be there before the fact.
    Rotates at LOG_MAX_BYTES, keeping one previous file.
    """
    from datetime import datetime

    line = f"{datetime.now().isoformat(timespec='milliseconds')} [pid {os.getpid()}] {msg}"
    if _debug:
        print(line, file=sys.stderr)
    try:
        path = state_dir() / "debug.log"
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_name("debug.log.1"))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def log_debug(msg: str) -> None:
    """Debug-only line. For hot paths — the PortAudio callback must not do file
    I/O on every block, or logging itself would cause the dropouts it reports."""
    if _debug:
        log(msg)


# --------------------------------------------------------------------------- #
# Desktop integration: notifications & clipboard
# --------------------------------------------------------------------------- #
def notify(title: str, body: str = "", icon: str = "audio-input-microphone",
           category: str = "chatter", *, force: bool = False) -> None:
    """Send a desktop notification if the current level permits the category.

    Safe with arbitrary text (no shell). Every notification is logged in debug
    mode, even when the level suppresses it. ``force=True`` always shows — for
    explicit user actions like resend where silence reads as "broken".
    """
    allowed = force or category in NOTIFY_LEVELS.get(_notify_level, set())
    log(f"notify[{category}] {'show' if allowed else 'suppressed'}: {title} — {body}")
    if not allowed or not shutil.which("notify-send"):
        return
    cmd = [
        "notify-send",
        "--app-name", "Dictation",
        # Make successive dictation notifications replace each other instead of stacking.
        "-h", "string:x-canonical-private-synchronous:dictation",
    ]
    if icon:
        cmd += ["-i", icon]
    cmd += [title, body]
    try:
        subprocess.run(cmd, check=False)
    except Exception:
        pass


# Given no --type, wl-copy guesses the MIME type from the content with file(1),
# and the guess is wrong for ordinary prose: a transcript starting with "From "
# looks like an mbox, so the selection advertises only "application/mbox" and
# every normal paste target — which asks for text/plain — silently gets nothing.
# Naming the type also makes wl-copy offer the text/plain;charset=utf-8, TEXT,
# STRING and UTF8_STRING aliases. xclip/xsel don't sniff, so they need no flag.
CLIPBOARD_TYPE = "text/plain"


def _wayland_clipboard_holds(text: str) -> bool:
    """Read the clipboard back and confirm it serves `text` as plain text.

    Fails open: only a successful read of *different* content, or wl-paste
    refusing the type (exactly how the mbox mis-sniff shows up), counts as a
    failure. A missing tool, timeout or crash leaves the copy reported as ok.
    """
    if not shutil.which("wl-paste"):
        return True
    try:
        proc = subprocess.run(
            ["wl-paste", "--no-newline", "--type", CLIPBOARD_TYPE],
            capture_output=True, timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"clipboard: verify skipped ({exc})")
        return True
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        log(f"clipboard: verify failed rc={proc.returncode} {err}")
        return False
    got = proc.stdout.decode("utf-8", "replace")
    if got.strip() != text.strip():
        log(f"clipboard: verify mismatch — holds {len(got)} chars, expected {len(text)}")
        return False
    return True


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the clipboard. Prefers Wayland (wl-copy), falls back to X11."""
    candidates = [
        ("wl-copy", ["wl-copy", "--type", CLIPBOARD_TYPE]),
        ("xclip", ["xclip", "-selection", "clipboard"]),
        ("xsel", ["xsel", "--clipboard", "--input"]),
    ]
    for tool, cmd in candidates:
        if shutil.which(tool):
            try:
                # wl-copy/xclip/xsel fork a daemon to keep serving the selection.
                # Detach it and send its stdio to /dev/null so it never holds an
                # inherited pipe open (which would hang a parent capturing output).
                subprocess.run(
                    cmd, input=text.encode("utf-8"), check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except subprocess.CalledProcessError as exc:
                log(f"clipboard: {tool} failed: {exc}")
                continue
            if tool == "wl-copy" and not _wayland_clipboard_holds(text):
                log("clipboard: wl-copy reported success but clipboard is unusable")
                return False
            log(f"clipboard: copied {len(text)} chars via {tool}")
            return True
    log("clipboard: no working tool found")
    return False


# --------------------------------------------------------------------------- #
# PID file helpers
# --------------------------------------------------------------------------- #
def _process_alive(pid: int) -> bool:
    """True if pid exists and is running. A zombie (exited, not yet reaped) still
    answers os.kill(pid, 0), so check /proc state and treat 'Z' as not alive."""
    try:
        os.kill(pid, 0)  # signal 0 = existence check
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else (shouldn't happen)
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            state = fh.read().rpartition(")")[2].split()[0]
        return state != "Z"
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False


def _process_start_ticks(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat: when the process started, in clock ticks.

    A pid alone is not an identity. pids are recycled — a busy machine walks the
    whole default pid_max in a day — so a recorder that died without cleaning up
    (OOM, SIGKILL, a crash) leaves a PID file naming a number some unrelated
    process later owns. Pairing the pid with its start time tells our recorder
    from a stranger wearing its number.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rpartition(")")[2].split()[19]
    except (OSError, IndexError):
        return None


def _cmdline_is_dictation(pid: int) -> bool:
    """Last-resort identity check: does this process's command line name us?

    Only reached for a PID file with no recorded start time — one written by a
    version before that existed. A pid alone is worth nothing, so check what the
    process actually is rather than trusting the number.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return b"dictation" in fh.read().lower()
    except OSError:
        return False


def _is_our_recorder(pid: int, started: str) -> bool:
    """True if `pid` is a live process that is the recorder the PID file meant."""
    if not _process_alive(pid):
        return False
    if started != "-":
        return _process_start_ticks(pid) == started
    return _cmdline_is_dictation(pid)


def _slot_record(pid: int) -> str:
    """What the PID file holds: the pid, and the identity that survives a recycle."""
    return f"{pid} {_process_start_ticks(pid) or '-'}"


def _slot_owner() -> int | None:
    """The pid the PID file names, alive or not. None if absent or unreadable."""
    try:
        return int(PID_PATH.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def active_recorder_pid() -> int | None:
    """Return the PID of a live recorder, or None (cleaning up stale PID files).

    A file we cannot parse is stale by definition: claim_recorder_slot()
    publishes name and contents in one step, so a half-written claim is never
    visible here — anything unreadable is leftover junk, and leaving it in place
    would jam every later claim against it.
    """
    try:
        fields = PID_PATH.read_text().split()
        pid = int(fields[0])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, IndexError):
        log("clearing an unreadable recorder pid file")
        PID_PATH.unlink(missing_ok=True)
        return None
    if not _is_our_recorder(pid, fields[1] if len(fields) > 1 else "-"):
        PID_PATH.unlink(missing_ok=True)  # dead recorder, or its pid reused by a stranger
        return None
    return pid


def claim_recorder_slot() -> bool:
    """Atomically claim the recorder slot. False if a live recorder already holds it.

    os.link() makes the claim one indivisible step, and publishes a file whose
    contents were complete before its name existed. Both halves matter:

    - Checking active_recorder_pid() and then writing is not atomic: the gap
      spans the sounddevice/Qt imports (~1 s), so two presses of the toggle key
      both passed the check and both recorded. The loser overwrote the PID file,
      orphaning the winner — a recorder still holding the microphone that no
      stop/toggle could ever reach.
    - O_CREAT|O_EXCL claims the name but leaves the file empty until the write a
      moment later. A recorder that died in that window (SIGKILL, a full disk)
      left a file nothing could read and nothing would clear, so every later
      claim lost to it: "Already recording", forever, with nothing recording.
    """
    tmp = PID_PATH.with_name(f"{PID_PATH.name}.{os.getpid()}")
    try:
        tmp.write_text(_slot_record(os.getpid()))
        for _ in range(2):
            try:
                os.link(tmp, PID_PATH)
            except FileExistsError:
                # Either a live recorder owns it, or it is stale — active_recorder_pid()
                # tells the two apart and clears a stale file, so the retry can win it.
                if active_recorder_pid() is not None:
                    return False
                continue
            return True
        return False
    except OSError as exc:
        log(f"recorder slot claim failed: {exc}")
        return False
    finally:
        tmp.unlink(missing_ok=True)


def release_recorder_slot() -> None:
    """Drop the PID file only if it is still ours — never steal another recorder's."""
    if _slot_owner() == os.getpid():
        PID_PATH.unlink(missing_ok=True)


def _release_audio() -> None:
    """Best-effort release of PortAudio/sounddevice so the mic indicator clears."""
    try:
        import sounddevice as sd

        sd.stop()
    except Exception:
        pass


def _wait_recorder_exit(pid: int) -> None:
    """Wait for the recorder process to exit; SIGKILL if it outlives STOP_TIMEOUT."""
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return
        time.sleep(0.05)

    log(f"recorder pid={pid} did not exit in {STOP_TIMEOUT}s; sending SIGKILL")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return

    kill_deadline = time.monotonic() + 2.0
    while time.monotonic() < kill_deadline and _process_alive(pid):
        time.sleep(0.05)


# --------------------------------------------------------------------------- #
# Recorder
# --------------------------------------------------------------------------- #
def record(cfg: dict, simple: bool = False) -> None:
    """Record the microphone until we receive SIGTERM/SIGINT, then exit cleanly.

    `simple` records the plain old way: no overlay, and stop() will run a
    single-pass transcription. The mode is remembered in MODE_PATH so the
    session finishes the way it started, whichever shortcut stops it.
    """
    if not claim_recorder_slot():
        # Only say "already recording" when something actually is: a claim that
        # failed for any other reason (an unwritable runtime dir) is a fault, and
        # reporting it as normal contention is how a broken key looks like a
        # working one.
        if active_recorder_pid():
            notify("🎙️ Dictation", "Already recording.", "audio-input-microphone", "chatter")
        else:
            notify("⚠️ Dictation", f"Could not start — see {state_dir() / 'debug.log'}",
                   "dialog-warning", "problem")
        return

    try:
        MODE_PATH.write_text("simple" if simple else "full")
    except OSError:
        pass

    # The slot is held for the whole session and released here — including on
    # the early returns inside the recorders (e.g. Deepgram with no API key).
    try:
        if simple:
            record_groq(cfg)
        elif cfg["stt_backend"] == "deepgram":
            record_deepgram(cfg)
        elif cfg["overlay"]:
            record_overlay(cfg)
        else:
            record_groq(cfg)
    finally:
        release_recorder_slot()


def record_groq(cfg: dict) -> None:
    """Record mic audio to a WAV file for batch Groq transcription."""
    import sounddevice as sd
    import soundfile as sf

    log(f"recorder starting; input device: {sd.query_devices(kind='input')['name']}")

    stop_event = threading.Event()

    def request_stop(signum, frame):  # noqa: ARG001
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    audio_q: queue.Queue = queue.Queue()

    def callback(indata, frames, time_info, status):  # noqa: ARG001
        if status:
            log_debug(f"audio status: {status}")
        audio_q.put(indata.copy())

    AUDIO_PATH.unlink(missing_ok=True)  # the slot is already claimed by record()
    frames_written = 0

    try:
        with sf.SoundFile(
            AUDIO_PATH, mode="x", samplerate=SAMPLE_RATE, channels=1, subtype="PCM_16"
        ) as audio_file:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, callback=callback):
                # Only announce once capture is truly live — this is the user's "go" cue.
                notify(
                    "🎙️ Recording…",
                    "Speak freely. Press your stop/toggle key when done.",
                    "audio-input-microphone",
                    "start",
                )
                while not stop_event.is_set():
                    try:
                        block = audio_q.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    audio_file.write(block)
                    frames_written += len(block)
                log("stop signal received; flushing buffer")
                # Flush whatever is still buffered before closing the file.
                while True:
                    try:
                        block = audio_q.get_nowait()
                    except queue.Empty:
                        break
                    audio_file.write(block)
                    frames_written += len(block)
        log(f"recorder closed WAV cleanly: {frames_written / SAMPLE_RATE:.2f}s captured")
    except Exception as exc:  # noqa: BLE001
        import traceback
        log("recorder failed:\n" + traceback.format_exc())
        AUDIO_PATH.unlink(missing_ok=True)
        notify("❌ Dictation", f"Recording failed: {exc}", "dialog-error", "problem")
    finally:
        _release_audio()


def record_overlay(cfg: dict) -> None:
    """Record to WAV (for the ensemble) while a frameless always-on-top overlay
    shows the transcription live via Deepgram streaming.

    The overlay is display-only: the clipboard still gets the settled ensemble
    text from stop(). Runs on XWayland (xcb) when possible — the only backend
    where a window can restore its remembered screen position on GNOME Wayland.
    Falls back to plain record_groq() if Qt cannot start.
    """
    try:
        if os.environ.get("DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
            os.environ["QT_QPA_PLATFORM"] = "xcb"
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QColor, QGuiApplication, QTextCharFormat, QTextCursor
        from PySide6.QtWidgets import QApplication, QTextEdit
    except Exception as exc:  # noqa: BLE001
        log(f"overlay unavailable ({exc}); recording without it")
        record_groq(cfg)
        return

    import json

    import numpy as np
    import sounddevice as sd
    import soundfile as sf

    stop_event = threading.Event()

    def request_stop(signum, frame):  # noqa: ARG001
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    AUDIO_PATH.unlink(missing_ok=True)

    events: queue.Queue[tuple] = queue.Queue()  # ("partial"|"final"|"quit", text)
    audio_q: queue.Queue = queue.Queue()

    def callback(indata, frames, time_info, status):  # noqa: ARG001
        if status:
            log_debug(f"audio status: {status}")
        audio_q.put(indata.copy())

    # Live-lane state. The Deepgram connection is strictly display-side and
    # must never delay or endanger the WAV capture, so it connects in its own
    # thread and the recorder just checks conn_box each loop.
    conn_box: list = [None]
    ctx_box: list = [None]
    dg_dead = threading.Event()

    def connect_live() -> None:
        if not cfg["deepgram_api_key"]:
            events.put(("status", "● recording… (no Deepgram key — no live text)"))
            return
        try:
            from deepgram import DeepgramClient
            from deepgram.core.events import EventType

            connect_kwargs: dict = {
                "model": "nova-3",
                "encoding": "linear16",
                "sample_rate": SAMPLE_RATE,
                "channels": 1,
                "interim_results": True,
                "smart_format": True,
                "punctuate": True,
                "endpointing": 300,
            }
            language = (cfg.get("language") or "").strip().lower()
            connect_kwargs["language"] = "multi" if language in ("", "auto") else language
            if cfg.get("keyterms"):
                connect_kwargs["keyterm"] = cfg["keyterms"]

            client = DeepgramClient(api_key=cfg["deepgram_api_key"])
            conn = None
            for attempt in (1, 2):
                if stop_event.is_set():
                    return
                try:
                    ctx = client.listen.v1.connect(**connect_kwargs)
                    conn = ctx.__enter__()
                    break
                except Exception as exc:  # noqa: BLE001
                    log(f"overlay live connect attempt {attempt} failed: {exc}")
            if conn is None:
                events.put(("status", "● recording… (live text unavailable)"))
                return

            def on_message(message) -> None:
                try:
                    if getattr(message, "type", None) != "Results":
                        return
                    channel = getattr(message, "channel", None)
                    alts = getattr(channel, "alternatives", None) if channel else None
                    if not alts:
                        return
                    text = (getattr(alts[0], "transcript", None) or "").strip()
                    if not text:
                        return
                    is_final = bool(getattr(message, "is_final", False))
                    speech_final = bool(getattr(message, "speech_final", False))
                    events.put(("final" if (is_final or speech_final) else "partial", text))
                except Exception as exc:  # noqa: BLE001
                    log(f"overlay dg message error: {exc}")

            conn.on(EventType.MESSAGE, on_message)
            conn.on(EventType.ERROR, lambda e: (log(f"overlay dg error: {e}"), dg_dead.set()))
            conn.on(EventType.CLOSE, lambda _=None: dg_dead.set())
            threading.Thread(
                target=conn.start_listening, name="dictation-overlay-dg", daemon=True
            ).start()
            ctx_box[0] = ctx
            conn_box[0] = conn
            log("overlay live lane connected")
        except Exception as exc:  # noqa: BLE001
            log(f"overlay live lane failed: {exc}")
            events.put(("status", "● recording… (live text unavailable)"))

    def worker() -> None:
        """Audio loop in a background thread; Qt owns the main thread."""
        frames_written = 0
        try:
            with sf.SoundFile(
                AUDIO_PATH, mode="x", samplerate=SAMPLE_RATE, channels=1, subtype="PCM_16"
            ) as audio_file:
                threading.Thread(target=connect_live, name="dictation-overlay-conn", daemon=True).start()
                with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, callback=callback):
                    notify(
                        "🎙️ Recording…",
                        "Speak freely. Press your stop/toggle key when done.",
                        "audio-input-microphone",
                        "start",
                    )
                    last_keepalive = time.monotonic()
                    while not stop_event.is_set():
                        conn = conn_box[0]
                        if conn and not dg_dead.is_set() and time.monotonic() - last_keepalive >= DG_KEEPALIVE_SEC:
                            try:
                                conn.send_keep_alive()
                                last_keepalive = time.monotonic()
                            except Exception:  # noqa: BLE001
                                dg_dead.set()
                        try:
                            block = audio_q.get(timeout=0.2)
                        except queue.Empty:
                            continue
                        audio_file.write(block)
                        frames_written += len(block)
                        if conn and not dg_dead.is_set():
                            pcm = (block * 32767).clip(-32768, 32767).astype(np.int16).tobytes()
                            try:
                                conn.send_media(pcm)
                            except Exception as exc:  # noqa: BLE001
                                log(f"overlay dg send failed: {exc}")
                                dg_dead.set()
                    while True:
                        try:
                            block = audio_q.get_nowait()
                        except queue.Empty:
                            break
                        audio_file.write(block)
                        frames_written += len(block)
                if conn_box[0]:
                    try:
                        conn_box[0].send_close_stream()
                    except Exception:  # noqa: BLE001
                        pass
                if ctx_box[0]:
                    try:
                        ctx_box[0].__exit__(None, None, None)
                    except Exception:  # noqa: BLE001
                        pass
            log(f"overlay recorder closed WAV cleanly: {frames_written / SAMPLE_RATE:.2f}s captured")
        except Exception as exc:  # noqa: BLE001
            import traceback

            log("overlay recorder failed:\n" + traceback.format_exc())
            AUDIO_PATH.unlink(missing_ok=True)
            notify("❌ Dictation", f"Recording failed: {exc}", "dialog-error", "problem")
        finally:
            events.put(("quit", ""))

    # ---- Qt overlay (main thread) ------------------------------------------
    class Overlay(QTextEdit):
        def __init__(self) -> None:
            super().__init__()
            self.setReadOnly(True)
            self.setWindowTitle("Dictation")
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.Tool
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.WindowDoesNotAcceptFocus
            )
            self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.setPlaceholderText("● recording…")
            self.setStyleSheet(
                "QTextEdit { background-color: #0e1013; color: #e6e8ee;"
                " border: 1px solid #23272f; border-radius: 6px; padding: 6px;"
                " selection-background-color: #3d4f6f; }"
                " QScrollBar:vertical { background: transparent; width: 6px; }"
                " QScrollBar::handle:vertical { background: #262b35; border-radius: 3px; }"
                " QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            )
            font = self.font()
            font.setPointSize(max(7, cfg["overlay_font_size"]))
            self.setFont(font)
            self._has_partial = False
            self._drag_from = None
            self._save_timer = QTimer(self)
            self._save_timer.setSingleShot(True)
            self._save_timer.setInterval(400)
            self._save_timer.timeout.connect(self.save_geometry)

        # Drag anywhere (there is no title bar).
        def mousePressEvent(self, event) -> None:  # noqa: N802
            if event.button() == Qt.MouseButton.LeftButton:
                self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

        def mouseMoveEvent(self, event) -> None:  # noqa: N802
            if self._drag_from is not None and event.buttons() & Qt.MouseButton.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag_from)

        def mouseReleaseEvent(self, event) -> None:  # noqa: N802
            self._drag_from = None

        def moveEvent(self, event) -> None:  # noqa: N802
            super().moveEvent(event)
            self._save_timer.start()

        def save_geometry(self) -> None:
            g = self.geometry()
            try:
                OVERLAY_STATE.write_text(
                    json.dumps({"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height()})
                )
            except OSError:
                pass

        def restore_geometry(self) -> None:
            w, h = 520, 150
            x = y = None
            try:
                saved = json.loads(OVERLAY_STATE.read_text())
                x, y = int(saved["x"]), int(saved["y"])
                w, h = int(saved.get("w", w)), int(saved.get("h", h))
            except Exception:  # noqa: BLE001
                pass
            screen = QGuiApplication.primaryScreen()
            avail = screen.availableVirtualGeometry() if screen else None
            if x is None or avail is None:
                if avail is not None:
                    x = avail.center().x() - w // 2
                    y = avail.bottom() - h - 80
                else:
                    x, y = 200, 200
            elif avail is not None:
                x = min(max(x, avail.left()), max(avail.left(), avail.right() - w))
                y = min(max(y, avail.top()), max(avail.top(), avail.bottom() - h))
            self.resize(w, h)
            self.move(x, y)
            # Some WMs apply their own placement on first map — re-assert.
            QTimer.singleShot(80, lambda: self.move(x, y))

        def _clear_partial(self) -> None:
            if not self._has_partial:
                return
            doc = self.document()
            cursor = self.textCursor()
            cursor.setPosition(max(0, doc.lastBlock().position() - 1))
            cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
            self._has_partial = False

        def _append(self, text: str, color: str, partial: bool) -> None:
            self._clear_partial()
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if self.toPlainText():
                cursor.insertText("\n")
            cursor.insertText(text, fmt)
            self._has_partial = partial
            self.setTextCursor(cursor)
            self.ensureCursorVisible()

        def add_partial(self, text: str) -> None:
            self._append(text, "#8b91a0", partial=True)

        def add_final(self, text: str) -> None:
            self._append(text, "#e6e8ee", partial=False)

    app = QApplication.instance() or QApplication([])
    win = Overlay()
    win.restore_geometry()
    win.show()

    def drain() -> None:
        while True:
            try:
                kind, text = events.get_nowait()
            except queue.Empty:
                break
            if kind == "partial":
                win.add_partial(text)
            elif kind == "final":
                win.add_final(text)
            elif kind == "status":
                win.setPlaceholderText(text)
            elif kind == "quit":
                win.save_geometry()
                app.quit()
                return

    # The timer both drains display events and lets Python run its signal
    # handlers (SIGTERM from stop()) while Qt owns the main loop.
    timer = QTimer()
    timer.setInterval(80)
    timer.timeout.connect(drain)
    timer.start()

    # Debug affordance: DICTATION_SHOT=/path.png grabs the overlay after
    # DICTATION_SHOT_MS (default 6000) without quitting — for headless checks.
    shot = os.environ.get("DICTATION_SHOT")
    if shot:
        QTimer.singleShot(
            int(os.environ.get("DICTATION_SHOT_MS", "6000") or 6000),
            lambda: win.grab().save(shot),
        )

    threading.Thread(target=worker, name="dictation-overlay-rec", daemon=True).start()
    try:
        app.exec()
    finally:
        _release_audio()


def record_deepgram(cfg: dict) -> None:
    """Stream mic audio to Deepgram; write accumulated transcript on stop."""
    if not cfg["deepgram_api_key"]:
        notify(
            "❌ Dictation",
            "No Deepgram API key. Add deepgram_api_key to ~/.config/dictation/config.toml "
            "or reuse ~/.config/bridge/config.toml.",
            "dialog-error",
            "problem",
        )
        return

    import numpy as np
    import sounddevice as sd
    from deepgram import DeepgramClient
    from deepgram.core.events import EventType

    log(f"deepgram recorder starting; input: {sd.query_devices(kind='input')['name']}")

    stop_event = threading.Event()

    def request_stop(signum, frame):  # noqa: ARG001
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    audio_q: queue.Queue = queue.Queue()
    finals: list[str] = []
    last_partial = [""]
    last_partial_notify_at = [0.0]

    def callback(indata, frames, time_info, status):  # noqa: ARG001
        if status:
            log_debug(f"audio status: {status}")
        audio_q.put(indata.copy())

    def maybe_notify_partial(text: str) -> None:
        if not text or text == last_partial[0]:
            return
        now = time.monotonic()
        if now - last_partial_notify_at[0] < PARTIAL_NOTIFY_SEC:
            return
        last_partial_notify_at[0] = now
        last_partial[0] = text
        preview = text if len(text) <= 80 else text[:77] + "…"
        notify("🎙️ Live dictation", preview, "audio-input-microphone", "chatter")

    TRANSCRIPT_PATH.unlink(missing_ok=True)

    connect_kwargs: dict = {
        "model": cfg["model"] or "nova-3",
        "encoding": "linear16",
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "interim_results": True,
        "smart_format": True,
        "punctuate": True,
        "endpointing": 300,
    }
    language = (cfg.get("language") or "").strip().lower()
    if language in ("", "auto"):
        connect_kwargs["language"] = "multi"
    else:
        connect_kwargs["language"] = language
    if cfg.get("keyterms"):
        connect_kwargs["keyterm"] = cfg["keyterms"]

    try:
        client = DeepgramClient(api_key=cfg["deepgram_api_key"])
        with client.listen.v1.connect(**connect_kwargs) as connection:
            closed = threading.Event()

            def on_message(message) -> None:
                try:
                    if getattr(message, "type", None) != "Results":
                        return
                    channel = getattr(message, "channel", None)
                    alts = getattr(channel, "alternatives", None) if channel else None
                    if not alts:
                        return
                    text = (getattr(alts[0], "transcript", None) or "").strip()
                    if not text:
                        return
                    is_final = bool(getattr(message, "is_final", False))
                    speech_final = bool(getattr(message, "speech_final", False))
                    if is_final or speech_final:
                        finals.append(text)
                        last_partial[0] = ""
                        log(f"deepgram final: {text[:80]!r}")
                    else:
                        maybe_notify_partial(text)
                except Exception as exc:  # noqa: BLE001
                    log(f"deepgram message error: {exc}")

            def on_close(_msg=None) -> None:
                closed.set()

            def on_error(err) -> None:
                log(f"deepgram error: {err}")
                closed.set()

            connection.on(EventType.MESSAGE, on_message)
            connection.on(EventType.ERROR, on_error)
            connection.on(EventType.CLOSE, on_close)

            listen_thread = threading.Thread(
                target=connection.start_listening,
                name="dictation-dg-listen",
                daemon=True,
            )
            listen_thread.start()

            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, callback=callback):
                notify(
                    "🎙️ Recording (live)…",
                    "Deepgram is transcribing as you speak. Press stop/toggle when done.",
                    "audio-input-microphone",
                    "start",
                )
                last_keepalive = time.monotonic()
                while not stop_event.is_set() and not closed.is_set():
                    now = time.monotonic()
                    if now - last_keepalive >= DG_KEEPALIVE_SEC:
                        try:
                            connection.send_keep_alive()
                            last_keepalive = now
                        except Exception as exc:  # noqa: BLE001
                            log(f"deepgram keepalive failed: {exc}")
                            break

                    try:
                        block = audio_q.get(timeout=0.15)
                    except queue.Empty:
                        continue

                    pcm = (block * 32767).clip(-32768, 32767).astype(np.int16).tobytes()
                    try:
                        connection.send_media(pcm)
                    except Exception as exc:  # noqa: BLE001
                        log(f"deepgram send failed: {exc}")
                        break

                log("stop signal received; closing Deepgram stream")
                while True:
                    try:
                        block = audio_q.get_nowait()
                    except queue.Empty:
                        break
                    pcm = (block * 32767).clip(-32768, 32767).astype(np.int16).tobytes()
                    try:
                        connection.send_media(pcm)
                    except Exception:
                        break

                try:
                    connection.send_close_stream()
                except Exception:
                    pass
                listen_thread.join(timeout=2.0)

        text = " ".join(finals).strip()
        if last_partial[0] and last_partial[0] not in text:
            text = f"{text} {last_partial[0]}".strip() if text else last_partial[0]
        TRANSCRIPT_PATH.write_text(text, encoding="utf-8")
        log(f"deepgram transcript: {len(text)} chars")
    except Exception as exc:  # noqa: BLE001
        import traceback
        log("deepgram recorder failed:\n" + traceback.format_exc())
        TRANSCRIPT_PATH.unlink(missing_ok=True)
        notify("❌ Dictation", f"Deepgram recording failed: {exc}", "dialog-error", "problem")
    finally:
        _release_audio()


# --------------------------------------------------------------------------- #
# Transcription
# --------------------------------------------------------------------------- #
def transcribe(cfg: dict, audio_path: Path, model: str | None = None) -> str:
    from groq import Groq

    client = Groq(api_key=cfg["api_key"])
    kwargs = {
        "file": (audio_path.name, audio_path.read_bytes()),
        "model": model or cfg["model"],
        "response_format": "text",
    }
    language = (cfg.get("language") or "").strip()
    if language and language.lower() != "auto":
        kwargs["language"] = language
    if cfg.get("prompt"):
        kwargs["prompt"] = cfg["prompt"]

    log(f"groq request: model={kwargs['model']} language={language or 'auto'} "
        f"bytes={audio_path.stat().st_size}")
    started = time.monotonic()
    result = str(client.audio.transcriptions.create(**kwargs)).strip()
    log(f"groq responded in {time.monotonic() - started:.2f}s; {len(result)} chars")
    return result


# Apostrophes are dropped rather than spaced: engines disagree about them
# constantly ("let's" vs "lets"), and a split into "let s" is the difference
# between two engines agreeing outright and paying for a judge call that can
# rewrite the text. Every other punctuation mark becomes a space, so
# "state-of-the-art" still matches "state of the art".
_APOSTROPHES = "'\u2019\u02bc"


def norm_text(text: str) -> str:
    """Compare transcripts ignoring case, punctuation, and whitespace."""
    kept = "".join(
        "" if ch in _APOSTROPHES else ch if ch.isalnum() or ch.isspace() else " "
        for ch in text.lower()
    )
    return " ".join(kept.split())


def strip_hallucinated_tail(text: str) -> str:
    """Drop trailing sentences that are known ASR silence-boilerplate."""
    import re

    sentences = re.split(r"(?<=[.!?…])\s+", text.strip())
    while sentences:
        last = norm_text(sentences[-1])
        # Only drop sentences that are essentially the bare boilerplate —
        # "thank you for watching over my plants" must survive.
        if last and any(
            marker in last and len(last) <= len(marker) + 8 for marker in HALLUCINATED_TAILS
        ):
            log(f"stripped hallucinated tail: {sentences[-1]!r}")
            sentences.pop()
        else:
            break
    return " ".join(sentences).strip()


def transcribe_deepgram_batch(cfg: dict, audio_path: Path) -> str:
    """Second opinion: Deepgram nova-3 prerecorded over the same WAV."""
    import json
    import urllib.parse
    import urllib.request

    params = [("model", "nova-3"), ("smart_format", "true"), ("punctuate", "true")]
    language = (cfg.get("language") or "").strip().lower()
    if language and language != "auto":
        params.append(("language", language))
    for term in cfg.get("keyterms", []):
        params.append(("keyterm", term))
    req = urllib.request.Request(
        "https://api.deepgram.com/v1/listen?" + urllib.parse.urlencode(params),
        data=audio_path.read_bytes(),
        headers={
            "Authorization": f"Token {cfg['deepgram_api_key']}",
            "Content-Type": "audio/wav",
        },
    )
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    text = data["results"]["channels"][0]["alternatives"][0]["transcript"].strip()
    log(f"deepgram batch responded in {time.monotonic() - started:.2f}s; {len(text)} chars")
    return text


def judge_transcripts(cfg: dict, hyps: list[tuple[str, str]]) -> str:
    """One Gemini call merging engine hypotheses. Empty string on any failure."""
    import json
    import urllib.request

    labels = "ABC"
    parts = [
        "Fix speech-recognition errors in one dictated passage.",
        f"{len(hyps)} independent automatic transcriptions of the same audio:",
    ]
    for i, (_name, text) in enumerate(hyps):
        parts.append(f"{labels[i]}: {text}")
    if cfg.get("prompt"):
        parts.append(f"Domain: {cfg['prompt']}")
    if cfg.get("keyterms"):
        parts.append(f"Glossary (correct spellings): {', '.join(cfg['keyterms'])}")
    parts.append(
        "Reply with ONLY the corrected text. Reconstruct what was actually "
        "spoken: prefer wording supported by the majority of transcriptions. "
        "A phrase appearing in only one transcription is suspect — especially "
        "trailing boilerplate (e.g. 'thank you for watching'): drop it. Do not "
        "drop content that appears in two or more. Never paraphrase, reorder, "
        "or add anything that is in none of them."
    )
    body: dict = {
        "contents": [{"parts": [{"text": "\n".join(parts)}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 2048},
    }
    if "2.5" in cfg["judge_model"]:
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{cfg['judge_model']}:generateContent",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": cfg["gemini_api_key"]},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=JUDGE_TIMEOUT_SEC) as resp:
            data = json.load(resp)
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as exc:  # noqa: BLE001
        log(f"judge error: {exc}")
        return ""
    log(f"judge {cfg['judge_model']} responded in {time.monotonic() - started:.2f}s")
    # Distrust answers that ballooned — no verdict is safer than a bad one.
    longest = max(len(t) for _n, t in hyps)
    if not text or len(text) > 2 * longest + 80:
        return ""
    return text


def ensemble_transcribe(cfg: dict, audio_path: Path) -> tuple[str, str, dict]:
    """Three engines vote, Gemini judges only genuine disagreement.

    Voters: Groq whisper-large-v3, Groq turbo (fast, nearly free), Deepgram
    nova-3. Hallucinated tails are stripped from every hypothesis. If two
    voters agree the result is used directly (no judge latency, no invention
    risk); a straggler is abandoned ENSEMBLE_GRACE_SEC after the first
    answer lands, never before one has. Returns
    (text, how, report) — the report feeds the lab (dictation.py lab).
    """
    started = time.monotonic()
    results: dict[str, str] = {}
    errors: dict[str, str] = {}
    report: dict = {"engines": {}}
    # Engine threads and the abandon sweep below both write these, so the
    # "has this engine been written off yet?" check and the write that follows
    # it have to be one step — otherwise a result can land in `results` after
    # the sweep has already recorded the engine as abandoned.
    lock = threading.Lock()

    def run(name: str, fn) -> None:
        t0 = time.monotonic()
        try:
            text = fn()
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors[name] = str(exc)
                if name not in report["engines"]:
                    report["engines"][name] = {
                        "seconds": round(time.monotonic() - t0, 2),
                        "error": str(exc),
                    }
            log(f"ensemble {name} failed: {exc}")
            return
        # A timed-out engine may finish late — don't mutate the report then.
        with lock:
            if name not in report["engines"]:
                results[name] = text
                report["engines"][name] = {
                    "seconds": round(time.monotonic() - t0, 2),
                    "text": text,
                }

    ens_model = cfg["ensemble_model"]
    fast_model = cfg["model"]
    names = [f"groq {ens_model}", "deepgram nova-3"]
    # daemon: an abandoned engine's answer is discarded anyway, so it must not
    # keep the process alive after the text is already on the clipboard.
    threads = [
        threading.Thread(target=run, args=(names[0], lambda: transcribe(cfg, audio_path, ens_model)), daemon=True),
        threading.Thread(target=run, args=(names[1], lambda: transcribe_deepgram_batch(cfg, audio_path)), daemon=True),
    ]
    if fast_model != ens_model:
        names.append(f"groq {fast_model}")
        threads.append(
            threading.Thread(
                target=run, args=(names[2], lambda: transcribe(cfg, audio_path, fast_model)), daemon=True
            )
        )
    for t in threads:
        t.start()

    # Two-phase wait. A single flat deadline made long dictations fail outright:
    # on a 392 s recording every engine was still uploading at 6 s, all three
    # were written off, and ensemble_transcribe raised "all engines failed" —
    # while the text they returned moments later was thrown away. The cap was
    # only ever meant to stop a straggler adding latency to a result already in
    # hand, so it now starts counting from the first answer. Until then we wait,
    # scaled by how much audio there is to upload and decode.
    # The grace clock starts on the first real transcript, never on an engine
    # that merely errored out — otherwise one instant failure (a bad Deepgram
    # key) would cut short the engine that was about to answer correctly.
    # Engines that all fail fast still return fast: the loop ends as soon as no
    # thread is alive, so the wait below only ever applies to a genuine hang.
    audio_seconds = audio_path.stat().st_size / (SAMPLE_RATE * 2)
    first_deadline = time.monotonic() + min(
        ENSEMBLE_MAX_WAIT_SEC, max(ENSEMBLE_MIN_WAIT_SEC, audio_seconds)
    )
    grace_deadline: float | None = None
    while any(t.is_alive() for t in threads):
        now = time.monotonic()
        if grace_deadline is None:
            # A *transcript* starts the clock, not merely a returned engine. An
            # engine that answers instantly with nothing (an empty result — a
            # scope-less Deepgram key returns one) is no more an answer in hand
            # than an error is, and letting it start the grace period abandons
            # the engines still working on the real text six seconds later.
            with lock:
                if any(text.strip() for text in results.values()):
                    grace_deadline = now + ENSEMBLE_GRACE_SEC
        if now >= (grace_deadline if grace_deadline is not None else first_deadline):
            break
        time.sleep(0.05)

    waited = time.monotonic() - started
    with lock:
        for name, t in zip(names, threads):
            if t.is_alive() and name not in report["engines"]:
                report["engines"][name] = {"error": f"abandoned after {waited:.0f}s"}
                log(f"ensemble {name} abandoned after {waited:.0f}s")

    def finish(text: str, how: str) -> tuple[str, str, dict]:
        report["final"] = {"text": text, "via": how}
        report["total_seconds"] = round(time.monotonic() - started, 2)
        return text, how, report

    hyps = [
        (name, strip_hallucinated_tail(results[name]))
        for name in names
        if results.get(name, "").strip()
    ]
    hyps = [(n, t) for n, t in hyps if t]
    if not hyps:
        raise RuntimeError(next(iter(errors.values()), "all engines failed"))
    if len(hyps) == 1:
        return finish(hyps[0][1], f"{hyps[0][0]} only")

    # Majority vote: if any two hypotheses agree (normalized), trust them —
    # no judge call, no invention risk, no extra latency.
    for i in range(len(hyps)):
        for j in range(i + 1, len(hyps)):
            if norm_text(hyps[i][1]) == norm_text(hyps[j][1]):
                return finish(hyps[i][1], f"majority ({hyps[i][0]} + {hyps[j][0]})")

    t0 = time.monotonic()
    verdict = judge_transcripts(cfg, hyps)
    report["judge"] = {"seconds": round(time.monotonic() - t0, 2), "text": verdict or None}
    if verdict:
        return finish(verdict, "judged")
    return finish(hyps[0][1], f"judge unavailable — using {hyps[0][0]}")


LAB_KEEP = 20  # dictation sessions kept for quality debugging


def save_lab_record(audio_path: Path, report: dict, audio_seconds: float) -> None:
    """Keep the WAV + per-engine comparison so real-usage quality is debuggable."""
    import json
    import shutil
    from datetime import datetime

    d = state_dir() / "lab"
    try:
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        report = {"audio_seconds": round(audio_seconds, 1), **report}
        shutil.copy2(audio_path, d / f"{stamp}.wav")
        (d / f"{stamp}.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        for old in sorted(p.stem for p in d.glob("*.json"))[:-LAB_KEEP]:
            (d / f"{old}.json").unlink(missing_ok=True)
            (d / f"{old}.wav").unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        log(f"lab save failed: {exc}")


def lab_show(count: int = 5) -> int:
    """Print the last dictation sessions' engine-by-engine comparison."""
    import json

    d = state_dir() / "lab"
    files = sorted(d.glob("*.json"))[-count:]
    if not files:
        print("No lab records yet — dictate something first (lab = true).")
        return 0
    for f in files:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        print(
            f"\n=== {f.stem} · {r.get('audio_seconds', '?')}s audio · "
            f"total {r.get('total_seconds', '?')}s · via {r.get('final', {}).get('via', '?')}"
        )
        for name, e in r.get("engines", {}).items():
            body = e.get("text") if e.get("text") is not None else f"ERROR: {e.get('error')}"
            print(f"  {name:<26} {e.get('seconds', '?'):>6}s  {body}")
        judge = r.get("judge")
        if judge:
            print(f"  {'judge':<26} {judge.get('seconds', '?'):>6}s  {judge.get('text') or '(no verdict)'}")
        print(f"  {'CLIPBOARD':<26}         {r.get('final', {}).get('text', '')}")
    print(f"\nAudio + records: {d} (last {LAB_KEEP} kept)")
    return 0


def save_history(text: str) -> None:
    from datetime import datetime

    stamp = datetime.now().isoformat(timespec="seconds")
    with open(state_dir() / "history.log", "a", encoding="utf-8") as fh:
        fh.write(f"\n--- {stamp} ---\n{text}\n")


# --------------------------------------------------------------------------- #
# Last recording (recovery slot)
# --------------------------------------------------------------------------- #
def load_last_meta() -> dict:
    try:
        import json

        return json.loads(LAST_META_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_last_meta(meta: dict) -> None:
    import json

    try:
        LAST_META_PATH.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        log(f"last meta save failed: {exc}")


def save_last_recording(wav_path: Path, mode: str) -> None:
    """Atomically persist the latest WAV + metadata; invalidate any old transcript."""
    from datetime import datetime

    try:
        size = wav_path.stat().st_size
        tmp = LAST_WAV_PATH.with_suffix(".wav.tmp")
        shutil.copy2(wav_path, tmp)
        tmp.replace(LAST_WAV_PATH)
        LAST_TXT_PATH.unlink(missing_ok=True)
        save_last_meta({
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "duration_sec": round(size / (SAMPLE_RATE * 2), 1),
            "mode": mode,
            "bytes": size,
            "transcribed_ok": False,
            "clipboard_ok": False,
        })
        log(f"saved last recording: {size} bytes, mode={mode}")
    except OSError as exc:
        log(f"last recording save failed: {exc}")


def save_last_transcript_only(text: str, *, backend: str = "deepgram") -> None:
    """Persist transcript without WAV (Deepgram streaming backend)."""
    from datetime import datetime

    try:
        LAST_TXT_PATH.write_text(text, encoding="utf-8")
        save_last_meta({
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "mode": backend,
            "bytes": 0,
            "transcribed_ok": True,
            "clipboard_ok": False,
        })
    except OSError as exc:
        log(f"last transcript save failed: {exc}")


def mark_transcribed(text: str) -> None:
    try:
        LAST_TXT_PATH.write_text(text, encoding="utf-8")
        meta = load_last_meta()
        meta["transcribed_ok"] = True
        save_last_meta(meta)
    except OSError as exc:
        log(f"mark transcribed failed: {exc}")


def mark_clipboard_ok() -> None:
    meta = load_last_meta()
    meta["clipboard_ok"] = True
    save_last_meta(meta)


def last_wav_matches_meta() -> bool:
    meta = load_last_meta()
    expected = meta.get("bytes")
    if not expected or not LAST_WAV_PATH.is_file():
        return False
    try:
        return LAST_WAV_PATH.stat().st_size == expected
    except OSError:
        return False


def _is_transient_error(exc: Exception) -> bool:
    import urllib.error

    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return True
    status = getattr(exc, "status_code", None)
    if status in (408, 429, 500, 502, 503, 504):
        return True
    msg = str(exc).lower()
    return any(token in msg for token in ("timeout", "connection reset", " temporarily ", "503", "502"))


def use_ensemble_for_mode(cfg: dict, mode: str) -> bool:
    return bool(
        mode != "simple"
        and cfg["ensemble"]
        and cfg["deepgram_api_key"]
        and cfg["gemini_api_key"]
    )


def transcribe_audio(cfg: dict, audio_path: Path, mode: str) -> tuple[str, dict | None]:
    """Transcribe one WAV. Returns (text, lab_report_or_none)."""
    report: dict | None = None
    if use_ensemble_for_mode(cfg, mode):
        text, how, report = ensemble_transcribe(cfg, audio_path)
        log(f"ensemble result via: {how}")
    else:
        text = transcribe(cfg, audio_path)
    return text.strip(), report


def transcribe_with_retry(cfg: dict, audio_path: Path, mode: str) -> tuple[str, dict | None]:
    """Transcribe with automatic retries on transient API/network errors."""
    last_exc: Exception | None = None
    for attempt in range(TRANSCRIBE_RETRIES + 1):
        try:
            return transcribe_audio(cfg, audio_path, mode)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < TRANSCRIBE_RETRIES and _is_transient_error(exc):
                log(f"transient transcribe error (attempt {attempt + 1}): {exc}")
                time.sleep(TRANSCRIBE_RETRY_DELAY)
                continue
            raise
    raise last_exc  # pragma: no cover


def notify_transcribing(cfg: dict, mode: str) -> None:
    if use_ensemble_for_mode(cfg, mode):
        notify("🧠 Dictation", "Transcribing (2 engines + judge)…", "emblem-synchronizing", "chatter")
    else:
        notify("🧠 Dictation", "Transcribing with Groq…", "emblem-synchronizing", "chatter")


def deliver_text(cfg: dict, text: str, *, resend: bool = False) -> bool:
    """Copy text to clipboard, notify, optionally append history. Returns clipboard ok."""
    if cfg["save_history"]:
        try:
            save_history(text)
        except Exception:  # noqa: BLE001
            pass

    if copy_to_clipboard(text):
        mark_clipboard_ok()
        preview = text if len(text) <= 60 else text[:57] + "…"
        title = "📋 Resent — ready to paste" if resend else "📋 Transcribed — ready to paste"
        notify(title, preview, "emblem-ok", "done", force=resend)
        print(text)
        return True

    notify(
        "⚠️ Dictation",
        f"Transcribed, but the clipboard did not take it. {RESEND_HINT}",
        "dialog-warning",
        "problem",
        force=resend,
    )
    print(text)
    return False


def finish_wav_session(
    cfg: dict,
    audio_path: Path,
    mode: str,
    *,
    save_recording: bool,
    resend: bool = False,
) -> None:
    """Transcribe a WAV, persist recovery state, and deliver to the clipboard."""
    size = audio_path.stat().st_size
    if save_recording and cfg["save_last_recording"]:
        save_last_recording(audio_path, mode)

    if not cfg["api_key"]:
        notify(
            "❌ Dictation",
            f"No Groq API key. Add it to ~/.config/dictation/config.toml. {RESEND_HINT}",
            "dialog-error",
            "problem",
        )
        return

    notify_transcribing(cfg, mode)
    try:
        text, report = transcribe_with_retry(cfg, audio_path, mode)
        if report and cfg["lab"]:
            save_lab_record(audio_path, report, size / (SAMPLE_RATE * 2))
    except Exception as exc:  # noqa: BLE001
        import traceback

        log("transcription failed:\n" + traceback.format_exc())
        notify(
            "❌ Dictation",
            f"Transcription error: {exc}. {RESEND_HINT}",
            "dialog-error",
            "problem",
        )
        return

    if not text:
        notify("⚠️ Dictation", "No speech detected.", "dialog-warning", "problem")
        return

    mark_transcribed(text)
    deliver_text(cfg, text, resend=resend)


# --------------------------------------------------------------------------- #
# Stopper
# --------------------------------------------------------------------------- #
def stop(cfg: dict) -> None:
    """Stop the active recorder, transcribe the audio, and copy it to the clipboard."""
    pid = active_recorder_pid()
    if not pid:
        notify("🎙️ Dictation", "Nothing is recording.", "dialog-information", "chatter")
        return

    log(f"stopping recorder pid={pid}")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    _wait_recorder_exit(pid)
    # Safety net for a recorder we had to SIGKILL (it never ran its own cleanup).
    # Scoped to the pid we killed so it can't clear a fresh recorder's claim.
    if _slot_owner() == pid:
        PID_PATH.unlink(missing_ok=True)

    if cfg["stt_backend"] == "deepgram":
        text = TRANSCRIPT_PATH.read_text(encoding="utf-8").strip() if TRANSCRIPT_PATH.exists() else ""
        TRANSCRIPT_PATH.unlink(missing_ok=True)
        log(f"deepgram captured transcript: {len(text)} chars")
        if not text:
            notify("⚠️ Dictation", "No speech detected.", "dialog-warning", "problem")
            return
        if cfg["save_last_recording"]:
            save_last_transcript_only(text)
        mark_transcribed(text)
        deliver_text(cfg, text)
        return

    size = AUDIO_PATH.stat().st_size if AUDIO_PATH.exists() else 0
    log(f"captured audio: {size} bytes")
    if size < MIN_AUDIO_BYTES:
        AUDIO_PATH.unlink(missing_ok=True)
        notify(
            "⚠️ Dictation",
            f"Recording too short — speak longer, then stop. {RESEND_HINT}",
            "dialog-warning",
            "problem",
        )
        return

    try:
        mode = MODE_PATH.read_text().strip()
    except OSError:
        mode = "full"
    MODE_PATH.unlink(missing_ok=True)

    try:
        finish_wav_session(cfg, AUDIO_PATH, mode, save_recording=True)
    finally:
        AUDIO_PATH.unlink(missing_ok=True)


def resend(cfg: dict) -> None:
    """Re-transcribe or re-copy the last saved recording after a failure."""
    if active_recorder_pid():
        notify(
            "🎙️ Dictation",
            "Still recording — stop first.",
            "dialog-warning",
            "problem",
            force=True,
        )
        return

    meta = load_last_meta()
    has_wav = LAST_WAV_PATH.is_file() and LAST_WAV_PATH.stat().st_size >= MIN_AUDIO_BYTES

    if has_wav and last_wav_matches_meta() and LAST_TXT_PATH.is_file() and meta.get("transcribed_ok"):
        text = LAST_TXT_PATH.read_text(encoding="utf-8").strip()
        if text:
            log("resend: copying saved transcript (clipboard-only)")
            notify(
                "🔄 Dictation",
                "Copying last transcript…",
                "emblem-synchronizing",
                "start",
                force=True,
            )
            deliver_text(cfg, text, resend=True)
            return

    if has_wav:
        mode = str(meta.get("mode") or "full")
        log(f"resend: re-transcribing last.wav mode={mode}")
        notify(
            "🔄 Dictation",
            "Re-transcribing last recording…",
            "emblem-synchronizing",
            "start",
            force=True,
        )
        finish_wav_session(cfg, LAST_WAV_PATH, mode, save_recording=False, resend=True)
        return

    if LAST_TXT_PATH.is_file() and meta.get("transcribed_ok"):
        text = LAST_TXT_PATH.read_text(encoding="utf-8").strip()
        if text:
            log("resend: copying saved transcript (no WAV)")
            notify(
                "🔄 Dictation",
                "Copying last transcript…",
                "emblem-synchronizing",
                "start",
                force=True,
            )
            deliver_text(cfg, text, resend=True)
            return

    notify(
        "🎙️ Dictation",
        "No previous recording to resend.",
        "dialog-warning",
        "problem",
        force=True,
    )


# --------------------------------------------------------------------------- #
# Doctor
# --------------------------------------------------------------------------- #
def doctor(cfg: dict) -> bool:
    print("Dictation — setup check\n")
    ok = True

    try:
        import sounddevice as sd
        import soundfile as sf

        print(f"[ok] sounddevice {sd.__version__} · soundfile {sf.__version__}")
        try:
            dev = sd.query_devices(kind="input")
            print(f"[ok] default input device: {dev['name']}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"[!!] no usable input device: {exc}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[!!] audio libraries failed to import: {exc}")

    print(f"[..] stt_backend={cfg['stt_backend']}  model={cfg['model']}  language={cfg['language']}")

    if cfg["stt_backend"] == "deepgram":
        try:
            import deepgram  # noqa: F401

            print("[ok] deepgram-sdk installed")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"[!!] deepgram-sdk failed to import: {exc}")
        if cfg["deepgram_api_key"]:
            key = cfg["deepgram_api_key"]
            masked = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "set"
            print(f"[ok] Deepgram API key: {masked}")
        else:
            ok = False
            print(
                "[!!] no Deepgram API key — set deepgram_api_key in "
                "~/.config/dictation/config.toml, $DEEPGRAM_API_KEY, or Bridge config"
            )
    else:
        try:
            import groq

            print(f"[ok] groq {groq.__version__}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"[!!] groq failed to import: {exc}")
        if cfg["api_key"]:
            key = cfg["api_key"]
            masked = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "set"
            print(f"[ok] Groq API key: {masked}")
        else:
            ok = False
            print("[!!] no Groq API key — set it in ~/.config/dictation/config.toml or $GROQ_API_KEY")

        if cfg["overlay"]:
            live = "with live text" if cfg["deepgram_api_key"] else "no live text (no Deepgram key)"
            print(f"[ok] overlay: on — {live}")
        else:
            print("[--] overlay = false — no live window while recording")

        if cfg["ensemble"] and cfg["deepgram_api_key"] and cfg["gemini_api_key"]:
            print(
                f"[ok] ensemble: groq {cfg['ensemble_model']} + groq {cfg['model']} + "
                f"deepgram nova-3 → majority, else judge {cfg['judge_model']}"
            )
        elif cfg["ensemble"]:
            missing = ", ".join(
                name
                for name, present in (
                    ("Deepgram key", cfg["deepgram_api_key"]),
                    ("Gemini key", cfg["gemini_api_key"]),
                )
                if not present
            )
            print(f"[--] ensemble unavailable (missing {missing}) — single-pass Groq")
        else:
            print("[--] ensemble = false — single-pass Groq")
        if cfg["lab"]:
            print(f"[ok] lab: keeping last {LAB_KEEP} sessions in {state_dir() / 'lab'} — review with: dictation.py lab")

    clip = next((t for t in ("wl-copy", "xclip", "xsel") if shutil.which(t)), None)
    if clip:
        print(f"[ok] clipboard tool: {clip} (copying as {CLIPBOARD_TYPE})")
        if clip == "wl-copy" and not shutil.which("wl-paste"):
            print("[--] wl-paste missing — copies can't be verified after writing")
    else:
        ok = False
        print("[!!] no clipboard tool — install wl-clipboard")

    if shutil.which("notify-send"):
        print("[ok] notify-send found")
    else:
        print("[--] notify-send missing — notifications disabled (install libnotify)")

    print(f"[..] save_history={cfg['save_history']}  save_last_recording={cfg['save_last_recording']}")
    print(f"[..] notifications={_notify_level}  debug={_debug}")
    rec_pid = active_recorder_pid()
    if rec_pid:
        print(f"[..] recording right now (pid {rec_pid}) — stop it with: dictation.py stop")
    if cfg["save_last_recording"] and LAST_WAV_PATH.is_file():
        meta = load_last_meta()
        size = LAST_WAV_PATH.stat().st_size
        print(
            f"[ok] last recording: {size} bytes · {meta.get('duration_sec', '?')}s · "
            f"mode={meta.get('mode', '?')} · resend with Super+Shift+D or: dictation.py resend"
        )
    elif cfg["save_last_recording"]:
        print(f"[..] last recording: none yet — saved to {LAST_WAV_PATH} after each dictation")
    else:
        print("[--] save_last_recording = false — no recovery slot")
    print(f"[..] log={state_dir() / 'debug.log'}")
    print("\nResult:", "READY ✅" if ok else "issues found ⚠️  (fix the [!!] lines above)")
    return ok


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Push-to-talk dictation via Groq Whisper or Deepgram streaming.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="toggle",
        choices=["toggle", "start", "record", "stop", "resend", "doctor", "lab"],
        help="toggle (default), start/record, stop, resend, doctor, or lab (compare recent sessions)",
    )
    parser.add_argument(
        "--simple",
        action="store_true",
        help="plain single-pass dictation: no overlay, no ensemble (the pre-ensemble behavior)",
    )
    parser.add_argument(
        "--notify",
        choices=["all", "minimal", "errors", "none"],
        default=None,
        help="notification level (overrides config; default 'all')",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="suppress all notifications (same as --notify none)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="verbose logging to stderr and ~/.local/state/dictation/debug.log",
    )
    args = parser.parse_args()
    cfg = load_config()

    level = "none" if args.quiet else (args.notify or cfg["notifications"])
    configure(level, args.debug or cfg["debug"])
    log(f"action={args.action} notify_level={_notify_level}")

    if args.action in ("start", "record"):
        record(cfg, simple=args.simple)
    elif args.action == "stop":
        stop(cfg)
    elif args.action == "resend":
        resend(cfg)
    elif args.action == "doctor":
        return 0 if doctor(cfg) else 1
    elif args.action == "lab":
        return lab_show()
    else:  # toggle
        if active_recorder_pid():
            stop(cfg)
        else:
            record(cfg, simple=args.simple)
    return 0


if __name__ == "__main__":
    sys.exit(main())

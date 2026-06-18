#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "sounddevice",
#     "soundfile",
#     "numpy",
#     "groq",
# ]
# ///
"""Push-to-talk dictation for Wayland/GNOME, powered by Groq Whisper.

Records your microphone for as long as you want, transcribes it with Groq,
and drops the text on your clipboard ready to paste.

Usage:
    uv run dictation.py toggle    # start if idle, stop+transcribe if recording  (recommended)
    uv run dictation.py start     # begin recording (alias: record)
    uv run dictation.py stop      # stop recording, transcribe, copy to clipboard
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
PID_PATH = runtime_dir() / "recorder.pid"


# --------------------------------------------------------------------------- #
# Config & runtime configuration
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    cfg: dict = {}
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    for candidate in (
        config_home / "dictation" / "config.toml",
        Path(__file__).resolve().parent / "config.toml",
    ):
        if candidate.is_file():
            with open(candidate, "rb") as fh:
                cfg = tomllib.load(fh)
            break

    api_key = os.environ.get("GROQ_API_KEY") or cfg.get("api_key", "")
    if "PASTE_YOUR_KEY" in api_key:  # unedited example placeholder counts as "no key"
        api_key = ""

    return {
        "api_key": api_key,
        "model": cfg.get("model", "whisper-large-v3-turbo"),
        "language": cfg.get("language", "en"),
        "prompt": cfg.get("prompt", ""),
        "save_history": bool(cfg.get("save_history", False)),
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
    """Write a timestamped line to stderr and the debug log — only when --debug."""
    if not _debug:
        return
    from datetime import datetime

    line = f"{datetime.now().isoformat(timespec='milliseconds')} [pid {os.getpid()}] {msg}"
    print(line, file=sys.stderr)
    try:
        with open(state_dir() / "debug.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Desktop integration: notifications & clipboard
# --------------------------------------------------------------------------- #
def notify(title: str, body: str = "", icon: str = "audio-input-microphone",
           category: str = "chatter") -> None:
    """Send a desktop notification if the current level permits the category.

    Safe with arbitrary text (no shell). Every notification is logged in debug
    mode, even when the level suppresses it.
    """
    allowed = category in NOTIFY_LEVELS.get(_notify_level, set())
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


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the clipboard. Prefers Wayland (wl-copy), falls back to X11."""
    candidates = [
        ("wl-copy", ["wl-copy"]),
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
                log(f"clipboard: copied {len(text)} chars via {tool}")
                return True
            except subprocess.CalledProcessError as exc:
                log(f"clipboard: {tool} failed: {exc}")
                continue
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


def active_recorder_pid() -> int | None:
    """Return the PID of a live recorder, or None (cleaning up stale PID files)."""
    try:
        pid = int(PID_PATH.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    if not _process_alive(pid):
        PID_PATH.unlink(missing_ok=True)  # stale
        return None
    return pid


# --------------------------------------------------------------------------- #
# Recorder
# --------------------------------------------------------------------------- #
def record(cfg: dict) -> None:
    """Record the microphone until we receive SIGTERM/SIGINT, then exit cleanly."""
    if active_recorder_pid():
        notify("🎙️ Dictation", "Already recording.", "audio-input-microphone", "chatter")
        return

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
            log(f"audio status: {status}")
        audio_q.put(indata.copy())

    AUDIO_PATH.unlink(missing_ok=True)
    # Claim the lock as early as possible so a fast second press routes to "stop".
    PID_PATH.write_text(str(os.getpid()))
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
        PID_PATH.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Transcription
# --------------------------------------------------------------------------- #
def transcribe(cfg: dict, audio_path: Path) -> str:
    from groq import Groq

    client = Groq(api_key=cfg["api_key"])
    kwargs = {
        "file": (audio_path.name, audio_path.read_bytes()),
        "model": cfg["model"],
        "response_format": "text",
    }
    language = (cfg.get("language") or "").strip()
    if language and language.lower() != "auto":
        kwargs["language"] = language
    if cfg.get("prompt"):
        kwargs["prompt"] = cfg["prompt"]

    log(f"groq request: model={cfg['model']} language={language or 'auto'} "
        f"bytes={audio_path.stat().st_size}")
    started = time.monotonic()
    result = str(client.audio.transcriptions.create(**kwargs)).strip()
    log(f"groq responded in {time.monotonic() - started:.2f}s; {len(result)} chars")
    return result


def save_history(text: str) -> None:
    from datetime import datetime

    stamp = datetime.now().isoformat(timespec="seconds")
    with open(state_dir() / "history.log", "a", encoding="utf-8") as fh:
        fh.write(f"\n--- {stamp} ---\n{text}\n")


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
    # Ask the recorder to stop, then wait for it to flush and close the WAV file.
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        # The recorder removes its PID file only after finalizing the WAV, so its
        # disappearance is the definitive "done" signal — and unlike os.kill(pid, 0)
        # it isn't fooled by a not-yet-reaped zombie process.
        if not PID_PATH.exists() or not _process_alive(pid):
            break
        time.sleep(0.05)
    else:
        log(f"recorder pid={pid} did not exit in {STOP_TIMEOUT}s; sending SIGKILL")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    PID_PATH.unlink(missing_ok=True)

    size = AUDIO_PATH.stat().st_size if AUDIO_PATH.exists() else 0
    log(f"captured audio: {size} bytes")
    if size < MIN_AUDIO_BYTES:
        AUDIO_PATH.unlink(missing_ok=True)
        notify("⚠️ Dictation", "No audio captured.", "dialog-warning", "problem")
        return

    if not cfg["api_key"]:
        AUDIO_PATH.unlink(missing_ok=True)
        notify(
            "❌ Dictation",
            "No Groq API key. Add it to ~/.config/dictation/config.toml.",
            "dialog-error",
            "problem",
        )
        return

    notify("🧠 Dictation", "Transcribing with Groq…", "emblem-synchronizing", "chatter")
    try:
        text = transcribe(cfg, AUDIO_PATH)
    except Exception as exc:  # noqa: BLE001
        import traceback
        log("groq call failed:\n" + traceback.format_exc())
        notify("❌ Dictation", f"Groq error: {exc}", "dialog-error", "problem")
        return
    finally:
        AUDIO_PATH.unlink(missing_ok=True)

    if not text:
        notify("⚠️ Dictation", "No speech detected.", "dialog-warning", "problem")
        return

    if copy_to_clipboard(text):
        preview = text if len(text) <= 60 else text[:57] + "…"
        notify("📋 Transcribed — ready to paste", preview, "emblem-ok", "done")
    else:
        notify(
            "⚠️ Dictation",
            "Transcribed, but no clipboard tool found (install wl-clipboard).",
            "dialog-warning",
            "problem",
        )

    if cfg["save_history"]:
        try:
            save_history(text)
        except Exception:  # noqa: BLE001
            pass

    print(text)


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

    try:
        import groq

        print(f"[ok] groq {groq.__version__}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[!!] groq failed to import: {exc}")

    clip = next((t for t in ("wl-copy", "xclip", "xsel") if shutil.which(t)), None)
    if clip:
        print(f"[ok] clipboard tool: {clip}")
    else:
        ok = False
        print("[!!] no clipboard tool — install wl-clipboard")

    if shutil.which("notify-send"):
        print("[ok] notify-send found")
    else:
        print("[--] notify-send missing — notifications disabled (install libnotify)")

    if cfg["api_key"]:
        key = cfg["api_key"]
        masked = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "set"
        print(f"[ok] Groq API key: {masked}")
    else:
        ok = False
        print("[!!] no Groq API key — set it in ~/.config/dictation/config.toml or $GROQ_API_KEY")

    print(f"[..] model={cfg['model']}  language={cfg['language']}  save_history={cfg['save_history']}")
    print(f"[..] notifications={_notify_level}  debug={_debug}  log={state_dir() / 'debug.log'}")
    print("\nResult:", "READY ✅" if ok else "issues found ⚠️  (fix the [!!] lines above)")
    return ok


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Push-to-talk dictation via Groq Whisper.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="toggle",
        choices=["toggle", "start", "record", "stop", "doctor"],
        help="toggle (default), start/record, stop, or doctor",
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
        record(cfg)
    elif args.action == "stop":
        stop(cfg)
    elif args.action == "doctor":
        return 0 if doctor(cfg) else 1
    else:  # toggle
        if active_recorder_pid():
            stop(cfg)
        else:
            record(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())

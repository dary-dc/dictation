# TODO & known limitations

Things worth doing to this project later, and things deliberately left as they
are. Every entry says *why* it matters and how you would know it is finished —
an item nobody can act on is just a worry with a bullet point.

Nothing here is blocking. The tool works; this is the list of what would make it
better, roughly in the order I would do it.

References are function names (they survive edits better than line numbers);
`file:line` is a starting point, not a promise.

---

## 1. Recording starts a moment after the key

**The one real quality issue left.** The microphone only goes live after the
process starts and imports its audio libraries. Measured from a real session log:

```
08:47:23.960 action=toggle  →  08:47:24.255 mic live   (0.30s, simple path)
22:58:18.761 action=toggle  →  22:58:19.278 mic live   (0.52s, overlay path)
```

and that is measured from the tool's *own first log line* — the key press is
earlier still, by however long `uv run` plus interpreter startup takes
(`time uv run dictation.py doctor` measures it on a given machine). Anything
spoken in that window is not recorded. The usual symptom is a first word that
never made it, or a short take that transcribes as one stray word.

**What would fix it:** a small resident process that holds PortAudio open and
keeps a rolling buffer, with the shortcut only signalling it. Press-to-live
drops to milliseconds and the first syllable survives.

**Why it hasn't been done:** it is a new component with a real cost — a
long-lived process holding the microphone device, its own lifecycle (start on
login, restart after a crash, release the mic when idle), and a socket or
signal protocol between shortcut and daemon. Everything else in this project is
a script you can read end to end; this is the change that ends that.

**Done when:** press-to-first-sample is under ~50 ms, the daemon releases the
device when idle, `doctor` reports whether it is running, and killing it mid-
dictation still leaves the audio recoverable (the same guarantee `stop()` has
now).

**Cheaper half-measures, if the daemon is too much:**
- Start capturing before the overlay is built (the Qt import is the slow half of
  the overlay path) so full mode is no worse than simple mode.
- Ship a pre-warm command run at login, so the first press of the day is not the
  slowest one.

## 2. Two things to check on the machine, not in the code

Neither is a bug — both showed up in a real log and are worth confirming.

- **The ensemble may not be running for the key you actually use.** `Super+D` is
  bound to `toggle --simple` (single pass, no overlay, no vote); `Super+W` is the
  full path. A log where nearly every line says `mode=simple` means the ensemble,
  the overlay and the judge are never reached. Rebind or use `Super+W` if that
  isn't what you wanted.
- **Deepgram may be returning nothing.** One observed ensemble run:
  `deepgram batch responded in 0.95s; 0 chars` for 3.58 s of speech, and the
  result fell back to a single engine. A fast, empty answer looks like a key
  without the right scope rather than a transcription failure. Take one `Super+W`
  dictation and run `uv run dictation.py lab` to see whether nova-3 ever returns
  text. If it never does, the "three engines" are two.

## 3. Smaller code items

Each of these is an hour or less.

- **`history.log` grows without bound** (`save_history`, dictation.py:1806).
  Only when `save_history = true`, and it is text, so it grows slowly — but
  nothing ever trims it. The debug log rotates; this should too.
- **Log rotation races between processes** (`log`, dictation.py:280). Two
  processes can both decide to rotate at the same moment; one `debug.log.1`
  overwrites the other. Rare, and it costs old log lines rather than anything
  live. A lock file, or accept it and write it down (this note is the writing
  down).
- **The judge timeout is flat** (`JUDGE_TIMEOUT_SEC`, dictation.py:68). 6 s
  regardless of how much text the judge was given. The engine wait was fixed to
  scale with audio length; this one wasn't. Scale it the same way, or drop the
  judge for very long transcripts.
- **Transient-error classification is string matching** (`_is_transient_error`,
  dictation.py:1904). It reads exception text for "timeout", "503" and friends.
  It works, but it is guessing at the SDKs' wording; each SDK exposes a status
  code that would be better.
- **Hallucination markers are maintained by hand** (`HALLUCINATED_TAILS`,
  dictation.py:78, used by `strip_hallucinated_tail`, dictation.py:1521). A
  marker only matches a sentence within 8 characters of its own length, so every
  new variant Whisper invents needs adding — that guard is why
  "Subtitles by Amara.org" went unstripped for so long. The 8 is a guess worth
  revisiting with real lab data.
- **Tuning constants are not configurable** (`AUDIO_STALL_SEC` 3 s,
  `MAX_RESCUE_SEC` 30 min, `CLIPBOARD_HANDOFF_SEC` 3 s). All are judgement calls
  that have never been tested against a slow machine. If one of them proves
  wrong for a setup, it should be config rather than a patch.
- **Long orphans are left on disk** (`preserve_recording`, dictation.py:676). A
  capture longer than `MAX_RESCUE_SEC` is deliberately not put in the recovery
  slot, so it sits in the runtime dir until logout clears it. `doctor` reports
  it, but nothing offers to transcribe or delete it — a `dictation.py recover`
  that lists waiting recordings and acts on one would close the loop.
- **Errors could survive Do Not Disturb.** Failure notifications no longer get
  replaced by routine ones, but `notify-send -u critical` would also make them
  outlast DND. That is deliberately intrusive — worth it only if a failure is
  ever missed *because* of DND.

## 4. Known limitations (deliberate — revisit only if they bite)

- **The streaming backend keeps no audio.** `record_deepgram`
  (dictation.py:1290) writes only text, so if that connection fails the only
  thing that can be preserved is what Deepgram had already settled. The WAV
  backends can always be re-transcribed; this one cannot. Recording a parallel
  WAV would fix it at the cost of the thing that makes streaming cheap.
- **The recovery slot holds exactly one recording.** `Super+Shift+D` recovers
  the last one. Two failures in a row and the first is gone. A small ring of
  three would be easy; nobody has needed it yet.
- **Clipboard verification fails open** (`_wayland_clipboard_holds`,
  dictation.py:403). A missing `wl-paste`, a timeout or a crash counts as
  success, because a false "clipboard broken" on a good copy is worse than a
  missed check. Only a successful read of *different* content is treated as
  failure.
- **The installer is GNOME-only.** `install-shortcuts.py` drives `gsettings`.
  Other desktops have to bind the commands by hand; the tool itself doesn't care.
- **The lab keeps 20 sessions with their audio** (`LAB_KEEP`, dictation.py:1752).
  Bounded, but 20 long dictations is real disk. Fine as a debugging aid, worth
  remembering if `lab = true` is left on forever.
- **Tests cover only what runs without hardware.** The PID-file claim, the
  ensemble's waiting, the vote, the tail stripper, WAV repair, session
  isolation, the clipboard handoff and the notification rules are tested;
  the recorders themselves, the overlay and the request shaping are not — they
  need a microphone, a compositor and a network, and mocking those would only
  test the mocks. Those paths are covered by `doctor`, the always-on log, and
  using the thing.
- **`pyproject.toml` caps Python at `<3.14`** while the tests run fine on 3.14.
  The cap is about the app's dependency wheels, not the code. Raise it when
  those are known good.

---

## Working on any of this

```bash
python3 test_dictation.py     # 41 tests; no mic, network, clipboard or deps
uv run dictation.py doctor    # setup, keys, live recorder, waiting recordings
tail -f ~/.local/state/dictation/debug.log   # always written, rotated at 1 MB
```

The log is the point. Most of what is fixed in this project was found by reading
it after the fact, not by reasoning about the code — including the two failures
that motivated session-isolated filenames. When something behaves oddly, the log
usually already knows why.

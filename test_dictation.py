#!/usr/bin/env python3
"""Tests for the parts that fail silently and can be checked without hardware.

    python3 test_dictation.py          (no dependencies; pytest also runs it)

Deliberately narrow. Recording, uploading and pasting need a mic, a network and
a compositor, and mocking them would only test the mocks. What is here is the
logic that has actually lost a dictation: the PID-file claim that decides
whether a second recorder starts, the vote that picks the text, and the tail
stripper that can eat the end of it.
"""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

# dictation resolves its runtime/state paths at import, so redirect them first —
# a test run must never touch a real recorder's PID file.
_TMP = tempfile.mkdtemp(prefix="dictation-test-")
os.environ["XDG_RUNTIME_DIR"] = _TMP
os.environ["XDG_STATE_HOME"] = os.path.join(_TMP, "state")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dictation as d  # noqa: E402


class TailStripping(unittest.TestCase):
    def test_strips_bare_boilerplate(self):
        self.assertEqual(
            d.strip_hallucinated_tail("Buy milk on the way home. Thank you for watching."),
            "Buy milk on the way home.",
        )

    def test_strips_several_stacked_tails(self):
        self.assertEqual(
            d.strip_hallucinated_tail("The meeting is at four. Thanks for watching! Subtitles by Amara.org"),
            "The meeting is at four.",
        )

    def test_spares_dictated_text_that_merely_starts_the_same(self):
        # The whole reason for the length guard: this is a sentence someone said.
        text = "Thank you for watching over my plants while I was away."
        self.assertEqual(d.strip_hallucinated_tail(text), text)

    def test_strips_the_amara_community_variant(self):
        self.assertEqual(
            d.strip_hallucinated_tail("Call the dentist. Subtitles by the Amara.org community"),
            "Call the dentist.",
        )

    def test_leaves_ordinary_text_and_empties_alone(self):
        self.assertEqual(d.strip_hallucinated_tail("Ship it."), "Ship it.")
        self.assertEqual(d.strip_hallucinated_tail(""), "")


class VoteComparison(unittest.TestCase):
    """norm_text decides when two engines 'agree' and the judge is skipped."""

    def test_ignores_case_punctuation_and_spacing(self):
        self.assertEqual(
            d.norm_text("Let's ship it, finally!"),
            d.norm_text("lets  ship it finally"),
        )

    def test_matches_hyphenated_against_spaced(self):
        self.assertEqual(d.norm_text("state-of-the-art"), d.norm_text("state of the art"))

    def test_still_separates_different_words(self):
        self.assertNotEqual(d.norm_text("ship it"), d.norm_text("skip it"))


class RecorderSlot(unittest.TestCase):
    """The claim that stops a double-press from leaving a recorder on the mic."""

    def setUp(self):
        d.PID_PATH.unlink(missing_ok=True)
        self.addCleanup(lambda: d.PID_PATH.unlink(missing_ok=True))

    def _live_stranger(self):
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (proc.terminate(), proc.wait()))
        time.sleep(0.1)
        return proc.pid

    def test_claim_then_release_round_trip(self):
        self.assertTrue(d.claim_recorder_slot())
        self.assertEqual(d.active_recorder_pid(), os.getpid())
        d.release_recorder_slot()
        self.assertIsNone(d.active_recorder_pid())

    def test_second_claim_loses_to_a_live_holder(self):
        self.assertTrue(d.claim_recorder_slot())
        self.assertFalse(d.claim_recorder_slot())
        self.assertEqual(d._slot_owner(), os.getpid())

    # Real recorders are separate processes started by a keyboard shortcut, so
    # the presses here are too: subprocesses agree on a wall-clock instant and
    # all claim at it. (multiprocessing would be shorter, but its default start
    # method is per-version — 3.14 switched Linux to forkserver, which pickles
    # the target and cannot take a local function.)
    PRESS = """
import os, sys, time
sys.path.insert(0, os.environ["DICTATION_DIR"])
import dictation as d
time.sleep(max(0.0, float(sys.argv[1]) - time.time()))
ok = d.claim_recorder_slot()
print(f"{os.getpid()} {int(ok)}", flush=True)
time.sleep(2)  # a real recorder holds the slot for as long as it records
"""

    def test_simultaneous_starts_produce_exactly_one_recorder(self):
        # The original bug: both presses passed an "is it recording?" check
        # during the ~1 s of imports before either wrote the PID file.
        n = 6
        press_at = time.time() + 1.0
        env = dict(os.environ, DICTATION_DIR=os.path.dirname(os.path.abspath(__file__)))
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", self.PRESS, str(press_at)],
                stdout=subprocess.PIPE, text=True, env=env,
            )
            for _ in range(n)
        ]
        for proc in procs:
            self.addCleanup(lambda p=proc: (p.poll() is None and (p.kill(), p.wait())))

        results = [proc.stdout.readline().split() for proc in procs]
        owner = d._slot_owner()  # while the winner is still holding it

        winners = [int(pid) for pid, ok in results if ok == "1"]
        self.assertEqual(len(winners), 1, f"{len(winners)} recorders started at once")
        self.assertEqual(owner, winners[0], "the PID file names a loser, orphaning the winner")

    def test_junk_pid_file_does_not_wedge_the_key(self):
        # A recorder killed mid-claim used to leave a file that could not be read
        # and was never cleared: every later start answered "Already recording".
        for junk in ("", "   ", "not-a-pid"):
            d.PID_PATH.write_text(junk)
            self.assertTrue(d.claim_recorder_slot(), f"wedged by pid file {junk!r}")
            d.release_recorder_slot()

    def test_recycled_pid_is_not_mistaken_for_the_recorder(self):
        # Stale file + a reused pid = stop() SIGTERMs an unrelated process.
        stranger = self._live_stranger()
        d.PID_PATH.write_text(f"{stranger} 1")  # right pid, wrong start time
        self.assertIsNone(d.active_recorder_pid())
        self.assertFalse(d.PID_PATH.exists(), "the stale file should be cleared")
        self.assertTrue(d._process_alive(stranger), "the stranger must not be signalled")

    def test_pid_only_file_falls_back_to_the_command_line(self):
        stranger = self._live_stranger()
        d.PID_PATH.write_text(str(stranger))  # written by a version before start times
        self.assertIsNone(d.active_recorder_pid())

    def test_release_never_drops_another_recorder_claim(self):
        d.PID_PATH.write_text(d._slot_record(os.getpid()).replace(str(os.getpid()), "999999", 1))
        d.release_recorder_slot()
        self.assertTrue(d.PID_PATH.exists(), "released a slot we do not own")


class RescuingAudio(unittest.TestCase):
    """The recording is the one thing a session cannot reconstruct, so nothing
    may delete it: not a failure handler, and not another session's cleanup."""

    SECONDS = 120

    def setUp(self):
        self.cfg = {"save_last_recording": True}
        self.audio = d.session_audio_path(os.getpid())
        for path in (self.audio, d.LAST_WAV_PATH, d.LAST_TXT_PATH, d.LAST_META_PATH,
                     d.session_mode_path(os.getpid())):
            path.unlink(missing_ok=True)
            self.addCleanup(path.unlink, True)

    def _record(self, path=None, seconds=None):
        import wave

        path = path or self.audio
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(d.SAMPLE_RATE)
            w.writeframes(b"\x01\x02" * (d.SAMPLE_RATE * (seconds or self.SECONDS)))
        return path

    def _readable_seconds(self, path):
        import wave

        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()

    def _live_process(self):
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (proc.terminate(), proc.wait()))
        time.sleep(0.1)
        return proc

    def test_a_finished_session_cannot_delete_the_next_recording(self):
        # The failure in the log: stop() deletes the WAV when transcription
        # finishes, by which time the next dictation is already recording. With
        # one shared name that cleanup deleted the live recording, which went on
        # filling an unlinked inode and arrived at its own stop as 0 bytes.
        finished = self._record(d.session_audio_path(4242), seconds=29)
        self.addCleanup(finished.unlink, True)
        live = self._record(seconds=189)

        finished.unlink()  # what stop()'s cleanup does, three minutes later

        self.assertTrue(live.is_file(), "an old stopper deleted the live recording")
        self.assertEqual(self._readable_seconds(live), 189)

    def test_a_crash_keeps_the_audio_it_captured(self):
        self._record()
        d.report_recorder_failure(self.cfg, RuntimeError("PortAudio: device disconnected"))
        self.assertFalse(self.audio.exists(), "working file should be cleared once saved")
        self.assertEqual(self._readable_seconds(d.LAST_WAV_PATH), self.SECONDS)
        self.assertTrue(d.last_wav_matches_meta(), "resend must be able to pick it up")

    def test_a_killed_recorders_file_is_repaired_not_read_as_empty(self):
        # libsndfile writes the lengths on close, so a SIGKILL leaves every
        # sample on disk behind a header claiming none of them.
        self._record()
        with open(self.audio, "r+b") as fh:
            fh.seek(4)
            fh.write((36).to_bytes(4, "little"))
            fh.seek(40)
            fh.write((0).to_bytes(4, "little"))
        self.assertEqual(self._readable_seconds(self.audio), 0.0)
        self.assertTrue(d.repair_wav_header(self.audio))
        self.assertEqual(self._readable_seconds(self.audio), self.SECONDS)

    def test_repair_leaves_a_healthy_file_untouched(self):
        self._record(seconds=1)
        before = self.audio.read_bytes()
        self.assertFalse(d.repair_wav_header(self.audio))
        self.assertEqual(self.audio.read_bytes(), before)

    def test_the_sweep_rescues_what_a_dead_recorder_left(self):
        self._record()
        d.sweep_orphaned_recordings(self.cfg)
        self.assertFalse(self.audio.exists())
        self.assertEqual(self._readable_seconds(d.LAST_WAV_PATH), self.SECONDS)

    def test_the_sweep_never_touches_a_live_recorders_file(self):
        proc = self._live_process()
        live = self._record(d.session_audio_path(proc.pid))
        self.addCleanup(live.unlink, True)
        d.sweep_orphaned_recordings(self.cfg)
        self.assertTrue(live.is_file(), "swept away a recording still being written")
        self.assertFalse(d.LAST_WAV_PATH.exists())

    def test_an_abandoned_hours_long_capture_does_not_take_the_slot(self):
        # A recorder nobody stopped is not a lost dictation; overwriting the slot
        # with it would discard the real one and hand resend an unusable upload.
        self.addCleanup(setattr, d, "MAX_RESCUE_SEC", d.MAX_RESCUE_SEC)
        d.MAX_RESCUE_SEC = 10.0
        self._record(seconds=60)
        d.sweep_orphaned_recordings(self.cfg)
        self.assertFalse(d.LAST_WAV_PATH.exists(), "clobbered the recovery slot")
        self.assertTrue(self.audio.is_file(), "deleted it instead of leaving it for the user")

    def test_with_the_slot_off_the_only_copy_is_left_alone(self):
        self._record()
        d.sweep_orphaned_recordings({"save_last_recording": False})
        self.assertTrue(self.audio.is_file(), "deleted the only copy of the audio")
        self.assertFalse(d.LAST_WAV_PATH.exists(), "wrote audio the user asked not to keep")

    def test_a_genuinely_tiny_take_is_not_hoarded(self):
        self.audio.write_bytes(b"\0" * 100)
        self.assertEqual(d.preserve_recording(self.cfg, self.audio, "full", "tiny"), (0.0, False))
        d.sweep_orphaned_recordings(self.cfg)
        self.assertFalse(self.audio.exists())


class StopIsolation(unittest.TestCase):
    """stop() end to end, with a second session recording alongside it — the
    shape of the failure in the log: 189.30s captured, 0 bytes at the stop."""

    def setUp(self):
        self.cfg = {
            "stt_backend": "groq", "save_last_recording": True, "api_key": "k",
            "ensemble": False, "deepgram_api_key": "", "gemini_api_key": "",
            "lab": False, "save_history": False,
        }
        self.notes = []
        for name, stub in (
            ("transcribe_with_retry", lambda cfg, path, mode: (f"text from {path.name}", None)),
            ("deliver_text", lambda cfg, text, **kw: self.delivered.append(text)),
            ("notify", lambda title, body="", *a, **k: self.notes.append(f"{title} {body}")),
        ):
            self.addCleanup(setattr, d, name, getattr(d, name))
            setattr(d, name, stub)
        self.delivered = []
        d.PID_PATH.unlink(missing_ok=True)
        self.addCleanup(d.PID_PATH.unlink, True)
        for path in (d.LAST_WAV_PATH, d.LAST_TXT_PATH, d.LAST_META_PATH):
            path.unlink(missing_ok=True)
            self.addCleanup(path.unlink, True)

    def _session(self, seconds, mode="simple"):
        """A live 'recorder' process with its own WAV and mode file."""
        import wave

        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (proc.poll() is None and (proc.terminate(), proc.wait())))
        wav = d.session_audio_path(proc.pid)
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(d.SAMPLE_RATE)
            w.writeframes(b"\x01\x02" * (d.SAMPLE_RATE * seconds))
        d.session_mode_path(proc.pid).write_text(mode)
        self.addCleanup(wav.unlink, True)
        self.addCleanup(d.session_mode_path(proc.pid).unlink, True)
        return proc, wav

    def test_stop_transcribes_its_own_session_and_leaves_the_other_alone(self):
        stopping, stopping_wav = self._session(30)
        recording, recording_wav = self._session(189)
        d.PID_PATH.write_text(d._slot_record(stopping.pid))

        d.stop(self.cfg)

        self.assertEqual(self.delivered, [f"text from {stopping_wav.name}"])
        self.assertFalse(stopping_wav.exists(), "its own file should be cleaned up")
        self.assertTrue(recording_wav.is_file(), "deleted a recording still in progress")
        self.assertEqual(recording_wav.stat().st_size, d.SAMPLE_RATE * 2 * 189 + 44)
        self.assertNotIn("too short", " ".join(self.notes).lower())

    def test_a_recorder_that_wrote_nothing_is_not_blamed_on_the_speaker(self):
        stopping, stopping_wav = self._session(30)
        stopping_wav.unlink()  # a crash deleted it, the old way
        d.PID_PATH.write_text(d._slot_record(stopping.pid))

        d.stop(self.cfg)

        joined = " ".join(self.notes).lower()
        self.assertIn("captured nothing", joined)
        self.assertNotIn("too short", joined)
        self.assertEqual(self.delivered, [])


class StopBeforeLive(unittest.TestCase):
    """A stop can arrive in the ~0.3 s (~0.8 s with Qt) a recorder spends
    loading, after it has claimed the slot. Until it is armed for that, the
    signal takes the default disposition and kills it where it stands."""

    # A recorder claiming the slot, then "importing" for a second. With the
    # handlers armed first, the SIGTERM in between is recorded and obeyed;
    # without, the default disposition kills the process outright.
    RECORDER = """
import os, sys, time
sys.path.insert(0, os.environ["DICTATION_DIR"])
import dictation as d
d.claim_recorder_slot()
if os.environ.get("ARMED") == "1":
    d.arm_stop_signals()
print("claimed", flush=True)
time.sleep(1.0)                                   # the sounddevice / Qt imports
print("stopped" if d.stopped_before_going_live("recorder") else "live", flush=True)
d.release_recorder_slot()
"""

    def setUp(self):
        d.PID_PATH.unlink(missing_ok=True)
        self.addCleanup(d.PID_PATH.unlink, True)

    def _run_recorder(self, armed):
        env = dict(os.environ, DICTATION_DIR=os.path.dirname(os.path.abspath(__file__)),
                   ARMED="1" if armed else "0")
        proc = subprocess.Popen([sys.executable, "-c", self.RECORDER],
                                stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(lambda: (proc.poll() is None and (proc.kill(), proc.wait())))
        self.assertEqual(proc.stdout.readline().strip(), "claimed")
        proc.terminate()  # the stop press, while it is still loading
        return proc

    def test_a_stop_during_startup_is_obeyed_not_fatal(self):
        proc = self._run_recorder(armed=True)
        self.assertEqual(proc.stdout.readline().strip(), "stopped")
        self.assertEqual(proc.wait(timeout=5), 0, "should exit cleanly, not on a signal")
        self.assertFalse(d.PID_PATH.exists(), "a clean exit releases the recorder slot")

    def test_without_arming_the_same_stop_kills_the_recorder(self):
        # Guards the ordering in record(): arm, then import. If someone moves
        # the arming back after the imports, this is what the user gets.
        proc = self._run_recorder(armed=False)
        self.assertEqual(proc.wait(timeout=5), -signal.SIGTERM, "expected a hard kill")
        self.assertEqual(proc.stdout.read().strip(), "", "it never reached its own cleanup")
        self.assertTrue(d.PID_PATH.exists(), "and the claim is left behind")


class ProblemNotificationsStay(unittest.TestCase):
    """A failure the user never sees is a failure they cannot act on."""

    def setUp(self):
        self.sent = []
        self.addCleanup(setattr, d, "subprocess", d.subprocess)
        real_which, real_run = d.shutil.which, d.subprocess.run
        d.shutil.which = lambda name: f"/usr/bin/{name}"
        d.subprocess.run = lambda cmd, **kw: self.sent.append(cmd)
        self.addCleanup(setattr, d.shutil, "which", real_which)
        self.addCleanup(setattr, d.subprocess, "run", real_run)
        level = d._notify_level
        self.addCleanup(d.configure, level, d._debug)
        d.configure("all", False)

    def _tag_in_last(self):
        return any("x-canonical-private-synchronous" in str(arg) for arg in self.sent[-1])

    def test_routine_notifications_still_replace_each_other(self):
        d.notify("🎙️ Recording…", "speak", "icon", "start")
        self.assertTrue(self._tag_in_last(), "routine cards should collapse into one")

    def test_a_problem_is_not_replaceable(self):
        d.notify("❌ Dictation", "Recording failed", "icon", "problem")
        self.assertFalse(self._tag_in_last(), "an error can be wiped by the next press")


class ClipboardHandoff(unittest.TestCase):
    """A clipboard tool is the clipboard: it keeps running to serve the
    selection. Waiting for it to exit once cost 31 minutes."""

    def test_a_tool_that_keeps_running_is_treated_as_serving(self):
        import warnings

        started = time.monotonic()
        with warnings.catch_warnings():
            # Leaving it running is the point: it is serving the selection.
            warnings.simplefilter("ignore", ResourceWarning)
            ok = d._hand_to_clipboard_tool("fake", ["sh", "-c", "cat >/dev/null; sleep 5"], b"hi")
        waited = time.monotonic() - started
        self.assertTrue(ok)
        self.assertLess(waited, d.CLIPBOARD_HANDOFF_SEC * 2 + 1, "waited on the serving daemon")

    def test_a_tool_that_fails_is_still_reported(self):
        self.assertFalse(d._hand_to_clipboard_tool("fake", ["sh", "-c", "cat >/dev/null; exit 3"], b"hi"))

    def test_a_missing_tool_is_reported(self):
        self.assertFalse(d._hand_to_clipboard_tool("nope", ["/nonexistent/clipboard-tool"], b"hi"))


class SettledText(unittest.TestCase):
    """What the streaming backend has to show for itself when it falls over."""

    def test_joins_finals_and_keeps_an_unclosed_partial(self):
        self.assertEqual(d.settled_text(["One two.", "Three."], "four fi"),
                         "One two. Three. four fi")

    def test_drops_a_partial_already_contained_in_the_finals(self):
        self.assertEqual(d.settled_text(["One two three."], "two three"), "One two three.")

    def test_a_partial_alone_still_counts(self):
        self.assertEqual(d.settled_text([], "half a sentence"), "half a sentence")
        self.assertEqual(d.settled_text([], ""), "")


class MicrophoneStall(unittest.TestCase):
    """A device that goes away mid-session raises nothing — the callback just
    stops. Silence has to be reported while the user can still act on it."""

    def setUp(self):
        self.notes = []
        real = d.notify
        d.notify = lambda title, body="", *a, **k: self.notes.append((title, body))
        self.addCleanup(setattr, d, "notify", real)

    def test_warns_once_when_audio_stops_then_again_after_it_returns(self):
        watch = d.StallWatch()
        watch.check()
        self.assertEqual(self.notes, [], "should not warn while audio is flowing")

        watch.last -= d.AUDIO_STALL_SEC + 1
        watch.check()
        watch.check()
        self.assertEqual(len(self.notes), 1, "one warning per stall, not one per loop")
        self.assertIn("microphone", self.notes[0][1].lower())

        watch.saw_audio()
        self.assertEqual(len(self.notes), 2, "recovery should be reported too")
        watch.last -= d.AUDIO_STALL_SEC + 1
        watch.check()
        self.assertEqual(len(self.notes), 3, "a second stall must warn again")


class EnsembleWait(unittest.TestCase):
    """When to stop waiting for engines. Both directions cost a dictation:
    too eager throws away finished text, too patient hangs on a dead engine."""

    GOOD = "The quick brown fox jumps over the lazy dog."

    def setUp(self):
        self.audio = d.runtime_dir() / "test.wav"
        self.audio.write_bytes(b"\0" * 320_000)  # ~10 s of 16 kHz mono s16
        self.addCleanup(lambda: self.audio.unlink(missing_ok=True))
        self.cfg = {
            "ensemble_model": "whisper-large-v3", "model": "whisper-large-v3-turbo",
            "deepgram_api_key": "k", "gemini_api_key": "k",
            "judge_model": "gemini-2.5-flash", "prompt": "", "keyterms": [],
        }
        for name, value in (("ENSEMBLE_GRACE_SEC", 0.5), ("ENSEMBLE_MIN_WAIT_SEC", 3.0),
                            ("ENSEMBLE_MAX_WAIT_SEC", 3.0)):
            self.addCleanup(setattr, d, name, getattr(d, name))
            setattr(d, name, value)
        for name in ("transcribe", "transcribe_deepgram_batch", "judge_transcripts"):
            self.addCleanup(setattr, d, name, getattr(d, name))

    def _engines(self, groq, deepgram):
        d.transcribe = lambda cfg, path, model: groq()
        d.transcribe_deepgram_batch = lambda cfg, path: deepgram()
        d.judge_transcripts = lambda cfg, hyps: ""

    def test_slow_engines_are_not_abandoned_before_any_answer(self):
        # A 392 s recording lost every engine to a flat 6 s deadline.
        def slow():
            time.sleep(1.5)
            return self.GOOD

        self._engines(slow, slow)
        text, how, _ = d.ensemble_transcribe(self.cfg, self.audio)
        self.assertEqual(text, self.GOOD, how)

    def test_an_empty_answer_does_not_start_the_grace_clock(self):
        # An engine that returns nothing instantly is not an answer in hand;
        # counting it as one abandoned the engines still fetching the real text.
        def slow():
            time.sleep(1.5)
            return self.GOOD

        self._engines(slow, lambda: "")
        text, how, _ = d.ensemble_transcribe(self.cfg, self.audio)
        self.assertEqual(text, self.GOOD, how)

    def test_a_straggler_is_abandoned_once_real_text_is_in_hand(self):
        def hung():
            time.sleep(30)
            return "too late"

        self._engines(lambda: self.GOOD, hung)
        started = time.monotonic()
        text, _how, report = d.ensemble_transcribe(self.cfg, self.audio)
        self.assertEqual(text, self.GOOD)
        self.assertLess(time.monotonic() - started, 3.0, "waited on a hung engine")
        self.assertTrue(any("abandoned" in str(e) for e in report["engines"].values()))

    def test_every_engine_failing_reports_the_error_without_waiting(self):
        def boom():
            raise RuntimeError("401 unauthorized")

        self._engines(boom, boom)
        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            d.ensemble_transcribe(self.cfg, self.audio)
        self.assertLess(time.monotonic() - started, 2.0, "fast failures should fail fast")


if __name__ == "__main__":
    unittest.main(verbosity=2)

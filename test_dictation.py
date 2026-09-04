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
import multiprocessing as mp
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

    def test_simultaneous_starts_produce_exactly_one_recorder(self):
        # The original bug: both presses passed an "is it recording?" check
        # during the ~1 s of imports before either wrote the PID file.
        n = 6
        barrier, queue, done = mp.Barrier(n), mp.Queue(), mp.Event()

        def press(barrier, queue, done):
            barrier.wait()
            queue.put((os.getpid(), d.claim_recorder_slot()))
            done.wait(10)  # a real recorder holds the slot while it records

        procs = [mp.Process(target=press, args=(barrier, queue, done)) for _ in range(n)]
        for p in procs:
            p.start()
        winners = [pid for pid, ok in (queue.get() for _ in range(n)) if ok]
        owner = d._slot_owner()
        done.set()
        for p in procs:
            p.join()
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

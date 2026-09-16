"""ElevenLabs v3 paragraph-cut contract tests (no network).

Background: audio/elevenlabs_tts.py sends a paragraph and cuts it back into
per-line clips at the model's ``voice_segments`` boundaries. The CLAUDE.md
exception that allows this requires every cut to be verified against the
energy mask. These tests build synthetic paragraphs (tone bursts + silence)
and check:

  A. alignment marks (first / last character start per line) yield one clip
     per line whose edges are silent, and a comma pause inside the next line
     is never taken for the seam
  B. two lines with no pause between them are rejected, not "fixed"
  C. the response cross-checks (count / order / character span)
  D. chunk planning keeps script order and respects both caps
  E. local time-stretch changes duration by the requested factor
"""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from pydub import AudioSegment
from pydub.generators import Sine

import audio.elevenlabs_tts as el
from audio.elevenlabs_tts import (
    ElevenLabsError,
    _Chunk,
    alignment_marks,
    boundaries_from_response,
    cut_paragraph,
    plan_chunks,
    reject_store,
    time_stretch,
)
from audio.splitter import FRAME_MS, speech_mask


def tone(ms: int) -> AudioSegment:
    return Sine(440).to_audio_segment(duration=ms).apply_gain(-6)


def silence(ms: int) -> AudioSegment:
    return AudioSegment.silent(duration=ms)


def paragraph(speech_ms: list[int], pause_ms: int) -> tuple[AudioSegment, list[tuple[int, int]]]:
    """Speech bursts separated by pauses; returns audio + true speech spans."""
    audio = silence(50)
    spans = []
    for i, ms in enumerate(speech_ms):
        start = len(audio)
        audio += tone(ms)
        spans.append((start, len(audio)))
        if i < len(speech_ms) - 1:
            audio += silence(pause_ms)
    audio += silence(50)
    return audio, spans


def marks_for(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """What alignment_marks would say for tone bursts: the first character
    starts with the burst, the last one starts ~100 ms before it ends."""
    return [(s, max(s, e - 100)) for s, e in spans]


class ParagraphCut(unittest.TestCase):
    """The seam between two lines is a silent run inside the alignment's
    interval for it; nothing is read from the generator's clock any more."""

    def _assert_edges_silent(self, clips, n):
        self.assertEqual(len(clips), n)
        for clip in clips:
            m = speech_mask(clip)
            self.assertFalse(m[0], "clip starts inside speech")
            self.assertFalse(m[-1], "clip ends inside speech")
            self.assertTrue(m.any(), "clip has no speech at all")

    def test_each_line_gets_its_own_burst(self):
        audio, spans = paragraph([900, 1200, 700], pause_ms=600)
        clips = cut_paragraph(audio, marks_for(spans))
        self._assert_edges_silent(clips, 3)
        for clip, (s, e) in zip(clips, spans):
            self.assertLess(abs(len(clip) - (e - s) - 400), 250)

    def test_comma_pause_inside_the_next_line_is_not_taken(self):
        """The failure the volume checks let through: line k+1 opens with a
        short clause ("そこでやっと、"), and a seam that lands in that comma
        pause moves the clause into clip k. The comma pause lies after k+1's
        first character, so it is outside the seam's interval by construction."""
        audio = (silence(50) + tone(1500) + silence(700)
                 + tone(400) + silence(250) + tone(1200)     # line 1: clause, comma, rest
                 + silence(700) + tone(1000) + silence(50))
        l0 = (50, 1550); l1 = (2250, 2250 + 400 + 250 + 1200); l2 = (l1[1] + 700, l1[1] + 700 + 1000)
        clips = cut_paragraph(audio, marks_for([l0, l1, l2]))
        self._assert_edges_silent(clips, 3)
        self.assertLess(abs(len(clips[0]) - 1500 - 400), 250)
        self.assertLess(abs(len(clips[1]) - 1850 - 400), 250)   # clause + comma + rest
        self.assertLess(abs(len(clips[2]) - 1000 - 400), 250)

    def test_aligner_placing_the_onset_late_is_tolerated(self):
        # the energy mask hears a consonant 100-200 ms before the aligner places
        # the character; marks 200 ms late must still find the pause
        audio, spans = paragraph([900, 1200, 700], pause_ms=600)
        late = [(s + 200, e - 100) for s, e in spans]
        clips = cut_paragraph(audio, late)
        self._assert_edges_silent(clips, 3)

    def test_lines_run_together_are_rejected(self):
        # 50 ms between two lines is a stop closure, not a pause: nothing to cut in
        audio = silence(50) + tone(1500) + silence(50) + tone(1500) + silence(50)
        with self.assertRaises(ElevenLabsError):
            cut_paragraph(audio, marks_for([(50, 1550), (1600, 3100)]))

    def test_long_pauses_are_not_kept_in_the_clip(self):
        audio, spans = paragraph([1000, 1000, 1000], pause_ms=2500)
        clips = cut_paragraph(audio, marks_for(spans))
        for clip in clips:
            self.assertLess(abs(len(clip) - 1400), 300)


class AlignmentMarks(unittest.TestCase):
    """Reading the forced-alignment response: character START times only,
    positional match against the text sent, punctuation carries no sound."""

    @staticmethod
    def _fa(text: str, step_ms: int = 100) -> dict:
        chars, t = [], 0.0
        for ch in text:
            chars.append({"text": ch, "start": t, "end": t + step_ms / 1000})
            if ch.strip() and ch not in el._NO_SOUND:
                t += step_ms / 1000
        return {"characters": chars, "words": [], "loss": 0.5}

    def test_first_and_last_voiced_character(self):
        texts = ["はい、そうです。", "次は？"]
        marks = alignment_marks(self._fa("\n".join(texts)), texts)
        self.assertEqual(len(marks), 2)
        # line 0: は at 0, す (last voiced) at 500 ms — the 、 and 。 are skipped
        self.assertEqual(marks[0], (0, 500))
        # line 1 starts after the 6 voiced characters of line 0
        self.assertEqual(marks[1][0], 600)
        self.assertLess(marks[0][1], marks[1][0])

    def test_normalised_text_is_refused(self):
        texts = ["数字は 150 ミリ秒です。"]
        fa = self._fa("数字は 百五十 ミリ秒です。")
        with self.assertRaises(ElevenLabsError):
            alignment_marks(fa, texts)

    def test_line_with_only_punctuation_is_refused(self):
        with self.assertRaises(ElevenLabsError):
            alignment_marks(self._fa("はい\n…"), ["はい", "…"])


class ResponseChecks(unittest.TestCase):
    def _resp(self, segs):
        return {"voice_segments": segs}

    def test_valid_response(self):
        chunk = _Chunk([0, 1], ["こんにちは。", "React の話です。"], ["v1", "v2"])
        resp = self._resp([
            {"dialogue_input_index": 0, "character_start_index": 0, "character_end_index": 6,
             "start_time_seconds": 0.0, "end_time_seconds": 1.2},
            {"dialogue_input_index": 1, "character_start_index": 6, "character_end_index": 17,
             "start_time_seconds": 1.2, "end_time_seconds": 3.0},
        ])
        self.assertEqual(boundaries_from_response(resp, chunk), [(0, 1200), (1200, 3000)])

    def test_count_mismatch(self):
        chunk = _Chunk([0, 1], ["a。", "b。"], ["v", "v"])
        with self.assertRaises(ElevenLabsError):
            boundaries_from_response(self._resp([{"dialogue_input_index": 0, "character_start_index": 0,
                                                  "character_end_index": 2, "start_time_seconds": 0, "end_time_seconds": 1}]), chunk)

    def test_span_mismatch(self):
        chunk = _Chunk([0], ["abcd。"], ["v"])
        with self.assertRaises(ElevenLabsError):
            boundaries_from_response(self._resp([{"dialogue_input_index": 0, "character_start_index": 0,
                                                  "character_end_index": 3, "start_time_seconds": 0, "end_time_seconds": 1}]), chunk)


class ChunkPlanning(unittest.TestCase):
    def test_line_cap_and_order(self):
        texts = [f"line{i}" for i in range(23)]
        chunks = plan_chunks(texts, ["v"] * 23, max_lines=10, max_chars=10_000)
        self.assertEqual([len(c.indices) for c in chunks], [10, 10, 3])
        self.assertEqual([i for c in chunks for i in c.indices], list(range(23)))

    def test_char_cap(self):
        texts = ["x" * 600] * 5
        chunks = plan_chunks(texts, ["v"] * 5, max_lines=10, max_chars=1500)
        self.assertEqual([len(c.indices) for c in chunks], [2, 2, 1])

    def test_overlong_single_line_gets_own_chunk(self):
        texts = ["short", "y" * 3000, "short"]
        chunks = plan_chunks(texts, ["v"] * 3, max_lines=10, max_chars=2000)
        self.assertEqual([c.indices for c in chunks], [[0], [1], [2]])

    def test_boundaries_follow_cached_paragraphs(self):
        # A script that was voiced as 10-line blocks, then had two lines
        # inserted at position 5. Fixed blocks would miss every cached
        # paragraph after the edit; the plan re-uses them and pays only for
        # the inserted lines (plus whatever fill it needs).
        old = [f"line{i}" for i in range(30)]
        cached_sets = {tuple(old[i:i + 10]) for i in range(0, 30, 10)}
        new = old[:5] + ["new-a", "new-b"] + old[5:]
        chunks = plan_chunks(new, ["v"] * len(new), max_lines=10, max_chars=10_000,
                             cached=lambda c: tuple(c.texts) in cached_sets)
        self.assertEqual([i for c in chunks for i in c.indices], list(range(len(new))))
        fresh = [c.texts for c in chunks if tuple(c.texts) not in cached_sets]
        # the two cached blocks after the edit are re-used verbatim
        self.assertIn(tuple(old[10:20]), {tuple(c.texts) for c in chunks})
        self.assertIn(tuple(old[20:30]), {tuple(c.texts) for c in chunks})
        # and everything fresh is only the edited head (<= 12 lines, one or two paragraphs)
        self.assertLessEqual(sum(len(t) for t in fresh), 12)
        # without a cache the plan is the plain max-length grouping
        plain = plan_chunks(new, ["v"] * len(new), max_lines=10, max_chars=10_000)
        self.assertEqual([len(c.indices) for c in plain], [10, 10, 10, 2])


class TimeStretch(unittest.TestCase):
    def test_identity_and_factor(self):
        clip = tone(2000)
        self.assertIs(time_stretch(clip, 1.0), clip)
        faster = time_stretch(clip, 1.25)
        self.assertLess(abs(len(faster) - 1600), 60)
        slower = time_stretch(clip, 0.8)
        self.assertLess(abs(len(slower) - 2500), 60)


if __name__ == "__main__":
    unittest.main()


class RejectedAttemptIsKept(unittest.TestCase):
    """A rejected paragraph must leave evidence behind.

    On 2026-09-12 five paragraphs were rejected and the audio was already gone
    by the time anyone looked — the temp file is overwritten by the retry — so
    the cause had to be guessed from the paragraphs that succeeded. The model's
    ``voice_segments`` is the part that cannot be recovered from the audio, so
    it is the part that must be on disk.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._saved = el.REJECT_DIR
        el.REJECT_DIR = Path(self._dir)
        self.addCleanup(setattr, el, "REJECT_DIR", self._saved)
        self.addCleanup(shutil.rmtree, self._dir, True)
        self.raw = Path(self._dir) / "src.mp3"
        tone(300).export(self.raw, format="mp3").close()
        self.chunk = _Chunk(indices=[7, 8], texts=["あ。", "い。"], voice_ids=["v1", "v1"])

    def _resp(self):
        return {"voice_segments": [{"start_time_seconds": 0.0, "end_time_seconds": 1.0}],
                "character_cost": 42}

    def test_audio_and_alignment_are_written(self):
        out = reject_store(self.raw, self._resp(), self.chunk, 3, 1, 99,
                           ElevenLabsError("line 0 holds 1000 ms of speech"))
        self.assertIsNotNone(out)
        self.assertTrue(out.exists(), "rejected audio was not kept")
        meta = json.loads(out.with_suffix(".json").read_text())
        self.assertEqual(meta["paragraph"], 3)
        self.assertEqual(meta["attempt"], 1)
        self.assertEqual(meta["seed"], 99)
        self.assertEqual(meta["lines"], [7, 8])
        self.assertEqual(meta["texts"], ["あ。", "い。"])
        self.assertIn("1000 ms of speech", meta["error"])
        # the irrecoverable part
        self.assertEqual(meta["voice_segments"], self._resp()["voice_segments"])

    def test_pruned_to_the_newest_and_pairs_stay_together(self):
        el_keep = el.REJECT_KEEP
        el.REJECT_KEEP = 3
        self.addCleanup(setattr, el, "REJECT_KEEP", el_keep)
        for i in range(6):
            reject_store(self.raw, self._resp(), self.chunk, i, 1, i,
                         ElevenLabsError(f"err{i}"))
            time.sleep(0.01)  # distinct mtimes
        mp3s = sorted(Path(self._dir).glob("*.mp3"))
        jsons = sorted(Path(self._dir).glob("*.json"))
        self.assertEqual(len(jsons), 3, "not pruned to REJECT_KEEP")
        self.assertEqual(len(mp3s), 3 + 1, "mp3 orphaned or source removed")  # +1 = src.mp3
        kept = {json.loads(f.read_text())["paragraph"] for f in jsons}
        self.assertEqual(kept, {3, 4, 5}, "pruned the wrong ones")
        for f in jsons:
            self.assertTrue(f.with_suffix(".mp3").exists(), "json left without its audio")

    def test_a_save_failure_never_raises(self):
        el.REJECT_DIR = Path("/dev/null/not-a-dir")   # mkdir will fail
        self.assertIsNone(
            reject_store(self.raw, self._resp(), self.chunk, 0, 1, 0,
                         ElevenLabsError("boom"))
        )


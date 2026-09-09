"""ElevenLabs v3 paragraph-cut contract tests (no network).

Background: audio/elevenlabs_tts.py sends a paragraph and cuts it back into
per-line clips at the model's ``voice_segments`` boundaries. The CLAUDE.md
exception that allows this requires every cut to be verified against the
energy mask. These tests build synthetic paragraphs (tone bursts + silence)
and check:

  A. contiguous model boundaries with the pause booked to either side still
     yield one clip per line whose edges are silent
  B. a boundary inside continuous speech is rejected, not "fixed"
  C. the response cross-checks (count / order / character span)
  D. chunk planning keeps script order and respects both caps
  E. local time-stretch changes duration by the requested factor
"""

import shutil
import unittest

from pydub import AudioSegment
from pydub.generators import Sine

from audio.elevenlabs_tts import (
    BOUNDARY_WINDOW_MS,
    ElevenLabsError,
    _Chunk,
    assign_seams,
    boundaries_from_response,
    cut_paragraph,
    plan_chunks,
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


class ParagraphCut(unittest.TestCase):
    def _assert_edges_silent(self, audio, clips, bounds):
        mask = speech_mask(audio)
        self.assertEqual(len(clips), len(bounds))
        for clip in clips:
            m = speech_mask(clip)
            self.assertFalse(m[0], "clip starts inside speech")
            self.assertFalse(m[-1], "clip ends inside speech")
            self.assertTrue(m.any(), "clip has no speech at all")

    def test_pause_booked_to_previous_line(self):
        # like /with-timestamps: line k's end == line k+1's start, pause on the end side
        audio, spans = paragraph([900, 1200, 700], pause_ms=600)
        bounds = [(spans[0][0], spans[1][0]), (spans[1][0], spans[2][0]), (spans[2][0], spans[2][1])]
        clips = cut_paragraph(audio, bounds)
        self._assert_edges_silent(audio, clips, bounds)
        # each clip holds its own burst (± pads), not the neighbour's
        for clip, (s, e) in zip(clips, spans):
            self.assertLess(abs(len(clip) - (e - s) - 400), 250)

    def test_pause_booked_to_next_line(self):
        # like /text-to-dialogue: the pause lands at the start of the next segment
        audio, spans = paragraph([900, 1200, 700], pause_ms=600)
        bounds = [(spans[0][0], spans[0][1]), (spans[0][1], spans[1][1]), (spans[1][1], spans[2][1])]
        clips = cut_paragraph(audio, bounds)
        self._assert_edges_silent(audio, clips, bounds)

    def test_boundary_inside_speech_is_rejected(self):
        # one long burst with the "boundary" in the middle: nothing to back out to
        audio = silence(50) + tone(3000) + silence(50)
        bounds = [(50, 1500), (1500, 3050)]
        with self.assertRaises(ElevenLabsError):
            cut_paragraph(audio, bounds)


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


class SeamAssignment(unittest.TestCase):
    """The seam->pause matching is a monotonic DP, not a greedy nearest walk.

    Real paragraphs carry extra silent runs (comma pauses) between the sentence
    pauses. A greedy walk can consume the wrong run for an early seam and then
    fail on every later one; the DP pays a small local cost to keep the whole
    assignment feasible. Frames are 10 ms.
    """

    def test_extra_candidates_between_real_pauses(self):
        # runs at 1.0s, 1.4s(comma), 3.0s, 3.4s(comma), 5.0s ; seams at 1.0/3.0/5.0
        runs = [(100, 110), (140, 150), (300, 310), (340, 350), (500, 510)]
        picked = assign_seams(runs, [1000, 3000, 5000])
        self.assertEqual(picked, [(100, 110), (300, 310), (500, 510)])

    def test_greedy_trap_is_survived(self):
        # seam 0's nearest run is the one seam 1 needs; a greedy pick would
        # leave nothing for seam 1. The DP takes the second-nearest for seam 0.
        runs = [(100, 110), (200, 210)]
        picked = assign_seams(runs, [1900, 2000])
        self.assertEqual(picked, [(100, 110), (200, 210)])

    def test_assignment_is_strictly_increasing(self):
        runs = [(100, 110), (300, 310), (500, 510), (700, 710)]
        picked = assign_seams(runs, [1000, 3000, 7000])
        self.assertEqual(picked, sorted(set(picked)))
        self.assertEqual(len(picked), 3)

    def test_too_few_pauses_raises(self):
        with self.assertRaises(ElevenLabsError):
            assign_seams([(100, 110)], [1000, 3000])

    def test_seam_far_from_every_pause_raises(self):
        far = BOUNDARY_WINDOW_MS * 4
        with self.assertRaises(ElevenLabsError):
            assign_seams([(100, 110), (200, 210)], [1000, far])


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not on PATH")
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

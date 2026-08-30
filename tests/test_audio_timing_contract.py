import tempfile
import unittest
from pathlib import Path

from pydub import AudioSegment

from audio.assembler import EchoTiming
from audio.timing import (
    ACTUAL_AUDIO_TIMING,
    require_actual_audio_timing,
    time_audio_segments,
)
from export.lrc_writer import generate_echo_lrc
from parser.lrc_parser import Segment


class AudioTimingContractTests(unittest.TestCase):
    def test_final_segments_use_decoded_clip_lengths_not_text_lengths(self) -> None:
        texts = ["短い文。", "文字数とは無関係に長い文。", "最後。"]
        audios = [
            AudioSegment.silent(duration=1700),
            AudioSegment.silent(duration=320),
            AudioSegment.silent(duration=2410),
        ]

        timed = time_audio_segments(texts, audios, start_ms=250)

        self.assertEqual(
            [(item.start_ms, item.end_ms) for item in timed],
            [(250, 1950), (1950, 2270), (2270, 4680)],
        )
        self.assertTrue(
            all(item.timing_source == ACTUAL_AUDIO_TIMING for item in timed)
        )

    def test_exact_timing_rejects_non_1_to_1_inputs_and_estimates(self) -> None:
        with self.assertRaisesRegex(ValueError, "1:1 mapping"):
            time_audio_segments(
                ["一。", "二。"],
                [AudioSegment.silent(duration=100)],
            )
        with self.assertRaisesRegex(ValueError, "decoded audio duration"):
            require_actual_audio_timing("character_ratio_estimate")

    def test_lrc_positions_accumulate_real_target_and_native_lengths(self) -> None:
        segments = [
            Segment(0, 0, 0, "第一。", "第一。"),
            Segment(1, 0, 0, "第二。", "第二。"),
        ]
        target = [
            AudioSegment.silent(duration=1234),
            AudioSegment.silent(duration=2876),
        ]
        native = [
            AudioSegment.silent(duration=700),
            AudioSegment.silent(duration=1100),
        ]
        timing = EchoTiming(0.8, 0.5, 1.2)

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "exact.lrc"
            generate_echo_lrc(
                segments,
                target,
                native,
                timing,
                output,
                delimiter="|||",
                variant="full",
                tnt_repeats=1,
                tst_repeats=0,
            )
            lines = output.read_text(encoding="utf-8").splitlines()

        # 2*1234 target + 700 native + 800/500/1200 silence = 5668 ms.
        self.assertEqual(lines[0], "[00:00.00]第一。|||第一。")
        self.assertEqual(lines[1], "[00:05.67]第二。|||第二。")


if __name__ == "__main__":
    unittest.main()

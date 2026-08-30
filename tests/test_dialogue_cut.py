"""剪辑规则的契约测试 —— 对应 docs/dialogue_pipeline_claude_v2.md 的剪辑规则一节。

核心不变量只有两条，其余都是它们的推论：
  A. 每一刀都落在「已连续 QUIET_RUN_MS 到达本地底噪」的区间内部。
  B. 每一段 VAD 语音都被完整保留，一帧不少。

测试用「谱面串」构造输入：'#' = 有声，'.' = 底噪，一个字符 = 一帧 = 10ms。
这样规则可以脱离音频单独验证，也不依赖任何真实素材。
"""

import unittest

import numpy as np

from tools.dialogue_cut import (
    FRAME_MS,
    KEEP_EACH_SIDE_MS,
    QUIET_RUN_MS,
    FLOOR_WINDOW_MS,
    build_keep_intervals,
    masks_from_audible,
)

EPS_FRAMES = KEEP_EACH_SIDE_MS // FRAME_MS
RUN_FRAMES = QUIET_RUN_MS // FRAME_MS


def score(pattern: str):
    """谱面串 -> (audible, quiet_fwd, quiet_bwd, 总帧数)。"""
    audible = np.array([c == "#" for c in pattern], dtype=bool)
    fwd, bwd = masks_from_audible(audible)
    return audible, fwd, bwd, len(pattern)


def cut(pattern: str, regions):
    audible, fwd, bwd, n = score(pattern)
    return build_keep_intervals(regions, fwd, bwd, n), audible, n


def removed_spans(intervals, total_frames):
    """保留区间之外、被剪掉的那些源区间（单位：帧）。"""
    spans = []
    cursor = 0
    for item in intervals:
        start = item.source_start_ms // FRAME_MS
        if start > cursor:
            spans.append((cursor, start))
        cursor = item.source_end_ms // FRAME_MS
    if cursor < total_frames:
        spans.append((cursor, total_frames))
    return spans


class CutRuleTests(unittest.TestCase):
    # 10 帧静音 + 20 帧语音 + 40 帧静音 + 20 帧语音 + 10 帧静音
    LONG_GAP = "." * 10 + "#" * 20 + "." * 40 + "#" * 20 + "." * 10
    LONG_GAP_REGIONS = [(10, 30), (70, 90)]

    def test_rule_constants_are_self_consistent(self):
        """常量必须能整除成帧，且留量至少一帧，否则规则退化。"""
        self.assertEqual(KEEP_EACH_SIDE_MS % FRAME_MS, 0)
        self.assertEqual(QUIET_RUN_MS % FRAME_MS, 0)
        self.assertEqual(FLOOR_WINDOW_MS % FRAME_MS, 0)
        self.assertGreaterEqual(EPS_FRAMES, 1)
        self.assertGreaterEqual(RUN_FRAMES, 1)

    def test_cut_lands_only_in_verified_quiet(self):
        """不变量 A：被剪掉的每一帧都必须是底噪帧。"""
        intervals, audible, n = cut(self.LONG_GAP, self.LONG_GAP_REGIONS)
        for lo, hi in removed_spans(intervals, n):
            self.assertFalse(
                audible[lo:hi].any(),
                f"剪掉的区间 {lo}~{hi} 帧里还有声音",
            )

    def test_speech_is_never_cut(self):
        """不变量 B：每段语音完整落在某个保留区间里，一毫秒都不能少。"""
        intervals, _, _ = cut(self.LONG_GAP, self.LONG_GAP_REGIONS)
        for start, end in self.LONG_GAP_REGIONS:
            s_ms, e_ms = start * FRAME_MS, end * FRAME_MS
            self.assertTrue(
                any(
                    i.source_start_ms <= s_ms and e_ms <= i.source_end_ms
                    for i in intervals
                ),
                f"语音段 {s_ms}~{e_ms}ms 没有被完整保留",
            )

    def test_each_side_of_a_cut_keeps_exactly_epsilon(self):
        """下刀时两边各留 KEEP_EACH_SIDE_MS —— 输出里的句间空白就是它的两倍。"""
        intervals, _, _ = cut(self.LONG_GAP, self.LONG_GAP_REGIONS)
        self.assertEqual(len(intervals), 2)
        first, second = intervals
        # 上一段语音在第 30 帧结束，下一段在第 70 帧开始
        self.assertEqual(first.source_end_ms, (30 + EPS_FRAMES) * FRAME_MS)
        self.assertEqual(second.source_start_ms, (70 - EPS_FRAMES) * FRAME_MS)

    def test_no_cut_when_floor_is_not_reached(self):
        """够不到底噪就不下刀 —— 两段连着保留，绝不硬剪。"""
        pattern = "." * 10 + "#" * 20 + "." * 3 + "#" * 20 + "." * 10
        intervals, audible, n = cut(pattern, [(10, 30), (33, 53)])
        self.assertEqual(len(intervals), 1, "间隙短于 QUIET_RUN_MS，不该下刀")
        for lo, hi in removed_spans(intervals, n):
            self.assertFalse(audible[lo:hi].any())

    def test_quiet_run_shorter_than_threshold_does_not_count(self):
        """静音必须连续够 QUIET_RUN_MS 才算数，差一帧都不行。"""
        short = "." * 10 + "#" * 20 + "." * (RUN_FRAMES - 1) + "#" * 20 + "." * 10
        long_enough = (
            "." * 10 + "#" * 20 + "." * (RUN_FRAMES + 2 * EPS_FRAMES + 1) + "#" * 20 + "." * 10
        )
        a = len(cut(short, [(10, 30), (30 + RUN_FRAMES - 1, 50 + RUN_FRAMES - 1)])[0])
        gap = RUN_FRAMES + 2 * EPS_FRAMES + 1
        b = len(cut(long_enough, [(10, 30), (30 + gap, 50 + gap)])[0])
        self.assertEqual(a, 1, "静音差一帧就不该下刀")
        self.assertEqual(b, 2, "静音够长就该下刀")

    def test_head_and_tail_silence_are_trimmed_to_epsilon(self):
        """片头片尾的静音也按同样规则处理，各留 KEEP_EACH_SIDE_MS。"""
        intervals, _, _ = cut(self.LONG_GAP, self.LONG_GAP_REGIONS)
        self.assertEqual(intervals[0].source_start_ms, (10 - EPS_FRAMES) * FRAME_MS)
        self.assertEqual(intervals[-1].source_end_ms, (90 + EPS_FRAMES) * FRAME_MS)

    def test_output_timeline_is_monotonic_with_no_gaps_or_overlaps(self):
        intervals, _, _ = cut(self.LONG_GAP, self.LONG_GAP_REGIONS)
        cursor = 0
        for item in intervals:
            self.assertEqual(item.output_start_ms, cursor)
            self.assertGreater(item.source_end_ms, item.source_start_ms)
            self.assertEqual(item.duration_ms, item.output_end_ms - item.output_start_ms)
            cursor = item.output_end_ms
        self.assertEqual(cursor, sum(i.duration_ms for i in intervals))

    def test_never_expands_the_source(self):
        """剪辑只能变短，绝不能比源还长。"""
        intervals, _, n = cut(self.LONG_GAP, self.LONG_GAP_REGIONS)
        self.assertLess(sum(i.duration_ms for i in intervals), n * FRAME_MS)

    def test_single_region(self):
        pattern = "." * 20 + "#" * 20 + "." * 20
        intervals, _, _ = cut(pattern, [(20, 40)])
        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0].source_start_ms, (20 - EPS_FRAMES) * FRAME_MS)
        self.assertEqual(intervals[0].source_end_ms, (40 + EPS_FRAMES) * FRAME_MS)

    def test_three_regions_mixed_gaps(self):
        """一段能剪、一段不能剪混在一起时，各按各的规则走。"""
        pattern = (
            "." * 10 + "#" * 15 + "." * 40 + "#" * 15 + "." * 2 + "#" * 15 + "." * 10
        )
        regions = [(10, 25), (65, 80), (82, 97)]
        intervals, audible, n = cut(pattern, regions)
        self.assertEqual(len(intervals), 2, "第一处该剪，第二处静音太短不该剪")
        for lo, hi in removed_spans(intervals, n):
            self.assertFalse(audible[lo:hi].any())
        for start, end in regions:
            s_ms, e_ms = start * FRAME_MS, end * FRAME_MS
            self.assertTrue(
                any(i.source_start_ms <= s_ms and e_ms <= i.source_end_ms for i in intervals)
            )

    def test_empty_regions_raises(self):
        _, fwd, bwd, n = score("." * 50)
        with self.assertRaises(ValueError):
            build_keep_intervals([], fwd, bwd, n)


if __name__ == "__main__":
    unittest.main()

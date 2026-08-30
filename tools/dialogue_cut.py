#!/usr/bin/env python3
"""把分离后的人声轨剪掉静音，产出只剩对白的连续音频。

设计依据见 docs/dialogue_pipeline_claude_v2.md。这一版（2026-08-30）改掉了原来
「VAD 边界两侧各留固定 300ms」的做法，理由是实测：

- VAD 给出的边界**落在声音内部**，不是声音的边缘。515 个语音段实测，VAD 起点
  之前平均还有 50ms 声音（p90 226ms，最大 460ms）；终点之后 40ms（p90 270ms）。
  清辅音本来就是从几乎无声慢慢爬上来的，任何阈值都必然触发得晚 —— 这不是 VAD
  的毛病，是「有没有声音」本身没有一条清晰的界线。
- 所以留量不能是一个常数。300ms 对某些句子多余，对另一些不够（零留量会削掉
  92.4% 的词头，300ms 仍会削掉一部分）。

改成：**逐段量出声音真正衰减到本地底噪的位置，只在那里下刀。** 够不到底噪就
不剪 —— 那意味着这里根本没有静音可剪，硬剪必然切进词里。

代价是压缩率下降且随素材而变（英语电影去掉约 23%，对白密集的动漫只去掉约
11%）。这条已评估并接受：目标是「没有空白」，不是「尽可能短」。

转写在本步**之后**跑，喂的就是本步的输出（决策 1.1，2026-08-30 翻转）。所以这里
不再需要 whisper 结果，也不再需要把词时间戳重映射回去 —— 词时间戳天然就在剪辑
后的时间轴上。timeline.json 仍然产出，供审计和回溯源时间用。

用法：
    python tools/dialogue_cut.py --stem vocals.flac --out 输出目录 --name movie_dialogue
    # 可选：用同一条时间轴也剪一份原混音，方便 A/B 试听
    python tools/dialogue_cut.py ... --original mix.wav
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
from faster_whisper.audio import decode_audio
from faster_whisper.vad import VadOptions, get_speech_timestamps
from pydub import AudioSegment

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_MS = 10
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000

# --- 剪辑规则常量 ---
FLOOR_WINDOW_MS = 2000    # 本地噪声底 = 这个窗口内的能量最小值
FLOOR_MARGIN_DB = 6       # 高于本地底噪这么多就算「有声音」
QUIET_RUN_MS = 50         # 要连续安静这么久，才算「到达底噪」；防单帧抖动骗过判定
KEEP_EACH_SIDE_MS = 30    # 下刀时两边各留的真实音频；输出里的句间空白 = 它的两倍
FADE_MS = 5               # 接缝淡入淡出，防爆音

# --- 剪辑 VAD 参数（与转写那次刻意不同，见决策 2.2）---
CUT_VAD = VadOptions(
    threshold=0.35,               # 与现有转写调校一致
    min_silence_duration_ms=200,  # 转写用的 700 太粗，短停顿根本不会成为边界
    speech_pad_ms=0,              # 留量由本脚本控制，VAD 不要自己加
)

TIMELINE_VERSION = 2


@dataclass(frozen=True)
class Interval:
    """一段被保留的音频，同时记录它在源/输出两条时间轴上的位置。"""

    source_start_ms: int
    source_end_ms: int
    output_start_ms: int
    output_end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.source_end_ms - self.source_start_ms


def frame_db(pcm: np.ndarray) -> np.ndarray:
    """把波形切成 10ms 帧，返回每帧的 dBFS。"""

    n = len(pcm) // FRAME_SAMPLES
    frames = pcm[: n * FRAME_SAMPLES].reshape(n, FRAME_SAMPLES).astype(np.float64)
    return 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-12)


def local_floor(db: np.ndarray) -> np.ndarray:
    """本地噪声底：±FLOOR_WINDOW_MS 范围内的能量最小值。

    用滚动最小值而不是全局阈值，是因为分离残留的底噪在整片里并不均匀 ——
    一个全局阈值会在安静段过于敏感、在嘈杂段完全失效。
    """

    w = FLOOR_WINDOW_MS // FRAME_MS
    nb = (len(db) + w - 1) // w
    pad = np.full(nb * w, np.inf)
    pad[: len(db)] = db
    blk = pad.reshape(nb, w).min(axis=1)
    smoothed = np.minimum(np.minimum(blk, np.roll(blk, 1)), np.roll(blk, -1))
    return np.repeat(smoothed, w)[: len(db)]


def masks_from_audible(audible: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """返回 (quiet_fwd, quiet_bwd)。

    quiet_fwd[i]: 从第 i 帧起，连续 QUIET_RUN_MS 都在底噪上
    quiet_bwd[i]: 第 i 帧之前，连续 QUIET_RUN_MS 都在底噪上

    要求「连续」而不是「单帧」，是因为静音里的能量在阈值附近会来回抖动，
    单帧判定一步就会被骗过去（实测过，刀口会落在还有声音的地方）。
    """

    run = QUIET_RUN_MS // FRAME_MS
    n = len(audible)
    acc = np.convolve(audible.astype(int), np.ones(run, dtype=int), "full")
    fwd = np.zeros(n, dtype=bool)
    bwd = np.zeros(n, dtype=bool)
    fwd[: n - run + 1] = acc[run - 1 : n] == 0
    bwd[run:] = acc[run - 1 : n - 1] == 0
    return fwd, bwd


def quiet_masks(db: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """从每帧 dBFS 算出两张静音掩码。"""

    return masks_from_audible(db > local_floor(db) + FLOOR_MARGIN_DB)


def find_speech_regions(stem_path: Path) -> tuple[list[tuple[int, int]], np.ndarray, np.ndarray, int]:
    """在人声轨上跑 VAD，同时算出静音判定所需的两张掩码。

    返回 (语音段[帧], quiet_fwd, quiet_bwd, 总帧数)。
    """

    pcm = decode_audio(str(stem_path), sampling_rate=SAMPLE_RATE)
    db = frame_db(pcm)
    fwd, bwd = quiet_masks(db)
    stamps = get_speech_timestamps(pcm, CUT_VAD, sampling_rate=SAMPLE_RATE)
    regions = [
        (s["start"] // FRAME_SAMPLES, s["end"] // FRAME_SAMPLES) for s in stamps
    ]
    logger.info("VAD 找到 %d 段语音", len(regions))
    return regions, fwd, bwd, len(db)


def build_keep_intervals(
    regions: Sequence[tuple[int, int]],
    quiet_fwd: Sequence[bool],
    quiet_bwd: Sequence[bool],
    total_frames: int,
) -> list[Interval]:
    """按新规则算出要保留哪些段，以及它们在输出时间轴上的位置。

    规则：对每个相邻语音段之间的空隙，从左边那段往右走到底噪得 k，从右边那段
    往左走到底噪得 j。若两边各留 KEEP_EACH_SIDE_MS 之后仍有余量（k+ε < j-ε），
    就在中间下刀；否则这一刀不剪，两段连着保留。

    不变量：每一刀都严格落在「已连续 QUIET_RUN_MS 到达底噪」的区间内部。
    """

    if not regions:
        raise ValueError("VAD 没找到任何语音，无法剪辑")
    if total_frames <= 0:
        raise ValueError("音频长度为 0")

    eps = KEEP_EACH_SIDE_MS // FRAME_MS
    last = total_frames - 1

    def walk_back(frame: int, limit: int) -> int:
        """从 frame 往回找最近的「到达底噪」位置，不越过 limit。"""
        i = min(max(frame, 0), last)
        while i > limit and not quiet_bwd[i]:
            i -= 1
        return i

    def walk_fwd(frame: int, limit: int) -> int:
        """从 frame 往后找最近的「到达底噪」位置，不越过 limit。"""
        i = min(max(frame, 0), last)
        while i < limit and not quiet_fwd[i]:
            i += 1
        return i

    spans: list[list[int]] = []
    cursor = max(0, walk_back(regions[0][0], 0) - eps)

    for index, (_, end) in enumerate(regions):
        is_last = index + 1 >= len(regions)
        next_start = last if is_last else regions[index + 1][0]

        k = walk_fwd(end, last if is_last else next_start)
        if is_last:
            spans.append([cursor, min(total_frames, k + eps)])
            break

        j = walk_back(next_start, k)
        if k + eps < j - eps:                 # 中间确实有一段可验证的静音
            spans.append([cursor, k + eps])
            cursor = j - eps
        # 否则不下刀：cursor 不动，两段连着留到下一轮

    intervals: list[Interval] = []
    out = 0
    for src_start, src_end in spans:
        if src_end <= src_start:
            continue
        start_ms, end_ms = src_start * FRAME_MS, src_end * FRAME_MS
        length = end_ms - start_ms
        intervals.append(Interval(start_ms, end_ms, out, out + length))
        out += length
    if not intervals:
        raise ValueError("剪辑后没有任何内容")
    return intervals


def render_audio(path: Path, intervals: Sequence[Interval]) -> AudioSegment:
    """按保留区间拼出新音频。接缝处淡入淡出，不做重叠交叉淡化。

    刻意用「对接 + 短淡入淡出」而不是重叠 crossfade：重叠会让输出比各段之和短，
    时间映射得为此做偏移补偿；对接则让 output_duration == sum(段长) 精确成立。
    接缝落在已验证到达底噪的区间内，5ms 淡入淡出足以防爆音。
    """

    source = AudioSegment.from_file(path)
    out = AudioSegment.empty()
    for item in intervals:
        clip = source[item.source_start_ms : item.source_end_ms]
        if len(clip) > 2 * FADE_MS:
            clip = clip.fade_in(FADE_MS).fade_out(FADE_MS)
        out += clip
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--stem", required=True, help="分离出的人声轨（VAD 和电平判定都跑在它上面）")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--name", default="dialogue", help="输出文件名前缀")
    ap.add_argument("--original", help="可选：用同一条时间轴也剪一份原混音")
    ap.add_argument("--format", default="mp3", help="输出音频格式（默认 mp3）")
    args = ap.parse_args()

    stem = Path(args.stem)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    regions, fwd, bwd, total_frames = find_speech_regions(stem)
    intervals = build_keep_intervals(regions, fwd, bwd, total_frames)

    total_ms = total_frames * FRAME_MS
    kept = sum(i.duration_ms for i in intervals)
    logger.info(
        "源 %.1f 分钟 -> 输出 %.1f 分钟（剪掉 %.1f%%），%d 个保留段 / %d 处刀口",
        total_ms / 60000, kept / 60000, 100 * (1 - kept / total_ms),
        len(intervals), len(intervals) - 1,
    )
    skipped = len(regions) - len(intervals)
    if skipped > 0:
        logger.info(
            "  其中 %d 处边界够不到底噪，按规则没有下刀（那里没有可剪的静音）", skipped
        )

    # --- 不变量：映射单调、无重叠、无遗漏；每段语音完整保留 ---
    cursor = 0
    for item in intervals:
        assert item.output_start_ms == cursor, "输出时间轴有空洞或重叠"
        assert item.source_end_ms > item.source_start_ms, "空区间"
        cursor = item.output_end_ms
    assert cursor == kept, "输出总长与各段之和不符"
    for start, end in regions:
        s_ms, e_ms = start * FRAME_MS, end * FRAME_MS
        assert any(
            i.source_start_ms <= s_ms and e_ms <= i.source_end_ms for i in intervals
        ), f"语音段 {s_ms}~{e_ms}ms 没有被完整保留"

    dialogue = render_audio(stem, intervals)
    assert abs(len(dialogue) - kept) <= len(intervals), "渲染后的时长与映射不符"
    audio_path = out_dir / f"{args.name}.{args.format}"
    dialogue.export(audio_path, format=args.format)
    logger.info("✓ %s（%.1f 分钟）", audio_path.name, len(dialogue) / 60000)

    if args.original:
        mix = render_audio(Path(args.original), intervals)
        mix_path = out_dir / f"{args.name}_原混音剪辑.{args.format}"
        mix.export(mix_path, format=args.format)
        logger.info("✓ %s（同一条时间轴的原混音版）", mix_path.name)

    timeline = {
        "version": TIMELINE_VERSION,
        "rule": {
            "keep_each_side_ms": KEEP_EACH_SIDE_MS,
            "floor_window_ms": FLOOR_WINDOW_MS,
            "floor_margin_db": FLOOR_MARGIN_DB,
            "quiet_run_ms": QUIET_RUN_MS,
            "fade_ms": FADE_MS,
            "vad": {
                "threshold": CUT_VAD.threshold,
                "min_silence_duration_ms": CUT_VAD.min_silence_duration_ms,
                "speech_pad_ms": CUT_VAD.speech_pad_ms,
            },
        },
        "source_duration_ms": total_ms,
        "output_duration_ms": kept,
        "intervals": [
            {
                "source_start_ms": i.source_start_ms,
                "source_end_ms": i.source_end_ms,
                "output_start_ms": i.output_start_ms,
                "output_end_ms": i.output_end_ms,
            }
            for i in intervals
        ],
    }
    (out_dir / f"{args.name}.timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2)
    )
    logger.info("✓ %s.timeline.json（仅供审计回溯源时间；转写跑在本步输出上）", args.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

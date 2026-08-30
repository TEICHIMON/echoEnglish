#!/usr/bin/env python3
"""把剪辑后的对白切成若干块，每块单独走分句 / 翻译 / Echo。

为什么需要这一步：Echo 产物大约是对白时长的 4 倍。一部 90 分钟的电影约有 60 分钟
对白，直接生成会得到 4 小时以上的单个文件，实际没法听。

切点选在**字幕之间的空隙**，而且优先选空隙最长的那处 —— 那通常是场景转换。
这样每一块都从一句话的开头开始、到一句话的结尾结束，不会把句子劈开。

用法：
    python tools/dialogue_chunk.py \
        --audio dialogue.mp3 --result dialogue.result.json \
        --out 输出目录 --minutes 10
    # 只要前 N 块：
    python tools/dialogue_chunk.py ... --limit 1
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from pydub import AudioSegment

logger = logging.getLogger(__name__)

# 切点在目标时长附近多大范围内挑「最长空隙」。给得太窄会挑不到好切点，
# 太宽则每块时长偏离目标太远。±20% 是个折中。
SEARCH_WINDOW_RATIO = 0.20


def pick_split_indices(segments: list[dict], target_ms: int) -> list[int]:
    """挑出切点（segment 下标），每个切点处开启新的一块。

    在目标时长附近的窗口里，选**前后两句间隔最大**的那个位置 —— 间隔越长
    越可能是场景转换，从那里切开语义最完整。
    """

    if not segments:
        raise ValueError("没有字幕，无法切块")

    splits: list[int] = []
    block_start_ms = 0.0

    i = 1
    while i < len(segments):
        elapsed = segments[i]["start"] * 1000 - block_start_ms
        if elapsed < target_ms * (1 - SEARCH_WINDOW_RATIO):
            i += 1
            continue

        # 进入候选窗口，收集窗口内所有位置的「与前一句的间隔」
        upper = target_ms * (1 + SEARCH_WINDOW_RATIO)
        best_idx, best_gap = i, -1.0
        j = i
        while j < len(segments):
            if segments[j]["start"] * 1000 - block_start_ms > upper:
                break
            gap = segments[j]["start"] - segments[j - 1]["end"]
            if gap > best_gap:
                best_gap, best_idx = gap, j
            j += 1

        splits.append(best_idx)
        logger.info(
            "  切点 #%d: 第 %d 句前，空隙 %.2f 秒，此块 %.1f 分钟",
            len(splits), best_idx, best_gap,
            (segments[best_idx]["start"] * 1000 - block_start_ms) / 60000,
        )
        block_start_ms = segments[best_idx]["start"] * 1000
        i = best_idx + 1

    return splits


def slice_result(result: dict, segs: list[dict], offset_s: float) -> dict:
    """把一块的字幕重新以 0 为起点，产出可直接喂给分句器的 result.json。"""

    def shift(seg: dict) -> dict:
        out = dict(seg)
        out["start"] = round(seg["start"] - offset_s, 3)
        out["end"] = round(seg["end"] - offset_s, 3)
        if seg.get("words"):
            out["words"] = [
                {**w,
                 "start": round(w["start"] - offset_s, 3),
                 "end": round(w["end"] - offset_s, 3)}
                for w in seg["words"]
            ]
        return out

    shifted = [shift(s) for s in segs]
    return {
        **{k: v for k, v in result.items() if k not in ("segments", "duration", "lrc")},
        "segments": shifted,
        "duration": round(shifted[-1]["end"], 3),
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True, help="剪辑后的对白音频")
    ap.add_argument("--result", required=True, help="重映射后的 result.json")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--minutes", type=float, default=10.0, help="每块目标时长（分钟）")
    ap.add_argument("--name", default="chunk", help="输出文件名前缀")
    ap.add_argument("--limit", type=int, help="只生成前 N 块")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = json.loads(Path(args.result).read_text())
    segments = result.get("segments") or []
    audio = AudioSegment.from_file(args.audio)
    target_ms = int(args.minutes * 60000)

    logger.info(
        "输入 %.1f 分钟 / %d 句，目标每块 %.0f 分钟",
        len(audio) / 60000, len(segments), args.minutes,
    )
    splits = pick_split_indices(segments, target_ms)

    bounds = [0, *splits, len(segments)]
    blocks = list(zip(bounds, bounds[1:]))

    # 末块太短就并进前一块。零头单独成文件很别扭：一个 2 分钟的 Echo
    # 文件既不够练，又要单独管理。阈值取目标时长的 40%。
    if len(blocks) > 1:
        lo, hi = blocks[-1]
        tail_ms = segments[hi - 1]["end"] * 1000 - segments[lo]["start"] * 1000
        if tail_ms < target_ms * 0.4:
            logger.info(
                "  末块只有 %.1f 分钟，并入前一块", tail_ms / 60000
            )
            blocks = blocks[:-2] + [(blocks[-2][0], hi)]
    if args.limit:
        blocks = blocks[: args.limit]
    logger.info("共 %d 块，本次生成 %d 块", len(bounds) - 1, len(blocks))

    for n, (lo, hi) in enumerate(blocks, start=1):
        segs = segments[lo:hi]
        # 块的音频边界取「上一句结束」到「本块最后一句结束」，都落在空隙里
        start_ms = 0 if lo == 0 else int(segments[lo - 1]["end"] * 1000)
        end_ms = len(audio) if hi >= len(segments) else int(segs[-1]["end"] * 1000)

        stem = f"{args.name}_{n:02d}"
        clip = audio[start_ms:end_ms]
        clip.export(out_dir / f"{stem}.mp3", format="mp3")

        sliced = slice_result(result, segs, start_ms / 1000)
        (out_dir / f"{stem}.result.json").write_text(
            json.dumps(sliced, ensure_ascii=False, indent=2)
        )

        # 不变量：切完的字幕时间必须落在这块音频的范围内
        assert sliced["segments"][0]["start"] >= -0.001, "块内首句时间为负"
        assert sliced["duration"] <= len(clip) / 1000 + 0.5, "块内末句超出音频长度"

        logger.info(
            "✓ %s  %.1f 分钟 / %d 句（原时间轴 %.1f~%.1f 分钟）",
            stem, len(clip) / 60000, len(segs),
            start_ms / 60000, end_ms / 60000,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

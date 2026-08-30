"""
Audio splitter module.

Extracts audio segments from a source audio file based on LRC timestamp data.
Uses pydub for audio manipulation.

关于每段的结束时间（2026-08-30）：LRC 每行只有一个开始时间，`parse_lrc` 只能
把 `end_ms` 推成「下一行的开始时间」。直接照着这个值切会同时犯两个错：

- 两句连着说、中间没停顿时，这一刀砍在词中间 —— 上一句的尾巴被削掉。
  实测约 40% 的行边界落在连续语音里。
- 反过来，一句话后面跟着长停顿时，这段停顿全被算进 clip —— 实测尾部空转
  占源时长 7.7%，Echo 里 target 播两遍所以翻倍，而且和 `after_first_target`
  配置的间隔叠加，造成节奏忽长忽短。

所以结束时间**逐句从音频量**：找到这一行最后一个有声帧，往后留 TAIL_PAD_MS。
不用上游的时间戳 —— whisper 的词 `end` 实测系统性偏早 120ms（81% 的句尾会被
切掉），比这里量出来的差。开始时间一个字节不动，所以下一行不受影响。
"""

from pathlib import Path

import numpy as np
from pydub import AudioSegment

from parser.lrc_parser import Segment

FRAME_MS = 10
SPEECH_WINDOW_MS = 2000   # 局部语音电平的窗口；用局部值而不是全局阈值，
                          # 因为一整条音频里的说话音量并不均匀
SPEECH_REL_DB = 25        # 低于局部峰值这么多算静音
HEAD_PAD_MS = 200         # 第一个有声帧之前留这么多，别削掉起音（清辅音是从
                          # 几乎无声爬上来的，起点定在能量跃起处就已经晚了）
TAIL_PAD_MS = 200         # 最后一个有声帧之后再留这么多，让尾音收干净
MAX_HEAD_EXTEND_MS = 400  # 最多允许往回退多少。退不到静音就不动 —— 那说明这一行
                          # 和上一行是连着说的，往回退只会把上一个词的尾巴拖进来。
                          # 实测无条件往前加留量会让指标从 34.9% 恶化到 52%。
MAX_TAIL_EXTEND_MS = 400  # 最多允许越过下一行起点多少。没有这个上限，
                          # 连着说的一长串里这一句会一路吞掉下一句。
                          # 400 的依据：日语素材里边界落在语音中的 41 句，
                          # 需要往后走的距离中位 40ms、最大 360ms，400 全覆盖。
                          # 英语电影素材更深（p90 590ms、最大 1010ms），那边
                          # 会有一部分收不干净 —— 这是有意的取舍：再放宽就会
                          # 把下一句的开头整段带进来，比尾巴略缺更难听。


def load_audio(audio_path: str | Path) -> AudioSegment:
    """
    Load an audio file into a pydub AudioSegment.
    Supports mp3, wav, m4a, ogg, flac, etc.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    suffix = audio_path.suffix.lower().lstrip(".")
    # Map common extensions to pydub format names
    format_map = {
        "mp3": "mp3",
        "wav": "wav",
        "m4a": "m4a",
        "aac": "aac",
        "ogg": "ogg",
        "flac": "flac",
        "wma": "wma",
    }
    fmt = format_map.get(suffix, suffix)
    return AudioSegment.from_file(str(audio_path), format=fmt)


def speech_mask(audio: AudioSegment) -> np.ndarray:
    """每 10ms 一帧，标出哪些帧有人在说话。

    判据是**相对**的：高于「附近 ±2 秒内的峰值 − 25dB」才算有声。
    用全局阈值会在安静段过敏、在响的段落失效 —— 同一条音频里不同场景的
    说话音量能差二十几 dB。
    """
    samples = np.array(audio.get_array_of_samples(), dtype=np.float64)
    if audio.channels > 1:
        samples = samples.reshape(-1, audio.channels).mean(axis=1)
    full_scale = float(2 ** (8 * audio.sample_width - 1))
    samples /= full_scale

    per_frame = max(1, audio.frame_rate * FRAME_MS // 1000)
    n = len(samples) // per_frame
    if n == 0:
        return np.zeros(0, dtype=bool)
    frames = samples[: n * per_frame].reshape(n, per_frame)
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-12)

    w = SPEECH_WINDOW_MS // FRAME_MS
    nb = (n + w - 1) // w
    pad = np.full(nb * w, -200.0)
    pad[:n] = db
    blk = pad.reshape(nb, w).max(axis=1)
    local_peak = np.repeat(
        np.maximum(np.maximum(blk, np.roll(blk, 1)), np.roll(blk, -1)), w
    )[:n]
    return (db > local_peak - SPEECH_REL_DB) & (local_peak > -55)


def resolve_start_ms(mask: np.ndarray, start_ms: int, boundary_ms: int) -> int:
    """算出一段的真实开始时间：本段第一个有声帧 − HEAD_PAD_MS。

    和 resolve_end_ms 是镜像：
    起点落在语音里 -> 往回退到这段语流的开头（受 MAX_HEAD_EXTEND_MS 限制，
    退不到就不动，说明和上一行连着说）；
    起点落在静音里 -> 往后找第一个有声帧，把开头多余的空转剪掉。
    """
    n = len(mask)
    if n == 0:
        return start_ms

    f = min(n - 1, max(1, start_ms // FRAME_MS))
    hi = min(n - 1, max(f, boundary_ms // FRAME_MS))

    if mask[f] or mask[f - 1]:
        lo = max(0, f - MAX_HEAD_EXTEND_MS // FRAME_MS)
        i = f if mask[f] else f - 1
        while i > lo and mask[i - 1]:
            i -= 1
        if i <= lo:              # 退到上限还没出语音 -> 连着说的，不动
            return start_ms
        first_audible = i
    else:
        i = f
        while i < hi and not mask[i]:
            i += 1
        if not mask[i]:          # 整段都没有声音，不动
            return start_ms
        first_audible = i

    # 留量只吃静音：碰到上一句的声音就停。否则这一段会把邻句的片段带进来 ——
    # 和「无条件加前置留量」是同一个毛病，只是轻一些。
    i = first_audible
    budget = HEAD_PAD_MS // FRAME_MS
    while i > 0 and budget > 0 and not mask[i - 1]:
        i -= 1
        budget -= 1
    return max(0, i * FRAME_MS)


def resolve_end_ms(mask: np.ndarray, start_ms: int, boundary_ms: int) -> int:
    """算出一段的真实结束时间：本段最后一个有声帧 + TAIL_PAD_MS。

    ``boundary_ms`` 是 LRC 推出来的结束时间（= 下一行的开始时间）。
    边界处还在说话 -> 往后走到这段语流结束（受 MAX_TAIL_EXTEND_MS 限制）；
    已经是静音 -> 往回找本段最后一个有声帧，把多余的空转剪掉。
    """
    n = len(mask)
    if n == 0:
        return boundary_ms + TAIL_PAD_MS

    b = min(n - 1, max(1, boundary_ms // FRAME_MS))
    lo = max(0, min(b, start_ms // FRAME_MS))

    if mask[b] or mask[b - 1]:
        cap = min(n - 1, b + MAX_TAIL_EXTEND_MS // FRAME_MS)
        i = b
        while i < cap and mask[i + 1]:
            i += 1
        last_audible = i
    else:
        i = b
        while i > lo and not mask[i]:
            i -= 1
        if not mask[i]:            # 整段都没有声音，退回原边界
            return boundary_ms + TAIL_PAD_MS
        last_audible = i

    # 同上：留量只吃静音，不越过下一句的起音
    i = last_audible + 1
    budget = TAIL_PAD_MS // FRAME_MS
    while i < n and budget > 0 and not mask[i]:
        i += 1
        budget -= 1
    return i * FRAME_MS


def extract_segment(
    audio: AudioSegment,
    segment: Segment,
    mask: np.ndarray | None = None,
) -> AudioSegment:
    """
    Extract a single audio segment based on its start and end timestamps.

    Args:
        audio: The full source AudioSegment
        segment: A Segment object with start_ms and end_ms
        mask: 可选的语音掩码（见 speech_mask）。给了就逐句重算起止时间；
              不给就按 LRC 原样切，保持旧行为。

    Returns:
        The extracted AudioSegment
    """
    start = max(0, segment.start_ms)
    end = segment.end_ms
    if mask is not None and len(mask):
        start = max(0, resolve_start_ms(mask, start, segment.end_ms))
        end = resolve_end_ms(mask, segment.start_ms, segment.end_ms)
    # 结束时间只能往后挪到源音频的尽头，且必须晚于开始
    end = min(len(audio), end)
    if end <= start:
        end = min(len(audio), max(segment.end_ms, start + FRAME_MS))
    return audio[start:end]


def extract_all_segments(
    audio: AudioSegment,
    segments: list[Segment],
    *,
    trim_tail: bool = True,
) -> list[AudioSegment]:
    """
    Extract all audio segments from the source audio.

    Args:
        audio: The full source AudioSegment
        segments: List of Segment objects
        trim_tail: 逐句从音频量起止时间（默认开）。掩码只算一次，全部段共用。

    Returns:
        List of extracted AudioSegments in the same order
    """
    mask = speech_mask(audio) if trim_tail else None
    return [extract_segment(audio, seg, mask=mask) for seg in segments]


def get_audio_duration_ms(audio_path: str | Path) -> int:
    """Get the duration of an audio file in milliseconds without loading fully."""
    audio = load_audio(audio_path)
    return len(audio)

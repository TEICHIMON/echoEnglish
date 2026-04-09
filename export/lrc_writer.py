"""
Echo Loop LRC writer module.

Generates an LRC subtitle file for the assembled Echo Loop audio.
Supports full, progressive, and shadow variants.
"""

from pathlib import Path

from pydub import AudioSegment

from audio.assembler import EchoTiming
from parser.lrc_parser import Segment


def _fmt_lrc_time(ms: int) -> str:
    """Format milliseconds into LRC timestamp: [mm:ss.xx]."""
    total_seconds = ms / 1000.0
    minutes = int(total_seconds // 60)
    seconds = total_seconds % 60
    return f"[{minutes:02d}:{seconds:05.2f}]"


def _loop_duration_ms(
    target_dur: int,
    native_dur: int,
    timing: EchoTiming,
) -> int:
    """Calculate the total duration in ms of one T-S-?-S-T-S loop."""
    return (
        target_dur
        + int(timing.after_first_target * 1000)
        + native_dur  # actual audio or equal-length silence
        + int(timing.after_native * 1000)
        + target_dur
        + int(timing.after_second_target * 1000)
    )


def generate_echo_lrc(
    segments: list[Segment],
    target_audios: list[AudioSegment],
    native_audios: list[AudioSegment],
    timing: EchoTiming,
    output_path: str | Path,
    delimiter: str = "-",
    variant: str = "full",
) -> Path:
    """
    Generate an LRC subtitle file matching the Echo Loop audio.

    Args:
        segments: Original parsed segments (for text content)
        target_audios: Target language AudioSegments
        native_audios: Native language TTS AudioSegments
        timing: EchoTiming configuration
        output_path: Where to write the .lrc file
        delimiter: Delimiter between target and native text
        variant: "full", "progressive", or "shadow"

    Returns:
        Path to the written LRC file
    """
    if len(segments) != len(target_audios) or len(segments) != len(native_audios):
        raise ValueError(
            f"Length mismatch: {len(segments)} segments, "
            f"{len(target_audios)} target audios, "
            f"{len(native_audios)} native audios"
        )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    pos_ms = 0

    for i, seg in enumerate(segments):
        target_dur = len(target_audios[i])
        native_dur = len(native_audios[i])
        text = f"{seg.target_text}{delimiter}{seg.native_text}"
        loop_dur = _loop_duration_ms(target_dur, native_dur, timing)

        if variant == "shadow":
            lines.append(f"{_fmt_lrc_time(pos_ms)}{text}")
            pos_ms += loop_dur

        elif variant == "progressive":
            # Pass 1: full (with native audio)
            lines.append(f"{_fmt_lrc_time(pos_ms)}{text}")
            pos_ms += loop_dur
            # Pass 2: shadow (native replaced with silence, same duration)
            lines.append(f"{_fmt_lrc_time(pos_ms)}{text}")
            pos_ms += loop_dur

        else:  # full
            lines.append(f"{_fmt_lrc_time(pos_ms)}{text}")
            pos_ms += loop_dur

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"  LRC written: {output_path} ({len(lines)} lines)")
    return output_path
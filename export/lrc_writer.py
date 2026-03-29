"""
Echo Loop LRC writer module.

Generates an LRC subtitle file for the assembled Echo Loop audio.
One line per loop, preserving the original bilingual text format
(target + delimiter + native). Only the timestamps are recalculated
to match the T-S-N-S-T-S assembled audio.
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


def generate_echo_lrc(
    segments: list[Segment],
    target_audios: list[AudioSegment],
    native_audios: list[AudioSegment],
    timing: EchoTiming,
    output_path: str | Path,
    delimiter: str = "-",
) -> Path:
    """
    Generate an LRC subtitle file matching the Echo Loop audio.

    One line per loop with the original bilingual text. Timestamps are
    recalculated by walking through the same T-S-N-S-T-S structure
    used by the assembler.

    Args:
        segments: Original parsed segments (for text content)
        target_audios: Target language AudioSegments
        native_audios: Native language TTS AudioSegments
        timing: EchoTiming configuration (silence durations)
        output_path: Where to write the .lrc file
        delimiter: Delimiter between target and native text in output

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

    silence_after_t1_ms = int(timing.after_first_target * 1000)
    silence_after_n_ms = int(timing.after_native * 1000)
    silence_after_t2_ms = int(timing.after_second_target * 1000)

    lines: list[str] = []
    pos_ms = 0

    for i, seg in enumerate(segments):
        target_dur = len(target_audios[i])
        native_dur = len(native_audios[i])

        # Record the loop start timestamp with original bilingual text
        text = f"{seg.target_text}{delimiter}{seg.native_text}"
        lines.append(f"{_fmt_lrc_time(pos_ms)}{text}")

        # Advance through T-S-N-S-T-S to find next loop's start
        pos_ms += target_dur                # T1
        pos_ms += silence_after_t1_ms       # S1
        pos_ms += native_dur                # N
        pos_ms += silence_after_n_ms        # S2
        pos_ms += target_dur                # T2
        pos_ms += silence_after_t2_ms       # S3

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"  LRC written: {output_path} ({len(lines)} lines)")
    return output_path
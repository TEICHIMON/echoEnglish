"""Exact timing helpers shared by TTS and hybrid-audio workflows.

The core invariant is deliberately small: final text segmentation happens before
audio generation, and every timestamp comes from the decoded length of the audio
clip that represents that exact segment.  No text-length timing estimates belong
in an output pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from pydub import AudioSegment


ACTUAL_AUDIO_TIMING = "actual_audio_duration"


@dataclass(frozen=True)
class TimedAudioSegment:
    """A final subtitle segment with an exact audio-derived interval."""

    text: str
    start_ms: int
    end_ms: int
    timing_source: str = ACTUAL_AUDIO_TIMING

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def time_audio_segments(
    texts: Sequence[str],
    audios: Sequence[AudioSegment],
    *,
    start_ms: int = 0,
) -> list[TimedAudioSegment]:
    """Assign exact contiguous intervals using each decoded clip's duration.

    ``texts`` must already be in their final subtitle segmentation.  This
    function intentionally accepts no character-count or speaking-rate inputs.
    """

    if len(texts) != len(audios):
        raise ValueError(
            f"Exact timing requires a 1:1 mapping: {len(texts)} texts vs "
            f"{len(audios)} audio clips"
        )
    if start_ms < 0:
        raise ValueError("start_ms cannot be negative")

    cursor = start_ms
    timed: list[TimedAudioSegment] = []
    for index, (text, audio) in enumerate(zip(texts, audios)):
        if not text.strip():
            raise ValueError(f"Empty final subtitle text at index {index}")
        duration_ms = len(audio)
        if duration_ms <= 0:
            raise ValueError(f"Empty audio clip at index {index}")
        timed.append(
            TimedAudioSegment(
                text=text,
                start_ms=cursor,
                end_ms=cursor + duration_ms,
            )
        )
        cursor += duration_ms
    return timed


def require_actual_audio_timing(timing_source: str) -> None:
    """Reject estimated timing provenance at an output boundary."""

    if timing_source != ACTUAL_AUDIO_TIMING:
        raise ValueError(
            "TTS/LRC timing must come from decoded audio duration; "
            f"got {timing_source!r}"
        )

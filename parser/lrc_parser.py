"""
LRC subtitle parser.

Parses LRC format files with bilingual content separated by a delimiter.
Example LRC line:
  [00:00.39]一度の接種で...MMRワクチンについて-关于一次接种即可预防...的MMR疫苗

Produces a list of Segment objects with start/end times and both language texts.
"""

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Segment:
    """A single subtitle segment with timing and bilingual text."""
    index: int
    start_ms: int          # start time in milliseconds
    end_ms: int            # end time in milliseconds (derived from next segment's start)
    target_text: str       # target language text (Japanese/English)
    native_text: str       # native language text (Chinese)

    @property
    def start_sec(self) -> float:
        return self.start_ms / 1000.0

    @property
    def end_sec(self) -> float:
        return self.end_ms / 1000.0

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def __repr__(self) -> str:
        return (
            f"Segment({self.index}, "
            f"{self._fmt_time(self.start_ms)}→{self._fmt_time(self.end_ms)}, "
            f"T=\"{self.target_text[:20]}...\", "
            f"N=\"{self.native_text[:20]}...\")"
        )

    @staticmethod
    def _fmt_time(ms: int) -> str:
        s = ms / 1000.0
        m = int(s // 60)
        s = s % 60
        return f"{m:02d}:{s:05.2f}"


# Regex to match LRC timestamp: [mm:ss.xx] or [mm:ss.xxx]
LRC_PATTERN = re.compile(r"\[(\d{2}):(\d{2})\.(\d{2,3})\](.+)")


def _parse_timestamp(minutes: str, seconds: str, centis: str) -> int:
    """Convert LRC timestamp components to milliseconds."""
    ms = int(minutes) * 60 * 1000 + int(seconds) * 1000
    # Handle both centiseconds (2 digits) and milliseconds (3 digits)
    if len(centis) == 2:
        ms += int(centis) * 10
    else:
        ms += int(centis)
    return ms


def _split_bilingual(text: str, delimiter: str, strategy: str = "last") -> tuple[str, str]:
    """
    Split a bilingual text line into target and native parts.
    
    Args:
        text: The full text line containing both languages
        delimiter: The character(s) separating the two languages
        strategy: "last" splits on the last occurrence, "first" on the first
        
    Returns:
        Tuple of (target_text, native_text)
    """
    if delimiter not in text:
        # No delimiter found, treat entire text as target, empty native
        return text.strip(), ""

    if strategy == "last":
        idx = text.rfind(delimiter)
    else:
        idx = text.find(delimiter)

    target = text[:idx].strip()
    native = text[idx + len(delimiter):].strip()
    return target, native


def parse_lrc(
    lrc_path: str | Path,
    delimiter: str = "-",
    split_strategy: str = "last",
    audio_duration_ms: int | None = None,
) -> list[Segment]:
    """
    Parse an LRC file into a list of Segments.
    
    Each segment's end_ms is set to the next segment's start_ms.
    The last segment's end_ms is set to audio_duration_ms if provided,
    otherwise it is estimated by adding 5 seconds to its start.
    
    Args:
        lrc_path: Path to the .lrc file
        delimiter: Character separating target and native text
        split_strategy: "last" or "first" - where to split on delimiter
        audio_duration_ms: Total audio duration in ms (for last segment)
        
    Returns:
        List of Segment objects
    """
    lrc_path = Path(lrc_path)
    if not lrc_path.exists():
        raise FileNotFoundError(f"LRC file not found: {lrc_path}")

    raw_entries: list[tuple[int, str, str]] = []  # (start_ms, target, native)

    with open(lrc_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            match = LRC_PATTERN.match(line)
            if not match:
                continue

            minutes, seconds, centis, text = match.groups()
            start_ms = _parse_timestamp(minutes, seconds, centis)
            target_text, native_text = _split_bilingual(text, delimiter, split_strategy)

            # Skip lines with no native text (could be metadata lines)
            if not native_text:
                continue

            raw_entries.append((start_ms, target_text, native_text))

    if not raw_entries:
        raise ValueError(f"No valid bilingual entries found in {lrc_path}")

    # Sort by start time
    raw_entries.sort(key=lambda x: x[0])

    # Build segments with end times
    segments: list[Segment] = []
    for i, (start_ms, target, native) in enumerate(raw_entries):
        if i + 1 < len(raw_entries):
            end_ms = raw_entries[i + 1][0]
        elif audio_duration_ms is not None:
            end_ms = audio_duration_ms
        else:
            # Fallback: estimate 5 seconds for last segment
            end_ms = start_ms + 5000

        segments.append(Segment(
            index=i,
            start_ms=start_ms,
            end_ms=end_ms,
            target_text=target,
            native_text=native,
        ))

    return segments

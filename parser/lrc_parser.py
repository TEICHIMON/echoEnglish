"""
LRC subtitle parser.

Parses LRC format files with bilingual content separated by a delimiter.
Example LRC line:
  [00:00.39]一度の接種で...MMRワクチンについて-关于一次接种即可预防...的MMR疫苗

Produces a list of Segment objects with start/end times and both language texts.
"""

import logging
import re
import statistics
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Guard against a truncated LRC swallowing the tail of the audio. The last
# segment normally runs to the end of the file, but if the subtitles stop early
# that single segment would cover everything after them — and the Echo Loop
# repeats it, turning minutes of untranslated audio into hours of output. Cap it
# at a few times the typical segment length instead, and say so.
_LAST_SEGMENT_MAX_FACTOR = 3
_LAST_SEGMENT_MIN_CAP_MS = 30_000
_LAST_SEGMENT_FALLBACK_MS = 5_000


# Furigana annotation like 漢字（かんじ）/ 2024年（にせんにじゅうよねん）: full- or
# half-width parentheses whose content is entirely kana (hiragana + katakana +
# the long-vowel mark ー). Only all-kana parentheticals match, so this is a
# no-op for English/Chinese and for Japanese without furigana.
_FURIGANA_RE = re.compile(r"[（(][぀-ヿ]+[)）]")


def strip_furigana(text: str) -> str:
    """Remove parenthetical kana readings (furigana) from text.

    Kept in the subtitle for readability, but stripped before TTS so the
    reading kana isn't spoken a second time.
    """
    return _FURIGANA_RE.sub("", text)


@dataclass
class Segment:
    """A single subtitle segment with timing and bilingual text."""
    index: int
    start_ms: int          # start time in milliseconds
    end_ms: int            # end time in milliseconds (derived from next segment's start)
    target_text: str       # target language text (Japanese/English)
    native_text: str       # native language text (Chinese)
    role: str = ""         # optional speaker role, e.g. "q" / "a" in interview mode
    target_tts_text: str = ""  # target text for TTS; furigana stripped (see __post_init__)

    def __post_init__(self) -> None:
        # Keep furigana in target_text (shown in the LRC subtitle) but feed a
        # kana-stripped version to TTS so the reading isn't spoken twice.
        if not self.target_tts_text:
            self.target_tts_text = strip_furigana(self.target_text)

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
SPEAKER_PREFIX_PATTERN = re.compile(
    r"^\s*(q|question|interviewer|a|answer|candidate|interviewee)\s*[:：]\s*(.+)$",
    re.IGNORECASE,
)


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
        return _strip_speaker_prefix(text), ""

    if strategy == "last":
        idx = text.rfind(delimiter)
    else:
        idx = text.find(delimiter)

    target = _strip_speaker_prefix(text[:idx])
    native = _strip_speaker_prefix(text[idx + len(delimiter):])
    return target, native


def _strip_speaker_prefix(text: str) -> str:
    """Remove leading interview/transcript role markers before TTS."""
    text = text.strip()
    match = SPEAKER_PREFIX_PATTERN.match(text)
    if match:
        return match.group(2).strip()
    return text


def _resolve_last_end_ms(
    raw_entries: list[tuple[int, str, str]],
    audio_duration_ms: int | None,
    lrc_path: Path,
) -> int:
    """End time for the final segment, capped so a short LRC can't run away.

    Normally the last subtitle runs to the end of the audio. When the subtitles
    stop well before the audio does — a truncated transcription or translation —
    that would make one segment tens of minutes long, and the Echo Loop repeats
    every segment several times. Cap it and report the uncovered tail.
    """
    start_ms = raw_entries[-1][0]

    if audio_duration_ms is None:
        return start_ms + _LAST_SEGMENT_FALLBACK_MS

    tail_ms = audio_duration_ms - start_ms
    if len(raw_entries) < 2:
        return audio_duration_ms

    gaps = [
        raw_entries[i + 1][0] - raw_entries[i][0]
        for i in range(len(raw_entries) - 1)
    ]
    cap_ms = max(
        _LAST_SEGMENT_MIN_CAP_MS,
        int(statistics.median(gaps) * _LAST_SEGMENT_MAX_FACTOR),
    )
    if tail_ms <= cap_ms:
        return audio_duration_ms

    logger.warning(
        "  ⚠ Subtitles end at %s but the audio runs to %s — %s of audio has no "
        "subtitles. Capping the final segment at %s instead of letting it "
        "absorb the tail. Check %s for a truncated transcription/translation.",
        _fmt_ms(start_ms), _fmt_ms(audio_duration_ms), _fmt_ms(tail_ms),
        _fmt_ms(cap_ms), lrc_path.name,
    )
    return start_ms + cap_ms


def _fmt_ms(ms: int) -> str:
    total_seconds = ms // 1000
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"


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
    untranslated: list[int] = []  # line numbers of timestamped lines with no translation

    with open(lrc_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue

            match = LRC_PATTERN.match(line)
            if not match:
                continue

            minutes, seconds, centis, text = match.groups()
            start_ms = _parse_timestamp(minutes, seconds, centis)
            target_text, native_text = _split_bilingual(text, delimiter, split_strategy)

            # Timestamped but untranslated. Usually a truncated translation pass
            # rather than metadata, so record it instead of dropping it quietly.
            if not native_text:
                untranslated.append(line_no)
                continue

            raw_entries.append((start_ms, target_text, native_text))

    if untranslated:
        preview = ", ".join(str(n) for n in untranslated[:5])
        if len(untranslated) > 5:
            preview += f", ... (+{len(untranslated) - 5} more)"
        logger.warning(
            "  ⚠ Skipped %d timestamped line(s) with no '%s' translation in %s "
            "(line %s). These produce no audio — check whether the translation "
            "pass was truncated.",
            len(untranslated), delimiter, lrc_path.name, preview,
        )

    if not raw_entries:
        raise ValueError(f"No valid bilingual entries found in {lrc_path}")

    # Sort by start time
    raw_entries.sort(key=lambda x: x[0])

    last_end_ms = _resolve_last_end_ms(raw_entries, audio_duration_ms, lrc_path)

    # Build segments with end times
    segments: list[Segment] = []
    for i, (start_ms, target, native) in enumerate(raw_entries):
        if i + 1 < len(raw_entries):
            end_ms = raw_entries[i + 1][0]
        else:
            end_ms = last_end_ms

        segments.append(Segment(
            index=i,
            start_ms=start_ms,
            end_ms=end_ms,
            target_text=target,
            native_text=native,
        ))

    return segments

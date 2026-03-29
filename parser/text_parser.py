"""
Text input parser for text-only TTS mode.

Parses a simple text file with bilingual entries (one per line)
into Segment objects — no timestamps or source audio required.

Supported format:
    target_text<delimiter>native_text

Example (delimiter = "-"):
    一度の接種でハシカのMMRワクチンについて-关于一次接种即可预防麻疹的MMR疫苗
    厚生労働省の専門家部会は了承しました-厚生劳动省的专家委员会已批准

Lines starting with # are comments. Blank lines are ignored.
"""

from pathlib import Path

from parser.lrc_parser import Segment, _split_bilingual


def parse_text(
    text_path: str | Path,
    delimiter: str = "-",
    split_strategy: str = "last",
) -> list[Segment]:
    """
    Parse a bilingual text file into Segment objects.

    Since there is no source audio, start_ms / end_ms are set to 0
    and will not be used — both target and native audio are generated
    via TTS in text-only mode.

    Args:
        text_path: Path to the text file
        delimiter: Character(s) separating target and native text
        split_strategy: "last" or "first" — where to split on delimiter

    Returns:
        List of Segment objects (timestamps are placeholders)
    """
    text_path = Path(text_path)
    if not text_path.exists():
        raise FileNotFoundError(f"Text file not found: {text_path}")

    segments: list[Segment] = []

    with open(text_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            target_text, native_text = _split_bilingual(
                line, delimiter, split_strategy
            )

            if not target_text or not native_text:
                continue

            segments.append(
                Segment(
                    index=len(segments),
                    start_ms=0,
                    end_ms=0,
                    target_text=target_text,
                    native_text=native_text,
                )
            )

    if not segments:
        raise ValueError(f"No valid bilingual entries found in {text_path}")

    return segments
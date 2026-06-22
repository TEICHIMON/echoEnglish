"""
Mock interview parser.

Parses role-marked bilingual interview text into Segment objects. Each role
line still uses the regular target/native delimiter:

    Q:What would you improve next?|||接下来你会改进什么？
    A:I would improve replay tooling.|||我会改进 replay 工具。

Supported role prefixes:
    Q: / Question: / Interviewer:
    A: / Answer: / Candidate: / Interviewee:
"""

import re
from pathlib import Path

from parser.lrc_parser import Segment, _split_bilingual


ROLE_PATTERN = re.compile(
    r"^(q|question|interviewer|a|answer|candidate|interviewee)\s*[:：]\s*(.*)$",
    re.IGNORECASE,
)

QUESTION_ROLES = {"q", "question", "interviewer"}
ANSWER_ROLES = {"a", "answer", "candidate", "interviewee"}


def _role_kind(role: str) -> str:
    role = role.lower()
    if role in QUESTION_ROLES:
        return "q"
    if role in ANSWER_ROLES:
        return "a"
    raise ValueError(f"Unknown interview role: {role}")


def _flush_block(
    blocks: list[tuple[str, str, int]],
    current_role: str | None,
    current_lines: list[str],
    line_no: int,
) -> None:
    if current_role is None:
        return
    text = " ".join(part.strip() for part in current_lines if part.strip()).strip()
    if text:
        blocks.append((current_role, text, line_no))


def parse_interview_text(
    text_path: str | Path,
    delimiter: str = "|||",
    split_strategy: str = "last",
) -> list[Segment]:
    """
    Parse a bilingual Q/A mock interview text file into Segment objects.

    Lines without a role prefix continue the previous Q or A block, which keeps
    longer entries readable. Every block must contain the bilingual delimiter.
    """
    text_path = Path(text_path)
    if not text_path.exists():
        raise FileNotFoundError(f"Interview text file not found: {text_path}")

    blocks: list[tuple[str, str, int]] = []
    current_role: str | None = None
    current_lines: list[str] = []
    current_start_line = 0

    with open(text_path, "r", encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            match = ROLE_PATTERN.match(line)
            if match:
                _flush_block(
                    blocks, current_role, current_lines, current_start_line,
                )
                current_role = _role_kind(match.group(1))
                current_lines = [match.group(2)]
                current_start_line = line_no
            elif current_role is not None:
                current_lines.append(line)
            else:
                raise ValueError(
                    f"Line {line_no}: expected Q: or A: role prefix before text"
                )

    _flush_block(blocks, current_role, current_lines, current_start_line)

    if not blocks:
        raise ValueError(f"No valid interview entries found in {text_path}")

    segments: list[Segment] = []
    for role, text, line_no in blocks:
        target_text, native_text = _split_bilingual(
            text, delimiter, split_strategy,
        )
        if not target_text or not native_text:
            raise ValueError(
                f"Line {line_no}: expected '<English>{delimiter}<translation>' "
                "after the role prefix"
            )

        segments.append(
            Segment(
                index=len(segments),
                start_ms=0,
                end_ms=0,
                target_text=target_text,
                native_text=native_text,
                role=role,
            )
        )

    return segments

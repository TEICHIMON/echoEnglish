#!/usr/bin/env python3
"""Merge ID-keyed translations with extracted EPUB units.

Produces an auditable bilingual JSONL file and a paste-ready Echo English
``target|||native`` text file.  Translation text stays separate from source
text so missing, duplicate, or unexpected IDs can be rejected before output.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


DELIMITER = "|||"

# The source EPUB contains a small set of repeatable conversion artifacts.
# They are repaired only in generated bilingual/audio files; source_units.jsonl
# remains a faithful extraction that can always be audited against the EPUB.
SOURCE_TEXT_REPLACEMENTS = {
    "iteMs": "items",
    "exaMs": "exams",
    "jaMs": "jams",
    "firSt": "first",
    "exiSt": "exist",
    "sawduSt": "sawdust",
    "probleMs": "problems",
    "loSt": "lost",
    "podcaSt": "podcast",
    "coSt": "cost",
    "faSt": "fast",
    "terMs": "terms",
    "mechanisMs": "mechanisms",
    "rent,and": "rent, and",
    "language,in": "language, in",
    "mother — She": "mother—she",
    "lips—moved": "lips moved",
    "It was—explosive.": "It was explosive.",
    "language wall—cracked": "language wall cracked",
    "His hand—moved": "His hand moved",
    "T • MT• T": "T • MT • T",
}


class BilingualBuildError(RuntimeError):
    """Raised when source and translation units do not align exactly."""


def clean_english_for_tts(text: str) -> str:
    """Repair known EPUB artifacts without changing the archival source."""

    cleaned = text
    for source, replacement in SOURCE_TEXT_REPLACEMENTS.items():
        cleaned = cleaned.replace(source, replacement)
    return cleaned


def read_source(path: Path, section_id: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as source_file:
        for line_number, line in enumerate(source_file, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BilingualBuildError(
                    f"Invalid source JSON on line {line_number}: {exc}"
                ) from exc
            if record.get("section_id") == section_id:
                records.append(record)

    if not records:
        raise BilingualBuildError(f"No source units found for section: {section_id}")
    ids = [str(record.get("id", "")) for record in records]
    if not all(ids) or len(ids) != len(set(ids)):
        raise BilingualBuildError("Source unit IDs are empty or duplicated")
    return records


def read_translations(path: Path) -> dict[str, str]:
    translations: dict[str, str] = {}
    with path.open(encoding="utf-8", newline="") as translation_file:
        reader = csv.DictReader(translation_file, delimiter="\t")
        if reader.fieldnames != ["id", "zh"]:
            raise BilingualBuildError("Translation TSV header must be: id<TAB>zh")
        for line_number, row in enumerate(reader, 2):
            unit_id = (row.get("id") or "").strip()
            chinese = (row.get("zh") or "").strip()
            if not unit_id or not chinese:
                raise BilingualBuildError(
                    f"Empty translation ID or text on TSV line {line_number}"
                )
            if unit_id in translations:
                raise BilingualBuildError(f"Duplicate translation ID: {unit_id}")
            if "\n" in chinese or "\r" in chinese or DELIMITER in chinese:
                raise BilingualBuildError(f"Unsafe translation text for ID: {unit_id}")
            translations[unit_id] = chinese
    return translations


def build(
    source_path: Path,
    translation_path: Path,
    section_id: str,
    jsonl_output: Path,
    web_output: Path,
) -> int:
    source_records = read_source(source_path, section_id)
    translations = read_translations(translation_path)
    source_ids = {str(record["id"]) for record in source_records}
    translation_ids = set(translations)

    missing = sorted(source_ids - translation_ids)
    unexpected = sorted(translation_ids - source_ids)
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        raise BilingualBuildError("Translation IDs do not align (" + "; ".join(details) + ")")

    jsonl_output.parent.mkdir(parents=True, exist_ok=True)
    web_output.parent.mkdir(parents=True, exist_ok=True)
    web_lines: list[str] = []
    with jsonl_output.open("w", encoding="utf-8") as jsonl_file:
        for source in source_records:
            unit_id = str(source["id"])
            source_english = str(source.get("en", "")).strip()
            english = clean_english_for_tts(source_english)
            chinese = translations[unit_id]
            if not english or "\n" in english or "\r" in english or DELIMITER in english:
                raise BilingualBuildError(f"Unsafe English source text for ID: {unit_id}")
            bilingual = dict(source)
            if english != source_english:
                bilingual["source_en"] = source_english
                bilingual["en"] = english
            bilingual["zh"] = chinese
            jsonl_file.write(json.dumps(bilingual, ensure_ascii=False) + "\n")
            web_lines.append(f"{english}{DELIMITER}{chinese}")

    web_output.write_text("\n".join(web_lines) + "\n", encoding="utf-8")
    return len(source_records)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build bilingual JSONL and Echo website text from ID-keyed translations."
    )
    parser.add_argument("source", type=Path, help="Extracted source_units.jsonl")
    parser.add_argument("translations", type=Path, help="TSV with id and zh columns")
    parser.add_argument("section_id", help="Section ID to build, for example sec-01")
    parser.add_argument("jsonl_output", type=Path, help="Bilingual review JSONL output")
    parser.add_argument("web_output", type=Path, help="Paste-ready target|||native output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        count = build(
            args.source,
            args.translations,
            args.section_id,
            args.jsonl_output,
            args.web_output,
        )
    except (BilingualBuildError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Built {count} bilingual units")
    print(f"Review JSONL: {args.jsonl_output.resolve()}")
    print(f"Website text: {args.web_output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

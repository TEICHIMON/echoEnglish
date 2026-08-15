#!/usr/bin/env python3
"""Combine translated EPUB sections into chapter-sized Echo packages.

EPUB navigation often stores epigraphs and part dividers as separate sections.
That is useful for archival extraction but produces poor two-line audio files.
This tool keeps those source sections intact and assembles a second, audio-ready
layer according to a declarative package plan.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


DELIMITER = "|||"


class PackageBuildError(RuntimeError):
    """Raised when translated sections cannot be packaged safely."""


def read_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise PackageBuildError(f"Invalid JSON in {path}:{line_number}: {exc}") from exc
    if not records:
        raise PackageBuildError(f"Empty bilingual file: {path}")
    return records


def discover_sections(translated_dir: Path) -> dict[str, tuple[Path, list[dict[str, object]]]]:
    sections: dict[str, tuple[Path, list[dict[str, object]]]] = {}
    for path in sorted(translated_dir.glob("*.bilingual.jsonl")):
        records = read_jsonl(path)
        section_ids = {str(record.get("section_id", "")) for record in records}
        if len(section_ids) != 1 or "" in section_ids:
            raise PackageBuildError(f"Expected exactly one section in: {path}")
        section_id = next(iter(section_ids))
        if section_id in sections:
            raise PackageBuildError(f"Duplicate bilingual section: {section_id}")
        sections[section_id] = (path, records)
    return sections


def build_packages(plan_path: Path, translated_dir: Path, output_dir: Path) -> dict[str, object]:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PackageBuildError(f"Invalid package plan: {exc}") from exc
    packages = plan.get("packages")
    if not isinstance(packages, list) or not packages:
        raise PackageBuildError("Package plan must contain a non-empty 'packages' list")

    available = discover_sections(translated_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    used_sections: set[str] = set()
    package_manifest: list[dict[str, object]] = []

    for package in packages:
        name = str(package.get("name", "")).strip()
        title = str(package.get("title", "")).strip()
        section_ids = package.get("sections")
        if not name or not title or not isinstance(section_ids, list) or not section_ids:
            raise PackageBuildError("Each package needs name, title, and sections")
        if any(section_id in used_sections for section_id in section_ids):
            raise PackageBuildError(f"A section is assigned more than once in package: {name}")

        records: list[dict[str, object]] = []
        source_files: list[str] = []
        for section_id in section_ids:
            if section_id not in available:
                raise PackageBuildError(
                    f"Translated section {section_id} required by {name} was not found"
                )
            path, section_records = available[section_id]
            records.extend(section_records)
            source_files.append(path.name)
            used_sections.add(section_id)

        ids = [str(record.get("id", "")) for record in records]
        if not all(ids) or len(ids) != len(set(ids)):
            raise PackageBuildError(f"Empty or duplicate unit IDs in package: {name}")

        jsonl_path = output_dir / f"{name}.bilingual.jsonl"
        website_path = output_dir / f"{name}.website.txt"
        jsonl_path.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )
        website_path.write_text(
            "".join(f"{record['en']}{DELIMITER}{record['zh']}\n" for record in records),
            encoding="utf-8",
        )
        package_manifest.append(
            {
                "name": name,
                "title": title,
                "sections": section_ids,
                "units": len(records),
                "source_files": source_files,
                "bilingual_jsonl": jsonl_path.name,
                "website_text": website_path.name,
            }
        )

    result = {
        "schema_version": 1,
        "package_count": len(package_manifest),
        "unit_count": sum(int(package["units"]) for package in package_manifest),
        "packages": package_manifest,
    }
    (output_dir / "package_manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build chapter-sized Echo packages.")
    parser.add_argument("plan", type=Path, help="JSON package plan")
    parser.add_argument("translated_dir", type=Path, help="Directory of section bilingual JSONL")
    parser.add_argument("output_dir", type=Path, help="Package output directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = build_packages(args.plan, args.translated_dir, args.output_dir)
    except (PackageBuildError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Built {manifest['package_count']} packages with "
        f"{manifest['unit_count']} bilingual units"
    )
    print(f"Output: {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

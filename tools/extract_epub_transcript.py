#!/usr/bin/env python3
"""Extract an EPUB into a reviewable transcript and stable translation units.

The extractor intentionally produces an intermediate representation instead of
the web app's ``English|||Chinese`` format.  Keeping source units and stable IDs
separate makes later AI translation resumable and auditable.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import csv
import json
import posixpath
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import unquote
from zipfile import BadZipFile, ZipFile
from xml.etree import ElementTree as ET


BLOCK_TAGS = {
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "blockquote",
    "dt",
    "dd",
}

DEFAULT_SKIPPED_SECTIONS = {
    "title page",
    "contents",
    "copyright",
}

DECORATIVE_IMAGE_ALTS = {
    "brain",
    "bullseye",
    "check mark button",
    "gear",
    "globe with meridians",
    "green book",
    "high voltage",
    "light bulb",
    "old key",
    "repeat button",
    "small blue diamond",
}

INLINE_IMAGE_TEXT = {
    "digit zero, variation selector-16, combining enclosing keycap": "0",
    "digit one, variation selector-16, combining enclosing keycap": "1",
    "digit two, variation selector-16, combining enclosing keycap": "2",
    "digit three, variation selector-16, combining enclosing keycap": "3",
    "digit four, variation selector-16, combining enclosing keycap": "4",
    "digit five, variation selector-16, combining enclosing keycap": "5",
    "digit six, variation selector-16, combining enclosing keycap": "6",
    "digit seven, variation selector-16, combining enclosing keycap": "7",
    "digit eight, variation selector-16, combining enclosing keycap": "8",
    "digit nine, variation selector-16, combining enclosing keycap": "9",
    "minus": "−",
    "wavy dash": "~",
}

ABBREVIATIONS = (
    "e.g.",
    "i.e.",
    "etc.",
    "vs.",
    "Mr.",
    "Mrs.",
    "Ms.",
    "Dr.",
    "Prof.",
    "Sr.",
    "Jr.",
    "St.",
    "Fig.",
    "No.",
    "U.S.",
    "U.K.",
)

WORD_RE = re.compile(r"\b[\w’'-]+\b", re.UNICODE)


class EpubExtractionError(RuntimeError):
    """Raised for invalid or unsupported EPUB structure."""


@dataclass
class TocEntry:
    title: str
    href: str
    order: int


@dataclass
class TextBlock:
    tag: str
    text: str
    source_href: str
    source_block_index: int


@dataclass
class ImageUse:
    source_href: str
    source_block_index: int | None
    src: str
    alt: str
    context: str
    kind: str


@dataclass
class Section:
    number: int
    title: str
    toc_href: str
    source_hrefs: list[str] = field(default_factory=list)
    blocks: list[TextBlock] = field(default_factory=list)
    images: list[ImageUse] = field(default_factory=list)

    @property
    def section_id(self) -> str:
        return f"sec-{self.number:02d}"


def local_name(tag: str) -> str:
    """Return an XML tag name without its namespace."""

    return tag.rsplit("}", 1)[-1].lower()


def normalize_zip_path(base: PurePosixPath, href: str) -> str:
    """Resolve a percent-encoded EPUB href to a safe archive member path."""

    decoded = unquote(href.split("#", 1)[0])
    resolved = posixpath.normpath(str(base / decoded))
    if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
        raise EpubExtractionError(f"Unsafe EPUB path: {href}")
    return resolved


def normalize_text(text: str) -> str:
    """Normalize ebook spacing while preserving authored punctuation."""

    text = unicodedata.normalize("NFC", text)
    text = (
        text.replace("\u00a0", " ")
        .replace("\u2007", " ")
        .replace("\u202f", " ")
        .replace("\u2060", "")
        .replace("\ufeff", "")
        .replace("\u200b", "")
        .replace("\u00ad", "")
    )
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([([])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]])", r"\1", text)
    # Some EPUB exports omit the space after an ellipsis or bibliography year
    # (for example, ``learning…These`` and ``(2006).Toward``).  Requiring a
    # capital followed by lowercase avoids changing filenames such as ``1.WAV``.
    text = re.sub(r"([.!?…])(?=[A-Z][a-z])", r"\1 ", text)
    return text


def image_alt_to_inline_text(alt: str) -> str:
    """Turn symbol images into text without reading decorative icons aloud."""

    normalized = normalize_text(alt)
    lowered = normalized.casefold()
    if lowered in INLINE_IMAGE_TEXT:
        return INLINE_IMAGE_TEXT[lowered]
    if lowered in DECORATIVE_IMAGE_ALTS:
        return ""
    # Diagram titles and unknown images are represented in images.tsv instead
    # of being injected into surrounding prose and spoken twice.
    return ""


def element_text(element: ET.Element) -> str:
    """Extract visible inline text from an XHTML block."""

    pieces: list[str] = []

    def visit(node: ET.Element) -> None:
        if node.text:
            pieces.append(node.text)
        for child in node:
            tag = local_name(child.tag)
            if tag == "img":
                inline = image_alt_to_inline_text(child.attrib.get("alt", ""))
                if inline:
                    pieces.extend((" ", inline, " "))
            elif tag == "br":
                pieces.append(" ")
            else:
                visit(child)
            if child.tail:
                pieces.append(child.tail)

    visit(element)
    return normalize_text("".join(pieces))


def classify_image(alt: str) -> str:
    lowered = normalize_text(alt).casefold()
    if lowered in INLINE_IMAGE_TEXT:
        return "inline-symbol"
    if lowered in DECORATIVE_IMAGE_ALTS:
        return "decorative"
    if lowered:
        return "described"
    return "unlabeled"


def extract_document(
    archive: ZipFile,
    href: str,
) -> tuple[list[TextBlock], list[ImageUse]]:
    """Extract ordered text blocks and image references from one XHTML file."""

    try:
        root = ET.fromstring(archive.read(href))
    except KeyError as exc:
        raise EpubExtractionError(f"Spine document is missing: {href}") from exc
    except ET.ParseError as exc:
        raise EpubExtractionError(f"Invalid XHTML in {href}: {exc}") from exc

    body = next((node for node in root.iter() if local_name(node.tag) == "body"), root)
    blocks: list[TextBlock] = []
    images: list[ImageUse] = []
    seen_image_ids: set[int] = set()
    doc_dir = PurePosixPath(href).parent

    for node in body.iter():
        tag = local_name(node.tag)
        is_list_item_without_paragraph = tag == "li" and not any(
            local_name(descendant.tag) in BLOCK_TAGS
            for descendant in node.iter()
            if descendant is not node
        )
        if tag not in BLOCK_TAGS and not is_list_item_without_paragraph:
            continue

        text = element_text(node)
        if not text:
            continue

        block_index = len(blocks) + 1
        blocks.append(
            TextBlock(
                tag=tag,
                text=text,
                source_href=href,
                source_block_index=block_index,
            )
        )
        for image in (item for item in node.iter() if local_name(item.tag) == "img"):
            seen_image_ids.add(id(image))
            src = normalize_zip_path(doc_dir, image.attrib.get("src", ""))
            alt = normalize_text(image.attrib.get("alt", ""))
            images.append(
                ImageUse(
                    source_href=href,
                    source_block_index=block_index,
                    src=src,
                    alt=alt,
                    context=text,
                    kind=classify_image(alt),
                )
            )

    for image in (item for item in body.iter() if local_name(item.tag) == "img"):
        if id(image) in seen_image_ids:
            continue
        src = normalize_zip_path(doc_dir, image.attrib.get("src", ""))
        alt = normalize_text(image.attrib.get("alt", ""))
        images.append(
            ImageUse(
                source_href=href,
                source_block_index=None,
                src=src,
                alt=alt,
                context="",
                kind=classify_image(alt),
            )
        )

    return blocks, images


def protect_periods(text: str) -> str:
    marker = "\ue000"
    protected = text
    for abbreviation in ABBREVIATIONS:
        protected = re.sub(
            re.escape(abbreviation),
            abbreviation.replace(".", marker),
            protected,
            flags=re.IGNORECASE,
        )
    protected = re.sub(
        r"\b([A-Z])\.(?=\s+[A-Z][a-z])",
        lambda match: match.group(1) + marker,
        protected,
    )
    protected = re.sub(
        r"(?<=\d)\.(?=\d)",
        marker,
        protected,
    )
    return protected


def split_sentences(text: str) -> list[str]:
    """Split prose conservatively, leaving headings and fragments intact."""

    marker = "\ue000"
    boundary = "\ue001"
    protected = protect_periods(text)
    protected = re.sub(
        r"([.!?…][\"'”’]?)\s+(?=[\"'“‘(\[]*[A-Z0-9])",
        rf"\1{boundary}",
        protected,
    )
    sentences = [
        normalize_text(part.replace(marker, "."))
        for part in protected.split(boundary)
    ]
    return [sentence for sentence in sentences if sentence]


def slugify(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value).strip("-").lower()
    return slug[:72] or "section"


def read_epub_structure(
    archive: ZipFile,
) -> tuple[dict[str, str], list[str], list[TocEntry]]:
    """Return metadata, spine paths, and navigation entries."""

    try:
        container_root = ET.fromstring(archive.read("META-INF/container.xml"))
    except (KeyError, ET.ParseError) as exc:
        raise EpubExtractionError("EPUB is missing a valid META-INF/container.xml") from exc

    rootfile = next(
        (node for node in container_root.iter() if local_name(node.tag) == "rootfile"),
        None,
    )
    if rootfile is None or not rootfile.attrib.get("full-path"):
        raise EpubExtractionError("EPUB container does not name a package document")

    opf_path = rootfile.attrib["full-path"]
    opf_dir = PurePosixPath(opf_path).parent
    try:
        package = ET.fromstring(archive.read(opf_path))
    except (KeyError, ET.ParseError) as exc:
        raise EpubExtractionError(f"EPUB package document is invalid: {opf_path}") from exc

    metadata: dict[str, str] = {}
    for node in package.iter():
        name = local_name(node.tag)
        if name in {"title", "creator", "language", "publisher", "date"} and node.text:
            metadata.setdefault(name, normalize_text(node.text))

    manifest: dict[str, tuple[str, str]] = {}
    for node in package.iter():
        if local_name(node.tag) != "item":
            continue
        item_id = node.attrib.get("id")
        href = node.attrib.get("href")
        if item_id and href:
            manifest[item_id] = (
                normalize_zip_path(opf_dir, href),
                node.attrib.get("media-type", ""),
            )

    spine_ids: list[str] = []
    spine_toc_id = ""
    for node in package.iter():
        if local_name(node.tag) == "spine":
            spine_toc_id = node.attrib.get("toc", "")
            spine_ids.extend(
                child.attrib["idref"]
                for child in node
                if local_name(child.tag) == "itemref" and child.attrib.get("idref")
            )
            break

    spine: list[str] = []
    for item_id in spine_ids:
        if item_id not in manifest:
            raise EpubExtractionError(f"Spine references missing manifest item: {item_id}")
        spine.append(manifest[item_id][0])

    ncx_path = ""
    if spine_toc_id in manifest:
        ncx_path = manifest[spine_toc_id][0]
    if not ncx_path:
        ncx_path = next(
            (path for path, media_type in manifest.values() if media_type == "application/x-dtbncx+xml"),
            "",
        )
    if not ncx_path:
        raise EpubExtractionError("EPUB 2 navigation document (NCX) was not found")

    try:
        navigation = ET.fromstring(archive.read(ncx_path))
    except (KeyError, ET.ParseError) as exc:
        raise EpubExtractionError(f"EPUB navigation document is invalid: {ncx_path}") from exc

    ncx_dir = PurePosixPath(ncx_path).parent
    toc: list[TocEntry] = []
    for nav_point in (node for node in navigation.iter() if local_name(node.tag) == "navpoint"):
        label = next(
            (
                normalize_text(descendant.text or "")
                for descendant in nav_point.iter()
                if local_name(descendant.tag) == "text" and normalize_text(descendant.text or "")
            ),
            "",
        )
        content = next(
            (descendant for descendant in nav_point if local_name(descendant.tag) == "content"),
            None,
        )
        if label and content is not None and content.attrib.get("src"):
            toc.append(
                TocEntry(
                    title=label,
                    href=normalize_zip_path(ncx_dir, content.attrib["src"]),
                    order=len(toc) + 1,
                )
            )

    if not spine:
        raise EpubExtractionError("EPUB spine is empty")
    if not toc:
        raise EpubExtractionError("EPUB navigation is empty")
    return metadata, spine, toc


def build_sections(
    archive: ZipFile,
    spine: list[str],
    toc: list[TocEntry],
    include_front_matter: bool,
) -> tuple[list[Section], list[str]]:
    """Group spine documents into consecutive TOC sections."""

    toc_by_document: dict[str, TocEntry] = {}
    for entry in toc:
        toc_by_document.setdefault(entry.href, entry)

    sections: list[Section] = []
    skipped: list[str] = []
    current: Section | None = None
    next_number = 1

    for href in spine:
        entry = toc_by_document.get(href)
        if entry is not None:
            if current is not None and (current.blocks or current.images):
                sections.append(current)
            current = Section(
                number=next_number,
                title=entry.title,
                toc_href=entry.href,
            )
            next_number += 1

        blocks, images = extract_document(archive, href)
        if current is None:
            if not blocks:
                continue
            fallback_title = next(
                (block.text for block in blocks if block.tag.startswith("h")),
                PurePosixPath(href).stem,
            )
            current = Section(
                number=next_number,
                title=fallback_title,
                toc_href=href,
            )
            next_number += 1

        current.source_hrefs.append(href)
        current.blocks.extend(blocks)
        current.images.extend(images)

    if current is not None and (current.blocks or current.images):
        sections.append(current)

    included: list[Section] = []
    renumbered = 1
    for section in sections:
        if (
            not include_front_matter
            and section.title.casefold().strip() in DEFAULT_SKIPPED_SECTIONS
        ):
            skipped.append(section.title)
            continue
        section.number = renumbered
        renumbered += 1
        included.append(section)
    return included, skipped


def block_kind(tag: str) -> str:
    if re.fullmatch(r"h[1-6]", tag):
        return "heading"
    if tag == "blockquote":
        return "quote"
    if tag in {"li", "dt", "dd"}:
        return "list-item"
    return "sentence"


def write_markdown(section: Section, destination: Path) -> tuple[int, int]:
    lines = [
        "---",
        f"section_id: {section.section_id}",
        f"toc_title: {json.dumps(section.title, ensure_ascii=False)}",
        "source_documents:",
        *(f"  - {json.dumps(href, ensure_ascii=False)}" for href in section.source_hrefs),
        "---",
        "",
    ]
    word_count = 0
    for block in section.blocks:
        word_count += len(WORD_RE.findall(block.text))
        if re.fullmatch(r"h[1-6]", block.tag):
            level = int(block.tag[1])
            lines.extend((f"{'#' * level} {block.text}", ""))
        elif block.tag == "blockquote":
            lines.extend((f"> {block.text}", ""))
        elif block.tag in {"li", "dt", "dd"}:
            lines.extend((f"- {block.text}", ""))
        else:
            lines.extend((block.text, ""))

    destination.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return len(section.blocks), word_count


def write_outputs(
    epub_path: Path,
    output_dir: Path,
    metadata: dict[str, str],
    sections: list[Section],
    skipped_sections: list[str],
    archive: ZipFile,
    extract_images: bool,
) -> dict[str, object]:
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True)
    image_dir = output_dir / "images"
    if extract_images:
        image_dir.mkdir()

    units_path = output_dir / "source_units.jsonl"
    images_path = output_dir / "images.tsv"
    section_records: list[dict[str, object]] = []
    all_image_uses: list[tuple[Section, ImageUse]] = []
    total_blocks = 0
    total_words = 0
    total_units = 0
    long_units = 0

    with units_path.open("w", encoding="utf-8") as units_file:
        for section in sections:
            markdown_name = f"{section.number:02d}-{slugify(section.title)}.md"
            block_count, word_count = write_markdown(section, raw_dir / markdown_name)
            section_unit_count = 0

            for block_position, block in enumerate(section.blocks, 1):
                kind = block_kind(block.tag)
                sentences = (
                    [block.text]
                    if kind in {"heading", "list-item"}
                    else split_sentences(block.text)
                )
                for sentence_position, sentence in enumerate(sentences, 1):
                    unit_words = len(WORD_RE.findall(sentence))
                    if unit_words > 30:
                        long_units += 1
                    record = {
                        "id": (
                            f"{section.section_id}-b{block_position:04d}"
                            f"-s{sentence_position:02d}"
                        ),
                        "section_id": section.section_id,
                        "chapter": section.title,
                        "type": kind,
                        "en": sentence,
                        "word_count": unit_words,
                        "source_href": block.source_href,
                        "source_block_index": block.source_block_index,
                    }
                    units_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                    total_units += 1
                    section_unit_count += 1

            section_records.append(
                {
                    "id": section.section_id,
                    "title": section.title,
                    "toc_href": section.toc_href,
                    "source_hrefs": section.source_hrefs,
                    "markdown": f"raw/{markdown_name}",
                    "blocks": block_count,
                    "translation_units": section_unit_count,
                    "words": word_count,
                    "images": len(section.images),
                }
            )
            total_blocks += block_count
            total_words += word_count
            all_image_uses.extend((section, image) for image in section.images)

    unique_images = sorted({image.src for _, image in all_image_uses if image.src})
    extracted_paths: dict[str, str] = {}
    if extract_images:
        for source_path in unique_images:
            try:
                data = archive.read(source_path)
            except KeyError:
                continue
            relative = PurePosixPath(source_path)
            safe_parts = [part for part in relative.parts if part not in {"", ".", ".."}]
            destination = image_dir.joinpath(*safe_parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            extracted_paths[source_path] = destination.relative_to(output_dir).as_posix()

    with images_path.open("w", encoding="utf-8", newline="") as images_file:
        writer = csv.writer(images_file, delimiter="\t")
        writer.writerow(
            [
                "section_id",
                "chapter",
                "source_href",
                "source_block_index",
                "image_src",
                "extracted_path",
                "kind",
                "alt",
                "context",
            ]
        )
        for section, image in all_image_uses:
            writer.writerow(
                [
                    section.section_id,
                    section.title,
                    image.source_href,
                    image.source_block_index or "",
                    image.src,
                    extracted_paths.get(image.src, ""),
                    image.kind,
                    image.alt,
                    image.context,
                ]
            )

    image_kind_counts = Counter(image.kind for _, image in all_image_uses)
    manifest = {
        "schema_version": 1,
        "source_epub": str(epub_path.resolve()),
        "metadata": metadata,
        "extraction": {
            "sections": len(sections),
            "blocks": total_blocks,
            "translation_units": total_units,
            "words": total_words,
            "units_over_30_words": long_units,
            "image_references": len(all_image_uses),
            "unique_images": len(unique_images),
            "image_kinds": dict(sorted(image_kind_counts.items())),
            "skipped_sections": skipped_sections,
        },
        "sections": section_records,
    }
    (output_dir / "book_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract an EPUB into chapter Markdown and JSONL translation units."
    )
    parser.add_argument("epub", type=Path, help="Source .epub file")
    parser.add_argument("output", type=Path, help="New or empty output directory")
    parser.add_argument(
        "--include-front-matter",
        action="store_true",
        help="Include Title Page, Contents, and Copyright sections",
    )
    parser.add_argument(
        "--extract-images",
        action="store_true",
        help="Also copy referenced images under OUTPUT/images/",
    )
    return parser.parse_args(argv)


def prepare_output_directory(output_dir: Path) -> None:
    if output_dir.exists():
        if not output_dir.is_dir():
            raise EpubExtractionError(f"Output path is not a directory: {output_dir}")
        if any(output_dir.iterdir()):
            raise EpubExtractionError(
                f"Output directory must be empty to prevent overwrites: {output_dir}"
            )
    else:
        output_dir.mkdir(parents=True)


def run(args: argparse.Namespace) -> dict[str, object]:
    epub_path: Path = args.epub
    output_dir: Path = args.output
    if not epub_path.is_file():
        raise EpubExtractionError(f"EPUB file not found: {epub_path}")
    prepare_output_directory(output_dir)

    try:
        with ZipFile(epub_path) as archive:
            metadata, spine, toc = read_epub_structure(archive)
            sections, skipped = build_sections(
                archive,
                spine,
                toc,
                include_front_matter=args.include_front_matter,
            )
            if not sections:
                raise EpubExtractionError("No transcript sections were extracted")
            return write_outputs(
                epub_path,
                output_dir,
                metadata,
                sections,
                skipped,
                archive,
                extract_images=args.extract_images,
            )
    except BadZipFile as exc:
        raise EpubExtractionError(f"Not a valid EPUB/ZIP file: {epub_path}") from exc


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = run(args)
    except (EpubExtractionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    extraction = manifest["extraction"]
    metadata = manifest["metadata"]
    print(f"Extracted: {metadata.get('title', args.epub.name)}")
    print(f"Output: {args.output.resolve()}")
    print(
        "Sections: {sections} | Blocks: {blocks} | Units: {translation_units} "
        "| Words: {words}".format(**extraction)
    )
    print(
        "Images: {unique_images} unique / {image_references} references | "
        "Units over 30 words: {units_over_30_words}".format(**extraction)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

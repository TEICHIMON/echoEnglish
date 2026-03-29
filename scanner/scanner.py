"""
Folder scanner for batch processing.

Scans a directory for audio+LRC pairs and/or text files,
builds a work list, and returns it for batch processing.
"""

from dataclasses import dataclass
from pathlib import Path

# Audio extensions we recognise (lowercase, without dot)
AUDIO_EXTENSIONS = {"mp3", "m4a", "wav", "flac", "ogg", "aac", "wma"}

# Text extension for text-only mode
TEXT_EXTENSION = "txt"


@dataclass
class ScanItem:
    """A single item in the batch work list."""
    mode: str            # "audio" or "text"
    audio_path: Path | None = None
    lrc_path: Path | None = None
    text_path: Path | None = None

    @property
    def label(self) -> str:
        """Short display label for progress output."""
        if self.mode == "audio" and self.audio_path:
            return self.audio_path.name
        elif self.mode == "text" and self.text_path:
            return self.text_path.name
        return "unknown"


def scan_folder(
    folder: str | Path,
    mode: str = "",
) -> list[ScanItem]:
    """
    Scan a folder and build a work list of items to process.

    Args:
        folder: Path to the directory to scan
        mode: "" (auto — find both), "audio" (audio+lrc pairs only),
              "text" (text files only)

    Returns:
        List of ScanItem objects, sorted by filename
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(f"Scan path is not a directory: {folder}")

    items: list[ScanItem] = []
    paired_stems: set[str] = set()  # stems already claimed by audio pairs

    scan_audio = mode in ("", "audio")
    scan_text = mode in ("", "text")

    # --- Pass 1: find audio + LRC pairs ---
    if scan_audio:
        # Collect all audio files
        audio_files: dict[str, Path] = {}
        for f in folder.iterdir():
            if f.is_file() and f.suffix.lower().lstrip(".") in AUDIO_EXTENSIONS:
                audio_files[f.stem] = f

        # Match each audio file with its LRC
        for stem, audio_path in sorted(audio_files.items()):
            lrc_path = folder / f"{stem}.lrc"
            if lrc_path.exists():
                items.append(ScanItem(
                    mode="audio",
                    audio_path=audio_path,
                    lrc_path=lrc_path,
                ))
                paired_stems.add(stem)
            else:
                print(f"  ⚠ Skipping {audio_path.name}: no matching .lrc file")

    # --- Pass 2: find text files ---
    if scan_text:
        for f in sorted(folder.iterdir()):
            if (
                f.is_file()
                and f.suffix.lower().lstrip(".") == TEXT_EXTENSION
                and f.stem not in paired_stems
            ):
                items.append(ScanItem(
                    mode="text",
                    text_path=f,
                ))

    return items


def print_scan_summary(items: list[ScanItem]) -> None:
    """Print a summary of what was found in the scan."""
    audio_count = sum(1 for it in items if it.mode == "audio")
    text_count = sum(1 for it in items if it.mode == "text")

    parts = []
    if audio_count:
        parts.append(f"{audio_count} audio+LRC pair{'s' if audio_count != 1 else ''}")
    if text_count:
        parts.append(f"{text_count} text file{'s' if text_count != 1 else ''}")

    if parts:
        print(f"  Found {', '.join(parts)}")
    else:
        print("  No processable files found")
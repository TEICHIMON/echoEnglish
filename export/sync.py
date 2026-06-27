"""
Output sync module.

Optional post-generation step that copies the finished Echo Loop audio (and its
matching ``.lrc`` subtitle) to a destination after each run. Two methods:

* ``copy``   — plain filesystem copy into a local folder. Pointing it at a
               Google Drive for Desktop synced folder on a desktop makes Google
               Drive upload the files automatically.
* ``rclone`` — upload to an rclone remote (e.g. ``gdrive:echoEnglish``). This is
               how the headless VPS deployment pushes generated audio to Google
               Drive: a Linux server can't run Google Drive for Desktop, so it
               uses rclone with a configured ``gdrive:`` remote instead.

Driven by the ``sync`` section of ``config.yaml`` (and ``ECHO_SYNC_*`` env vars;
see ``load_config`` in ``main.py``). Disabled by default. Failures are logged as
warnings and never propagate — the audio has already been written safely before
this runs, so a sync problem must never lose a generation.
"""

import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Audio suffixes we treat as primary outputs worth syncing.
_AUDIO_EXTS = (".m4a", ".mp3", ".wav", ".flac", ".ogg", ".aac")

# Maps the target-language preset code to the human label used in run folders,
# matching the user's existing "..._English" / "..._Japanese" convention.
_LANG_LABELS = {"en": "English", "ja": "Japanese"}

# How long to wait for a single rclone upload before giving up.
_RCLONE_TIMEOUT = 600


def sync_outputs(written_files: list[Path], config: dict) -> None:
    """Copy/upload generated output files to the configured sync destination.

    ``written_files`` is the list of paths produced by one generation run
    (audio first, e.g. the ``_echo`` / ``_tnt`` / ``_tst`` files). When
    ``sync.include_lrc`` is set, the sibling ``.lrc`` of each audio file is
    included too. No-op unless ``sync.enabled`` is true and ``sync.dest`` is set.
    """
    sync_cfg = config.get("sync") or {}
    if not sync_cfg.get("enabled") or not sync_cfg.get("dest"):
        return

    try:
        files = _collect_files(written_files, bool(sync_cfg.get("include_lrc", True)))
        if not files:
            logger.warning("Drive sync: no output files found to copy")
            return

        subfolder = (
            _run_folder_name(written_files, config)
            if sync_cfg.get("layout", "run_folder") == "run_folder"
            else ""
        )

        method = sync_cfg.get("method", "copy")
        if method == "rclone":
            count = _sync_rclone(files, sync_cfg["dest"], subfolder)
        else:
            count = _sync_copy(files, sync_cfg["dest"], subfolder)

        dest_desc = sync_cfg["dest"] + (f"/{subfolder}" if subfolder else "")
        logger.info("✓ Synced %d file(s) to: %s", count, dest_desc)
    except Exception as exc:  # noqa: BLE001 — sync must never break a generation
        logger.warning("Drive sync failed: %s", exc)


def _collect_files(written_files: list[Path], include_lrc: bool) -> list[Path]:
    """Audio outputs plus, optionally, each one's sibling .lrc (existing only)."""
    out: list[Path] = []
    for path in written_files:
        path = Path(path)
        if not path.is_file() or path.suffix.lower() not in _AUDIO_EXTS:
            continue
        out.append(path)
        if include_lrc:
            lrc = path.with_suffix(".lrc")
            if lrc.is_file():
                out.append(lrc)
    return out


def _sync_copy(files: list[Path], dest: str, subfolder: str) -> int:
    """Local filesystem copy into ``dest[/subfolder]``."""
    dest_dir = Path(dest).expanduser()
    if subfolder:
        dest_dir = dest_dir / subfolder
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in files:
        shutil.copy2(src, dest_dir / src.name)
    return len(files)


def _sync_rclone(files: list[Path], dest: str, subfolder: str) -> int:
    """Upload each file to an rclone remote ``dest[/subfolder]``."""
    rclone_bin = os.environ.get("ECHO_RCLONE_BIN", "rclone")
    remote_dir = dest.rstrip("/")
    if subfolder:
        remote_dir = f"{remote_dir}/{subfolder}"

    for src in files:
        target = f"{remote_dir}/{src.name}"
        result = subprocess.run(
            [rclone_bin, "copyto", str(src), target],
            capture_output=True, text=True, timeout=_RCLONE_TIMEOUT,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip().splitlines()
            detail = stderr[-1] if stderr else f"exit code {result.returncode}"
            raise RuntimeError(f"rclone copyto {target}: {detail}")
    return len(files)


def _run_folder_name(written_files: list[Path], config: dict) -> str:
    """Build the run's sync subfolder name.

    With an explicit ``sync.label`` (a user-chosen theme name from the web UI)
    the folder is date-only ``YYYY-MM-DD_<label>`` so every file for that theme
    — including the separate English and Japanese runs — groups together. Without
    a label (e.g. CLI runs) it keeps the original ``YYYY-MM-DD_HH-MM-SS_<Lang>``.
    """
    label = _infer_label(written_files, config)
    if (config.get("sync") or {}).get("label"):
        date = datetime.now().strftime("%Y-%m-%d")
        return f"{date}_{label}" if label else date
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return f"{timestamp}_{label}" if label else timestamp


def _infer_label(written_files: list[Path], config: dict) -> str:
    """Pick a human label: explicit sync.label, else target language, else stem."""
    label = (config.get("sync") or {}).get("label")
    if label:
        return str(label).strip()

    # Lazy import avoids a circular dependency (main.py imports this module).
    try:
        from main import _infer_target_language

        lang = _infer_target_language(config)
        if lang in _LANG_LABELS:
            return _LANG_LABELS[lang]
    except Exception:
        pass

    # Fall back to the active target language (e.g. when --lang is used but the
    # input filename carries no language hint).
    lang = _lang_from_config(config)
    if lang in _LANG_LABELS:
        return _LANG_LABELS[lang]

    for path in written_files:
        stem = Path(path).stem
        for suffix in ("_echo", "_tnt", "_tst"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        if stem:
            return stem
    return "Echo"


def _lang_from_config(config: dict) -> str:
    """Infer the target language code (en/ja) from the active voices.

    Interview runs carry an explicit ``interview.lang``; text/audio runs are
    read from the engine's target voice, whose code is prefixed with the
    language (e.g. ``en-US-JennyNeural`` -> ``en``). Returns "" if unknown.
    """
    if config.get("paths", {}).get("interview"):
        lang = config.get("interview", {}).get("lang", "")
        return lang if lang in _LANG_LABELS else ""

    tts = config.get("tts", {})
    engine = tts.get("engine")
    if engine == "google":
        voice = tts.get("google", {}).get("target_voice", "")
    elif engine == "edge":
        voice = tts.get("target_voice", "")
    else:  # openai voices ("coral", ...) carry no language code
        voice = ""

    prefix = voice.split("-", 1)[0].lower()
    return prefix if prefix in _LANG_LABELS else ""

"""
Centralized logging configuration for Echo Loop Generator.

Three handlers attached to the root logger:
  1. Console (stdout)             — clean format, INFO+, preserves the existing UX
  2. Central file (per session)   — structured, DEBUG+, at echoEnglish/logs/<ts>_pipeline.log
  3. Per-folder file (dynamic)    — structured, DEBUG+, attached/detached on demand
                                    at <folder>/echo_run_<ts>.log

Modules use it like this:

    import logging
    logger = logging.getLogger(__name__)

    logger.info("Hello")
    logger.warning("Oops")

main.py calls:

    setup_logging()                       # at startup
    attach_folder_log(Path("..."))        # before processing a folder
    detach_folder_log()                   # after
    close_all_handlers()                  # at clean shutdown
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

# Project root — this file lives at echoEnglish/echo_logging.py
PROJECT_ROOT = Path(__file__).parent
CENTRAL_LOG_DIR = PROJECT_ROOT / "logs"

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_session_timestamp: str | None = None
_central_log_path: Path | None = None
_central_handler: logging.FileHandler | None = None
_folder_handlers: dict[Path, logging.FileHandler] = {}
_active_folder: Path | None = None


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

# Console: bare message, preserves the existing terminal look
_CONSOLE_FORMATTER = logging.Formatter("%(message)s")

# File: structured, every line independently timestamped + leveled
_FILE_FORMATTER = logging.Formatter(
    fmt="%(asctime)s [%(levelname)-7s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def setup_logging(console_level: int = logging.INFO) -> None:
    """Configure the root logger. Call once at program start."""
    global _session_timestamp, _central_log_path, _central_handler

    _session_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Wipe any pre-existing handlers (basicConfig from a transitive import etc.)
    for h in list(root.handlers):
        root.removeHandler(h)

    # --- Console handler ---
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level)
    console.setFormatter(_CONSOLE_FORMATTER)
    root.addHandler(console)

    # --- Central file handler ---
    try:
        CENTRAL_LOG_DIR.mkdir(parents=True, exist_ok=True)
        _central_log_path = CENTRAL_LOG_DIR / f"{_session_timestamp}_pipeline.log"
        _central_handler = logging.FileHandler(
            _central_log_path, mode="w", encoding="utf-8",
        )
        _central_handler.setLevel(logging.DEBUG)
        _central_handler.setFormatter(_FILE_FORMATTER)
        root.addHandler(_central_handler)
    except Exception as e:
        # Don't crash the program if /logs is unwritable — fall back to console-only
        print(
            f"⚠ Warning: could not create central log file: {e}",
            file=sys.stderr,
        )
        _central_log_path = None
        _central_handler = None

    # --- Quiet noisy third-party loggers ---
    for name in ("edge_tts", "openai", "httpx", "httpcore", "asyncio", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)

    logging.getLogger(__name__).debug(
        f"Logging initialized; central log at {_central_log_path}"
    )


def attach_folder_log(folder: str | Path) -> Path | None:
    """
    Attach a file handler that writes to <folder>/echo_run_<timestamp>.log.

    Calling with the same folder twice in one session reuses the same handler.
    Calling with a different folder detaches the current one first.

    Returns the log file path, or None if creation failed / setup_logging
    was never called.
    """
    global _active_folder

    folder = Path(folder).resolve()

    # Detach any currently active handler if it's a different folder
    if _active_folder is not None and _active_folder != folder:
        detach_folder_log()

    root = logging.getLogger()

    # Reuse an existing handler for this folder
    if folder in _folder_handlers:
        handler = _folder_handlers[folder]
        if handler not in root.handlers:
            root.addHandler(handler)
        _active_folder = folder
        return Path(handler.baseFilename)

    if _session_timestamp is None:
        return None  # setup_logging never called

    log_path = folder / f"echo_run_{_session_timestamp}.log"
    try:
        handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(_FILE_FORMATTER)
        root.addHandler(handler)
        _folder_handlers[folder] = handler
        _active_folder = folder
        return log_path
    except Exception as e:
        # Folder might be read-only or otherwise unwritable
        logging.getLogger(__name__).warning(
            f"Could not create folder log at {log_path}: {e}"
        )
        return None


def detach_folder_log() -> None:
    """Remove the currently active per-folder handler from the root logger."""
    global _active_folder

    if _active_folder is None:
        return

    root = logging.getLogger()
    handler = _folder_handlers.get(_active_folder)
    if handler is not None and handler in root.handlers:
        root.removeHandler(handler)
    _active_folder = None


def close_all_handlers() -> None:
    """Close every file handler. Call at clean shutdown."""
    global _active_folder, _central_handler

    root = logging.getLogger()

    for folder, handler in list(_folder_handlers.items()):
        try:
            handler.close()
        except Exception:
            pass
        if handler in root.handlers:
            root.removeHandler(handler)
    _folder_handlers.clear()
    _active_folder = None

    if _central_handler is not None:
        try:
            _central_handler.close()
        except Exception:
            pass
        if _central_handler in root.handlers:
            root.removeHandler(_central_handler)
        _central_handler = None


def get_central_log_path() -> Path | None:
    """Return the path of the central log file, or None if not created."""
    return _central_log_path


def get_session_timestamp() -> str | None:
    """Return the YYYYMMDD_HHMMSS string for this session."""
    return _session_timestamp
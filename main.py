#!/usr/bin/env python3
"""
Echo Loop Generator

Generates T-S-N-S-T-S (Target → Silence → Native → Silence → Target → Silence)
audio files for language learning, based on "Echo: Rebuilding the Natural Reflex
of Language" by H. Reeve.

Three modes:
  One path:    python main.py /path/to/folder-or-file  (then pick settings)
  Interactive: python main.py -i
  Audio mode:  python main.py lesson01.mp3 lesson01.lrc
  Text mode:   python main.py --text phrases.txt
  Interview:   python main.py --interview interview.txt
  Batch mode:  python main.py --scan /path/to/folder
  Config-only: python main.py  (paths set in config.yaml)
"""

import argparse
import copy
import logging
import os
import sys
import tempfile
import shutil
import time
import traceback
from pathlib import Path

import yaml
from dotenv import load_dotenv

from parser.lrc_parser import parse_lrc
from parser.text_parser import parse_text
from parser.interview_parser import parse_interview_text, ROLE_PATTERN
from audio.splitter import load_audio, extract_all_segments
from audio.tts_generator import generate_native_audio, generate_target_audio
from audio.assembler import assemble_all_loops, EchoTiming, resolve_loop_pattern
from export.exporter import export_audio
from export.lrc_writer import generate_echo_lrc
from export.sync import sync_outputs
from scanner.scanner import (
    scan_folder,
    print_scan_summary,
    ScanItem,
    AUDIO_EXTENSIONS,
    OUTPUT_SUFFIXES,
)

from echo_logging import (
    setup_logging,
    attach_folder_log,
    detach_folder_log,
    close_all_handlers,
    get_central_log_path,
)

logger = logging.getLogger(__name__)


# --lang preset: one flag to switch the target language across engines.
# Explicit --target-voice / --google-target-voice still override these.
# These are defaults only — config.yaml's `tts.lang_presets` can override
# any voice per language so English and Japanese sound like different speakers.
# NOTE: Chirp3-HD persona names (Charon, Puck, ...) carry the same timbre
# across languages, so ja and en must use *different* personas to sound apart.
LANG_PRESETS: dict[str, dict[str, str]] = {
    "ja": {
        "google": "ja-JP-Chirp3-HD-Charon",   # male persona
        "edge":   "ja-JP-NanamiNeural",
    },
    "en": {
        "google": "en-US-Chirp3-HD-Puck",     # distinct male persona from ja
        "edge":   "en-US-JennyNeural",
    },
}

INTERVIEW_LANG_PRESETS: dict[str, dict[str, object]] = {
    "ja": {
        "interviewer_voice": "ja-JP-KeitaNeural",
        "interviewee_voice": "ja-JP-NanamiNeural",
        "google": {
            "interviewer_voice": "ja-JP-Chirp3-HD-Charon",
            "interviewee_voice": "ja-JP-Chirp3-HD-Kore",
        },
        "openai": {
            "interviewer_voice": "echo",
            "interviewee_voice": "coral",
        },
    },
    "en": {
        "interviewer_voice": "en-US-GuyNeural",
        "interviewee_voice": "en-US-JennyNeural",
        "google": {
            "interviewer_voice": "en-US-Chirp3-HD-Charon",
            "interviewee_voice": "en-US-Chirp3-HD-Kore",
        },
        "openai": {
            "interviewer_voice": "echo",
            "interviewee_voice": "coral",
        },
    },
}

OPENAI_VOICES: tuple[str, ...] = (
    "alloy", "ash", "ballad", "cedar", "coral", "echo",
    "fable", "nova", "onyx", "sage", "shimmer", "verse",
)

# Google Chirp3-HD "persona" names offered for user voice selection. A Chirp3-HD
# voice name is <region>-Chirp3-HD-<persona> (e.g. en-US-Chirp3-HD-Puck), and the
# language code is derived from the region prefix. Swapping only the persona keeps
# the timbre choice independent of the target language, so a selected voice can
# never mismatch the text's language. These personas exist for en-US / ja-JP /
# cmn-CN. First four are male, last four female.
GOOGLE_PERSONAS: tuple[str, ...] = (
    "Charon", "Puck", "Fenrir", "Orus",     # male
    "Kore", "Aoede", "Leda", "Zephyr",      # female
)

# Speech-rate multiplier bounds. 1.0 = the engine's normal speed; Google and
# OpenAI both accept 0.25–4.0, and edge-tts is given the equivalent percentage.
RATE_MIN = 0.25
RATE_MAX = 4.0


def coerce_speaking_rate(value) -> float | None:
    """Clamp a user-supplied speech-rate multiplier, or None if unset/invalid."""
    if value is None or value == "":
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    return max(RATE_MIN, min(RATE_MAX, rate))


def apply_speaking_rate(config: dict, rate: float | None) -> None:
    """Set one speech-rate multiplier across all three TTS engines.

    Google and OpenAI take the multiplier directly; edge-tts wants a signed
    percentage delta, so 1.15 becomes "+15%". No-op when ``rate`` is None.
    """
    rate = coerce_speaking_rate(rate)
    if rate is None:
        return
    config["tts"]["google"]["speaking_rate"] = rate
    config["tts"]["openai"]["speed"] = rate
    config["tts"]["rate"] = f"{(rate - 1.0) * 100:+.0f}%"


def load_config(config_path: str | Path | None = None) -> dict:
    """Load configuration from YAML file, falling back to defaults."""
    defaults = {
        "mode": "",
        "paths": {
            "scan": "",
            "audio": "",
            "lrc": "",
            "text": "",
            "interview": "",
            "output": "",
            "output_lrc": "",
        },
        "timing": {
            "after_first_target": 0.8,
            "after_native": 0.5,
            "after_second_target": 1.2,
        },
        "tts": {
            "engine": "google",
            "target_voice": "ja-JP-NanamiNeural",
            "native_voice": "zh-CN-XiaoxiaoNeural",
            "rate": "+0%",
            "pitch": "+0Hz",
            "openai": {
                "model": "gpt-4o-mini-tts",
                "voice": "coral",
                "target_voice": "",
                "native_voice": "",
                "speed": 1.0,
                "instructions": "",
            },
            "google": {
                "target_voice": "ja-JP-Chirp3-HD-Charon",
                "native_voice": "cmn-CN-Chirp3-HD-Kore",
                "speaking_rate": 1.0,
                "pitch": 0.0,
            },
            "lang_presets": copy.deepcopy(LANG_PRESETS),
            "gain": 0,
            "normalize": None,
            # Speech-rate multiplier applied to the TARGET voice only (the
            # native/Chinese narration keeps the engine default, so it stays
            # comfortable to follow). None = no override.
            "target_rate": None,
        },
        "output": {
            "format": "m4a",
            "bitrate": "192k",
            "sample_rate": 44100,
        },
        "lrc": {
            "delimiter": "|||",
            "split_strategy": "last",
        },
        "loop": {
            "variant": "full",
            "tnt_repeats": None,
            "tst_repeats": None,
            "split_outputs": False,
        },
        "sync": {
            "enabled": False,
            "method": "copy",         # "copy" (local folder) | "rclone" (remote)
            "dest": "",
            "layout": "run_folder",   # "run_folder" | "flat"
            "include_lrc": True,
        },
        "interview": {
            "lang": "en",
            "interviewer_voice": "en-US-GuyNeural",
            "interviewee_voice": "en-US-JennyNeural",
            "google": {
                "interviewer_voice": "en-US-Chirp3-HD-Charon",
                "interviewee_voice": "en-US-Chirp3-HD-Kore",
            },
            "openai": {
                "interviewer_voice": "echo",
                "interviewee_voice": "coral",
            },
            "presets": copy.deepcopy(INTERVIEW_LANG_PRESETS),
            # Per-role speech-rate multipliers. Raising only the interviewer
            # makes the questions harder to follow (listening practice) while
            # the answers stay at a speed you can shadow. None = engine default.
            "interviewer_rate": None,
            "interviewee_rate": None,
        },
    }

    if config_path and Path(config_path).exists():
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f)
        if user_config:
            if "mode" in user_config:
                defaults["mode"] = user_config["mode"] or ""
            for section in ("paths", "timing", "output", "lrc", "loop", "sync"):
                if section in user_config and isinstance(user_config[section], dict):
                    defaults[section].update(user_config[section])
            if "interview" in user_config and isinstance(user_config["interview"], dict):
                interview_user = user_config["interview"]
                if "google" in interview_user and isinstance(interview_user["google"], dict):
                    defaults["interview"]["google"].update(interview_user["google"])
                if "openai" in interview_user and isinstance(interview_user["openai"], dict):
                    defaults["interview"]["openai"].update(interview_user["openai"])
                if "presets" in interview_user and isinstance(interview_user["presets"], dict):
                    for lang, preset in interview_user["presets"].items():
                        if not isinstance(preset, dict):
                            continue
                        target = defaults["interview"]["presets"].setdefault(lang, {})
                        for k, v in preset.items():
                            if k in ("google", "openai") and isinstance(v, dict):
                                target.setdefault(k, {}).update(v)
                            else:
                                target[k] = v
                for k, v in interview_user.items():
                    if k not in ("google", "openai", "presets"):
                        defaults["interview"][k] = v
                lang = defaults["interview"].get("lang")
                if lang in defaults["interview"].get("presets", {}):
                    _apply_interview_language_preset(defaults, lang)
            if "tts" in user_config and isinstance(user_config["tts"], dict):
                tts_user = user_config["tts"]
                if "openai" in tts_user and isinstance(tts_user["openai"], dict):
                    defaults["tts"]["openai"].update(tts_user["openai"])
                if "google" in tts_user and isinstance(tts_user["google"], dict):
                    defaults["tts"]["google"].update(tts_user["google"])
                if "lang_presets" in tts_user and isinstance(tts_user["lang_presets"], dict):
                    # Per-language merge so overriding one engine's voice for one
                    # language doesn't wipe the other language or the other engine.
                    for lang, preset in tts_user["lang_presets"].items():
                        if isinstance(preset, dict):
                            defaults["tts"]["lang_presets"].setdefault(lang, {}).update(preset)
                for k, v in tts_user.items():
                    if k not in ("openai", "google", "lang_presets"):
                        defaults["tts"][k] = v

    # Backward compatibility: old "voice" key → native_voice
    tts = defaults["tts"]
    if "voice" in tts and "native_voice" not in tts:
        tts["native_voice"] = tts.pop("voice")
    elif "voice" in tts:
        tts.pop("voice")

    if tts.get("engine") not in ("edge", "openai", "google"):
        tts["engine"] = "google"

    if tts.get("gain") is None:
        tts["gain"] = 0
    tts["gain"] = float(tts["gain"])

    norm = tts.get("normalize")
    if norm is not None and norm != "":
        tts["normalize"] = float(norm)
    else:
        tts["normalize"] = None

    tts["openai"]["speed"] = float(tts["openai"].get("speed", 1.0))

    tts["google"]["speaking_rate"] = float(tts["google"].get("speaking_rate", 1.0))
    tts["google"]["pitch"] = float(tts["google"].get("pitch", 0.0))

    tts["target_rate"] = coerce_speaking_rate(tts.get("target_rate"))
    interview = defaults["interview"]
    interview["interviewer_rate"] = coerce_speaking_rate(interview.get("interviewer_rate"))
    interview["interviewee_rate"] = coerce_speaking_rate(interview.get("interviewee_rate"))

    sync = defaults["sync"]
    # Environment overrides (used by the VPS/Docker deployment so the committed
    # config.yaml can stay disabled/local-oriented). Only applied when present.
    _apply_sync_env_overrides(sync)
    sync["enabled"] = bool(sync.get("enabled"))
    sync["dest"] = str(sync.get("dest") or "")
    sync["include_lrc"] = bool(sync.get("include_lrc", True))
    if sync.get("method") not in ("copy", "rclone"):
        sync["method"] = "copy"
    if sync.get("layout") not in ("run_folder", "flat"):
        sync["layout"] = "run_folder"

    return defaults


def _env_truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _apply_sync_env_overrides(sync: dict) -> None:
    """Apply ECHO_SYNC_* env vars onto the sync config (empty/unset = ignore)."""
    enabled = os.environ.get("ECHO_SYNC_ENABLED")
    if enabled:
        sync["enabled"] = _env_truthy(enabled)
    for env_key, cfg_key in (
        ("ECHO_SYNC_METHOD", "method"),
        ("ECHO_SYNC_DEST", "dest"),
        ("ECHO_SYNC_LAYOUT", "layout"),
    ):
        value = os.environ.get(env_key)
        if value:
            sync[cfg_key] = value
    include_lrc = os.environ.get("ECHO_SYNC_INCLUDE_LRC")
    if include_lrc:
        sync["include_lrc"] = _env_truthy(include_lrc)


class InputPathError(Exception):
    """A path handed to the CLI could not be turned into a runnable input."""


def _sibling_lrc(audio_path: Path) -> Path | None:
    """Find the LRC that belongs to a recording, matching the scanner's rule."""
    candidate = audio_path.with_suffix(".lrc")
    return candidate if candidate.is_file() else None


def _sibling_audio(lrc_path: Path) -> Path | None:
    """Find the recording that belongs to an LRC, matching the scanner's rule."""
    for ext in sorted(AUDIO_EXTENSIONS):
        candidate = lrc_path.with_suffix(f".{ext}")
        if candidate.is_file():
            return candidate
    return None


def _looks_like_interview(text_path: Path) -> bool:
    """True when a text file's lines carry Q:/A: role markers."""
    try:
        lines = text_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return bool(ROLE_PATTERN.match(stripped))
    return False


def resolve_input_path(raw: str) -> dict[str, str]:
    """Turn one pasted path into the config input paths it implies.

    Accepts whatever is convenient to drop at a shell:

      folder      -> batch scan (filtered to audio when it holds no text files)
      audio file  -> audio mode; the sibling ``<stem>.lrc`` is found for you
      .lrc file   -> audio mode; the sibling recording is found for you
      .txt file   -> interview mode when the lines carry Q:/A: role markers,
                     otherwise plain bilingual text mode

    Raises ``InputPathError`` with a message naming what was missing, so a typo
    or an unpaired recording fails before any TTS is billed.
    """
    path = Path(raw).expanduser()
    if not path.exists():
        raise InputPathError(f"path does not exist: {path}")

    if path.is_dir():
        items = _probe_scan(path)
        if not items:
            raise InputPathError(
                f"nothing to process in {path} — expected audio files with a "
                f"matching .lrc, or .txt files"
            )
        result = {"scan": str(path)}
        # Pin the filter when the folder is homogeneous, so the run never picks
        # up a stray file of the other kind later.
        kinds = {item.mode for item in items}
        if len(kinds) == 1:
            result["mode"] = kinds.pop()
        return result

    suffix = path.suffix.lower()

    if suffix.lstrip(".") in AUDIO_EXTENSIONS:
        lrc = _sibling_lrc(path)
        if lrc is None:
            raise InputPathError(
                f"no subtitle file next to {path.name} — expected "
                f"{path.with_suffix('.lrc').name} in the same folder"
            )
        return {"mode": "audio", "audio": str(path), "lrc": str(lrc)}

    if suffix == ".lrc":
        audio = _sibling_audio(path)
        if audio is None:
            raise InputPathError(
                f"no recording next to {path.name} — expected {path.stem} with "
                f"one of: {', '.join(sorted(AUDIO_EXTENSIONS))}"
            )
        return {"mode": "audio", "audio": str(audio), "lrc": str(path)}

    if suffix == ".txt":
        if _looks_like_interview(path):
            return {"mode": "interview", "interview": str(path)}
        return {"mode": "text", "text": str(path)}

    raise InputPathError(
        f"don't know how to process {path.name} — pass a folder, an audio file "
        f"with a matching .lrc, an .lrc, or a .txt"
    )


def _apply_positional_paths(args: argparse.Namespace) -> None:
    """Fold the positional path argument(s) into the named input options.

    Two positionals keep the long-standing ``main.py lesson.mp3 lesson.lrc``
    form. One positional goes through ``resolve_input_path``, which is what
    makes ``main.py <folder>`` and ``main.py <recording>`` work.
    """
    paths = list(args.paths or [])
    args.audio = None
    args.lrc = None
    args.smart_path = None

    if not paths:
        return

    if len(paths) > 2:
        raise InputPathError(
            f"expected at most 2 positional paths (audio + lrc), got {len(paths)}"
        )

    if len(paths) == 2:
        args.audio, args.lrc = paths
        return

    resolved = resolve_input_path(paths[0])
    args.smart_path = paths[0]
    if resolved.get("mode") and not args.mode:
        args.mode = resolved["mode"]
    args.audio = resolved.get("audio")
    args.lrc = resolved.get("lrc")
    if resolved.get("scan") and not args.scan_dir:
        args.scan_dir = resolved["scan"]
    if resolved.get("text") and not args.text_file:
        args.text_file = resolved["text"]
    if resolved.get("interview") and not args.interview_file:
        args.interview_file = resolved["interview"]


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Echo Loop Generator - T•N•T language learning audio",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  Just point at a path (folder or file) — the rest is chosen from menus:
    %(prog)s /path/to/lessons
    %(prog)s /path/to/lessons/lesson01.mp3
    %(prog)s /path/to/lessons -y          # skip the menus, use config.yaml

  Interactive mode (choose inputs and common settings from menus):
    %(prog)s -i
    %(prog)s --interactive

  Audio mode (extract target audio from file):
    %(prog)s lesson01.mp3 lesson01.lrc
    %(prog)s lesson01.mp3 lesson01.lrc -o output/echo.m4a

  Text-only mode (generate both target and native via TTS):
    %(prog)s --text phrases.txt
    %(prog)s --text phrases.txt -o output/echo.m4a

  Mock interview mode (Q:/A: text with two role voices):
    %(prog)s --interview interview.txt

  Batch mode (scan a folder for all audio+LRC pairs and/or text files):
    %(prog)s --scan /path/to/lessons
    %(prog)s --scan /path/to/lessons --mode audio
    %(prog)s --scan /path/to/lessons --mode text

  Config-only (all paths set in config.yaml):
    %(prog)s
    %(prog)s -c my_config.yaml
        """,
    )

    input_group = parser.add_argument_group("Input")
    input_group.add_argument(
        "paths", nargs="*", metavar="PATH", default=[],
        help="One path — a folder to scan, an audio file (its matching .lrc is "
             "found for you), an .lrc, or a .txt — and the settings menu opens. "
             "Two paths are still read as the classic AUDIO LRC pair.",
    )
    input_group.add_argument(
        "--text", "-t", dest="text_file", default=None,
        help="Bilingual text file — text-only mode",
    )
    input_group.add_argument(
        "--interview", dest="interview_file", default=None,
        help="Q:/A: mock interview text file — interview mode",
    )
    input_group.add_argument(
        "--scan", "-s", dest="scan_dir", default=None,
        help="Folder to scan for batch processing",
    )

    parser.add_argument(
        "--mode", choices=["audio", "text", "interview"], default=None,
        help="Force mode (overrides config and auto-detection)",
    )
    parser.add_argument(
        "-i", "--interactive", action="store_true",
        help="Start an interactive setup menu instead of composing CLI flags",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="Never prompt — run a given PATH straight through with the "
             "settings from config.yaml and any flags. Use for scripts/cron.",
    )

    output_group = parser.add_argument_group("Output paths")
    output_group.add_argument(
        "-o", "--output", default=None,
        help="Output audio file path (single-file mode only)",
    )
    output_group.add_argument(
        "--output-lrc", default=None,
        help="Output LRC file path (single-file mode only)",
    )
    output_group.add_argument(
        "--sync-dir", default=None,
        help="Copy generated audio (+LRC) into this folder after each run "
             "(e.g. a Google Drive for Desktop synced folder). Enables sync.",
    )
    output_group.add_argument(
        "--no-sync", action="store_true",
        help="Disable output sync even if enabled in config.yaml.",
    )

    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Config file path (default: config.yaml)",
    )

    timing_group = parser.add_argument_group("Timing (overrides config)")
    timing_group.add_argument("--after-first-target", type=float, default=None)
    timing_group.add_argument("--after-native", type=float, default=None)
    timing_group.add_argument("--after-second-target", type=float, default=None)

    loop_group = parser.add_argument_group("Loop pattern (overrides config)")
    loop_group.add_argument(
        "--variant", choices=["full", "progressive", "shadow"], default=None,
        help="Preset loop pattern",
    )
    loop_group.add_argument(
        "--tnt-repeats", type=int, default=None,
        help="Number of T-N-T passes per segment",
    )
    loop_group.add_argument(
        "--tst-repeats", type=int, default=None,
        help="Number of T-S-T shadow passes per segment",
    )
    loop_group.add_argument(
        "--split-outputs", dest="split_outputs", action="store_true", default=None,
        help="Emit two files per input: {stem}_tnt and {stem}_tst (TTS is generated once and shared)",
    )
    loop_group.add_argument(
        "--no-split-outputs", dest="split_outputs", action="store_false",
        help="Force single combined output even if split_outputs is set in config",
    )

    tts_group = parser.add_argument_group("TTS (overrides config)")
    tts_group.add_argument("--engine", choices=["edge", "openai", "google"], default=None)
    tts_group.add_argument(
        "--lang", choices=sorted(LANG_PRESETS.keys()), default=None,
        help="Target language preset (ja / en). Sets target voice for "
             "google + edge engines, and interview role voices in interview mode. "
             "Overridden by explicit voice flags.",
    )
    tts_group.add_argument(
        "--interview-lang",
        choices=sorted(INTERVIEW_LANG_PRESETS.keys()),
        default=None,
        help="Interview mode role-voice language preset (ja / en). "
             "Overrides --lang for interview role voices only.",
    )
    tts_group.add_argument("--target-voice", default=None)
    tts_group.add_argument("--native-voice", default=None)
    tts_group.add_argument("--voice", default=None)
    tts_group.add_argument("--rate", default=None)
    tts_group.add_argument("--gain", type=float, default=None)
    tts_group.add_argument("--normalize", type=float, default=None)
    tts_group.add_argument("--openai-voice", default=None)
    tts_group.add_argument("--openai-instructions", default=None)
    tts_group.add_argument("--google-voice", default=None)
    tts_group.add_argument("--google-target-voice", default=None)
    tts_group.add_argument(
        "--interviewer-voice", default=None,
        help="Interview mode edge-tts interviewer voice",
    )
    tts_group.add_argument(
        "--interviewee-voice", default=None,
        help="Interview mode edge-tts interviewee voice",
    )
    tts_group.add_argument(
        "--google-interviewer-voice", default=None,
        help="Interview mode Google interviewer voice",
    )
    tts_group.add_argument(
        "--google-interviewee-voice", default=None,
        help="Interview mode Google interviewee voice",
    )
    tts_group.add_argument(
        "--openai-interviewer-voice", default=None,
        help="Interview mode OpenAI interviewer voice",
    )
    tts_group.add_argument(
        "--openai-interviewee-voice", default=None,
        help="Interview mode OpenAI interviewee voice",
    )

    lrc_group = parser.add_argument_group("LRC / text parsing (overrides config)")
    lrc_group.add_argument("--delimiter", default=None)
    lrc_group.add_argument("--split-strategy", choices=["first", "last"], default=None)

    args = parser.parse_args()
    try:
        _apply_positional_paths(args)
    except (InputPathError, NotADirectoryError) as exc:
        parser.error(str(exc))
    return args


def _apply_target_language_preset(config: dict, lang: str) -> None:
    """Apply the normal target-language preset.

    Voices come from config's `tts.lang_presets` (merged from config.yaml),
    falling back to the built-in LANG_PRESETS defaults.
    """
    presets = config.get("tts", {}).get("lang_presets") or LANG_PRESETS
    preset = presets.get(lang) or LANG_PRESETS[lang]
    if preset.get("edge"):
        config["tts"]["target_voice"] = preset["edge"]
    if preset.get("google"):
        config["tts"]["google"]["target_voice"] = preset["google"]


def _apply_interview_language_preset(config: dict, lang: str) -> None:
    """Apply role voice presets for interview mode."""
    presets = config.get("interview", {}).get("presets", {})
    preset = presets.get(lang) or INTERVIEW_LANG_PRESETS[lang]

    config["interview"]["lang"] = lang
    if preset.get("interviewer_voice"):
        config["interview"]["interviewer_voice"] = preset["interviewer_voice"]
    if preset.get("interviewee_voice"):
        config["interview"]["interviewee_voice"] = preset["interviewee_voice"]

    google = preset.get("google", {})
    if isinstance(google, dict):
        if google.get("interviewer_voice"):
            config["interview"]["google"]["interviewer_voice"] = google["interviewer_voice"]
        if google.get("interviewee_voice"):
            config["interview"]["google"]["interviewee_voice"] = google["interviewee_voice"]

    openai = preset.get("openai", {})
    if isinstance(openai, dict):
        if openai.get("interviewer_voice"):
            config["interview"]["openai"]["interviewer_voice"] = openai["interviewer_voice"]
        if openai.get("interviewee_voice"):
            config["interview"]["openai"]["interviewee_voice"] = openai["interviewee_voice"]


def google_voice_with_persona(voice_name: str, persona: str) -> str:
    """Swap the persona of a Google Chirp3-HD voice name, keeping its region.

    ``en-US-Chirp3-HD-Puck`` + ``Charon`` -> ``en-US-Chirp3-HD-Charon``.
    The region/language prefix is preserved, so the result always matches the
    text's language. Non-Chirp3-HD names (e.g. edge's ``ja-JP-NanamiNeural``) and
    empty personas are returned unchanged.
    """
    if not persona:
        return voice_name
    if "Chirp3-HD-" in voice_name:
        return f"{voice_name.rsplit('-', 1)[0]}-{persona}"
    return voice_name


def apply_cli_overrides(config: dict, args: argparse.Namespace) -> dict:
    """Apply CLI argument overrides to config. CLI always wins."""
    if args.mode:
        config["mode"] = args.mode

    if args.audio:
        config["paths"]["audio"] = args.audio
    if args.lrc:
        config["paths"]["lrc"] = args.lrc
    if args.text_file:
        config["paths"]["text"] = args.text_file
    if args.interview_file:
        config["paths"]["interview"] = args.interview_file
    if args.scan_dir:
        config["paths"]["scan"] = args.scan_dir
    if args.output:
        config["paths"]["output"] = args.output
    if args.output_lrc:
        config["paths"]["output_lrc"] = args.output_lrc

    if args.sync_dir:
        config["sync"]["dest"] = args.sync_dir
        config["sync"]["enabled"] = True
    if args.no_sync:
        config["sync"]["enabled"] = False

    if args.after_first_target is not None:
        config["timing"]["after_first_target"] = args.after_first_target
    if args.after_native is not None:
        config["timing"]["after_native"] = args.after_native
    if args.after_second_target is not None:
        config["timing"]["after_second_target"] = args.after_second_target

    if args.engine:
        config["tts"]["engine"] = args.engine

    if args.lang:
        _apply_target_language_preset(config, args.lang)
        _apply_interview_language_preset(config, args.lang)
    if args.interview_lang:
        _apply_interview_language_preset(config, args.interview_lang)

    if args.target_voice:
        config["tts"]["target_voice"] = args.target_voice
    if args.native_voice:
        config["tts"]["native_voice"] = args.native_voice
    if args.voice:
        config["tts"]["native_voice"] = args.voice
    if args.rate:
        config["tts"]["rate"] = args.rate
    if args.gain is not None:
        config["tts"]["gain"] = args.gain
    if args.normalize is not None:
        config["tts"]["normalize"] = args.normalize

    if args.openai_voice:
        config["tts"]["openai"]["voice"] = args.openai_voice
    if args.openai_instructions:
        config["tts"]["openai"]["instructions"] = args.openai_instructions

    if args.google_voice:
        config["tts"]["google"]["native_voice"] = args.google_voice
    if args.google_target_voice:
        config["tts"]["google"]["target_voice"] = args.google_target_voice

    if args.interviewer_voice:
        config["interview"]["interviewer_voice"] = args.interviewer_voice
    if args.interviewee_voice:
        config["interview"]["interviewee_voice"] = args.interviewee_voice
    if args.google_interviewer_voice:
        config["interview"]["google"]["interviewer_voice"] = args.google_interviewer_voice
    if args.google_interviewee_voice:
        config["interview"]["google"]["interviewee_voice"] = args.google_interviewee_voice
    if args.openai_interviewer_voice:
        config["interview"]["openai"]["interviewer_voice"] = args.openai_interviewer_voice
    if args.openai_interviewee_voice:
        config["interview"]["openai"]["interviewee_voice"] = args.openai_interviewee_voice

    if args.delimiter:
        config["lrc"]["delimiter"] = args.delimiter
    if args.split_strategy:
        config["lrc"]["split_strategy"] = args.split_strategy

    if args.variant:
        config["loop"]["variant"] = args.variant
        config["loop"]["tnt_repeats"] = None
        config["loop"]["tst_repeats"] = None
    if args.tnt_repeats is not None:
        config["loop"]["tnt_repeats"] = args.tnt_repeats
    if args.tst_repeats is not None:
        config["loop"]["tst_repeats"] = args.tst_repeats
    if args.split_outputs is not None:
        config["loop"]["split_outputs"] = args.split_outputs

    return config


def _config_has_input(config: dict) -> bool:
    """Return True when config contains enough input paths to run."""
    paths = config["paths"]
    return bool(
        paths.get("scan")
        or paths.get("text")
        or paths.get("interview")
        or (paths.get("audio") and paths.get("lrc"))
    )


def _safe_input(prompt: str) -> str:
    """Read interactive input and turn Ctrl-C / EOF into clean exits."""
    try:
        return input(prompt)
    except KeyboardInterrupt:
        print("\nAborted.")
        raise SystemExit(130)
    except EOFError:
        print("\nInteractive input ended.")
        raise SystemExit(1)


def _prompt_choice(
    title: str,
    options: list[tuple[str, str]],
    default: str | None = None,
) -> str:
    """Prompt for a numbered choice and return the option value."""
    values = {value for value, _ in options}
    if default is not None and default not in values:
        default = None

    while True:
        print()
        print(title)
        for idx, (value, label) in enumerate(options, 1):
            suffix = " [default]" if default == value else ""
            print(f"  {idx}. {label}{suffix}")

        raw = _safe_input("Choose: ").strip().lower()
        if not raw and default is not None:
            return default
        if raw.isdigit():
            index = int(raw) - 1
            if 0 <= index < len(options):
                return options[index][0]
        for value, label in options:
            if raw == value.lower() or raw == label.lower():
                return value
        print("Please enter one of the listed numbers.")


def _prompt_yes_no(prompt: str, default: bool = True) -> bool:
    """Prompt for a yes/no answer."""
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        raw = _safe_input(prompt + suffix).strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("Please answer y or n.")


def _prompt_string(prompt: str, default: str = "", required: bool = False) -> str:
    """Prompt for a string, preserving a default on empty input."""
    suffix = f" [{default}]" if default else ""
    while True:
        value = _safe_input(f"{prompt}{suffix}: ").strip()
        if not value:
            value = default
        if value or not required:
            return value
        print("This value is required.")


def _clean_path_input(value: str) -> str:
    """Normalize common pasted path formats without touching real path spaces."""
    value = value.replace("\\ ", " ")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value


def _print_path_suggestions(path: Path, kind: str) -> None:
    """Print nearby paths when a user input almost matches a real file/folder."""
    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        return

    needle = path.name
    normalized_needle = needle.strip()
    suggestions: list[Path] = []
    for candidate in sorted(parent.iterdir()):
        if kind == "dir" and not candidate.is_dir():
            continue
        if kind == "file" and not candidate.is_file():
            continue
        candidate_name = candidate.name
        if (
            candidate_name.startswith(needle)
            or candidate_name.strip() == normalized_needle
            or (normalized_needle and candidate_name.startswith(normalized_needle))
        ):
            suggestions.append(candidate)
        if len(suggestions) >= 3:
            break

    if suggestions:
        print("Did you mean:")
        for suggestion in suggestions:
            print(f"  {suggestion}")


def _prompt_path(
    prompt: str,
    default: str = "",
    required: bool = True,
    kind: str = "file",
) -> str:
    """Prompt for an existing file or directory path."""
    while True:
        value = _clean_path_input(
            _prompt_string(prompt, default=default, required=required)
        )
        if not value:
            return ""

        path = Path(value).expanduser()
        if kind == "dir" and not path.is_dir():
            print(f"Directory not found: {path}")
            _print_path_suggestions(path, kind)
            continue
        if kind == "file" and not path.is_file():
            print(f"File not found: {path}")
            _print_path_suggestions(path, kind)
            continue
        return str(path)


def _prompt_float(prompt: str, default: float) -> float:
    while True:
        raw = _prompt_string(prompt, default=str(default), required=True)
        try:
            return float(raw)
        except ValueError:
            print("Please enter a number.")


def _prompt_non_negative_int(prompt: str, default: int) -> int:
    while True:
        raw = _prompt_string(prompt, default=str(default), required=True)
        try:
            value = int(raw)
        except ValueError:
            print("Please enter a whole number.")
            continue
        if value >= 0:
            return value
        print("Please enter 0 or greater.")


def _clear_input_paths(config: dict) -> None:
    for key in ("scan", "audio", "lrc", "text", "interview", "output", "output_lrc"):
        config["paths"][key] = ""


def _default_interactive_mode(config: dict) -> str:
    paths = config["paths"]
    if paths.get("scan"):
        return "batch"
    if paths.get("text"):
        return "text"
    if paths.get("interview"):
        return "interview"
    if paths.get("audio") and paths.get("lrc"):
        return "audio"
    return "batch"


def _interactive_select_inputs(config: dict) -> None:
    """Populate config input paths using a small menu."""
    while True:
        mode = _prompt_choice(
            "What do you want to process?",
            [
                ("batch", "Batch folder scan"),
                ("text", "One bilingual text file"),
                ("interview", "One mock interview Q/A file"),
                ("audio", "One audio file plus matching LRC"),
                ("config", "Use input paths already in config.yaml"),
            ],
            default=_default_interactive_mode(config),
        )
        if mode != "config" or _config_has_input(config):
            break
        print("config.yaml does not contain runnable input paths yet.")

    if mode == "batch":
        default_scan = config["paths"].get("scan", "")
        _clear_input_paths(config)
        config["paths"]["scan"] = _prompt_path(
            "Folder to scan", default=default_scan, kind="dir",
        )
        config["mode"] = _prompt_choice(
            "Which files should the scanner process?",
            [
                ("", "Audio+LRC pairs and text files"),
                ("audio", "Audio+LRC pairs only"),
                ("text", "Text files only"),
            ],
            default=config.get("mode") if config.get("mode") in ("audio", "text") else "text",
        )
    elif mode == "text":
        default_text = config["paths"].get("text", "")
        _clear_input_paths(config)
        config["mode"] = "text"
        config["paths"]["text"] = _prompt_path(
            "Bilingual text file", default=default_text, kind="file",
        )
        config["paths"]["output"] = _prompt_string(
            "Output audio path (empty = auto)",
        )
        if config["paths"]["output"]:
            config["paths"]["output_lrc"] = _prompt_string(
                "Output LRC path (empty = same stem as audio)",
            )
    elif mode == "interview":
        default_interview = config["paths"].get("interview", "")
        _clear_input_paths(config)
        config["mode"] = "interview"
        config["paths"]["interview"] = _prompt_path(
            "Mock interview Q/A text file", default=default_interview, kind="file",
        )
        config["paths"]["output"] = _prompt_string(
            "Output audio path (empty = auto)",
        )
        if config["paths"]["output"]:
            config["paths"]["output_lrc"] = _prompt_string(
                "Output LRC path (empty = same stem as audio)",
            )
    elif mode == "audio":
        default_audio = config["paths"].get("audio", "")
        default_lrc = config["paths"].get("lrc", "")
        _clear_input_paths(config)
        config["mode"] = "audio"
        config["paths"]["audio"] = _prompt_path(
            "Source audio file", default=default_audio, kind="file",
        )
        config["paths"]["lrc"] = _prompt_path(
            "LRC subtitle file", default=default_lrc, kind="file",
        )
        config["paths"]["output"] = _prompt_string(
            "Output audio path (empty = auto)",
        )
        if config["paths"]["output"]:
            config["paths"]["output_lrc"] = _prompt_string(
                "Output LRC path (empty = same stem as audio)",
            )


def _infer_target_language(config: dict) -> str:
    """Infer a target language preset from selected input path names."""
    path_text = " ".join(
        Path(value).name.lower()
        for value in config.get("paths", {}).values()
        if value
    )
    if "english" in path_text:
        return "en"
    if "japanese" in path_text:
        return "ja"
    return ""


def _interactive_apply_language(config: dict) -> None:
    inferred = _infer_target_language(config)
    current_interview_lang = config.get("interview", {}).get("lang", "")
    if _is_interview_config(config) and current_interview_lang in INTERVIEW_LANG_PRESETS:
        inferred = inferred or current_interview_lang
    language = _prompt_choice(
        "Target language preset",
        [
            ("keep", "Keep current voices from config"),
            (
                "ja",
                "Japanese target"
                + (" (inferred from input path)" if inferred == "ja" else ""),
            ),
            (
                "en",
                "English target"
                + (" (inferred from input path)" if inferred == "en" else ""),
            ),
        ],
        default=inferred or "keep",
    )
    if language in LANG_PRESETS:
        _apply_target_language_preset(config, language)
        _apply_interview_language_preset(config, language)


def _interactive_apply_tts(config: dict) -> None:
    engine = _prompt_choice(
        "TTS engine",
        [
            ("google", "Google Cloud TTS (stable, paid)"),
            ("edge", "edge-tts (free)"),
            ("openai", "OpenAI TTS (best for math / symbols, paid)"),
        ],
        default=config["tts"].get("engine", "google"),
    )
    config["tts"]["engine"] = engine

    if engine == "openai" and not _is_interview_config(config):
        current_voice = config["tts"]["openai"].get("voice", "coral")
        if current_voice not in OPENAI_VOICES:
            current_voice = "coral"
        config["tts"]["openai"]["voice"] = _prompt_choice(
            "OpenAI voice",
            [(voice, voice) for voice in OPENAI_VOICES],
            default=current_voice,
        )


def _interactive_apply_native_voice(config: dict) -> None:
    """Choose the voice — and pace — of the native-language narration.

    In an audio-only run this is the *only* voice the run synthesises, since the
    target side is cut from the recording, so the pace prompt is offered there
    too. In text/interview runs the same speaking rate would also move the
    target voice, so pace is left to `tts.target_rate` and the advanced menu.
    """
    engine = config["tts"]["engine"]
    audio_only = _run_is_audio_only(config)

    if engine == "google":
        google = config["tts"]["google"]
        current = google.get("native_voice", "")
        persona = current.rsplit("-", 1)[-1] if "Chirp3-HD-" in current else ""
        choice = _prompt_choice(
            "Native narration voice",
            [("keep", f"Keep current ({current or 'config default'})")]
            + [(name, name) for name in GOOGLE_PERSONAS],
            default=persona if persona in GOOGLE_PERSONAS else "keep",
        )
        if choice != "keep":
            google["native_voice"] = google_voice_with_persona(current, choice)
    elif engine == "openai":
        openai = config["tts"]["openai"]
        current = openai.get("voice", "coral")
        openai["voice"] = _prompt_choice(
            "Native narration voice",
            [(voice, voice) for voice in OPENAI_VOICES],
            default=current if current in OPENAI_VOICES else "coral",
        )
    elif engine == "edge":
        config["tts"]["native_voice"] = _prompt_string(
            "Native narration voice",
            config["tts"].get("native_voice", ""),
            required=True,
        )

    if not audio_only:
        return

    rate = _prompt_float(
        "Narration speed (1.0 = normal)", _native_rate(config),
    )
    _apply_native_rate(config, rate)


def _native_rate(config: dict) -> float:
    """Current speaking-rate multiplier for the native narration."""
    engine = config["tts"]["engine"]
    if engine == "google":
        return float(config["tts"]["google"].get("speaking_rate") or 1.0)
    if engine == "openai":
        return float(config["tts"]["openai"].get("speed") or 1.0)
    return _edge_rate_to_multiplier(config["tts"].get("rate", "+0%"))


def _apply_native_rate(config: dict, rate: float) -> None:
    """Set the narration speed for the active engine, clamped to the safe range.

    Only reachable from an audio-only run, where nothing but the narration is
    synthesised — so this never silently re-times a target voice.
    """
    rate = coerce_speaking_rate(rate)
    if rate is None:
        return
    engine = config["tts"]["engine"]
    if engine == "google":
        config["tts"]["google"]["speaking_rate"] = rate
    elif engine == "openai":
        config["tts"]["openai"]["speed"] = rate
    elif engine == "edge":
        config["tts"]["rate"] = f"{round((rate - 1.0) * 100):+d}%"


def _edge_rate_to_multiplier(rate: str) -> float:
    """Read edge-tts' signed percentage ("+20%") back as a multiplier."""
    try:
        return 1.0 + float(str(rate).strip().rstrip("%")) / 100.0
    except (TypeError, ValueError):
        return 1.0


def _is_interview_config(config: dict) -> bool:
    return bool(config.get("paths", {}).get("interview"))


def _run_is_audio_only(config: dict) -> bool:
    """True when every item this run will process is a recording + LRC pair.

    Such a run generates no target speech at all — the target side is sliced out
    of the recording — so target-voice and target-language prompts have nothing
    to act on and are skipped.
    """
    paths = config.get("paths", {})
    scan = paths.get("scan")
    if scan:
        mode_filter = config.get("mode", "").strip().lower()
        if mode_filter in ("audio", "text"):
            return mode_filter == "audio"
        try:
            items = _probe_scan(scan)
        except (OSError, NotADirectoryError):
            return False
        return bool(items) and all(item.mode == "audio" for item in items)
    return bool(paths.get("audio") and paths.get("lrc"))


def _probe_scan(folder: str | Path) -> list[ScanItem]:
    """Scan a folder just to inspect it, without logging.

    The real run scans and reports again; without this the scanner's
    "no matching .lrc" warnings would be printed twice per unpaired file.
    """
    scanner_log = logging.getLogger("scanner.scanner")
    previous = scanner_log.disabled
    scanner_log.disabled = True
    try:
        return scan_folder(folder)
    finally:
        scanner_log.disabled = previous


def _unpaired_audio(folder: str | Path) -> list[str]:
    """Recordings in a folder that have no matching .lrc, so cannot be used."""
    try:
        entries = list(Path(folder).iterdir())
    except OSError:
        return []
    return sorted(
        f.name
        for f in entries
        if f.is_file()
        and f.suffix.lower().lstrip(".") in AUDIO_EXTENSIONS
        and not any(f.stem.endswith(s) for s in OUTPUT_SUFFIXES)
        and not f.with_suffix(".lrc").is_file()
    )


def _print_input_preview(config: dict) -> None:
    """Show what a pasted path resolved to, before anything is generated."""
    paths = config["paths"]
    scan = paths.get("scan")

    print()
    if scan:
        mode_filter = config.get("mode", "").strip().lower()
        try:
            items = _probe_scan(scan)
        except (OSError, NotADirectoryError) as exc:
            print(f"Input: folder {scan} (could not be scanned: {exc})")
            return
        if mode_filter in ("audio", "text"):
            items = [item for item in items if item.mode == mode_filter]
        print(f"Input: folder {scan}")
        for item in items:
            if item.mode == "audio":
                print(f"  • {item.audio_path.name} + {item.lrc_path.name}")
            else:
                print(f"  • {item.text_path.name}")
        print(f"  {len(items)} item{'s' if len(items) != 1 else ''} to process")
        # Recordings without subtitles are silently dropped by the scanner;
        # say so here, while there is still a chance to fix the folder.
        unpaired = _unpaired_audio(scan)
        if unpaired:
            print(f"  skipping {len(unpaired)} recording(s) with no .lrc: "
                  f"{', '.join(unpaired[:4])}"
                  + (" …" if len(unpaired) > 4 else ""))
    elif paths.get("audio"):
        audio = Path(paths["audio"])
        print(f"Input: {audio.name} + {Path(paths['lrc']).name}")
        print(f"  in {audio.parent}")
    elif paths.get("interview"):
        print(f"Input: interview script {paths['interview']}")
    elif paths.get("text"):
        print(f"Input: bilingual text {paths['text']}")
    print("Output goes next to the source files.")


def _config_for_interview_role(config: dict, role: str) -> dict:
    """Return config with the target TTS voice and speed set for one role.

    Only the target audio goes through here, so a per-role speech rate never
    affects the Chinese narration.
    """
    interview = config.get("interview", {})
    c = copy.deepcopy(config)
    is_answer = role == "a"

    rate_key = "interviewee_rate" if is_answer else "interviewer_rate"
    apply_speaking_rate(c, interview.get(rate_key) or c["tts"].get("target_rate"))

    edge_key = "interviewee_voice" if is_answer else "interviewer_voice"
    c["tts"]["target_voice"] = interview.get(edge_key) or c["tts"]["target_voice"]

    google = interview.get("google", {})
    google_key = "interviewee_voice" if is_answer else "interviewer_voice"
    c["tts"]["google"]["target_voice"] = (
        google.get(google_key) or c["tts"]["google"]["target_voice"]
    )

    openai = interview.get("openai", {})
    openai_key = "interviewee_voice" if is_answer else "interviewer_voice"
    c["tts"]["openai"]["target_voice"] = (
        openai.get(openai_key) or c["tts"]["openai"].get("target_voice", "")
    )
    return c


def _interactive_apply_interview_voices(config: dict) -> None:
    """Let interview mode customize its two speaker voices."""
    if not _is_interview_config(config):
        return

    if not _prompt_yes_no("Customize interview role voices?", default=False):
        return

    engine = config["tts"]["engine"]
    if engine == "google":
        google = config["interview"]["google"]
        google["interviewer_voice"] = _prompt_string(
            "Google interviewer voice",
            google["interviewer_voice"],
            required=True,
        )
        google["interviewee_voice"] = _prompt_string(
            "Google interviewee voice",
            google["interviewee_voice"],
            required=True,
        )
    elif engine == "edge":
        config["interview"]["interviewer_voice"] = _prompt_string(
            "edge interviewer voice",
            config["interview"]["interviewer_voice"],
            required=True,
        )
        config["interview"]["interviewee_voice"] = _prompt_string(
            "edge interviewee voice",
            config["interview"]["interviewee_voice"],
            required=True,
        )
    elif engine == "openai":
        openai = config["interview"]["openai"]
        openai["interviewer_voice"] = _prompt_choice(
            "OpenAI interviewer voice",
            [(voice, voice) for voice in OPENAI_VOICES],
            default=openai["interviewer_voice"],
        )
        openai["interviewee_voice"] = _prompt_choice(
            "OpenAI interviewee voice",
            [(voice, voice) for voice in OPENAI_VOICES],
            default=openai["interviewee_voice"],
        )


def _interactive_apply_loop(config: dict) -> None:
    loop_choice = _prompt_choice(
        "Loop pattern",
        [
            ("keep", f"Keep current ({_loop_label(config)})"),
            ("full", "Full: 1 T-N-T pass"),
            ("progressive", "Progressive: 1 T-N-T + 1 T-S-T pass"),
            ("shadow", "Shadow: 1 T-S-T pass"),
            ("custom", "Custom repeat counts"),
        ],
        default="keep",
    )
    if loop_choice == "keep":
        return
    if loop_choice == "custom":
        config["loop"]["variant"] = "full"
        config["loop"]["tnt_repeats"] = _prompt_non_negative_int(
            "T-N-T repeats per segment", 1,
        )
        config["loop"]["tst_repeats"] = _prompt_non_negative_int(
            "T-S-T repeats per segment", 1,
        )
        if (
            config["loop"]["tnt_repeats"] == 0
            and config["loop"]["tst_repeats"] == 0
        ):
            print("Both repeat counts cannot be 0; using 1 T-N-T pass.")
            config["loop"]["tnt_repeats"] = 1
        return

    config["loop"]["variant"] = loop_choice
    config["loop"]["tnt_repeats"] = None
    config["loop"]["tst_repeats"] = None


def _interactive_apply_split_outputs(config: dict) -> None:
    current = "yes" if _split_enabled(config) else "no"
    choice = _prompt_choice(
        "Output layout",
        [
            ("keep", f"Keep current ({'split' if current == 'yes' else 'single file'})"),
            ("yes", "Split into _tnt and _tst files"),
            ("no", "Single combined Echo Loop file"),
        ],
        default="keep",
    )
    if choice != "keep":
        config["loop"]["split_outputs"] = choice == "yes"


def _interactive_apply_timing(config: dict) -> None:
    """The three silences that shape one Echo Loop pass."""
    if not _prompt_yes_no(
        f"Adjust silences? (currently {_timing_label(config)})", default=False,
    ):
        return
    config["timing"]["after_first_target"] = _prompt_float(
        "Silence after first target (seconds)",
        config["timing"]["after_first_target"],
    )
    config["timing"]["after_native"] = _prompt_float(
        "Silence after native audio (seconds)",
        config["timing"]["after_native"],
    )
    config["timing"]["after_second_target"] = _prompt_float(
        "Silence after second target / loop gap (seconds)",
        config["timing"]["after_second_target"],
    )


def _timing_label(config: dict) -> str:
    timing = config["timing"]
    return (
        f"{timing['after_first_target']}s / {timing['after_native']}s / "
        f"{timing['after_second_target']}s"
    )


def _interactive_apply_volume(config: dict) -> None:
    volume = _prompt_choice(
        "TTS volume",
        [
            ("keep", f"Keep current ({_volume_label(config)})"),
            ("none", "No gain / normalization"),
            ("gain", "Set fixed gain in dB"),
            ("normalize", "Normalize clips to target dBFS"),
        ],
        default="keep",
    )
    if volume == "none":
        config["tts"]["gain"] = 0
        config["tts"]["normalize"] = None
    elif volume == "gain":
        config["tts"]["gain"] = _prompt_float("Gain in dB", config["tts"]["gain"])
        config["tts"]["normalize"] = None
    elif volume == "normalize":
        default = config["tts"]["normalize"]
        config["tts"]["normalize"] = _prompt_float(
            "Target dBFS", -20.0 if default is None else default,
        )


def _interactive_apply_advanced(config: dict) -> None:
    """Rarely-touched escape hatches: raw voice names and the LRC delimiter.

    Voices entered here are full engine voice names and are asked last, so they
    override the persona picked in the narration-voice prompt above.
    """
    if not _prompt_yes_no("Adjust advanced settings?", default=False):
        return

    engine = config["tts"]["engine"]
    if _is_interview_config(config):
        if engine == "openai" and _prompt_yes_no(
            "Customize OpenAI instructions?", default=False,
        ):
            config["tts"]["openai"]["instructions"] = _prompt_string(
                "OpenAI instructions",
                config["tts"]["openai"].get("instructions", ""),
            )
    elif engine == "google":
        if _prompt_yes_no("Customize Google voices?", default=False):
            config["tts"]["google"]["target_voice"] = _prompt_string(
                "Google target voice",
                config["tts"]["google"]["target_voice"],
                required=True,
            )
            config["tts"]["google"]["native_voice"] = _prompt_string(
                "Google native voice",
                config["tts"]["google"]["native_voice"],
                required=True,
            )
    elif engine == "edge":
        if _prompt_yes_no("Customize edge-tts voices?", default=False):
            config["tts"]["target_voice"] = _prompt_string(
                "edge target voice", config["tts"]["target_voice"], required=True,
            )
            config["tts"]["native_voice"] = _prompt_string(
                "edge native voice", config["tts"]["native_voice"], required=True,
            )
            config["tts"]["rate"] = _prompt_string(
                "edge rate", config["tts"]["rate"], required=True,
            )
    elif engine == "openai":
        if _prompt_yes_no("Customize OpenAI instructions?", default=False):
            config["tts"]["openai"]["instructions"] = _prompt_string(
                "OpenAI instructions",
                config["tts"]["openai"].get("instructions", ""),
            )

    if _prompt_yes_no("Customize LRC/text delimiter?", default=False):
        config["lrc"]["delimiter"] = _prompt_string(
            "Delimiter", config["lrc"]["delimiter"], required=True,
        )
        config["lrc"]["split_strategy"] = _prompt_choice(
            "Split strategy",
            [("last", "Split on last delimiter"), ("first", "Split on first delimiter")],
            default=config["lrc"]["split_strategy"],
        )


def _interactive_mode_label(config: dict) -> str:
    if config["paths"].get("scan"):
        mode_filter = config.get("mode", "").strip().lower()
        if mode_filter in ("audio", "text"):
            return f"batch ({mode_filter} only)"
        return "batch (audio+LRC pairs and text files)"
    mode = _interactive_single_file_mode(config)
    if mode == "text":
        return "text"
    if mode == "interview":
        return "interview"
    if mode == "audio":
        return "audio"
    return "config-only"


def _interactive_single_file_mode(config: dict) -> str:
    paths = config["paths"]
    forced = config.get("mode", "").strip().lower()
    has_audio = bool(paths.get("audio")) and bool(paths.get("lrc"))
    has_text = bool(paths.get("text"))
    has_interview = bool(paths.get("interview"))

    if forced == "audio" and has_audio:
        return "audio"
    if forced == "text" and has_text:
        return "text"
    if forced == "interview" and has_interview:
        return "interview"
    if has_audio:
        return "audio"
    if has_text:
        return "text"
    if has_interview:
        return "interview"
    return ""


def _print_interactive_summary(config: dict) -> None:
    print()
    print("Run summary")
    print(f"  Mode:        {_interactive_mode_label(config)}")
    if config["paths"].get("scan"):
        print(f"  Scan folder: {config['paths']['scan']}")
    if config["paths"].get("text"):
        print(f"  Text file:   {config['paths']['text']}")
    if config["paths"].get("interview"):
        print(f"  Interview:   {config['paths']['interview']}")
    if config["paths"].get("audio"):
        print(f"  Audio file:  {config['paths']['audio']}")
    if config["paths"].get("lrc"):
        print(f"  LRC file:    {config['paths']['lrc']}")
    single_mode = _interactive_single_file_mode(config)
    if single_mode:
        mode = single_mode
        audio_out, lrc_out = resolve_output_paths(config, mode)
        print(f"  Output:      {audio_out}")
        print(f"  Output LRC:  {lrc_out}")
    print(f"  Engine:      {_engine_label(config)}")
    print(f"  Loop:        {_loop_label(config)}")
    print(f"  Silences:    {_timing_label(config)}")
    print(f"  Volume:      {_volume_label(config)}")
    print()


def interactive_setup(config: dict, select_inputs: bool = True) -> dict:
    """Collect a runnable configuration from an interactive menu.

    ``select_inputs=False`` skips the input menu — used when a PATH was already
    given on the command line, so the session starts straight at the settings.
    """
    config = copy.deepcopy(config)

    print()
    print("Echo Loop Generator - interactive setup")
    print("Press Enter to accept defaults shown in brackets.")

    if select_inputs:
        _interactive_select_inputs(config)
    else:
        _print_input_preview(config)

    # An audio-only run slices its target speech out of the recording, so the
    # target-language preset has nothing to apply to.
    if not _run_is_audio_only(config):
        _interactive_apply_language(config)
    _interactive_apply_tts(config)
    _interactive_apply_native_voice(config)
    _interactive_apply_interview_voices(config)
    _interactive_apply_loop(config)
    _interactive_apply_split_outputs(config)
    _interactive_apply_timing(config)
    _interactive_apply_volume(config)
    _interactive_apply_advanced(config)
    _print_interactive_summary(config)

    if not _prompt_yes_no("Run now?", default=True):
        raise SystemExit(0)

    return config


def resolve_mode(config: dict) -> str:
    """Determine which mode to run."""
    paths = config["paths"]

    if paths.get("scan"):
        return "batch"

    mode = config.get("mode", "").strip().lower()
    has_audio = bool(paths.get("audio")) and bool(paths.get("lrc"))
    has_text = bool(paths.get("text"))
    has_interview = bool(paths.get("interview"))

    if mode in ("audio", "text", "interview"):
        if mode == "audio" and not has_audio:
            logger.error("audio mode requires 'audio' and 'lrc' paths")
            sys.exit(1)
        if mode == "text" and not has_text:
            logger.error("text mode requires 'text' path")
            sys.exit(1)
        if mode == "interview" and not has_interview:
            logger.error("interview mode requires 'interview' path")
            sys.exit(1)
        return mode

    if has_audio:
        return "audio"
    if has_text:
        return "text"
    if has_interview:
        return "interview"

    logger.error(
        "no input specified. Provide audio+lrc, --text, --interview, "
        "or --scan via CLI or config.yaml"
    )
    sys.exit(1)


def resolve_output_paths(config: dict, mode: str) -> tuple[Path, Path]:
    """Determine output audio and LRC file paths."""
    paths = config["paths"]
    ext = config["output"]["format"]
    suffix = _single_output_suffix(config)

    if paths.get("output"):
        audio_out = Path(paths["output"])
    elif mode == "interview" and paths.get("interview"):
        stem = Path(paths["interview"]).stem
        audio_out = Path(paths["interview"]).parent / f"{stem}{suffix}.{ext}"
    elif mode == "text" and paths.get("text"):
        stem = Path(paths["text"]).stem
        audio_out = Path(paths["text"]).parent / f"{stem}{suffix}.{ext}"
    elif mode == "audio" and paths.get("audio"):
        stem = Path(paths["audio"]).stem
        audio_out = Path(paths["audio"]).parent / f"{stem}{suffix}.{ext}"
    else:
        audio_out = Path(f"output{suffix}.{ext}")

    if paths.get("output_lrc"):
        lrc_out = Path(paths["output_lrc"])
    else:
        lrc_out = audio_out.with_suffix(".lrc")

    return audio_out, lrc_out


def resolve_output_paths_for_item(item: ScanItem, config: dict) -> tuple[Path, Path]:
    """Determine output paths for a single batch item (same folder, _echo suffix)."""
    ext = config["output"]["format"]
    source = item.audio_path if item.mode == "audio" else item.text_path
    stem = source.stem
    parent = source.parent
    audio_out = parent / f"{stem}_echo.{ext}"
    lrc_out = audio_out.with_suffix(".lrc")
    return audio_out, lrc_out


def _volume_label(config: dict) -> str:
    norm = config["tts"]["normalize"]
    gain = config["tts"]["gain"]
    if norm is not None:
        return f"normalize to {norm} dBFS"
    elif gain != 0:
        sign = "+" if gain > 0 else ""
        return f"{sign}{gain} dB"
    return "default"


def _rate_label(config: dict) -> str:
    """Active speech-rate overrides, or '' when every voice is at engine speed."""
    parts = []
    target_rate = config["tts"].get("target_rate")
    if target_rate is not None:
        parts.append(f"target x{target_rate:g}")
    if _is_interview_config(config):
        interview = config.get("interview", {})
        for key, label in (("interviewer_rate", "Q"), ("interviewee_rate", "A")):
            if interview.get(key) is not None:
                parts.append(f"{label} x{interview[key]:g}")
    return ", ".join(parts)


def _engine_label(config: dict) -> str:
    engine = config["tts"]["engine"]
    if _is_interview_config(config):
        interview = config["interview"]
        if engine == "openai":
            oai = config["tts"]["openai"]
            role_voices = interview["openai"]
            return (
                f"openai ({oai['model']}, "
                f"interviewer={role_voices['interviewer_voice']}, "
                f"interviewee={role_voices['interviewee_voice']}, "
                f"native={oai.get('native_voice') or oai['voice']})"
            )
        if engine == "google":
            role_voices = interview["google"]
            return (
                f"google (interviewer={role_voices['interviewer_voice']}, "
                f"interviewee={role_voices['interviewee_voice']}, "
                f"native={config['tts']['google']['native_voice']})"
            )
        return (
            f"edge-tts (interviewer={interview['interviewer_voice']}, "
            f"interviewee={interview['interviewee_voice']}, "
            f"native={config['tts']['native_voice']})"
        )

    # An audio-only run never synthesises target speech, so naming a target
    # voice there would describe a voice the run cannot use.
    narration_only = _run_is_audio_only(config)

    if engine == "openai":
        oai = config["tts"]["openai"]
        native = oai.get("native_voice") or oai["voice"]
        if narration_only:
            return f"openai ({oai['model']}, native={native})"
        if oai.get("target_voice") or oai.get("native_voice"):
            return (
                f"openai ({oai['model']}, "
                f"target={oai.get('target_voice') or oai['voice']}, "
                f"native={native})"
            )
        return f"openai ({oai['model']}, voice={oai['voice']})"
    if engine == "google":
        g = config["tts"]["google"]
        if narration_only:
            return f"google (native={g['native_voice']})"
        return f"google (target={g['target_voice']}, native={g['native_voice']})"
    if narration_only:
        return f"edge-tts (native={config['tts']['native_voice']})"
    return "edge-tts"


def _loop_kwargs(config: dict) -> dict:
    loop = config.get("loop", {})
    return {
        "variant": loop.get("variant", "full"),
        "tnt_repeats": loop.get("tnt_repeats"),
        "tst_repeats": loop.get("tst_repeats"),
    }


def _loop_label(config: dict) -> str:
    pattern = resolve_loop_pattern(**_loop_kwargs(config))
    base = f"{pattern.tnt_repeats} T-N-T + {pattern.tst_repeats} T-S-T"
    if config.get("loop", {}).get("split_outputs"):
        base += " (split into _tnt + _tst files)"
    return base


def _split_enabled(config: dict) -> bool:
    return bool(config.get("loop", {}).get("split_outputs"))


def _single_output_suffix(config: dict) -> str:
    """Suffix for a single (non-split) output file, based on the loop pattern.

    A T-S-T-only run (the default "shadow" variant) is named ``_tst`` and a
    T-N-T-only run ``_tnt``; a mixed run keeps the neutral ``_echo``. When split
    output is enabled the caller derives ``_tnt`` / ``_tst`` itself (and strips a
    trailing ``_echo``), so we keep ``_echo`` here to feed that path cleanly.
    """
    if _split_enabled(config):
        return "_echo"
    pattern = resolve_loop_pattern(**_loop_kwargs(config))
    if pattern.tst_repeats > 0 and pattern.tnt_repeats == 0:
        return "_tst"
    if pattern.tnt_repeats > 0 and pattern.tst_repeats == 0:
        return "_tnt"
    return "_echo"


def _split_paths_from_stem(parent: Path, stem: str, ext: str) -> tuple[Path, Path, Path, Path]:
    """Return (tnt_audio, tnt_lrc, tst_audio, tst_lrc) for a given input stem."""
    tnt_audio = parent / f"{stem}_tnt.{ext}"
    tst_audio = parent / f"{stem}_tst.{ext}"
    return tnt_audio, tnt_audio.with_suffix(".lrc"), tst_audio, tst_audio.with_suffix(".lrc")


def resolve_split_output_paths(config: dict, mode: str) -> tuple[Path, Path, Path, Path]:
    """Compute split output paths for single-file modes (audio / text)."""
    paths = config["paths"]
    ext = config["output"]["format"]
    if mode == "interview" and paths.get("interview"):
        source = Path(paths["interview"])
    elif mode == "text" and paths.get("text"):
        source = Path(paths["text"])
    elif mode == "audio" and paths.get("audio"):
        source = Path(paths["audio"])
    else:
        source = Path("output")
    return _split_paths_from_stem(source.parent, source.stem, ext)


def resolve_split_output_paths_for_item(item: ScanItem, config: dict) -> tuple[Path, Path, Path, Path]:
    """Compute split output paths for a batch item."""
    ext = config["output"]["format"]
    source = item.audio_path if item.mode == "audio" else item.text_path
    return _split_paths_from_stem(source.parent, source.stem, ext)


def progress_bar(current: int, total: int) -> None:
    """
    Render progress bar for tty (interactive); log decile checkpoints otherwise.

    When stdout is redirected to a file (e.g. via `tee`), the \\r-overwriting
    progress bar would produce hundreds of useless lines in the log. In that
    case we degrade to ~10 INFO-level "Progress: N/M" entries instead.
    """
    if sys.stdout.isatty():
        bar_len = 30
        filled = int(bar_len * current / total)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\r  [{bar}] {current}/{total}", end="", flush=True)
        if current == total:
            print()  # final newline so subsequent log lines start fresh
    else:
        # Non-interactive: log only when crossing each 10% boundary or at the end
        prev_decile = (current - 1) * 10 // total if current > 0 else -1
        curr_decile = current * 10 // total
        if current == total or curr_decile > prev_decile:
            logger.info(f"  Progress: {current}/{total}")


def _print_segment_summary(segments: list) -> None:
    """Log a short summary of parsed segments."""
    logger.info(f"  Found {len(segments)} segments")
    for seg in segments[:3]:
        logger.info(f"    T: {seg.target_text[:40]}...")
        logger.info(f"    N: {seg.native_text[:40]}...")
    if len(segments) > 3:
        logger.info(f"    ... and {len(segments) - 3} more")


def _tts_volume_kwargs(config: dict) -> dict:
    return {
        "gain_db": config["tts"]["gain"],
        "normalize_target_dbfs": config["tts"]["normalize"],
    }


def _tts_engine_kwargs(config: dict) -> dict:
    return {
        "engine": config["tts"]["engine"],
        "openai_config": config["tts"]["openai"],
        "google_config": config["tts"]["google"],
    }


def _config_with_target_rate(config: dict) -> dict:
    """Config with ``tts.target_rate`` applied, or the original when unset."""
    rate = config["tts"].get("target_rate")
    if rate is None:
        return config
    c = copy.deepcopy(config)
    apply_speaking_rate(c, rate)
    return c


def _assemble_export_one(
    segments, target_audios, native_audios,
    timing, config, output_path, lrc_output_path,
    tnt_repeats: int, tst_repeats: int, label: str,
) -> tuple[Path, Path]:
    """Assemble and export a single output with explicit TNT/TST counts.

    Returns the (audio, lrc) paths written, so the caller can sync them.
    """
    logger.info(f"  Assembling {label} ({tnt_repeats} T-N-T + {tst_repeats} T-S-T)...")
    result = assemble_all_loops(
        target_audios, native_audios, timing, progress_bar,
        variant="full", tnt_repeats=tnt_repeats, tst_repeats=tst_repeats,
    )

    logger.info(f"  Exporting {label}...")
    export_audio(
        result,
        output_path,
        format=config["output"]["format"],
        bitrate=config["output"]["bitrate"],
        sample_rate=config["output"]["sample_rate"],
    )

    generate_echo_lrc(
        segments, target_audios, native_audios, timing,
        lrc_output_path, delimiter=config["lrc"]["delimiter"],
        variant="full", tnt_repeats=tnt_repeats, tst_repeats=tst_repeats,
    )

    logger.info(f"✓ {label} saved to: {output_path}")
    return output_path, lrc_output_path


def _assemble_and_export(
    segments, target_audios, native_audios,
    timing, config, output_path, lrc_output_path, work_dir,
) -> None:
    """Assemble Echo Loops, export audio and LRC, clean up.

    When loop.split_outputs is set, emits two files (T-N-T only and T-S-T only)
    derived from the same TTS / target audio — output_path is treated as the
    template and the _tnt / _tst variants are written alongside the source input.
    """
    pattern = resolve_loop_pattern(**_loop_kwargs(config))
    written: list[Path] = []

    try:
        if _split_enabled(config):
            parent = output_path.parent
            stem = output_path.stem
            # Strip a trailing _echo suffix if the caller derived the path that way.
            if stem.endswith("_echo"):
                stem = stem[:-5]
            ext = config["output"]["format"]
            tnt_audio_out, tnt_lrc_out, tst_audio_out, tst_lrc_out = \
                _split_paths_from_stem(parent, stem, ext)

            if pattern.tnt_repeats > 0:
                written.append(_assemble_export_one(
                    segments, target_audios, native_audios,
                    timing, config, tnt_audio_out, tnt_lrc_out,
                    tnt_repeats=pattern.tnt_repeats, tst_repeats=0,
                    label="T-N-T file",
                )[0])
            else:
                logger.info("  Skipping T-N-T output (tnt_repeats=0)")

            if pattern.tst_repeats > 0:
                written.append(_assemble_export_one(
                    segments, target_audios, native_audios,
                    timing, config, tst_audio_out, tst_lrc_out,
                    tnt_repeats=0, tst_repeats=pattern.tst_repeats,
                    label="T-S-T file",
                )[0])
            else:
                logger.info("  Skipping T-S-T output (tst_repeats=0)")
        else:
            written.append(_assemble_export_one(
                segments, target_audios, native_audios,
                timing, config, output_path, lrc_output_path,
                tnt_repeats=pattern.tnt_repeats,
                tst_repeats=pattern.tst_repeats,
                label="Echo Loop file",
            )[0])
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    # Optionally copy this run's outputs into the configured sync folder
    # (e.g. a Google Drive for Desktop synced directory). No-op unless enabled.
    sync_outputs(written, config)


def run_audio_mode(config: dict) -> None:
    """Audio mode: extract target audio from source file + LRC."""
    audio_path = Path(config["paths"]["audio"])
    lrc_path = Path(config["paths"]["lrc"])
    output_path, lrc_output_path = resolve_output_paths(config, "audio")

    timing = EchoTiming(
        after_first_target=config["timing"]["after_first_target"],
        after_native=config["timing"]["after_native"],
        after_second_target=config["timing"]["after_second_target"],
    )

    vol_kwargs = _tts_volume_kwargs(config)
    eng_kwargs = _tts_engine_kwargs(config)

    folder_log = attach_folder_log(audio_path.parent)
    try:
        logger.info("=" * 60)
        logger.info("  Echo Loop Generator — Audio Mode")
        logger.info("  T → S → N → S → T → S")
        logger.info("=" * 60)
        logger.info(f"  Audio:      {audio_path}")
        logger.info(f"  LRC:        {lrc_path}")
        logger.info(f"  Output:     {output_path}")
        logger.info(f"  Output LRC: {lrc_output_path}")
        logger.info(f"  Timing:     {timing.after_first_target}s / "
                    f"{timing.after_native}s / {timing.after_second_target}s")
        logger.info(f"  TTS Engine: {_engine_label(config)}")
        if config["tts"]["engine"] == "edge":
            logger.info(f"  Native TTS: {config['tts']['native_voice']}")
        logger.info(f"  TTS Volume: {_volume_label(config)}")
        logger.info(f"  Loop:       {_loop_label(config)}")
        if folder_log:
            logger.info(f"  Folder log: {folder_log}")
        central_log = get_central_log_path()
        if central_log:
            logger.info(f"  Central log: {central_log}")
        logger.info("=" * 60)

        start = time.monotonic()

        logger.info("[1/5] Loading source audio...")
        source_audio = load_audio(audio_path)
        audio_duration_ms = len(source_audio)
        logger.info(f"  Loaded: {audio_duration_ms / 1000:.1f}s, "
                    f"{source_audio.frame_rate}Hz, {source_audio.channels}ch")

        logger.info("[2/5] Parsing LRC subtitles...")
        segments = parse_lrc(
            lrc_path,
            delimiter=config["lrc"]["delimiter"],
            split_strategy=config["lrc"]["split_strategy"],
            audio_duration_ms=audio_duration_ms,
        )
        _print_segment_summary(segments)

        logger.info("[3/5] Extracting target audio segments...")
        target_audios = extract_all_segments(source_audio, segments)
        logger.info(f"  Extracted {len(target_audios)} segments")

        logger.info("[4/5] Generating native TTS audio...")
        work_dir = Path(tempfile.mkdtemp(prefix="echo_loop_"))
        native_audios = generate_native_audio(
            segments,
            voice=config["tts"]["native_voice"],
            rate=config["tts"]["rate"],
            pitch=config["tts"]["pitch"],
            work_dir=work_dir,
            **vol_kwargs,
            **eng_kwargs,
        )
        logger.info(f"  Generated {len(native_audios)} TTS audio clips")

        logger.info("[5/5] Assembling and exporting...")
        _assemble_and_export(
            segments, target_audios, native_audios,
            timing, config, output_path, lrc_output_path, work_dir,
        )

        elapsed = time.monotonic() - start
        logger.info(f"  Total time: {elapsed:.1f}s")
    finally:
        detach_folder_log()


def run_text_mode(config: dict) -> None:
    """Text-only mode: generate both target and native audio via TTS."""
    text_path = Path(config["paths"]["text"])
    output_path, lrc_output_path = resolve_output_paths(config, "text")

    timing = EchoTiming(
        after_first_target=config["timing"]["after_first_target"],
        after_native=config["timing"]["after_native"],
        after_second_target=config["timing"]["after_second_target"],
    )

    vol_kwargs = _tts_volume_kwargs(config)
    eng_kwargs = _tts_engine_kwargs(config)

    folder_log = attach_folder_log(text_path.parent)
    try:
        logger.info("=" * 60)
        logger.info("  Echo Loop Generator — Text-Only Mode")
        logger.info("  T → S → N → S → T → S")
        logger.info("=" * 60)
        logger.info(f"  Text:       {text_path}")
        logger.info(f"  Output:     {output_path}")
        logger.info(f"  Output LRC: {lrc_output_path}")
        logger.info(f"  Timing:     {timing.after_first_target}s / "
                    f"{timing.after_native}s / {timing.after_second_target}s")
        logger.info(f"  TTS Engine: {_engine_label(config)}")
        if config["tts"]["engine"] == "edge":
            logger.info(f"  Target TTS: {config['tts']['target_voice']}")
            logger.info(f"  Native TTS: {config['tts']['native_voice']}")
        logger.info(f"  TTS Volume: {_volume_label(config)}")
        if _rate_label(config):
            logger.info(f"  TTS Speed:  {_rate_label(config)}")
        logger.info(f"  Loop:       {_loop_label(config)}")
        if folder_log:
            logger.info(f"  Folder log: {folder_log}")
        central_log = get_central_log_path()
        if central_log:
            logger.info(f"  Central log: {central_log}")
        logger.info("=" * 60)

        start = time.monotonic()

        logger.info("[1/4] Parsing text file...")
        segments = parse_text(
            text_path,
            delimiter=config["lrc"]["delimiter"],
            split_strategy=config["lrc"]["split_strategy"],
        )
        _print_segment_summary(segments)

        work_dir = Path(tempfile.mkdtemp(prefix="echo_loop_"))

        logger.info("[2/4] Generating target TTS audio...")
        # tts.target_rate speeds up / slows down the target voice only; the
        # native narration below keeps the base config so it stays followable.
        target_config = _config_with_target_rate(config)
        target_audios = generate_target_audio(
            segments,
            voice=target_config["tts"]["target_voice"],
            rate=target_config["tts"]["rate"],
            pitch=target_config["tts"]["pitch"],
            work_dir=work_dir,
            **vol_kwargs,
            **_tts_engine_kwargs(target_config),
        )
        logger.info(f"  Generated {len(target_audios)} target TTS clips")

        logger.info("[3/4] Generating native TTS audio...")
        native_audios = generate_native_audio(
            segments,
            voice=config["tts"]["native_voice"],
            rate=config["tts"]["rate"],
            pitch=config["tts"]["pitch"],
            work_dir=work_dir,
            **vol_kwargs,
            **eng_kwargs,
        )
        logger.info(f"  Generated {len(native_audios)} native TTS clips")

        logger.info("[4/4] Assembling and exporting...")
        _assemble_and_export(
            segments, target_audios, native_audios,
            timing, config, output_path, lrc_output_path, work_dir,
        )

        elapsed = time.monotonic() - start
        logger.info(f"  Total time: {elapsed:.1f}s")
    finally:
        detach_folder_log()


def _generate_interview_target_audio(
    segments,
    config: dict,
    work_dir: Path,
    vol_kwargs: dict,
) -> list:
    """Generate target-language clips with interviewer/interviewee voices by role."""
    target_audios = [None] * len(segments)
    role_labels = {"q": "interviewer", "a": "interviewee"}

    for role, label in role_labels.items():
        indexed_segments = [
            (idx, seg)
            for idx, seg in enumerate(segments)
            if getattr(seg, "role", "") == role
        ]
        if not indexed_segments:
            continue

        role_config = _config_for_interview_role(config, role)
        role_segments = [seg for _, seg in indexed_segments]
        role_audios = generate_target_audio(
            role_segments,
            voice=role_config["tts"]["target_voice"],
            rate=role_config["tts"]["rate"],
            pitch=role_config["tts"]["pitch"],
            work_dir=work_dir / label,
            **vol_kwargs,
            **_tts_engine_kwargs(role_config),
        )
        for (original_idx, _), audio in zip(indexed_segments, role_audios):
            target_audios[original_idx] = audio

    missing = [idx for idx, audio in enumerate(target_audios) if audio is None]
    if missing:
        raise RuntimeError(f"Missing interview target audio for segments: {missing}")

    return target_audios


def run_interview_mode(config: dict) -> None:
    """Interview mode: generate Q/A mock interview audio via two TTS voices."""
    interview_path = Path(config["paths"]["interview"])
    output_path, lrc_output_path = resolve_output_paths(config, "interview")

    timing = EchoTiming(
        after_first_target=config["timing"]["after_first_target"],
        after_native=config["timing"]["after_native"],
        after_second_target=config["timing"]["after_second_target"],
    )

    vol_kwargs = _tts_volume_kwargs(config)
    eng_kwargs = _tts_engine_kwargs(config)

    folder_log = attach_folder_log(interview_path.parent)
    try:
        logger.info("=" * 60)
        logger.info("  Echo Loop Generator — Mock Interview Mode")
        logger.info("  English → S → Chinese → S → English → S")
        logger.info("=" * 60)
        logger.info(f"  Interview:  {interview_path}")
        logger.info(f"  Output:     {output_path}")
        logger.info(f"  Output LRC: {lrc_output_path}")
        logger.info(f"  Timing:     {timing.after_first_target}s / "
                    f"{timing.after_native}s / {timing.after_second_target}s")
        logger.info(f"  TTS Engine: {_engine_label(config)}")
        logger.info(f"  TTS Volume: {_volume_label(config)}")
        if _rate_label(config):
            logger.info(f"  TTS Speed:  {_rate_label(config)}")
        logger.info(f"  Loop:       {_loop_label(config)}")
        if folder_log:
            logger.info(f"  Folder log: {folder_log}")
        central_log = get_central_log_path()
        if central_log:
            logger.info(f"  Central log: {central_log}")
        logger.info("=" * 60)

        start = time.monotonic()

        logger.info("[1/4] Parsing interview file...")
        segments = parse_interview_text(
            interview_path,
            delimiter=config["lrc"]["delimiter"],
            split_strategy=config["lrc"]["split_strategy"],
        )
        q_count = sum(1 for seg in segments if getattr(seg, "role", "") == "q")
        a_count = sum(1 for seg in segments if getattr(seg, "role", "") == "a")
        logger.info(f"  Found {len(segments)} interview lines ({q_count} Q, {a_count} A)")
        for seg in segments[:3]:
            prefix = "Q" if getattr(seg, "role", "") == "q" else "A"
            logger.info(f"    {prefix}: {seg.target_text[:40]}...")
            logger.info(f"    N: {seg.native_text[:40]}...")
        if len(segments) > 3:
            logger.info(f"    ... and {len(segments) - 3} more")

        work_dir = Path(tempfile.mkdtemp(prefix="echo_interview_"))

        logger.info("[2/4] Generating role-based English TTS audio...")
        target_audios = _generate_interview_target_audio(
            segments, config, work_dir, vol_kwargs,
        )
        logger.info(f"  Generated {len(target_audios)} English TTS clips")

        logger.info("[3/4] Generating native translation TTS audio...")
        native_audios = generate_native_audio(
            segments,
            voice=config["tts"]["native_voice"],
            rate=config["tts"]["rate"],
            pitch=config["tts"]["pitch"],
            work_dir=work_dir,
            **vol_kwargs,
            **eng_kwargs,
        )
        logger.info(f"  Generated {len(native_audios)} native TTS clips")

        logger.info("[4/4] Assembling and exporting...")
        _assemble_and_export(
            segments, target_audios, native_audios,
            timing, config, output_path, lrc_output_path, work_dir,
        )

        elapsed = time.monotonic() - start
        logger.info(f"  Total time: {elapsed:.1f}s")
    finally:
        detach_folder_log()


def run_batch_mode(config: dict) -> None:
    """Batch mode: scan a folder and process all matching files."""
    scan_path = Path(config["paths"]["scan"])
    mode_filter = config.get("mode", "").strip().lower()

    if mode_filter not in ("audio", "text"):
        mode_filter = ""

    timing = EchoTiming(
        after_first_target=config["timing"]["after_first_target"],
        after_native=config["timing"]["after_native"],
        after_second_target=config["timing"]["after_second_target"],
    )

    folder_log = attach_folder_log(scan_path)
    try:
        logger.info("=" * 60)
        logger.info("  Echo Loop Generator — Batch Mode")
        logger.info("  T → S → N → S → T → S")
        logger.info("=" * 60)
        logger.info(f"  Scan folder: {scan_path}")
        if mode_filter:
            logger.info(f"  Filter:      {mode_filter} only")
        else:
            logger.info(f"  Filter:      auto (audio pairs + text files)")
        logger.info(f"  Timing:      {timing.after_first_target}s / "
                    f"{timing.after_native}s / {timing.after_second_target}s")
        logger.info(f"  TTS Engine:  {_engine_label(config)}")
        if config["tts"]["engine"] == "edge":
            logger.info(f"  Native TTS:  {config['tts']['native_voice']}")
            if mode_filter in ("", "text"):
                logger.info(f"  Target TTS:  {config['tts']['target_voice']}")
        logger.info(f"  TTS Volume:  {_volume_label(config)}")
        logger.info(f"  Loop:        {_loop_label(config)}")
        if folder_log:
            logger.info(f"  Folder log:  {folder_log}")
        central_log = get_central_log_path()
        if central_log:
            logger.info(f"  Central log: {central_log}")
        logger.info("=" * 60)

        batch_start = time.monotonic()

        logger.info("  Scanning folder...")
        items = scan_folder(scan_path, mode=mode_filter)
        print_scan_summary(items)

        if not items:
            logger.info("  Nothing to process.")
            return

        succeeded: list[str] = []
        skipped: list[str] = []
        failed: list[tuple[str, str]] = []
        total = len(items)

        for idx, item in enumerate(items, 1):
            label = item.label
            logger.info("─" * 60)
            logger.info(f"  [{idx}/{total}] {label}")
            logger.info("─" * 60)

            output_path, lrc_output_path = resolve_output_paths_for_item(item, config)
            if _split_enabled(config):
                tnt_a, tnt_l, tst_a, tst_l = resolve_split_output_paths_for_item(item, config)
                pattern = resolve_loop_pattern(**_loop_kwargs(config))
                expected: list[Path] = []
                if pattern.tnt_repeats > 0:
                    expected += [tnt_a, tnt_l]
                if pattern.tst_repeats > 0:
                    expected += [tst_a, tst_l]
                if expected and all(p.exists() for p in expected):
                    logger.info(
                        f"  ⏭  Skipped (already exists): "
                        + ", ".join(p.name for p in expected)
                    )
                    skipped.append(label)
                    continue
            elif output_path.exists() and lrc_output_path.exists():
                logger.info(
                    f"  ⏭  Skipped (already exists): "
                    f"{output_path.name}, {lrc_output_path.name}"
                )
                skipped.append(label)
                continue

            item_start = time.monotonic()
            try:
                item_config = _config_for_item(config, item)

                if item.mode == "audio":
                    _run_single_audio(
                        item_config, item, timing, output_path, lrc_output_path,
                    )
                else:
                    _run_single_text(
                        item_config, item, timing, output_path, lrc_output_path,
                    )

                succeeded.append(label)
                elapsed = time.monotonic() - item_start
                logger.info(f"  ✓ Item completed in {elapsed:.1f}s")

            except Exception as e:
                elapsed = time.monotonic() - item_start
                full_tb = traceback.format_exc()
                short_err = str(e) if len(str(e)) <= 80 else str(e)[:77] + "..."
                # Console + log file get the short version
                logger.error(
                    f"  ✗ Failed: {label} — {short_err} (after {elapsed:.1f}s)"
                )
                # Full traceback only goes to log files (DEBUG level), not console
                logger.debug(f"Full traceback for {label}:\n{full_tb}")
                failed.append((label, full_tb))

        total_elapsed = time.monotonic() - batch_start

        logger.info("=" * 60)
        logger.info("  Batch Complete")
        logger.info("=" * 60)
        logger.info(f"  ✓ Succeeded: {len(succeeded)}")
        if skipped:
            logger.info(f"  ⏭  Skipped:   {len(skipped)}")
        if failed:
            logger.error(f"  ✗ Failed:    {len(failed)}")
            for name, tb in failed:
                # First line of traceback's final line is usually the exception summary
                short = tb.strip().splitlines()[-1] if tb.strip() else "(no detail)"
                if len(short) > 100:
                    short = short[:97] + "..."
                logger.error(f"    - {name}: {short}")
            logger.info("  (full tracebacks recorded in log files)")
        logger.info(f"  Total time:  {total_elapsed:.1f}s")
        logger.info("=" * 60)
    finally:
        detach_folder_log()


def _config_for_item(config: dict, item: ScanItem) -> dict:
    """Create a config copy with paths set for a specific batch item."""
    c = copy.deepcopy(config)
    c["paths"]["scan"] = ""

    if item.mode == "audio":
        c["paths"]["audio"] = str(item.audio_path)
        c["paths"]["lrc"] = str(item.lrc_path)
        c["paths"]["text"] = ""
        c["paths"]["interview"] = ""
    else:
        c["paths"]["text"] = str(item.text_path)
        c["paths"]["audio"] = ""
        c["paths"]["lrc"] = ""
        c["paths"]["interview"] = ""

    c["paths"]["output"] = ""
    c["paths"]["output_lrc"] = ""

    return c


def _run_single_audio(
    config: dict,
    item: ScanItem,
    timing: EchoTiming,
    output_path: Path,
    lrc_output_path: Path,
) -> None:
    """Process a single audio+LRC pair in batch mode."""
    audio_path = item.audio_path
    lrc_path = item.lrc_path

    vol_kwargs = _tts_volume_kwargs(config)
    eng_kwargs = _tts_engine_kwargs(config)

    logger.info(f"  Audio: {audio_path.name}")
    logger.info(f"  LRC:   {lrc_path.name}")
    logger.info(f"  →      {output_path.name}")

    source_audio = load_audio(audio_path)
    audio_duration_ms = len(source_audio)
    logger.info(f"  Loaded: {audio_duration_ms / 1000:.1f}s")

    segments = parse_lrc(
        lrc_path,
        delimiter=config["lrc"]["delimiter"],
        split_strategy=config["lrc"]["split_strategy"],
        audio_duration_ms=audio_duration_ms,
    )
    logger.info(f"  Segments: {len(segments)}")

    target_audios = extract_all_segments(source_audio, segments)

    work_dir = Path(tempfile.mkdtemp(prefix="echo_batch_"))
    native_audios = generate_native_audio(
        segments,
        voice=config["tts"]["native_voice"],
        rate=config["tts"]["rate"],
        pitch=config["tts"]["pitch"],
        work_dir=work_dir,
        **vol_kwargs,
        **eng_kwargs,
    )

    _assemble_and_export(
        segments, target_audios, native_audios,
        timing, config, output_path, lrc_output_path, work_dir,
    )


def _run_single_text(
    config: dict,
    item: ScanItem,
    timing: EchoTiming,
    output_path: Path,
    lrc_output_path: Path,
) -> None:
    """Process a single text file in batch mode."""
    text_path = item.text_path

    vol_kwargs = _tts_volume_kwargs(config)
    eng_kwargs = _tts_engine_kwargs(config)

    logger.info(f"  Text: {text_path.name}")
    logger.info(f"  →     {output_path.name}")

    segments = parse_text(
        text_path,
        delimiter=config["lrc"]["delimiter"],
        split_strategy=config["lrc"]["split_strategy"],
    )
    logger.info(f"  Segments: {len(segments)}")

    work_dir = Path(tempfile.mkdtemp(prefix="echo_batch_"))

    target_audios = generate_target_audio(
        segments,
        voice=config["tts"]["target_voice"],
        rate=config["tts"]["rate"],
        pitch=config["tts"]["pitch"],
        work_dir=work_dir,
        **vol_kwargs,
        **eng_kwargs,
    )

    native_audios = generate_native_audio(
        segments,
        voice=config["tts"]["native_voice"],
        rate=config["tts"]["rate"],
        pitch=config["tts"]["pitch"],
        work_dir=work_dir,
        **vol_kwargs,
        **eng_kwargs,
    )

    _assemble_and_export(
        segments, target_audios, native_audios,
        timing, config, output_path, lrc_output_path, work_dir,
    )


def main():
    load_dotenv()  # OPENAI_API_KEY etc.

    args = parse_args()
    config = load_config(args.config)
    config = apply_cli_overrides(config, args)

    # -y is the scripting escape hatch. Otherwise -i always opens the menu, and
    # a terminal session opens it for a PATH argument or for a run with nothing
    # configured yet. A PATH skips the input menu — the path *is* the input.
    if not args.yes:
        if args.smart_path and not (args.interactive or sys.stdin.isatty()):
            # A bare PATH promises a settings menu, and there is no terminal to
            # show it in. Generating anyway would silently spend TTS budget on
            # settings nobody confirmed, so make the caller say -y for that.
            # Logging is not configured yet, so this goes straight to stderr.
            print(
                f"error: {args.smart_path} was given without a terminal to show "
                f"the settings menu.\n"
                f"       Add -y to run it with the settings from config.yaml.",
                file=sys.stderr,
            )
            sys.exit(2)
        interactive = args.interactive or (
            sys.stdin.isatty()
            and (bool(args.smart_path) or not _config_has_input(config))
        )
        if interactive:
            config = interactive_setup(config, select_inputs=not args.smart_path)

    setup_logging()

    try:
        mode = resolve_mode(config)

        if mode == "batch":
            run_batch_mode(config)
        elif mode == "interview":
            run_interview_mode(config)
        elif mode == "text":
            run_text_mode(config)
        else:
            run_audio_mode(config)
    except SystemExit:
        # resolve_mode and similar call sys.exit(); don't treat as crash
        raise
    except Exception:
        # Anything else is a real crash — record full traceback to log files
        logger.exception("Unhandled exception in main")
        raise
    finally:
        close_all_handlers()


if __name__ == "__main__":
    main()

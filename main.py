#!/usr/bin/env python3
"""
Echo Loop Generator

Generates T-S-N-S-T-S (Target → Silence → Native → Silence → Target → Silence)
audio files for language learning, based on "Echo: Rebuilding the Natural Reflex
of Language" by H. Reeve.

Three modes:
  Audio mode:  python main.py lesson01.mp3 lesson01.lrc
  Text mode:   python main.py --text phrases.txt
  Batch mode:  python main.py --scan /path/to/folder
  Config-only: python main.py  (paths set in config.yaml)
"""

import argparse
import copy
import logging
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
from audio.splitter import load_audio, extract_all_segments
from audio.tts_generator import generate_native_audio, generate_target_audio
from audio.assembler import assemble_all_loops, EchoTiming
from export.exporter import export_audio
from export.lrc_writer import generate_echo_lrc
from scanner.scanner import scan_folder, print_scan_summary, ScanItem

from echo_logging import (
    setup_logging,
    attach_folder_log,
    detach_folder_log,
    close_all_handlers,
    get_central_log_path,
)

logger = logging.getLogger(__name__)


def load_config(config_path: str | Path | None = None) -> dict:
    """Load configuration from YAML file, falling back to defaults."""
    defaults = {
        "mode": "",
        "paths": {
            "scan": "",
            "audio": "",
            "lrc": "",
            "text": "",
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
                "speed": 1.0,
                "instructions": "",
            },
            "google": {
                "target_voice": "ja-JP-Neural2-B",
                "native_voice": "cmn-CN-Chirp3-HD-Kore",
                "speaking_rate": 1.0,
                "pitch": 0.0,
            },
            "gain": 0,
            "normalize": None,
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
        },
    }

    if config_path and Path(config_path).exists():
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f)
        if user_config:
            if "mode" in user_config:
                defaults["mode"] = user_config["mode"] or ""
            for section in ("paths", "timing", "output", "lrc", "loop"):
                if section in user_config and isinstance(user_config[section], dict):
                    defaults[section].update(user_config[section])
            if "tts" in user_config and isinstance(user_config["tts"], dict):
                tts_user = user_config["tts"]
                if "openai" in tts_user and isinstance(tts_user["openai"], dict):
                    defaults["tts"]["openai"].update(tts_user["openai"])
                if "google" in tts_user and isinstance(tts_user["google"], dict):
                    defaults["tts"]["google"].update(tts_user["google"])
                for k, v in tts_user.items():
                    if k not in ("openai", "google"):
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

    return defaults


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Echo Loop Generator - T•N•T language learning audio",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  Audio mode (extract target audio from file):
    %(prog)s lesson01.mp3 lesson01.lrc
    %(prog)s lesson01.mp3 lesson01.lrc -o output/echo.m4a

  Text-only mode (generate both target and native via TTS):
    %(prog)s --text phrases.txt
    %(prog)s --text phrases.txt -o output/echo.m4a

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
        "audio", nargs="?", default=None,
        help="Source audio file (mp3, wav, m4a, etc.) — audio mode",
    )
    input_group.add_argument(
        "lrc", nargs="?", default=None,
        help="LRC subtitle file with bilingual content — audio mode",
    )
    input_group.add_argument(
        "--text", "-t", dest="text_file", default=None,
        help="Bilingual text file — text-only mode",
    )
    input_group.add_argument(
        "--scan", "-s", dest="scan_dir", default=None,
        help="Folder to scan for batch processing",
    )

    parser.add_argument(
        "--mode", choices=["audio", "text"], default=None,
        help="Force mode (overrides config and auto-detection)",
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

    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Config file path (default: config.yaml)",
    )

    timing_group = parser.add_argument_group("Timing (overrides config)")
    timing_group.add_argument("--after-first-target", type=float, default=None)
    timing_group.add_argument("--after-native", type=float, default=None)
    timing_group.add_argument("--after-second-target", type=float, default=None)

    parser.add_argument(
        "--variant", choices=["full", "progressive", "shadow"], default=None,
    )

    tts_group = parser.add_argument_group("TTS (overrides config)")
    tts_group.add_argument("--engine", choices=["edge", "openai", "google"], default=None)
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

    lrc_group = parser.add_argument_group("LRC / text parsing (overrides config)")
    lrc_group.add_argument("--delimiter", default=None)
    lrc_group.add_argument("--split-strategy", choices=["first", "last"], default=None)

    return parser.parse_args()


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
    if args.scan_dir:
        config["paths"]["scan"] = args.scan_dir
    if args.output:
        config["paths"]["output"] = args.output
    if args.output_lrc:
        config["paths"]["output_lrc"] = args.output_lrc

    if args.after_first_target is not None:
        config["timing"]["after_first_target"] = args.after_first_target
    if args.after_native is not None:
        config["timing"]["after_native"] = args.after_native
    if args.after_second_target is not None:
        config["timing"]["after_second_target"] = args.after_second_target

    if args.engine:
        config["tts"]["engine"] = args.engine
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

    if args.delimiter:
        config["lrc"]["delimiter"] = args.delimiter
    if args.split_strategy:
        config["lrc"]["split_strategy"] = args.split_strategy

    if args.variant:
        config["loop"]["variant"] = args.variant

    return config


def resolve_mode(config: dict) -> str:
    """Determine which mode to run."""
    paths = config["paths"]

    if paths.get("scan"):
        return "batch"

    mode = config.get("mode", "").strip().lower()
    has_audio = bool(paths.get("audio")) and bool(paths.get("lrc"))
    has_text = bool(paths.get("text"))

    if mode in ("audio", "text"):
        if mode == "audio" and not has_audio:
            logger.error("audio mode requires 'audio' and 'lrc' paths")
            sys.exit(1)
        if mode == "text" and not has_text:
            logger.error("text mode requires 'text' path")
            sys.exit(1)
        return mode

    if has_audio:
        return "audio"
    if has_text:
        return "text"

    logger.error(
        "no input specified. Provide audio+lrc, --text, or --scan via CLI or config.yaml"
    )
    sys.exit(1)


def resolve_output_paths(config: dict, mode: str) -> tuple[Path, Path]:
    """Determine output audio and LRC file paths."""
    paths = config["paths"]
    ext = config["output"]["format"]

    if paths.get("output"):
        audio_out = Path(paths["output"])
    elif mode == "text" and paths.get("text"):
        stem = Path(paths["text"]).stem
        audio_out = Path(paths["text"]).parent / f"{stem}_echo.{ext}"
    elif mode == "audio" and paths.get("audio"):
        stem = Path(paths["audio"]).stem
        audio_out = Path(paths["audio"]).parent / f"{stem}_echo.{ext}"
    else:
        audio_out = Path(f"output_echo.{ext}")

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


def _engine_label(config: dict) -> str:
    engine = config["tts"]["engine"]
    if engine == "openai":
        oai = config["tts"]["openai"]
        return f"openai ({oai['model']}, voice={oai['voice']})"
    if engine == "google":
        g = config["tts"]["google"]
        return f"google (target={g['target_voice']}, native={g['native_voice']})"
    return "edge-tts"


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


def _assemble_and_export(
    segments, target_audios, native_audios,
    timing, config, output_path, lrc_output_path, work_dir,
) -> None:
    """Assemble Echo Loops, export audio and LRC, clean up."""
    variant = config.get("loop", {}).get("variant", "full")

    logger.info("  Assembling Echo Loops...")
    result = assemble_all_loops(
        target_audios, native_audios, timing, progress_bar, variant=variant,
    )

    logger.info("  Exporting...")
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
        variant=variant,
    )

    shutil.rmtree(work_dir, ignore_errors=True)
    logger.info(f"✓ Done! Echo Loop file saved to: {output_path}")


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
        logger.info(f"  Variant:    {config['loop']['variant']}")
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
        target_audios = generate_target_audio(
            segments,
            voice=config["tts"]["target_voice"],
            rate=config["tts"]["rate"],
            pitch=config["tts"]["pitch"],
            work_dir=work_dir,
            **vol_kwargs,
            **eng_kwargs,
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
            if output_path.exists() and lrc_output_path.exists():
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
    else:
        c["paths"]["text"] = str(item.text_path)
        c["paths"]["audio"] = ""
        c["paths"]["lrc"] = ""

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

    setup_logging()

    try:
        mode = resolve_mode(config)

        if mode == "batch":
            run_batch_mode(config)
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
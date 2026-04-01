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
import sys
import tempfile
import shutil
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
            "engine": "edge",
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
            "gain": 0,
            "normalize": None,
        },
        "output": {
            "format": "m4a",
            "bitrate": "192k",
            "sample_rate": 44100,
        },
        "lrc": {
            "delimiter": "-",
            "split_strategy": "last",
        },
    }

    if config_path and Path(config_path).exists():
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f)
        if user_config:
            # Top-level mode
            if "mode" in user_config:
                defaults["mode"] = user_config["mode"] or ""
            # Deep merge dict sections (except tts.openai which is nested)
            for section in ("paths", "timing", "output", "lrc"):
                if section in user_config and isinstance(user_config[section], dict):
                    defaults[section].update(user_config[section])
            # TTS section — merge top-level keys, then openai sub-dict
            if "tts" in user_config and isinstance(user_config["tts"], dict):
                tts_user = user_config["tts"]
                # Merge openai sub-dict separately
                if "openai" in tts_user and isinstance(tts_user["openai"], dict):
                    defaults["tts"]["openai"].update(tts_user["openai"])
                # Merge remaining tts keys (skip openai to avoid overwriting)
                for k, v in tts_user.items():
                    if k != "openai":
                        defaults["tts"][k] = v

    # Backward compatibility: old "voice" key → native_voice
    tts = defaults["tts"]
    if "voice" in tts and "native_voice" not in tts:
        tts["native_voice"] = tts.pop("voice")
    elif "voice" in tts:
        tts.pop("voice")

    # Ensure engine is valid
    if tts.get("engine") not in ("edge", "openai"):
        tts["engine"] = "edge"

    # Ensure gain is numeric
    if tts.get("gain") is None:
        tts["gain"] = 0
    tts["gain"] = float(tts["gain"])

    # Ensure normalize is float or None
    norm = tts.get("normalize")
    if norm is not None and norm != "":
        tts["normalize"] = float(norm)
    else:
        tts["normalize"] = None

    # Ensure openai.speed is float
    tts["openai"]["speed"] = float(tts["openai"].get("speed", 1.0))

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

    # --- Input sources ---
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

    # --- Mode override ---
    parser.add_argument(
        "--mode", choices=["audio", "text"], default=None,
        help="Force mode (overrides config and auto-detection)",
    )

    # --- Output paths ---
    output_group = parser.add_argument_group("Output paths")
    output_group.add_argument(
        "-o", "--output", default=None,
        help="Output audio file path (single-file mode only)",
    )
    output_group.add_argument(
        "--output-lrc", default=None,
        help="Output LRC file path (single-file mode only)",
    )

    # --- Config ---
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Config file path (default: config.yaml)",
    )

    # --- Timing overrides ---
    timing_group = parser.add_argument_group("Timing (overrides config)")
    timing_group.add_argument(
        "--after-first-target", type=float, default=None,
        help="Silence after first target phrase (seconds)",
    )
    timing_group.add_argument(
        "--after-native", type=float, default=None,
        help="Silence after native phrase (seconds)",
    )
    timing_group.add_argument(
        "--after-second-target", type=float, default=None,
        help="Silence after second target phrase (seconds)",
    )

    # --- TTS overrides ---
    tts_group = parser.add_argument_group("TTS (overrides config)")
    tts_group.add_argument(
        "--engine", choices=["edge", "openai"], default=None,
        help="TTS engine: 'edge' (free) or 'openai' (paid, reads math)",
    )
    tts_group.add_argument(
        "--target-voice", default=None,
        help="Target language TTS voice (text-only mode, edge-tts)",
    )
    tts_group.add_argument(
        "--native-voice", default=None,
        help="Native language TTS voice (edge-tts)",
    )
    tts_group.add_argument(
        "--voice", default=None,
        help="Alias for --native-voice (backward compatible)",
    )
    tts_group.add_argument(
        "--rate", default=None,
        help="TTS speech rate (e.g., +10%%, -20%%) — edge-tts only",
    )
    tts_group.add_argument(
        "--gain", type=float, default=None,
        help="TTS volume gain in dB (e.g., -6 to reduce, +3 to boost)",
    )
    tts_group.add_argument(
        "--normalize", type=float, default=None,
        help="Normalize TTS volume to target dBFS (e.g., -20). Overrides --gain",
    )
    tts_group.add_argument(
        "--openai-voice", default=None,
        help="OpenAI TTS voice (e.g., coral, nova, sage)",
    )
    tts_group.add_argument(
        "--openai-instructions", default=None,
        help="OpenAI TTS instructions prompt",
    )

    # --- LRC overrides ---
    lrc_group = parser.add_argument_group("LRC / text parsing (overrides config)")
    lrc_group.add_argument(
        "--delimiter", default=None,
        help="Delimiter between target and native text",
    )
    lrc_group.add_argument(
        "--split-strategy", choices=["first", "last"], default=None,
        help="Split on first or last delimiter occurrence",
    )

    return parser.parse_args()


def apply_cli_overrides(config: dict, args: argparse.Namespace) -> dict:
    """Apply CLI argument overrides to config. CLI always wins."""
    # Mode
    if args.mode:
        config["mode"] = args.mode

    # Paths — CLI args override config paths
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

    # Timing
    if args.after_first_target is not None:
        config["timing"]["after_first_target"] = args.after_first_target
    if args.after_native is not None:
        config["timing"]["after_native"] = args.after_native
    if args.after_second_target is not None:
        config["timing"]["after_second_target"] = args.after_second_target

    # TTS
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

    # OpenAI TTS overrides
    if args.openai_voice:
        config["tts"]["openai"]["voice"] = args.openai_voice
    if args.openai_instructions:
        config["tts"]["openai"]["instructions"] = args.openai_instructions

    # LRC
    if args.delimiter:
        config["lrc"]["delimiter"] = args.delimiter
    if args.split_strategy:
        config["lrc"]["split_strategy"] = args.split_strategy

    return config


def resolve_mode(config: dict) -> str:
    """
    Determine which mode to run.

    Priority:
      0. If scan path is set → "batch"
      1. Explicit mode flag (CLI --mode or config mode:)
      2. Auto-detect from paths — if both audio+lrc and text exist, audio wins
      3. Error if no paths are set at all
    """
    paths = config["paths"]

    # Batch mode check — scan path takes precedence
    if paths.get("scan"):
        return "batch"

    mode = config.get("mode", "").strip().lower()
    has_audio = bool(paths.get("audio")) and bool(paths.get("lrc"))
    has_text = bool(paths.get("text"))

    if mode in ("audio", "text"):
        if mode == "audio" and not has_audio:
            print("Error: audio mode requires 'audio' and 'lrc' paths", file=sys.stderr)
            sys.exit(1)
        if mode == "text" and not has_text:
            print("Error: text mode requires 'text' path", file=sys.stderr)
            sys.exit(1)
        return mode

    # Auto-detect
    if has_audio:
        return "audio"
    if has_text:
        return "text"

    print(
        "Error: no input specified. Provide audio+lrc, --text, or --scan via CLI or config.yaml",
        file=sys.stderr,
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
    """Build a display label for the current volume settings."""
    norm = config["tts"]["normalize"]
    gain = config["tts"]["gain"]
    if norm is not None:
        return f"normalize to {norm} dBFS"
    elif gain != 0:
        sign = "+" if gain > 0 else ""
        return f"{sign}{gain} dB"
    return "default"


def _engine_label(config: dict) -> str:
    """Build a display label for the current TTS engine."""
    engine = config["tts"]["engine"]
    if engine == "openai":
        oai = config["tts"]["openai"]
        return f"openai ({oai['model']}, voice={oai['voice']})"
    return "edge-tts"


def progress_bar(current: int, total: int) -> None:
    """Print a progress bar."""
    bar_len = 30
    filled = int(bar_len * current / total)
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"\r  [{bar}] {current}/{total}", end="", flush=True)


def _print_segment_summary(segments: list) -> None:
    """Print a short summary of parsed segments."""
    print(f"  Found {len(segments)} segments")
    for seg in segments[:3]:
        print(f"    T: {seg.target_text[:40]}...")
        print(f"    N: {seg.native_text[:40]}...")
    if len(segments) > 3:
        print(f"    ... and {len(segments) - 3} more")


def _tts_volume_kwargs(config: dict) -> dict:
    """Extract volume keyword arguments from config for TTS functions."""
    return {
        "gain_db": config["tts"]["gain"],
        "normalize_target_dbfs": config["tts"]["normalize"],
    }


def _tts_engine_kwargs(config: dict) -> dict:
    """Extract engine keyword arguments from config for TTS functions."""
    return {
        "engine": config["tts"]["engine"],
        "openai_config": config["tts"]["openai"],
    }


def _assemble_and_export(
    segments, target_audios, native_audios,
    timing, config, output_path, lrc_output_path, work_dir,
) -> None:
    """Assemble Echo Loops, export audio and LRC, clean up."""
    print("\n  Assembling Echo Loops...")
    result = assemble_all_loops(target_audios, native_audios, timing, progress_bar)
    print()  # newline after progress bar

    print("\n  Exporting...")
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
    )

    # Cleanup
    shutil.rmtree(work_dir, ignore_errors=True)

    print(f"\n✓ Done! Echo Loop file saved to: {output_path}")


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

    # Banner
    print("=" * 60)
    print("  Echo Loop Generator — Audio Mode")
    print("  T → S → N → S → T → S")
    print("=" * 60)
    print(f"  Audio:      {audio_path}")
    print(f"  LRC:        {lrc_path}")
    print(f"  Output:     {output_path}")
    print(f"  Output LRC: {lrc_output_path}")
    print(f"  Timing:     {timing.after_first_target}s / "
          f"{timing.after_native}s / {timing.after_second_target}s")
    print(f"  TTS Engine: {_engine_label(config)}")
    if config["tts"]["engine"] == "edge":
        print(f"  Native TTS: {config['tts']['native_voice']}")
    print(f"  TTS Volume: {_volume_label(config)}")
    print("=" * 60)

    # Step 1: Load source audio
    print("\n[1/5] Loading source audio...")
    source_audio = load_audio(audio_path)
    audio_duration_ms = len(source_audio)
    print(f"  Loaded: {audio_duration_ms / 1000:.1f}s, "
          f"{source_audio.frame_rate}Hz, {source_audio.channels}ch")

    # Step 2: Parse LRC
    print("\n[2/5] Parsing LRC subtitles...")
    segments = parse_lrc(
        lrc_path,
        delimiter=config["lrc"]["delimiter"],
        split_strategy=config["lrc"]["split_strategy"],
        audio_duration_ms=audio_duration_ms,
    )
    _print_segment_summary(segments)

    # Step 3: Extract target audio segments
    print("\n[3/5] Extracting target audio segments...")
    target_audios = extract_all_segments(source_audio, segments)
    print(f"  Extracted {len(target_audios)} segments")

    # Step 4: Generate native TTS
    print("\n[4/5] Generating native TTS audio...")
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
    print(f"  Generated {len(native_audios)} TTS audio clips")

    # Step 5: Assemble and export
    print("\n[5/5] Assembling and exporting...")
    _assemble_and_export(
        segments, target_audios, native_audios,
        timing, config, output_path, lrc_output_path, work_dir,
    )


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

    # Banner
    print("=" * 60)
    print("  Echo Loop Generator — Text-Only Mode")
    print("  T → S → N → S → T → S")
    print("=" * 60)
    print(f"  Text:       {text_path}")
    print(f"  Output:     {output_path}")
    print(f"  Output LRC: {lrc_output_path}")
    print(f"  Timing:     {timing.after_first_target}s / "
          f"{timing.after_native}s / {timing.after_second_target}s")
    print(f"  TTS Engine: {_engine_label(config)}")
    if config["tts"]["engine"] == "edge":
        print(f"  Target TTS: {config['tts']['target_voice']}")
        print(f"  Native TTS: {config['tts']['native_voice']}")
    print(f"  TTS Volume: {_volume_label(config)}")
    print("=" * 60)

    # Step 1: Parse text file
    print("\n[1/4] Parsing text file...")
    segments = parse_text(
        text_path,
        delimiter=config["lrc"]["delimiter"],
        split_strategy=config["lrc"]["split_strategy"],
    )
    _print_segment_summary(segments)

    work_dir = Path(tempfile.mkdtemp(prefix="echo_loop_"))

    # Step 2: Generate target TTS
    print("\n[2/4] Generating target TTS audio...")
    target_audios = generate_target_audio(
        segments,
        voice=config["tts"]["target_voice"],
        rate=config["tts"]["rate"],
        pitch=config["tts"]["pitch"],
        work_dir=work_dir,
        **vol_kwargs,
        **eng_kwargs,
    )
    print(f"  Generated {len(target_audios)} target TTS clips")

    # Step 3: Generate native TTS
    print("\n[3/4] Generating native TTS audio...")
    native_audios = generate_native_audio(
        segments,
        voice=config["tts"]["native_voice"],
        rate=config["tts"]["rate"],
        pitch=config["tts"]["pitch"],
        work_dir=work_dir,
        **vol_kwargs,
        **eng_kwargs,
    )
    print(f"  Generated {len(native_audios)} native TTS clips")

    # Step 4: Assemble and export
    print("\n[4/4] Assembling and exporting...")
    _assemble_and_export(
        segments, target_audios, native_audios,
        timing, config, output_path, lrc_output_path, work_dir,
    )


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

    # Banner
    print("=" * 60)
    print("  Echo Loop Generator — Batch Mode")
    print("  T → S → N → S → T → S")
    print("=" * 60)
    print(f"  Scan folder: {scan_path}")
    if mode_filter:
        print(f"  Filter:      {mode_filter} only")
    else:
        print(f"  Filter:      auto (audio pairs + text files)")
    print(f"  Timing:      {timing.after_first_target}s / "
          f"{timing.after_native}s / {timing.after_second_target}s")
    print(f"  TTS Engine:  {_engine_label(config)}")
    if config["tts"]["engine"] == "edge":
        print(f"  Native TTS:  {config['tts']['native_voice']}")
        if mode_filter in ("", "text"):
            print(f"  Target TTS:  {config['tts']['target_voice']}")
    print(f"  TTS Volume:  {_volume_label(config)}")
    print("=" * 60)

    # Scan
    print("\n  Scanning folder...")
    items = scan_folder(scan_path, mode=mode_filter)
    print_scan_summary(items)

    if not items:
        print("\n  Nothing to process.")
        return

    # Process each item
    succeeded: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []
    total = len(items)

    for idx, item in enumerate(items, 1):
        label = item.label
        print(f"\n{'─' * 60}")
        print(f"  [{idx}/{total}] {label}")
        print(f"{'─' * 60}")

        # Skip if echo output already exists
        output_path, lrc_output_path = resolve_output_paths_for_item(item, config)
        if output_path.exists() and lrc_output_path.exists():
            print(f"  ⏭  Skipped (already exists): {output_path.name}, {lrc_output_path.name}")
            skipped.append(label)
            continue

        try:
            item_config = _config_for_item(config, item)

            if item.mode == "audio":
                _run_single_audio(item_config, item, timing, output_path, lrc_output_path)
            else:
                _run_single_text(item_config, item, timing, output_path, lrc_output_path)

            succeeded.append(label)

        except Exception as e:
            failed.append((label, str(e)))
            print(f"\n  ✗ Failed: {e}")

    # Summary
    print(f"\n{'=' * 60}")
    print(f"  Batch Complete")
    print(f"{'=' * 60}")
    print(f"  ✓ Succeeded: {len(succeeded)}")
    if skipped:
        print(f"  ⏭  Skipped:   {len(skipped)}")
    if failed:
        print(f"  ✗ Failed:    {len(failed)}")
        for name, error in failed:
            short_err = error if len(error) <= 80 else error[:77] + "..."
            print(f"    - {name}: {short_err}")
    print(f"{'=' * 60}")


def _config_for_item(config: dict, item: ScanItem) -> dict:
    """Create a config copy with paths set for a specific batch item."""
    import copy
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

    print(f"  Audio: {audio_path.name}")
    print(f"  LRC:   {lrc_path.name}")
    print(f"  →      {output_path.name}")

    source_audio = load_audio(audio_path)
    audio_duration_ms = len(source_audio)
    print(f"  Loaded: {audio_duration_ms / 1000:.1f}s")

    segments = parse_lrc(
        lrc_path,
        delimiter=config["lrc"]["delimiter"],
        split_strategy=config["lrc"]["split_strategy"],
        audio_duration_ms=audio_duration_ms,
    )
    print(f"  Segments: {len(segments)}")

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

    print(f"  Text: {text_path.name}")
    print(f"  →     {output_path.name}")

    segments = parse_text(
        text_path,
        delimiter=config["lrc"]["delimiter"],
        split_strategy=config["lrc"]["split_strategy"],
    )
    print(f"  Segments: {len(segments)}")

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
    load_dotenv()  # load .env file (OPENAI_API_KEY, etc.)

    args = parse_args()
    config = load_config(args.config)
    config = apply_cli_overrides(config, args)

    mode = resolve_mode(config)

    if mode == "batch":
        run_batch_mode(config)
    elif mode == "text":
        run_text_mode(config)
    else:
        run_audio_mode(config)


if __name__ == "__main__":
    main()
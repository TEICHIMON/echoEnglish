#!/usr/bin/env python3
"""
Echo Loop Generator

Generates T-S-N-S-T-S (Target → Silence → Native → Silence → Target → Silence)
audio files for language learning, based on "Echo: Rebuilding the Natural Reflex
of Language" by H. Reeve.

Usage:
    python main.py <audio_file> <lrc_file> [options]

Examples:
    python main.py lesson01.mp3 lesson01.lrc
    python main.py lesson01.mp3 lesson01.lrc -o output.m4a
    python main.py lesson01.mp3 lesson01.lrc --after-first-target 1.0 --after-native 0.6
    python main.py lesson01.mp3 lesson01.lrc -c custom_config.yaml
"""

import argparse
import sys
import tempfile
from pathlib import Path

import yaml

from parser.lrc_parser import parse_lrc
from audio.splitter import load_audio, extract_all_segments
from audio.tts_generator import generate_native_audio
from audio.assembler import assemble_all_loops, EchoTiming
from export.exporter import export_audio
from export.lrc_writer import generate_echo_lrc


def load_config(config_path: str | Path | None = None) -> dict:
    """Load configuration from YAML file, falling back to defaults."""
    defaults = {
        "timing": {
            "after_first_target": 0.8,
            "after_native": 0.5,
            "after_second_target": 1.2,
        },
        "tts": {
            "voice": "zh-CN-XiaoxiaoNeural",
            "rate": "+0%",
            "pitch": "+0Hz",
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
            # Deep merge user config into defaults
            for section in defaults:
                if section in user_config:
                    defaults[section].update(user_config[section])

    return defaults


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Echo Loop Generator - T•N•T language learning audio",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s lesson01.mp3 lesson01.lrc
  %(prog)s lesson01.mp3 lesson01.lrc -o echo_lesson01.m4a
  %(prog)s lesson01.mp3 lesson01.lrc --after-first-target 1.0 --voice zh-CN-YunxiNeural
        """,
    )

    parser.add_argument("audio", help="Source audio file (mp3, wav, m4a, etc.)")
    parser.add_argument("lrc", help="LRC subtitle file with bilingual content")

    parser.add_argument(
        "-o", "--output",
        help="Output file path (default: <audio_name>_echo.m4a)",
    )
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Config file path (default: config.yaml)",
    )

    # Timing overrides
    timing_group = parser.add_argument_group("Timing (overrides config)")
    timing_group.add_argument(
        "--after-first-target", type=float,
        help="Silence after first target phrase (seconds)",
    )
    timing_group.add_argument(
        "--after-native", type=float,
        help="Silence after native phrase (seconds)",
    )
    timing_group.add_argument(
        "--after-second-target", type=float,
        help="Silence after second target phrase (seconds)",
    )

    # TTS overrides
    tts_group = parser.add_argument_group("TTS (overrides config)")
    tts_group.add_argument(
        "--voice",
        help="edge-tts voice name (e.g., zh-CN-XiaoxiaoNeural)",
    )
    tts_group.add_argument(
        "--rate",
        help="TTS speech rate (e.g., +10%%, -20%%)",
    )

    # LRC overrides
    lrc_group = parser.add_argument_group("LRC parsing (overrides config)")
    lrc_group.add_argument(
        "--delimiter",
        help="Delimiter between target and native text",
    )
    lrc_group.add_argument(
        "--split-strategy",
        choices=["first", "last"],
        help="Split on first or last delimiter occurrence",
    )

    return parser.parse_args()


def apply_cli_overrides(config: dict, args: argparse.Namespace) -> dict:
    """Apply CLI argument overrides to config."""
    if args.after_first_target is not None:
        config["timing"]["after_first_target"] = args.after_first_target
    if args.after_native is not None:
        config["timing"]["after_native"] = args.after_native
    if args.after_second_target is not None:
        config["timing"]["after_second_target"] = args.after_second_target
    if args.voice:
        config["tts"]["voice"] = args.voice
    if args.rate:
        config["tts"]["rate"] = args.rate
    if args.delimiter:
        config["lrc"]["delimiter"] = args.delimiter
    if args.split_strategy:
        config["lrc"]["split_strategy"] = args.split_strategy
    return config


def main():
    args = parse_args()
    config = load_config(args.config)
    config = apply_cli_overrides(config, args)

    audio_path = Path(args.audio)
    lrc_path = Path(args.lrc)

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        ext = config["output"]["format"]
        output_path = audio_path.parent / f"{audio_path.stem}_echo.{ext}"

    # Print banner
    print("=" * 60)
    print("  Echo Loop Generator")
    print("  T → S → N → S → T → S")
    print("=" * 60)
    print(f"  Audio:  {audio_path}")
    print(f"  LRC:    {lrc_path}")
    print(f"  Output: {output_path}")
    print(f"  Timing: {config['timing']['after_first_target']}s / "
          f"{config['timing']['after_native']}s / "
          f"{config['timing']['after_second_target']}s")
    print(f"  Voice:  {config['tts']['voice']}")
    print("=" * 60)

    # Step 1: Load source audio
    print("\n[1/5] Loading source audio...")
    source_audio = load_audio(audio_path)
    audio_duration_ms = len(source_audio)
    print(f"  Loaded: {audio_duration_ms / 1000:.1f}s, "
          f"{source_audio.frame_rate}Hz, "
          f"{source_audio.channels}ch")

    # Step 2: Parse LRC
    print("\n[2/5] Parsing LRC subtitles...")
    segments = parse_lrc(
        lrc_path,
        delimiter=config["lrc"]["delimiter"],
        split_strategy=config["lrc"]["split_strategy"],
        audio_duration_ms=audio_duration_ms,
    )
    print(f"  Found {len(segments)} segments")
    for seg in segments[:3]:
        print(f"    [{seg._fmt_time(seg.start_ms)}→{seg._fmt_time(seg.end_ms)}] "
              f"T: {seg.target_text[:30]}...")
    if len(segments) > 3:
        print(f"    ... and {len(segments) - 3} more")

    # Step 3: Extract target audio segments
    print("\n[3/5] Extracting target audio segments...")
    target_audios = extract_all_segments(source_audio, segments)
    print(f"  Extracted {len(target_audios)} segments")

    # Step 4: Generate native TTS
    print("\n[4/5] Generating Chinese TTS audio...")
    work_dir = Path(tempfile.mkdtemp(prefix="echo_loop_"))
    native_audios = generate_native_audio(
        segments,
        voice=config["tts"]["voice"],
        rate=config["tts"]["rate"],
        pitch=config["tts"]["pitch"],
        work_dir=work_dir,
    )
    print(f"  Generated {len(native_audios)} TTS audio clips")

    # Step 5: Assemble Echo Loops and export
    print("\n[5/5] Assembling Echo Loops...")
    timing = EchoTiming(
        after_first_target=config["timing"]["after_first_target"],
        after_native=config["timing"]["after_native"],
        after_second_target=config["timing"]["after_second_target"],
    )

    def progress(current, total):
        bar_len = 30
        filled = int(bar_len * current / total)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\r  [{bar}] {current}/{total}", end="", flush=True)

    result = assemble_all_loops(target_audios, native_audios, timing, progress)
    print()  # newline after progress bar

    print("\n  Exporting...")
    export_audio(
        result,
        output_path,
        format=config["output"]["format"],
        bitrate=config["output"]["bitrate"],
        sample_rate=config["output"]["sample_rate"],
    )

    lrc_output_path = output_path.with_suffix(".lrc")
    generate_echo_lrc(
        segments, target_audios, native_audios, timing,
        lrc_output_path, delimiter=config["lrc"]["delimiter"],
    )

    # Cleanup
    import shutil
    shutil.rmtree(work_dir, ignore_errors=True)

    print(f"\n✓ Done! Echo Loop file saved to: {output_path}")


if __name__ == "__main__":
    main()

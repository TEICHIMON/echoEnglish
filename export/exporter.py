"""
Audio exporter module.

Exports the assembled Echo Loop audio to the desired output format (m4a by default).
Uses pydub with ffmpeg backend for format conversion.
"""

import subprocess
import tempfile
from pathlib import Path

from pydub import AudioSegment


def export_audio(
    audio: AudioSegment,
    output_path: str | Path,
    format: str = "m4a",
    bitrate: str = "192k",
    sample_rate: int = 44100,
) -> Path:
    """
    Export an AudioSegment to the specified format.
    
    Args:
        audio: The AudioSegment to export
        output_path: Output file path
        format: Output format (m4a, mp3, wav, etc.)
        bitrate: Audio bitrate
        sample_rate: Sample rate in Hz
        
    Returns:
        Path to the exported file
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Normalize audio to consistent sample rate
    audio = audio.set_frame_rate(sample_rate)

    if format == "m4a":
        # m4a requires special handling: export wav first, then convert via ffmpeg
        # ffmpeg uses "ipod" muxer for m4a format
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_wav = tmp.name
        audio.export(tmp_wav, format="wav")

        cmd = [
            "ffmpeg", "-y",
            "-i", tmp_wav,
            "-acodec", "aac",
            "-b:a", bitrate,
            "-ar", str(sample_rate),
            "-f", "ipod",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        Path(tmp_wav).unlink(missing_ok=True)

        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg m4a export failed:\n{result.stderr}")
    else:
        # For other formats, pydub handles it directly
        export_params = {
            "format": format,
            "bitrate": bitrate,
        }
        audio.export(str(output_path), **export_params)

    file_size = output_path.stat().st_size
    duration_sec = len(audio) / 1000.0

    print(f"  Exported: {output_path}")
    print(f"  Duration: {_format_duration(duration_sec)}")
    print(f"  Size: {_format_size(file_size)}")

    return output_path


def _format_duration(seconds: float) -> str:
    """Format seconds into mm:ss."""
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def _format_size(bytes: int) -> str:
    """Format bytes into human-readable size."""
    if bytes < 1024:
        return f"{bytes} B"
    elif bytes < 1024 * 1024:
        return f"{bytes / 1024:.1f} KB"
    else:
        return f"{bytes / (1024 * 1024):.1f} MB"

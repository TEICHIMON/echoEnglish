"""
Audio splitter module.

Extracts audio segments from a source audio file based on LRC timestamp data.
Uses pydub for audio manipulation.
"""

from pathlib import Path

from pydub import AudioSegment

from parser.lrc_parser import Segment


def load_audio(audio_path: str | Path) -> AudioSegment:
    """
    Load an audio file into a pydub AudioSegment.
    Supports mp3, wav, m4a, ogg, flac, etc.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    suffix = audio_path.suffix.lower().lstrip(".")
    # Map common extensions to pydub format names
    format_map = {
        "mp3": "mp3",
        "wav": "wav",
        "m4a": "m4a",
        "aac": "aac",
        "ogg": "ogg",
        "flac": "flac",
        "wma": "wma",
    }
    fmt = format_map.get(suffix, suffix)
    return AudioSegment.from_file(str(audio_path), format=fmt)


def extract_segment(audio: AudioSegment, segment: Segment) -> AudioSegment:
    """
    Extract a single audio segment based on its start and end timestamps.
    
    Args:
        audio: The full source AudioSegment
        segment: A Segment object with start_ms and end_ms
        
    Returns:
        The extracted AudioSegment
    """
    # Clamp to audio boundaries
    start = max(0, segment.start_ms)
    end = min(len(audio), segment.end_ms)
    return audio[start:end]


def extract_all_segments(
    audio: AudioSegment,
    segments: list[Segment],
) -> list[AudioSegment]:
    """
    Extract all audio segments from the source audio.
    
    Args:
        audio: The full source AudioSegment
        segments: List of Segment objects
        
    Returns:
        List of extracted AudioSegments in the same order
    """
    return [extract_segment(audio, seg) for seg in segments]


def get_audio_duration_ms(audio_path: str | Path) -> int:
    """Get the duration of an audio file in milliseconds without loading fully."""
    audio = load_audio(audio_path)
    return len(audio)

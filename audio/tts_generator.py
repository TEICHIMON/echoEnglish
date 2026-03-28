"""
TTS generator module.

Generates Chinese (native language) audio from text using edge-tts.
Supports async batch generation for efficiency.
"""

import asyncio
import tempfile
from pathlib import Path

import edge_tts
from pydub import AudioSegment

from parser.lrc_parser import Segment


async def _generate_single_tts(
    text: str,
    output_path: str,
    voice: str = "zh-CN-XiaoxiaoNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
) -> None:
    """Generate a single TTS audio file."""
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate,
        pitch=pitch,
    )
    await communicate.save(output_path)


async def _generate_batch_tts(
    texts: list[str],
    output_dir: Path,
    voice: str = "zh-CN-XiaoxiaoNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
) -> list[Path]:
    """
    Generate TTS audio for multiple texts concurrently.
    
    Returns list of output file paths in same order as input texts.
    """
    output_paths = []
    tasks = []

    for i, text in enumerate(texts):
        out_path = output_dir / f"tts_{i:04d}.mp3"
        output_paths.append(out_path)
        tasks.append(_generate_single_tts(text, str(out_path), voice, rate, pitch))

    # Run with limited concurrency to avoid overwhelming the service
    semaphore = asyncio.Semaphore(5)

    async def _limited_task(coro):
        async with semaphore:
            try:
                return await coro
            except Exception as e:
                print(f"  Warning: TTS generation failed: {e}")
                return None

    await asyncio.gather(*[_limited_task(t) for t in tasks])
    return output_paths


def generate_native_audio(
    segments: list[Segment],
    voice: str = "zh-CN-XiaoxiaoNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
    work_dir: Path | None = None,
) -> list[AudioSegment]:
    """
    Generate Chinese TTS audio for all segments.
    
    Args:
        segments: List of Segment objects with native_text
        voice: edge-tts voice name
        rate: Speech rate adjustment
        pitch: Pitch adjustment
        work_dir: Directory for temp files (auto-created if None)
        
    Returns:
        List of AudioSegment objects for the native language audio
    """
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="echo_tts_"))
    else:
        work_dir.mkdir(parents=True, exist_ok=True)

    texts = [seg.native_text for seg in segments]

    # Run async TTS generation
    output_paths = asyncio.run(
        _generate_batch_tts(texts, work_dir, voice, rate, pitch)
    )

    # Load generated audio files into AudioSegment objects
    native_audios = []
    for path in output_paths:
        if path.exists() and path.stat().st_size > 0:
            audio = AudioSegment.from_file(str(path), format="mp3")
            native_audios.append(audio)
        else:
            # Fallback: create a short silence if TTS failed
            print(f"  Warning: TTS failed for '{texts[output_paths.index(path)][:30]}...', using silence")
            native_audios.append(AudioSegment.silent(duration=500))

    return native_audios

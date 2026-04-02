"""
TTS generator module.

Generates audio from text using edge-tts or OpenAI gpt-4o-mini-tts.
Supports both native language and target language TTS generation.
Engine is selected via config: "edge" (free) or "openai" (paid, supports math).
"""

import asyncio
import os
import tempfile
from pathlib import Path

import edge_tts
from pydub import AudioSegment

from parser.lrc_parser import Segment


# ---------------------------------------------------------------------------
# Retry / concurrency settings for edge-tts
# ---------------------------------------------------------------------------

EDGE_CONCURRENCY = 3        # max parallel requests (lower = less 503s)
EDGE_MAX_RETRIES = 3        # retry attempts per TTS call
EDGE_RETRY_BASE_DELAY = 2.0 # seconds; doubles each retry (exponential backoff)


# ---------------------------------------------------------------------------
# Volume adjustment (shared by both engines)
# ---------------------------------------------------------------------------

def _adjust_volume(
    audio: AudioSegment,
    gain_db: float = 0.0,
    normalize_target_dbfs: float | None = None,
) -> AudioSegment:
    """
    Adjust the volume of an AudioSegment.

    Two modes (normalize takes priority if both are set):
      1. normalize_target_dbfs: normalize the clip so its average loudness
         (dBFS) matches the target value.
      2. gain_db: apply a fixed gain in dB.
    """
    if normalize_target_dbfs is not None:
        current_dbfs = audio.dBFS
        if current_dbfs > -60.0:
            delta = normalize_target_dbfs - current_dbfs
            audio = audio.apply_gain(delta)
    elif gain_db != 0.0:
        audio = audio.apply_gain(gain_db)
    return audio


# ---------------------------------------------------------------------------
# Edge-TTS engine
# ---------------------------------------------------------------------------

async def _edge_generate_single(
    text: str,
    output_path: str,
    voice: str = "zh-CN-XiaoxiaoNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
) -> None:
    """Generate a single TTS audio file via edge-tts."""
    communicate = edge_tts.Communicate(
        text=text, voice=voice, rate=rate, pitch=pitch,
    )
    await communicate.save(output_path)


async def _edge_generate_batch(
    texts: list[str],
    output_dir: Path,
    voice: str,
    rate: str,
    pitch: str,
    prefix: str,
) -> list[Path]:
    """Generate TTS audio for multiple texts via edge-tts with retry on 503."""
    output_paths: list[Path] = []
    for i in range(len(texts)):
        output_paths.append(output_dir / f"{prefix}_{i:04d}.mp3")

    semaphore = asyncio.Semaphore(EDGE_CONCURRENCY)

    async def _generate_with_retry(text: str, out_path: str):
        """Run a single TTS call with concurrency limit and exponential-backoff retry."""
        async with semaphore:
            for attempt in range(1, EDGE_MAX_RETRIES + 1):
                try:
                    await _edge_generate_single(text, out_path, voice, rate, pitch)
                    return
                except Exception as e:
                    err_str = str(e)
                    is_transient = ("503" in err_str or "Invalid response status" in err_str)
                    if is_transient and attempt < EDGE_MAX_RETRIES:
                        delay = EDGE_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                        short_text = text[:30].replace("\n", " ")
                        print(f"  ⟳ Retry {attempt}/{EDGE_MAX_RETRIES} in {delay:.0f}s "
                              f"(503) for '{short_text}...'")
                        await asyncio.sleep(delay)
                    else:
                        print(f"  Warning: edge-tts generation failed: {e}")
                        return

    tasks = [
        _generate_with_retry(text, str(out_path))
        for text, out_path in zip(texts, output_paths)
    ]
    await asyncio.gather(*tasks)
    return output_paths


def _run_edge_batch(
    texts: list[str],
    work_dir: Path,
    voice: str,
    rate: str,
    pitch: str,
    prefix: str,
    gain_db: float,
    normalize_target_dbfs: float | None,
) -> list[AudioSegment]:
    """Run edge-tts batch and load results into AudioSegments."""
    output_paths = asyncio.run(
        _edge_generate_batch(texts, work_dir, voice, rate, pitch, prefix)
    )
    return _load_audio_files(output_paths, texts, gain_db, normalize_target_dbfs)


# ---------------------------------------------------------------------------
# OpenAI TTS engine
# ---------------------------------------------------------------------------

def _get_openai_client():
    """Lazily import and create an OpenAI client."""
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError(
            "openai package is required for engine='openai'. "
            "Install it with: pip install openai"
        )

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY environment variable is not set. "
            "Set it before using engine='openai'."
        )
    return OpenAI(api_key=api_key)


def _openai_generate_single(
    client,
    text: str,
    output_path: Path,
    model: str,
    voice: str,
    instructions: str,
    speed: float,
) -> None:
    """Generate a single TTS audio file via OpenAI API."""
    kwargs = {
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": "mp3",
    }
    if instructions:
        kwargs["instructions"] = instructions
    if speed != 1.0:
        kwargs["speed"] = speed

    with client.audio.speech.with_streaming_response.create(**kwargs) as response:
        response.stream_to_file(str(output_path))


def _run_openai_batch(
    texts: list[str],
    work_dir: Path,
    prefix: str,
    openai_config: dict,
    gain_db: float,
    normalize_target_dbfs: float | None,
) -> list[AudioSegment]:
    """Generate TTS via OpenAI for a batch of texts (sequential to respect rate limits)."""
    client = _get_openai_client()
    model = openai_config.get("model", "gpt-4o-mini-tts")
    voice = openai_config.get("voice", "coral")
    instructions = openai_config.get("instructions", "")
    speed = float(openai_config.get("speed", 1.0))

    output_paths: list[Path] = []
    for i, text in enumerate(texts):
        out_path = work_dir / f"{prefix}_{i:04d}.mp3"
        output_paths.append(out_path)
        try:
            _openai_generate_single(
                client, text, out_path, model, voice, instructions, speed,
            )
        except Exception as e:
            print(f"  Warning: OpenAI TTS failed for '{text[:30]}...': {e}")

    return _load_audio_files(output_paths, texts, gain_db, normalize_target_dbfs)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _load_audio_files(
    paths: list[Path],
    texts: list[str],
    gain_db: float,
    normalize_target_dbfs: float | None,
) -> list[AudioSegment]:
    """Load generated mp3 files into AudioSegments with volume adjustment."""
    audios = []
    for i, path in enumerate(paths):
        if path.exists() and path.stat().st_size > 0:
            audio = AudioSegment.from_file(str(path), format="mp3")
            audio = _adjust_volume(audio, gain_db, normalize_target_dbfs)
            audios.append(audio)
        else:
            print(f"  Warning: TTS failed for '{texts[i][:30]}...', using silence")
            audios.append(AudioSegment.silent(duration=500))
    return audios


# ---------------------------------------------------------------------------
# Public API (engine-agnostic)
# ---------------------------------------------------------------------------

def generate_native_audio(
    segments: list[Segment],
    voice: str = "zh-CN-XiaoxiaoNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
    work_dir: Path | None = None,
    gain_db: float = 0.0,
    normalize_target_dbfs: float | None = None,
    engine: str = "edge",
    openai_config: dict | None = None,
) -> list[AudioSegment]:
    """
    Generate native language TTS audio for all segments.

    Args:
        segments: Segment objects with native_text
        voice: edge-tts voice name (ignored when engine="openai")
        rate: Speech rate adjustment (edge-tts only)
        pitch: Pitch adjustment (edge-tts only)
        work_dir: Directory for temp files
        gain_db: Fixed dB gain to apply
        normalize_target_dbfs: Normalize to this dBFS level
        engine: "edge" or "openai"
        openai_config: OpenAI engine settings dict

    Returns:
        List of AudioSegment objects
    """
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="echo_tts_"))
    else:
        work_dir.mkdir(parents=True, exist_ok=True)

    texts = [seg.native_text for seg in segments]

    if engine == "openai":
        return _run_openai_batch(
            texts, work_dir, "native",
            openai_config or {},
            gain_db, normalize_target_dbfs,
        )
    else:
        return _run_edge_batch(
            texts, work_dir, voice, rate, pitch, "native",
            gain_db, normalize_target_dbfs,
        )


def generate_target_audio(
    segments: list[Segment],
    voice: str = "ja-JP-NanamiNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
    work_dir: Path | None = None,
    gain_db: float = 0.0,
    normalize_target_dbfs: float | None = None,
    engine: str = "edge",
    openai_config: dict | None = None,
) -> list[AudioSegment]:
    """
    Generate target language TTS audio for all segments.

    Used in text-only mode where no source audio file is provided.

    Args:
        segments: Segment objects with target_text
        voice: edge-tts voice name (ignored when engine="openai")
        rate: Speech rate adjustment (edge-tts only)
        pitch: Pitch adjustment (edge-tts only)
        work_dir: Directory for temp files
        gain_db: Fixed dB gain to apply
        normalize_target_dbfs: Normalize to this dBFS level
        engine: "edge" or "openai"
        openai_config: OpenAI engine settings dict

    Returns:
        List of AudioSegment objects
    """
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="echo_tts_"))
    else:
        work_dir.mkdir(parents=True, exist_ok=True)

    texts = [seg.target_text for seg in segments]

    if engine == "openai":
        return _run_openai_batch(
            texts, work_dir, "target",
            openai_config or {},
            gain_db, normalize_target_dbfs,
        )
    else:
        return _run_edge_batch(
            texts, work_dir, voice, rate, pitch, "target",
            gain_db, normalize_target_dbfs,
        )
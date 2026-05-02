"""
TTS generator module.

Generates audio from text using edge-tts, OpenAI gpt-4o-mini-tts, or
Google Cloud Text-to-Speech.
Supports both native language and target language TTS generation.
Engine is selected via config: "edge", "openai", or "google".
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

import edge_tts
from pydub import AudioSegment

from parser.lrc_parser import Segment


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Retry / concurrency settings for edge-tts
# ---------------------------------------------------------------------------

EDGE_CONCURRENCY = 3        # max parallel requests (lower = less 503s)
EDGE_MAX_RETRIES = 4        # retry attempts per TTS call
EDGE_RETRY_BASE_DELAY = 2.0 # seconds; doubles each retry (exponential backoff)


# ---------------------------------------------------------------------------
# Retry / concurrency settings for Google Cloud TTS
# ---------------------------------------------------------------------------

GOOGLE_CONCURRENCY = 3
GOOGLE_MAX_RETRIES = 4
GOOGLE_RETRY_BASE_DELAY = 2.0


# Substrings in the exception message that indicate a transient failure
# worth retrying. Match any of these and we back off and try again.
TRANSIENT_ERROR_INDICATORS: tuple[tuple[str, str], ...] = (
    # HTTP-level transients
    ("503", "503"),
    ("Invalid response status", "bad-status"),
    # Connection / DNS errors (typical aiohttp / OSError messages)
    ("Cannot connect to host", "connect"),
    ("nodename nor servname", "dns"),                  # macOS / BSD
    ("Name or service not known", "dns"),              # Linux
    ("Temporary failure in name resolution", "dns"),   # Linux
    ("Connection reset", "reset"),
    ("Connection refused", "refused"),
    ("Connection aborted", "aborted"),
    ("ServerDisconnected", "disconnected"),
    ("TimeoutError", "timeout"),
    ("ClientConnectorError", "connect"),
    ("ConnectionError", "connect"),
)


def _classify_transient(err_str: str) -> str | None:
    """
    If the error string matches a known transient pattern, return a short
    label describing the kind of failure ('dns', 'connect', '503', ...).
    Returns None if the error is not retryable.
    """
    for needle, label in TRANSIENT_ERROR_INDICATORS:
        if needle in err_str:
            return label
    return None


# ---------------------------------------------------------------------------
# Volume adjustment (shared by both engines)
# ---------------------------------------------------------------------------

def _adjust_volume(
    audio: AudioSegment,
    gain_db: float = 0.0,
    normalize_target_dbfs: float | None = None,
) -> AudioSegment:
    """Adjust the volume of an AudioSegment."""
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
    """Generate TTS audio for multiple texts via edge-tts with retry on transient errors."""
    output_paths: list[Path] = []
    for i in range(len(texts)):
        output_paths.append(output_dir / f"{prefix}_{i:04d}.mp3")

    semaphore = asyncio.Semaphore(EDGE_CONCURRENCY)

    async def _generate_with_retry(text: str, out_path: str):
        """Run a single TTS call with concurrency limit + exponential-backoff retry."""
        async with semaphore:
            for attempt in range(1, EDGE_MAX_RETRIES + 1):
                try:
                    await _edge_generate_single(text, out_path, voice, rate, pitch)
                    return
                except Exception as e:
                    err_str = str(e)
                    err_label = _classify_transient(err_str)

                    if err_label and attempt < EDGE_MAX_RETRIES:
                        delay = EDGE_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                        short_text = text[:30].replace("\n", " ")
                        logger.warning(
                            f"⟳ Retry {attempt}/{EDGE_MAX_RETRIES} in {delay:.0f}s "
                            f"({err_label}) for '{short_text}...'"
                        )
                        await asyncio.sleep(delay)
                    else:
                        # Either non-retryable, or we've exhausted retries
                        kind = err_label or "non-retryable"
                        logger.warning(
                            f"edge-tts generation failed [{kind}, "
                            f"attempt {attempt}/{EDGE_MAX_RETRIES}]: {e}"
                        )
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
            logger.warning(f"OpenAI TTS failed for '{text[:30]}...': {e}")

    return _load_audio_files(output_paths, texts, gain_db, normalize_target_dbfs)


# ---------------------------------------------------------------------------
# Google Cloud TTS engine
# ---------------------------------------------------------------------------

def _get_google_client():
    """Lazily import and create a Google Cloud TTS client."""
    try:
        from google.cloud import texttospeech  # noqa: F401
    except ImportError:
        raise ImportError(
            "google-cloud-texttospeech is required for engine='google'. "
            "Install it with: pip install google-cloud-texttospeech"
        )

    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not cred_path:
        raise RuntimeError(
            "GOOGLE_APPLICATION_CREDENTIALS environment variable is not set. "
            "Set it to the path of your service account JSON file "
            "(e.g., in .env: GOOGLE_APPLICATION_CREDENTIALS=./google-credentials.json)"
        )
    if not Path(cred_path).exists():
        raise RuntimeError(f"Google credentials file not found: {cred_path}")

    from google.cloud import texttospeech
    return texttospeech.TextToSpeechClient()


def _language_code_from_voice(voice_name: str) -> str:
    """Extract BCP-47 language code from a Google voice name.

    Examples:
        'cmn-CN-Chirp3-HD-Kore' -> 'cmn-CN'
        'ja-JP-Neural2-B'       -> 'ja-JP'
    """
    parts = voice_name.split("-")
    if len(parts) < 2:
        raise ValueError(f"Invalid Google voice name: {voice_name!r}")
    return f"{parts[0]}-{parts[1]}"


def _google_generate_single(
    client,
    text: str,
    output_path: Path,
    voice_name: str,
    speaking_rate: float,
    pitch: float,
) -> None:
    """Generate a single TTS audio file via Google Cloud TTS."""
    from google.cloud import texttospeech

    language_code = _language_code_from_voice(voice_name)

    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code=language_code,
        name=voice_name,
    )

    audio_kwargs = {"audio_encoding": texttospeech.AudioEncoding.MP3}
    if speaking_rate != 1.0:
        audio_kwargs["speaking_rate"] = speaking_rate
    # Chirp3-HD voices reject the pitch parameter
    if pitch != 0.0 and "Chirp3" not in voice_name:
        audio_kwargs["pitch"] = pitch

    audio_config = texttospeech.AudioConfig(**audio_kwargs)

    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config,
    )

    with open(output_path, "wb") as f:
        f.write(response.audio_content)


# Google's per-request "sentence too long" rejection. Chinese clauses joined by
# 中文逗号 are treated as one sentence by Google, so a long passage like
# "地球虽然有六大洲，但当时像是一块大陆，海洋比例仅约三分之一，..." trips this
# 400 even though it has plenty of natural break points.
_GOOGLE_TOO_LONG_INDICATORS: tuple[str, ...] = (
    "sentences that are too long",
    "is too long",
)

# Strong (sentence-ending) and weak (clause-pause) punctuation in both Chinese
# and Western forms. Keeping the punctuation attached preserves prosody.
_TTS_STRONG_PUNCT = r"[。！？!?\.;；\n]+"
_TTS_WEAK_PUNCT = r"[，、,]+"

# Compiled probe for "does this string contain a sentence-ending mark?" — used
# during bundling to decide whether a candidate bundle is one long sentence
# (Google's rejection unit) or several short ones.
_TTS_STRONG_PROBE = re.compile(r"[。！？!?\.;；\n]")


def _is_too_long_error(err_str: str) -> bool:
    return any(s in err_str for s in _GOOGLE_TOO_LONG_INDICATORS)


def _has_strong_punct(s: str) -> bool:
    """True if s contains at least one sentence-ending punctuation mark."""
    return bool(_TTS_STRONG_PROBE.search(s))


def _split_on_punct(text: str, pattern: str) -> list[str]:
    """Split text on a punctuation regex, keeping the punctuation attached
    to the preceding chunk."""
    pieces = re.split(f"({pattern})", text)
    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        if not piece:
            continue
        buf += piece
        if re.fullmatch(pattern, piece):
            chunks.append(buf)
            buf = ""
    if buf:
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]


def _split_long_text(
    text: str,
    target_chars: int = 80,
    weak_target_chars: int = 50,
) -> list[str]:
    """Split a too-long text into TTS-friendly pieces.

    Google's "too long" error is measured per *sentence* (text between
    sentence-ending marks like 。！？.!?), not per request. A 70-character
    clause joined only by 中文逗号 is still one sentence to Google and trips
    the limit even though the request itself is short.

    Strategy:
      1. Split on strong punctuation (sentence boundaries).
      2. Any chunk still over `target_chars` is sub-split on weak punctuation;
         if a chunk has no internal punctuation at all, we hard-cut at the
         tighter `weak_target_chars` since it'll be one long sentence to Google.
      3. Bundle adjacent small pieces back together to avoid choppy prosody —
         but cap any all-comma bundle at `weak_target_chars`, not
         `target_chars`. This is the fix for the bug where bundling produced
         ~80-char comma-only chunks that Google still rejected.
    """
    text = text.strip()
    if not text:
        return []

    strong = _split_on_punct(text, _TTS_STRONG_PUNCT)
    if not strong:
        strong = [text]

    pieces: list[str] = []
    for chunk in strong:
        if len(chunk) <= target_chars:
            pieces.append(chunk)
            continue
        weak = _split_on_punct(chunk, _TTS_WEAK_PUNCT)
        if len(weak) <= 1:
            # No internal punctuation to split on. Hard-cut at the tighter
            # weak limit so we don't hand Google a comma-less wall of text.
            pieces.extend(
                chunk[i : i + weak_target_chars]
                for i in range(0, len(chunk), weak_target_chars)
            )
        else:
            pieces.extend(weak)

    # Bundle adjacent pieces. The applicable size limit depends on whether the
    # candidate bundle contains a sentence-ending mark anywhere:
    #   - If yes: Google sees multiple sentences inside, so target_chars is fine.
    #   - If no:  Google sees one long sentence, so cap at weak_target_chars.
    bundled: list[str] = []
    current = ""
    current_has_strong = False
    for p in pieces:
        p_has_strong = _has_strong_punct(p)
        if not current:
            current = p
            current_has_strong = p_has_strong
            continue
        candidate = current + p
        candidate_has_strong = current_has_strong or p_has_strong
        limit = target_chars if candidate_has_strong else weak_target_chars
        if len(candidate) <= limit:
            current = candidate
            current_has_strong = candidate_has_strong
        else:
            bundled.append(current)
            current = p
            current_has_strong = p_has_strong
    if current:
        bundled.append(current)
    return bundled


# Maximum recursion depth when a chunk produced by _split_long_text is itself
# rejected as too long by Google. Each level halves the target sizes; 3 levels
# is enough to bring an 80-char chunk down to ~10 chars, well below any
# plausible per-sentence limit.
_GOOGLE_SPLIT_MAX_DEPTH = 3


def _google_generate_with_split(
    client,
    text: str,
    output_path: Path,
    voice_name: str,
    speaking_rate: float,
    pitch: float,
    target_chars: int = 80,
    weak_target_chars: int = 50,
    _depth: int = 0,
) -> int:
    """Synthesize a too-long text by splitting it on punctuation, generating
    each chunk separately, and concatenating the result into output_path.

    If a chunk is itself rejected as too long, recursively re-split that chunk
    with progressively tighter limits (up to _GOOGLE_SPLIT_MAX_DEPTH levels).
    Without recursion, a single overlong comma-only clause that survived the
    first split would silently turn into a gap in the output.

    Returns the total number of leaf chunks synthesized. Raises if no level of
    splitting can produce more than one chunk (caller should treat as
    non-recoverable and fall back to silence).
    """
    chunks = _split_long_text(text, target_chars, weak_target_chars)
    if len(chunks) <= 1:
        raise RuntimeError(
            "text could not be split into smaller chunks for TTS"
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="echo_tts_split_"))
    try:
        chunk_paths: list[Path] = []
        leaf_count = 0
        for i, chunk in enumerate(chunks):
            cp = tmp_dir / f"chunk_d{_depth}_{i:03d}.mp3"
            try:
                _google_generate_single(
                    client, chunk, cp, voice_name, speaking_rate, pitch,
                )
                leaf_count += 1
            except Exception as e:
                err_str = str(e)
                if _is_too_long_error(err_str) and _depth < _GOOGLE_SPLIT_MAX_DEPTH:
                    # Recurse with halved limits. The recursive call writes its
                    # own concatenated MP3 to `cp`, which lives in OUR tmp_dir
                    # (not the recursive call's), so it survives that call's
                    # cleanup and we can read it back below.
                    sub_target = max(20, target_chars // 2)
                    sub_weak = max(15, weak_target_chars // 2)
                    short_text = chunk[:30].replace("\n", " ")
                    logger.info(
                        f"Google TTS recursing (depth {_depth + 1}, "
                        f"target={sub_target}/{sub_weak}) for "
                        f"'{short_text}...'"
                    )
                    leaf_count += _google_generate_with_split(
                        client, chunk, cp,
                        voice_name, speaking_rate, pitch,
                        target_chars=sub_target,
                        weak_target_chars=sub_weak,
                        _depth=_depth + 1,
                    )
                else:
                    # Either not a "too long" error, or we've exhausted depth.
                    # Re-raise so the outer _work() handler logs it and falls
                    # back to silence for this segment.
                    raise

            chunk_paths.append(cp)

        combined = AudioSegment.empty()
        for cp in chunk_paths:
            combined += AudioSegment.from_file(str(cp), format="mp3")
        combined.export(str(output_path), format="mp3")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return leaf_count


def _run_google_batch(
    texts: list[str],
    work_dir: Path,
    prefix: str,
    google_config: dict,
    gain_db: float,
    normalize_target_dbfs: float | None,
) -> list[AudioSegment]:
    """Generate TTS via Google Cloud for a batch of texts (concurrent + retries)."""
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    client = _get_google_client()

    voice_key = "native_voice" if prefix == "native" else "target_voice"
    voice_name = google_config.get(voice_key, "")
    if not voice_name:
        raise RuntimeError(
            f"Google TTS: '{voice_key}' is not configured in tts.google"
        )

    speaking_rate = float(google_config.get("speaking_rate", 1.0))
    pitch = float(google_config.get("pitch", 0.0))

    output_paths: list[Path] = [
        work_dir / f"{prefix}_{i:04d}.mp3" for i in range(len(texts))
    ]

    def _work(idx: int) -> None:
        text = texts[idx]
        out_path = output_paths[idx]
        for attempt in range(1, GOOGLE_MAX_RETRIES + 1):
            try:
                _google_generate_single(
                    client, text, out_path, voice_name, speaking_rate, pitch,
                )
                return
            except Exception as e:
                err_str = str(e)

                # "Sentence too long" is non-retryable as-is, but recoverable
                # by splitting the text on punctuation and synthesizing each
                # piece separately. The split helper recurses on tighter
                # limits if any of its chunks is itself too long, so we don't
                # need a retry loop here for that error class.
                if _is_too_long_error(err_str):
                    try:
                        n_chunks = _google_generate_with_split(
                            client, text, out_path,
                            voice_name, speaking_rate, pitch,
                        )
                        short_text = text[:30].replace("\n", " ")
                        logger.info(
                            f"Google TTS split long text into {n_chunks} chunks "
                            f"for '{short_text}...'"
                        )
                        return
                    except Exception as split_err:
                        logger.warning(
                            f"Google TTS split fallback failed for "
                            f"'{text[:30]}...': {split_err}"
                        )
                        return

                # Reuse the shared transient classifier; also catch Google's
                # gRPC quota / unavailable codes that aren't in the table.
                err_label = _classify_transient(err_str)
                if not err_label and any(
                    s in err_str
                    for s in ("429", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED")
                ):
                    err_label = "google-transient"

                if err_label and attempt < GOOGLE_MAX_RETRIES:
                    delay = GOOGLE_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    short_text = text[:30].replace("\n", " ")
                    logger.warning(
                        f"⟳ Retry {attempt}/{GOOGLE_MAX_RETRIES} in {delay:.0f}s "
                        f"({err_label}) for '{short_text}...'"
                    )
                    time.sleep(delay)
                else:
                    kind = err_label or "non-retryable"
                    logger.warning(
                        f"Google TTS failed [{kind}, "
                        f"attempt {attempt}/{GOOGLE_MAX_RETRIES}]: {e}"
                    )
                    return

    with ThreadPoolExecutor(max_workers=GOOGLE_CONCURRENCY) as pool:
        futures = [pool.submit(_work, i) for i in range(len(texts))]
        for f in as_completed(futures):
            f.result()

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
    """Load generated mp3 files into AudioSegments with volume adjustment.

    Tracks how many clips fell back to silence due to TTS failure and
    emits a single summary warning at the end if any did. Without this
    summary, individual per-clip warnings can scroll past unnoticed
    while the final m4a still appears to "succeed" (just with silent gaps).
    """
    audios = []
    fallback_indices: list[int] = []

    for i, path in enumerate(paths):
        if path.exists() and path.stat().st_size > 0:
            audio = AudioSegment.from_file(str(path), format="mp3")
            audio = _adjust_volume(audio, gain_db, normalize_target_dbfs)
            audios.append(audio)
        else:
            logger.warning(
                f"TTS failed for '{texts[i][:30]}...', using silence"
            )
            audios.append(AudioSegment.silent(duration=500))
            fallback_indices.append(i)

    if fallback_indices:
        # End-of-batch summary so silent gaps in the output don't go unnoticed
        n = len(fallback_indices)
        total = len(paths)
        idx_preview = ", ".join(str(i) for i in fallback_indices[:8])
        if n > 8:
            idx_preview += f", ... ({n - 8} more)"
        logger.warning(
            f"⚠ {n}/{total} TTS clips fell back to silence "
            f"(segment indices: {idx_preview}). "
            f"The output audio will have silent gaps at these positions."
        )
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
    google_config: dict | None = None,
) -> list[AudioSegment]:
    """Generate native language TTS audio for all segments."""
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="echo_tts_"))
    else:
        work_dir.mkdir(parents=True, exist_ok=True)

    texts = [seg.native_text for seg in segments]

    if engine == "google":
        return _run_google_batch(
            texts, work_dir, "native",
            google_config or {},
            gain_db, normalize_target_dbfs,
        )
    elif engine == "openai":
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
    google_config: dict | None = None,
) -> list[AudioSegment]:
    """Generate target language TTS audio for all segments."""
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="echo_tts_"))
    else:
        work_dir.mkdir(parents=True, exist_ok=True)

    texts = [seg.target_text for seg in segments]

    if engine == "google":
        return _run_google_batch(
            texts, work_dir, "target",
            google_config or {},
            gain_db, normalize_target_dbfs,
        )
    elif engine == "openai":
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
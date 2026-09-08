"""
ElevenLabs v3 target-language TTS.

Why this module looks nothing like the other engines
----------------------------------------------------
The Google / edge / OpenAI paths send one request per subtitle line. v3 can't
work that way: it refuses every form of stitching context (``previous_text``,
``previous_request_ids``, the ``eleven_v3_conversational`` variant — all HTTP
400 ``unsupported_model``), and an isolated 25-character line is 1/10 of the
length its prompting guide asks for. Blind listening on 2026-09-09 put v3 on a
whole paragraph far ahead of ``eleven_multilingual_v2`` with stitching.

So this module sends a *paragraph* (consecutive lines, each with its own
voice_id) to ``/v1/text-to-dialogue/with-timestamps`` and cuts the returned
audio back into one clip per line. The cut points come from the model's own
``voice_segments`` (per-input start/end seconds) — not from character counts or
a speaking-rate estimate — and every cut is then verified by the energy mask
in ``audio/splitter.py`` to sit in silence. A paragraph whose boundaries do not
all land in silence is regenerated; if that keeps failing the run fails. There
is no silence-fallback here (unlike the per-line engines), because a silent
gap in a paragraph-cut would be a timing error, not a missing clip.

This is the one sanctioned exception to the "no whole-paragraph TTS" rule in
CLAUDE.md; the rule text records why.

Two things v3 will not do for us, so they happen locally after the cut:
- ``speed`` — accepted by the API, silently ignored (0.8 / 1.0 / 1.2 gave the
  same duration). We time-stretch each clip with ffmpeg ``atempo`` instead.
- loudness — library voices differ by up to 17 dB; ``normalize`` in config.yaml
  is the fix, applied through the shared ``_adjust_volume`` like every engine.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import requests
from pydub import AudioSegment

from audio.splitter import FRAME_MS

logger = logging.getLogger(__name__)

API_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL_ID = "eleven_v3"
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"

# Paragraph size. v3 wants >= 250 chars of context; 10 lines of the interview
# scripts is ~260 Japanese chars. The char cap is far below the 5,000 request
# limit so a paragraph that has to be regenerated is cheap.
DEFAULT_MAX_CHUNK_LINES = 10
DEFAULT_MAX_CHUNK_CHARS = 2000

# Starter tier allows 3 concurrent requests, Free allows 2. Over the limit the
# API rejects immediately with 429 ``concurrent_limit_exceeded`` (no queueing).
DEFAULT_CONCURRENCY = 2
MAX_HTTP_RETRIES = 4
RETRY_BASE_DELAY = 2.0

# How many times to regenerate a paragraph whose cut points fail verification
# (fresh seed each time) before giving up on the whole run.
MAX_REGENERATE = 2

# ffmpeg atempo is clean within this band; beyond it artefacts show up.
ATEMPO_MIN = 0.5
ATEMPO_MAX = 2.0

TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class ElevenLabsError(RuntimeError):
    """Raised when synthesis or boundary verification fails for good."""


@dataclass
class _Chunk:
    """One paragraph request: consecutive segment indices with their voices."""
    indices: list[int]
    texts: list[str]
    voice_ids: list[str]


def _api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        raise ElevenLabsError(
            "ELEVENLABS_API_KEY environment variable is not set "
            "(put it in .env; see .env.example)"
        )
    return key


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def plan_chunks(
    texts: list[str],
    voice_ids: list[str],
    max_lines: int = DEFAULT_MAX_CHUNK_LINES,
    max_chars: int = DEFAULT_MAX_CHUNK_CHARS,
) -> list[_Chunk]:
    """Group consecutive lines into paragraphs.

    Lines stay in script order (the whole point is giving v3 the surrounding
    context). A single over-long line gets a chunk of its own rather than being
    split — one subtitle must map to one input.
    """
    chunks: list[_Chunk] = []
    cur = _Chunk([], [], [])
    cur_chars = 0
    for i, (text, voice) in enumerate(zip(texts, voice_ids)):
        if cur.indices and (
            len(cur.indices) >= max_lines or cur_chars + len(text) > max_chars
        ):
            chunks.append(cur)
            cur = _Chunk([], [], [])
            cur_chars = 0
        cur.indices.append(i)
        cur.texts.append(text)
        cur.voice_ids.append(voice)
        cur_chars += len(text)
    if cur.indices:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _post_dialogue(
    chunk: _Chunk,
    model_id: str,
    output_format: str,
    stability: float | None,
    seed: int,
    session: requests.Session,
) -> dict:
    """POST one paragraph to text-to-dialogue/with-timestamps, with retries.

    Retries only transient statuses (429 concurrency / 5xx). Anything else —
    bad key (401), plan limits (402), schema (400/422) — is a configuration
    problem and is raised as-is so it surfaces in the log.
    """
    body: dict = {
        "model_id": model_id,
        "inputs": [
            {"text": t, "voice_id": v} for t, v in zip(chunk.texts, chunk.voice_ids)
        ],
        "seed": seed,
    }
    if stability is not None:
        body["settings"] = {"stability": float(stability)}

    url = f"{API_BASE}/text-to-dialogue/with-timestamps"
    headers = {"xi-api-key": _api_key()}
    params = {"output_format": output_format}

    for attempt in range(1, MAX_HTTP_RETRIES + 1):
        r = session.post(url, headers=headers, params=params, json=body, timeout=180)
        if r.status_code == 200:
            return r.json()
        try:
            detail = r.json().get("detail", {})
            code = detail.get("status") or detail.get("code") or ""
            message = detail.get("message") or r.text[:300]
        except Exception:
            code, message = "", r.text[:300]
        if r.status_code in TRANSIENT_STATUS and attempt < MAX_HTTP_RETRIES:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warning(
                f"⟳ ElevenLabs retry {attempt}/{MAX_HTTP_RETRIES} in {delay:.1f}s "
                f"(HTTP {r.status_code} {code}) for lines "
                f"{chunk.indices[0]}–{chunk.indices[-1]}"
            )
            time.sleep(delay)
            continue
        raise ElevenLabsError(
            f"ElevenLabs HTTP {r.status_code} {code}: {message} "
            f"(lines {chunk.indices[0]}–{chunk.indices[-1]})"
        )
    raise ElevenLabsError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Cutting + verification
# ---------------------------------------------------------------------------

def boundaries_from_response(resp: dict, chunk: _Chunk) -> list[tuple[int, int]]:
    """Per-line (start_ms, end_ms) from ``voice_segments``, cross-checked.

    Checks that the model returned exactly one segment per input, in order,
    and that each segment's character span is the length of the text we sent
    — i.e. the alignment really is for our lines, not a re-normalised text.
    """
    segs = resp.get("voice_segments") or []
    if len(segs) != len(chunk.texts):
        raise ElevenLabsError(
            f"voice_segments count {len(segs)} != inputs {len(chunk.texts)}"
        )
    out: list[tuple[int, int]] = []
    for k, (seg, text) in enumerate(zip(segs, chunk.texts)):
        idx = seg.get("dialogue_input_index", k)
        if idx != k:
            raise ElevenLabsError(f"voice_segments out of order at {k}: {idx}")
        span = int(seg["character_end_index"]) - int(seg["character_start_index"])
        if span != len(text):
            raise ElevenLabsError(
                f"alignment span {span} != text length {len(text)} at line "
                f"{chunk.indices[k]}: {text[:30]!r}"
            )
        s = int(round(float(seg["start_time_seconds"]) * 1000))
        e = int(round(float(seg["end_time_seconds"]) * 1000))
        if e <= s:
            raise ElevenLabsError(f"empty voice_segment at line {chunk.indices[k]}")
        out.append((s, e))
    return out


# --- Where the cuts really come from -------------------------------------
#
# Measured on v3 output (2026-09-09): the model's per-line end times can run
# up to ~1 s EARLY (the last input of a dialogue ended at 5.36 s while speech
# went on to 6.32 s), and Japanese has 30–80 ms silent stop closures (促音,
# k/t closures) inside words. So a boundary can't be trusted to the frame and
# a "nearest silent frame" rule would cut inside words — that was the first
# version, and it clipped sentence endings.
#
# What IS reliable: between two lines v3 leaves a long true pause (500–1000 ms
# of near-digital silence, floor ≈ −85 dBFS) and the model boundary lands
# within about a second of it. So the cut points are the long silent runs of
# the paragraph; the model boundary only picks WHICH run separates line k from
# k+1. Each boundary must map to its own run, in order, or the paragraph is
# rejected and regenerated.
SOUND_DBFS = -55.0          # frames above this are "sound" (keeps the quiet
                            # devoiced endings like ます/です that sit 35 dB
                            # below the peak but far above the −85 dB floor)
MIN_PAUSE_MS = 200          # a silent run shorter than this is inside a word
BOUNDARY_WINDOW_MS = 1500   # how far from the model boundary the pause may be
EDGE_PAD_MS = 200           # silence kept before the first / after the last
                            # sound of a line (same figure as the splitter)


def _frame_dbfs(audio: AudioSegment):
    import numpy as np
    samples = np.array(audio.get_array_of_samples(), dtype=np.float64)
    if audio.channels > 1:
        samples = samples.reshape(-1, audio.channels).mean(axis=1)
    samples /= float(2 ** (8 * audio.sample_width - 1))
    per = max(1, audio.frame_rate * FRAME_MS // 1000)
    n = len(samples) // per
    if n == 0:
        return np.zeros(0)
    frames = samples[: n * per].reshape(n, per)
    return 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-12)


def sound_mask(audio: AudioSegment):
    """Per-10 ms frame: True where there is any sound above SOUND_DBFS."""
    return _frame_dbfs(audio) > SOUND_DBFS


def silent_runs(mask, min_ms: int = MIN_PAUSE_MS) -> list[tuple[int, int]]:
    """[(first_frame, last_frame)] of silent runs at least ``min_ms`` long."""
    runs: list[tuple[int, int]] = []
    n = len(mask)
    i = 0
    need = max(1, min_ms // FRAME_MS)
    while i < n:
        if not mask[i]:
            j = i
            while j + 1 < n and not mask[j + 1]:
                j += 1
            if j - i + 1 >= need:
                runs.append((i, j))
            i = j + 1
        else:
            i += 1
    return runs


def cut_paragraph(
    audio: AudioSegment,
    boundaries: list[tuple[int, int]],
) -> list[AudioSegment]:
    """Cut one paragraph into per-line clips at verified inter-line pauses.

    ``boundaries`` are the model's contiguous (start_ms, end_ms) per line. For
    each seam between line k and k+1 the long silent run nearest to the
    model's seam time (within BOUNDARY_WINDOW_MS) is chosen; runs must be
    distinct and in script order. Line k then ends EDGE_PAD_MS into that run
    and line k+1 starts EDGE_PAD_MS before the run ends — pads only ever eat
    silence. The paragraph edges use the leading / trailing silence the same
    way. Every clip therefore starts and ends in verified silence, and a
    paragraph with a missing pause raises ElevenLabsError.
    """
    mask = sound_mask(audio)
    n = len(mask)
    if n == 0 or not mask.any():
        raise ElevenLabsError("paragraph audio is empty or silent")
    runs = silent_runs(mask)
    # interior runs only: a leading/trailing silence is not a seam
    seams_available = [
        (a, b) for (a, b) in runs if a > 0 and b < n - 1
    ]

    seam_times = [e for (_, e) in boundaries[:-1]]  # model seam k|k+1
    chosen: list[tuple[int, int]] = []
    last_used = -1
    w = BOUNDARY_WINDOW_MS // FRAME_MS
    for k, t_ms in enumerate(seam_times):
        t = t_ms // FRAME_MS
        best = None
        for ri, (a, b) in enumerate(seams_available):
            if ri <= last_used:
                continue
            mid = (a + b) // 2
            d = 0 if a <= t <= b else min(abs(t - a), abs(t - b))
            if d > w:
                continue
            if best is None or d < best[0]:
                best = (d, ri, mid)
        if best is None:
            raise ElevenLabsError(
                f"no pause >= {MIN_PAUSE_MS} ms within {BOUNDARY_WINDOW_MS} ms of "
                f"the seam between lines {k} and {k + 1} (model {t_ms} ms)"
            )
        last_used = best[1]
        chosen.append(seams_available[best[1]])

    pad = EDGE_PAD_MS // FRAME_MS
    first_sound = int(mask.argmax())
    last_sound = n - 1 - int(mask[::-1].argmax())

    starts = [max(0, first_sound - pad)]
    ends: list[int] = []
    for (a, b) in chosen:
        # line ends `pad` frames into the pause, next line starts `pad` before it
        # ends; both stay inside the run so the cut is in silence by construction
        ends.append(min(a + pad, b))
        starts.append(max(b - pad + 1, a + 1))
    ends.append(min(n, last_sound + 1 + pad))

    clips: list[AudioSegment] = []
    for k, (sf, ef) in enumerate(zip(starts, ends)):
        if ef <= sf:
            raise ElevenLabsError(f"cut collapsed at line {k}: frames {sf}–{ef}")
        if (sf > 0 and mask[sf]) or (ef < n and mask[ef - 1]):
            raise ElevenLabsError(f"cut of line {k} is not in silence")  # pragma: no cover
        clips.append(audio[sf * FRAME_MS: ef * FRAME_MS])
    return clips


# ---------------------------------------------------------------------------
# Local post-processing
# ---------------------------------------------------------------------------

def time_stretch(audio: AudioSegment, speed: float) -> AudioSegment:
    """Change tempo (not pitch) with ffmpeg ``atempo``; identity at 1.0."""
    if speed is None or abs(speed - 1.0) < 1e-3:
        return audio
    speed = max(ATEMPO_MIN, min(ATEMPO_MAX, float(speed)))
    with tempfile.TemporaryDirectory(prefix="echo_atempo_") as td:
        src = Path(td) / "in.wav"
        dst = Path(td) / "out.wav"
        audio.export(src, format="wav")
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
            "-filter:a", f"atempo={speed:.4f}", str(dst),
        ]
        subprocess.run(cmd, check=True)
        return AudioSegment.from_file(dst, format="wav")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_elevenlabs_target_audio(
    texts: list[str],
    voice_ids: list[str],
    work_dir: Path,
    elevenlabs_config: dict,
    speeds: list[float] | None = None,
    gain_db: float = 0.0,
    normalize_target_dbfs: float | None = None,
) -> list[AudioSegment]:
    """Synthesize one clip per line via paragraph requests to v3.

    ``texts`` / ``voice_ids`` / ``speeds`` are parallel lists in script order.
    Returns clips in the same order. Raises ``ElevenLabsError`` rather than
    substituting silence — see the module docstring.
    """
    from audio.tts_generator import _adjust_volume  # shared volume helper

    if len(texts) != len(voice_ids):
        raise ValueError("texts and voice_ids must have the same length")
    if speeds is None:
        speeds = [1.0] * len(texts)
    if any(not v for v in voice_ids):
        raise ElevenLabsError("an ElevenLabs voice_id is missing for some lines")
    _api_key()  # fail early, before spending any time

    model_id = elevenlabs_config.get("model_id") or DEFAULT_MODEL_ID
    output_format = elevenlabs_config.get("output_format") or DEFAULT_OUTPUT_FORMAT
    stability = elevenlabs_config.get("stability", 0.5)
    concurrency = int(elevenlabs_config.get("concurrency") or DEFAULT_CONCURRENCY)
    max_lines = int(elevenlabs_config.get("max_chunk_lines") or DEFAULT_MAX_CHUNK_LINES)
    max_chars = int(elevenlabs_config.get("max_chunk_chars") or DEFAULT_MAX_CHUNK_CHARS)

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    chunks = plan_chunks(texts, voice_ids, max_lines, max_chars)
    logger.info(
        f"  ElevenLabs {model_id}: {len(texts)} lines in {len(chunks)} paragraph "
        f"request(s), concurrency {concurrency}"
    )

    results: dict[int, list[AudioSegment]] = {}
    session = requests.Session()

    def _work(ci: int) -> None:
        chunk = chunks[ci]
        seed = random.randint(0, 2**31 - 1)
        last_err: Exception | None = None
        for attempt in range(1, MAX_REGENERATE + 2):
            resp = _post_dialogue(chunk, model_id, output_format, stability, seed, session)
            raw = work_dir / f"el_para_{ci:03d}.mp3"
            raw.write_bytes(base64.b64decode(resp["audio_base64"]))
            try:
                bounds = boundaries_from_response(resp, chunk)
                audio = AudioSegment.from_file(raw, format="mp3")
                clips = cut_paragraph(audio, bounds)
            except ElevenLabsError as e:
                last_err = e
                logger.warning(
                    f"⟳ ElevenLabs paragraph {ci} (lines "
                    f"{chunk.indices[0]}–{chunk.indices[-1]}) failed boundary "
                    f"verification, regenerating ({attempt}/{MAX_REGENERATE + 1}): {e}"
                )
                seed = random.randint(0, 2**31 - 1)
                continue
            # Provenance: which model boundaries produced which clip.
            (work_dir / f"el_para_{ci:03d}.json").write_text(
                json.dumps(
                    {
                        "lines": chunk.indices,
                        "model_boundaries_ms": bounds,
                        "clip_ms": [len(c) for c in clips],
                        "seed": seed,
                        "character_cost": resp.get("character_cost"),
                    },
                    ensure_ascii=False,
                )
            )
            results[ci] = clips
            keep = os.environ.get("ELEVENLABS_KEEP_DIR")
            if keep:
                import shutil
                Path(keep).mkdir(parents=True, exist_ok=True)
                shutil.copy(raw, Path(keep) / raw.name)
                shutil.copy(work_dir / f"el_para_{ci:03d}.json", Path(keep) / f"el_para_{ci:03d}.json")
            return
        raise ElevenLabsError(
            f"ElevenLabs paragraph {ci} still failed after "
            f"{MAX_REGENERATE + 1} attempts: {last_err}"
        )

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(_work, ci) for ci in range(len(chunks))]
        for f in as_completed(futures):
            f.result()  # re-raise the first failure

    out: list[AudioSegment | None] = [None] * len(texts)
    for ci, chunk in enumerate(chunks):
        for k, idx in enumerate(chunk.indices):
            clip = time_stretch(results[ci][k], speeds[idx])
            out[idx] = _adjust_volume(clip, gain_db, normalize_target_dbfs)
    missing = [i for i, c in enumerate(out) if c is None]
    if missing:
        raise ElevenLabsError(f"no clip produced for lines {missing}")
    return out  # type: ignore[return-value]

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
# Measured word-internal silences: Japanese 促音 / stop closures 30-80 ms,
# English stop closures 60-90 ms. Real inter-sentence pauses are 240-870 ms in
# both languages. 120 ms sits in that gap. Extra candidates (commas) are
# harmless — the assignment below picks which run belongs to which seam.
MIN_PAUSE_MS = 120
BOUNDARY_WINDOW_MS = 2500   # how far a pause may sit from the model's seam time
EDGE_PAD_MS = 200           # silence kept before the first / after the last
                            # sound of a line (same figure as the splitter)

# The model's absolute timings drift against the audio it returns, and the drift
# is language-dependent: measured over 133 Japanese and 113 English clips, the
# Japanese audio matched voice_segments exactly (ratio 1.000) while the English
# audio ran up to 12% longer than the model claimed (median 1.042). Matching a
# seam to the nearest pause by absolute time therefore drifted on English and,
# because the assignment is monotonic, one wrong pick shifted every line after
# it. Seam times are rescaled by (audio length / model total) before matching:
# that removed 11 of the 12 English misalignments and changed nothing on
# Japanese. The 12th is caught by the speech cross-check below.
# The clip-vs-model check counts SOUND frames on both sides rather than total
# duration. Comparing total durations was biased: the model's per-line spans are
# contiguous, so span(k) swallows the pause FOLLOWING line k, while the clip
# deliberately keeps only EDGE_PAD_MS of it. Measured over the 50 cached
# paragraphs (488 clips, two scripts) the clip therefore ran short by a median
# 789 ms and the shortfall tracked the pause length (r = +0.69) — so material
# whose pauses ran long was rejected for having perfectly correct cuts. That is
# what happened on 2026-09-12: 5 of 36 paragraphs were regenerated (~800 wasted
# credits) on a script whose inter-line pauses averaged 1210 ms against the
# 650 ms of the 2026-09-09 material this check was first tuned on, and 30 of its
# 355 accepted clips sat within 500 ms of the same false rejection.
#
# Counting speech on both sides removes the pause from both. The separation is
# then clean and ABSOLUTE: a correct assignment's speech error is bounded by the
# model's own boundary imprecision (up to ~1 s early) and never exceeded 1150 ms,
# while shifting every seam to the next pause never scored below 1730 ms. The cap
# sits in that gap and holds 0 false rejections / 29 of 29 shifts caught anywhere
# in 1300-1500 ms, so it is not balanced on a single fitted point. The relative
# term keeps power on SHORT lines, where a shift displaces less speech.
SPEECH_TOLERANCE_MS = 400        # absolute floor
SPEECH_TOLERANCE_REL = 0.6       # ... or this fraction of the line's own speech
SPEECH_TOLERANCE_CAP_MS = 1400   # ... but never more than this


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


def assign_seams(
    runs: list[tuple[int, int]],
    seam_times_ms: list[int],
) -> list[tuple[int, int]]:
    """Match each model seam to its own silent run, in order, globally.

    A greedy "nearest free run" walk fails on real paragraphs: extra candidates
    (comma pauses) sit between the sentence pauses, and one early mis-pick makes
    every later seam fail. This is a small dynamic program instead — it picks
    the strictly increasing set of runs that minimises the total distance
    between each seam's model time and its run, so a wrong local choice can be
    paid back later. Distances beyond BOUNDARY_WINDOW_MS are refused outright.
    """
    n_seams, n_runs = len(seam_times_ms), len(runs)
    if n_seams == 0:
        return []
    if n_runs < n_seams:
        raise ElevenLabsError(
            f"only {n_runs} pause(s) >= {MIN_PAUSE_MS} ms for {n_seams} seam(s); "
            f"the model ran two lines together"
        )
    w = BOUNDARY_WINDOW_MS // FRAME_MS
    INF = float("inf")

    def dist(k: int, ri: int) -> float:
        a, b = runs[ri]
        t = seam_times_ms[k] // FRAME_MS
        d = 0 if a <= t <= b else min(abs(t - a), abs(t - b))
        return INF if d > w else float(d)

    # best[k][r] = min total cost assigning seams 0..k using runs 0..r, seam k -> run r
    best = [[INF] * n_runs for _ in range(n_seams)]
    back = [[-1] * n_runs for _ in range(n_seams)]
    for r in range(n_runs):
        best[0][r] = dist(0, r)
    for k in range(1, n_seams):
        run_min, run_arg = INF, -1
        for r in range(n_runs):
            if r >= 1:  # best predecessor among runs < r
                if best[k - 1][r - 1] < run_min:
                    run_min, run_arg = best[k - 1][r - 1], r - 1
            d = dist(k, r)
            if d < INF and run_min < INF:
                best[k][r] = run_min + d
                back[k][r] = run_arg
    last = min(range(n_runs), key=lambda r: best[n_seams - 1][r])
    if best[n_seams - 1][last] == INF:
        worst = max(
            range(n_seams),
            key=lambda k: min(dist(k, r) for r in range(n_runs)),
        )
        raise ElevenLabsError(
            f"no pause >= {MIN_PAUSE_MS} ms within {BOUNDARY_WINDOW_MS} ms of the "
            f"seam between lines {worst} and {worst + 1} "
            f"(model {seam_times_ms[worst]} ms)"
        )
    picked = [0] * n_seams
    for k in range(n_seams - 1, -1, -1):
        picked[k] = last
        last = back[k][last]
    return [runs[r] for r in picked]


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

    # Rescale the model's clock onto the audio's before matching (see the note
    # by SPEECH_TOLERANCE_CAP_MS): English v3 returns audio up to 12% longer than
    # its own timings say, which walks a seam onto the wrong pause.
    model_total = boundaries[-1][1]
    scale = (len(audio) / model_total) if model_total > 0 else 1.0
    seam_times = [int(e * scale) for (_, e) in boundaries[:-1]]
    chosen = assign_seams(seams_available, seam_times)


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

    # Landing every cut in silence is not enough: a seam matched to the wrong
    # pause also lands in silence, it just puts the wrong sentence in the clip.
    # The independent check is how much SPEECH the clip holds against how much
    # speech sits inside the model's own span for that line. Both sides count
    # sound frames only, so the inter-line pause — which the model's span
    # includes and the clip drops — cancels instead of biasing the comparison
    # (see the note by SPEECH_TOLERANCE_CAP_MS).
    for k, (sf, ef, (s_ms, e_ms)) in enumerate(zip(starts, ends, boundaries)):
        a = max(0, min(n, int(s_ms * scale) // FRAME_MS))
        b = max(0, min(n, int(e_ms * scale) // FRAME_MS))
        expected = int(mask[a:b].sum()) * FRAME_MS
        actual = int(mask[sf:ef].sum()) * FRAME_MS
        tol = min(
            SPEECH_TOLERANCE_CAP_MS,
            max(SPEECH_TOLERANCE_MS, SPEECH_TOLERANCE_REL * expected),
        )
        if abs(actual - expected) > tol:
            raise ElevenLabsError(
                f"line {k} holds {actual} ms of speech but the model says "
                f"{expected} ms (tolerance {tol:.0f} ms) — the seams are matched "
                f"to the wrong pauses"
            )
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
# Paragraph cache
# ---------------------------------------------------------------------------
#
# A run that fails on one paragraph used to throw away every paragraph that had
# already been paid for: one aborted 133-line English run burned 4,277 credits
# and produced nothing. Synthesis is deterministic enough to cache — the key is
# the exact request (model, format, stability, voices, texts) — so re-running
# after a fix only pays for what is genuinely new.

CACHE_DIR = Path(os.environ.get("ELEVENLABS_CACHE_DIR") or (Path.home() / ".cache" / "echoEnglish" / "elevenlabs"))


def cache_key(chunk: _Chunk, model_id: str, output_format: str, stability: float | None) -> str:
    import hashlib
    h = hashlib.sha256()
    for part in (model_id, output_format, str(stability), *chunk.voice_ids, *chunk.texts):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def cache_load(key: str) -> tuple[AudioSegment, list[tuple[int, int]]] | None:
    mp3, meta = CACHE_DIR / f"{key}.mp3", CACHE_DIR / f"{key}.json"
    if not (mp3.exists() and meta.exists()):
        return None
    try:
        bounds = [tuple(b) for b in json.loads(meta.read_text())["model_boundaries_ms"]]
        return AudioSegment.from_file(mp3, format="mp3"), bounds
    except Exception:
        return None


def cache_store(key: str, raw: Path, bounds: list[tuple[int, int]], chunk: _Chunk) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy(raw, CACHE_DIR / f"{key}.mp3")
        (CACHE_DIR / f"{key}.json").write_text(
            json.dumps({"model_boundaries_ms": bounds, "lines": chunk.indices,
                        "texts": chunk.texts, "voice_ids": chunk.voice_ids}, ensure_ascii=False)
        )
    except Exception as e:  # a cache problem must never fail a run
        logger.warning(f"ElevenLabs cache write failed: {e}")


# A rejected attempt is thrown away by design — bad audio must never reach the
# cache. But that left nothing to diagnose WITH: on 2026-09-12 five paragraphs
# were rejected, and by the time anyone looked, the audio was gone (the temp file
# is overwritten by the retry) and the model's alignment with it. The cause had
# to be reconstructed from the paragraphs that SUCCEEDED, which is how a wrong
# explanation got proposed first. So the rejected attempt is kept now.
#
# Kept by default, not behind a flag: these failures are unpredictable, and an
# env var you have to set in advance is useless the first time one happens.
# ``voice_segments`` is the part that matters most — the cut is derived from it
# and it cannot be recovered from the audio afterwards.
REJECT_DIR = Path(os.environ.get("ELEVENLABS_REJECT_DIR") or (CACHE_DIR / "rejected"))
REJECT_KEEP = 30   # newest rejected attempts kept; ~1 MB each


def _prune_rejects() -> None:
    """Keep only the newest REJECT_KEEP attempts, mp3 + json together."""
    metas = sorted(
        REJECT_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True
    )
    for old in metas[REJECT_KEEP:]:
        old.with_suffix(".mp3").unlink(missing_ok=True)
        old.unlink(missing_ok=True)


def reject_store(
    raw: Path,
    resp: dict,
    chunk: _Chunk,
    ci: int,
    attempt: int,
    seed: int,
    err: Exception,
) -> Path | None:
    """Keep one rejected attempt so the next occurrence is diagnosable.

    Returns the saved audio path, or None if nothing could be written.
    """
    try:
        REJECT_DIR.mkdir(parents=True, exist_ok=True)
        stem = f"{time.strftime('%Y%m%d_%H%M%S')}_p{ci:03d}_a{attempt}"
        mp3 = REJECT_DIR / f"{stem}.mp3"
        import shutil
        shutil.copy(raw, mp3)
        (REJECT_DIR / f"{stem}.json").write_text(
            json.dumps(
                {
                    "error": str(err),
                    "paragraph": ci,
                    "attempt": attempt,
                    "seed": seed,
                    "lines": chunk.indices,
                    "texts": chunk.texts,
                    "voice_ids": chunk.voice_ids,
                    "voice_segments": resp.get("voice_segments"),
                    "character_cost": resp.get("character_cost"),
                },
                ensure_ascii=False,
                indent=1,
            )
        )
        _prune_rejects()
        return mp3
    except Exception as e:  # diagnostics must never fail an already-paid run
        logger.warning(f"ElevenLabs rejected-attempt save failed: {e}")
        return None


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
        key = cache_key(chunk, model_id, output_format, stability)
        raw = work_dir / f"el_para_{ci:03d}.mp3"

        cached = cache_load(key)
        if cached is not None:
            audio, bounds = cached
            try:
                results[ci] = cut_paragraph(audio, bounds)
                audio.export(raw, format="mp3")
                (work_dir / f"el_para_{ci:03d}.json").write_text(
                    json.dumps({"lines": chunk.indices, "model_boundaries_ms": bounds,
                                "clip_ms": [len(c) for c in results[ci]], "cached": True},
                               ensure_ascii=False))
                logger.info(f"  paragraph {ci} (lines {chunk.indices[0]}–{chunk.indices[-1]}) from cache, 0 credits")
                return
            except ElevenLabsError:
                pass  # cached audio no longer cuts cleanly — regenerate below

        for attempt in range(1, MAX_REGENERATE + 2):
            resp = _post_dialogue(chunk, model_id, output_format, stability, seed, session)
            raw.write_bytes(base64.b64decode(resp["audio_base64"]))
            try:
                bounds = boundaries_from_response(resp, chunk)
                audio = AudioSegment.from_file(raw, format="mp3")
                clips = cut_paragraph(audio, bounds)
                cache_store(key, raw, bounds, chunk)
            except ElevenLabsError as e:
                last_err = e
                kept = reject_store(raw, resp, chunk, ci, attempt, seed, e)
                logger.warning(
                    f"⟳ ElevenLabs paragraph {ci} (lines "
                    f"{chunk.indices[0]}–{chunk.indices[-1]}) failed boundary "
                    f"verification, regenerating ({attempt}/{MAX_REGENERATE + 1}): {e}"
                    + (f"\n      rejected attempt kept at {kept}" if kept else "")
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

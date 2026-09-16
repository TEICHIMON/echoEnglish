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
audio back into one clip per line. Where each line sits in that audio comes
from ``/v1/forced-alignment`` — the known text aligned against the returned
audio, per character, by a separate model — not from character counts, a
speaking-rate estimate, or the generator's own ``voice_segments`` (see the
note above ``cut_paragraph`` for why those were dropped). Every seam is a
silent run inside the alignment's interval for it, verified by the energy mask
in ``audio/splitter.py``. A paragraph with two lines run together with no pause
is regenerated; if that keeps failing the run fails. There is no
silence-fallback here (unlike the per-line engines), because a silent gap in a
paragraph-cut would be a timing error, not a missing clip.

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
    cached=None,
) -> list[_Chunk]:
    """Group consecutive lines into paragraphs.

    Lines stay in script order (the whole point is giving v3 the surrounding
    context). A single over-long line gets a chunk of its own rather than being
    split — one subtitle must map to one input.

    ``cached`` is an optional predicate ``(_Chunk) -> bool`` saying whether a
    paragraph is already paid for (see the cache below). With it, boundaries
    are chosen to REUSE cached paragraphs: the cache key is the exact line
    set, so after a script edit that inserts or removes a line, fixed 10-line
    blocks drift off every cached paragraph after the edit and the whole
    script is re-synthesised (2026-09-13: an aborted re-run of a 298-line
    script re-requested paragraphs 3+ for nothing — 548 credits gone, and a
    full miss would have been ~9,000). The plan is a small DP over line
    positions minimising (characters to synthesise, number of paragraphs);
    without ``cached`` every paragraph is fresh and the result is the plain
    max-length grouping. Lines between two cached paragraphs become a
    fresh paragraph of their own, however short — that is the price of the
    edit, and cheaper than re-buying the neighbours.
    """
    n = len(texts)
    if n == 0:
        return []

    def chunk_at(i: int, length: int) -> _Chunk:
        return _Chunk(
            list(range(i, i + length)),
            list(texts[i:i + length]),
            list(voice_ids[i:i + length]),
        )

    INF = (float("inf"), float("inf"))
    best: list[tuple[float, float]] = [INF] * (n + 1)
    best[n] = (0.0, 0.0)
    choice = [0] * n
    for i in range(n - 1, -1, -1):
        chars = 0
        for length in range(1, min(max_lines, n - i) + 1):
            chars += len(texts[i + length - 1])
            if length > 1 and chars > max_chars:
                break
            tail = best[i + length]
            if tail == INF:
                continue
            fresh = 0 if (cached is not None and cached(chunk_at(i, length))) else chars
            cost = (fresh + tail[0], 1 + tail[1])
            # ties keep the longer paragraph (v3 wants context)
            if cost < best[i] or (cost == best[i] and length > choice[i]):
                best[i], choice[i] = cost, length
    chunks: list[_Chunk] = []
    i = 0
    while i < n:
        chunks.append(chunk_at(i, choice[i]))
        i += choice[i]
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


# ---------------------------------------------------------------------------
# Cutting: forced alignment decides the seams
# ---------------------------------------------------------------------------
#
# Where each line starts and ends inside the paragraph audio comes from
# POST /v1/forced-alignment: the known text, aligned against the returned audio
# per character by a separate model. On 2026-09-16 that replaced two
# generations of seam-finding from the generator's own ``voice_segments`` —
# first a uniform rescale of its clock, then an offset-tracking DP backed by a
# speech-volume cross-check. Each was tuned against the failures seen so far
# and each fix uncovered the next mode: the clock drifts in steps, a pause is
# booked to either neighbour, seams run early by up to a second. The last
# version accepted a paragraph with two seams sitting in the comma pauses of
# the FOLLOWING lines ("そこでやっと、" / "手順が長く、"), a whole clause in the
# wrong clip, and a forced-alignment audit of the 38 delivered paragraphs found
# three such cascades (10 lines) that every speech-volume check had passed. A
# volume check cannot tell a clause from a pause; the alignment can, and it
# does not share the generator's clock. Cost: ~18 credits per minute of audio
# (677 credits for that 38-minute audit), about 7% on top of synthesis, paid
# once and cached beside the paragraph.
#
# Only character START times are used. The alignment's end times are
# contiguous — a line's last character absorbs the pause after it, exactly as
# the generator's own alignment does — so "where line k ends" is never read
# from them. The seam between k and k+1 is a silent run that starts after the
# last character of k has begun and before the first character of k+1 begins;
# the longest such run is the inter-line pause, and the comma pauses inside
# either line lie outside that interval by construction. No run there means
# the two lines were spoken with no pause between them, and the paragraph is
# regenerated: the CLAUDE.md rule allows a cut only in verified silence.

SOUND_DBFS = -55.0          # frames above this are "sound" (keeps the quiet
                            # devoiced endings like ます/です that sit 35 dB
                            # below the peak but far above the −85 dB floor)
# Measured word-internal silences: Japanese 促音 / stop closures 30-80 ms,
# English stop closures 60-90 ms. Real inter-line pauses are 240-870 ms in
# both languages. 120 ms sits in that gap.
MIN_PAUSE_MS = 120
EDGE_PAD_MS = 200           # silence kept before the first / after the last
                            # sound of a line (same figure as the splitter)
# The energy mask hears a consonant onset 100-200 ms before the aligner places
# the character (measured on the 38-paragraph audit: silent runs ended 50-210 ms
# before the next line's first-character start). A candidate run may therefore
# begin this much before the last character's start and end this much after
# the next first character's start.
ALIGN_SLACK_MS = 250

# Characters the aligner gives no sound of their own (zero-length or absorbed
# spans); skipped when picking a line's first / last voiced character.
_NO_SOUND = set("、。，．,.!?！？「」『』（）()…・:：;；\"'“”‘’-—–~〜 \t\n")


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


def forced_align(
    audio_path: Path,
    texts: list[str],
    session: requests.Session,
    label: str = "",
) -> dict:
    """POST one paragraph's audio and text to /v1/forced-alignment.

    Returns the raw response (``characters`` / ``words`` / ``loss``); read it
    with ``alignment_marks``. Retries the same transient statuses as
    ``_post_dialogue``; anything else is a configuration problem and raises.
    """
    url = f"{API_BASE}/forced-alignment"
    headers = {"xi-api-key": _api_key()}
    text = "\n".join(texts)
    for attempt in range(1, MAX_HTTP_RETRIES + 1):
        with open(audio_path, "rb") as fh:
            r = session.post(
                url, headers=headers, timeout=180,
                files={"file": (audio_path.name, fh, "audio/mpeg")},
                data={"text": text},
            )
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
                f"⟳ ElevenLabs alignment retry {attempt}/{MAX_HTTP_RETRIES} in "
                f"{delay:.1f}s (HTTP {r.status_code} {code}) {label}"
            )
            time.sleep(delay)
            continue
        raise ElevenLabsError(
            f"ElevenLabs forced-alignment HTTP {r.status_code} {code}: {message} {label}"
        )
    raise ElevenLabsError("unreachable")  # pragma: no cover


def alignment_marks(fa: dict, texts: list[str]) -> list[tuple[int, int]]:
    """Per line: (start of its first voiced character, start of its last), ms.

    Characters are matched positionally against the text that was sent
    (``"\n".join(texts)``). A mismatch means the service normalised the text,
    so the marks would describe something else — that raises rather than
    guessing. Punctuation and whitespace carry no sound (see ``_NO_SOUND``).
    """
    chars = fa.get("characters") or []
    joined = "\n".join(texts)
    got = "".join(str(c.get("text", "")) for c in chars)
    if got != joined:
        at = next((i for i, (a, b) in enumerate(zip(got, joined)) if a != b), min(len(got), len(joined)))
        raise ElevenLabsError(
            f"forced alignment returned {len(got)} characters for {len(joined)} "
            f"sent; first difference at {at}: {got[at:at + 12]!r} vs {joined[at:at + 12]!r}"
        )
    marks: list[tuple[int, int]] = []
    pos = 0
    for k, text in enumerate(texts):
        voiced = [c for c in chars[pos:pos + len(text)] if c["text"] not in _NO_SOUND]
        if not voiced:
            raise ElevenLabsError(f"line {k} has no voiced characters: {text!r}")
        marks.append((
            int(round(float(voiced[0]["start"]) * 1000)),
            int(round(float(voiced[-1]["start"]) * 1000)),
        ))
        pos += len(text) + 1
    for k in range(1, len(marks)):
        if marks[k][0] < marks[k - 1][1]:
            raise ElevenLabsError(
                f"alignment out of order: line {k} starts at {marks[k][0]} ms, "
                f"before line {k - 1}'s last character at {marks[k - 1][1]} ms"
            )
    return marks


def cut_paragraph(
    audio: AudioSegment,
    marks: list[tuple[int, int]],
) -> list[AudioSegment]:
    """Cut one paragraph into per-line clips at the aligned inter-line pauses.

    ``marks`` are ``alignment_marks``: per line, the start of its first and of
    its last voiced character. The seam between line k and k+1 is the longest
    silent run (>= MIN_PAUSE_MS) that begins once k's last character has begun
    and before k+1's first character begins. Line k ends EDGE_PAD_MS into that
    run and line k+1 starts EDGE_PAD_MS before it ends, so every clip edge is
    silent by construction and the pads only ever eat silence. The paragraph
    edges use the leading / trailing silence the same way. A seam interval with
    no silent run raises ElevenLabsError — the lines were run together.
    """
    mask = sound_mask(audio)
    n = len(mask)
    if n == 0 or not mask.any():
        raise ElevenLabsError("paragraph audio is empty or silent")
    if len(marks) == 0:
        raise ElevenLabsError("no lines to cut")
    runs = silent_runs(mask)
    pad = EDGE_PAD_MS // FRAME_MS
    slack = ALIGN_SLACK_MS // FRAME_MS
    first_sound = int(mask.argmax())
    last_sound = n - 1 - int(mask[::-1].argmax())

    seams: list[tuple[int, int]] = []
    for k in range(len(marks) - 1):
        lo = marks[k][1] // FRAME_MS - slack        # k's last character has begun
        hi = marks[k + 1][0] // FRAME_MS + slack    # k+1's first character begins
        cands = [(a, b) for (a, b) in runs if lo <= a < hi and a > 0 and b < n - 1]
        if not cands:
            raise ElevenLabsError(
                f"no pause >= {MIN_PAUSE_MS} ms between lines {k} and {k + 1} "
                f"(alignment puts the gap at {marks[k][1]}–{marks[k + 1][0]} ms)"
            )
        seams.append(max(cands, key=lambda r: r[1] - r[0]))
    for k in range(1, len(seams)):
        if seams[k][0] <= seams[k - 1][0]:
            raise ElevenLabsError(f"lines {k} and {k + 1} share a single pause")

    starts = [max(0, first_sound - pad)]
    ends: list[int] = []
    for (a, b) in seams:
        ends.append(min(a + pad, b))
        starts.append(max(b - pad + 1, a + 1))
    ends.append(min(n, last_sound + 1 + pad))

    clips: list[AudioSegment] = []
    for k, (sf, ef) in enumerate(zip(starts, ends)):
        if ef <= sf:
            raise ElevenLabsError(f"cut collapsed at line {k}: frames {sf}–{ef}")
        if (sf > 0 and mask[sf]) or (ef < n and mask[ef - 1]):
            raise ElevenLabsError(f"cut of line {k} is not in silence")  # pragma: no cover
        first_char, last_char = marks[k]
        if first_char < sf * FRAME_MS - ALIGN_SLACK_MS or last_char > ef * FRAME_MS:
            raise ElevenLabsError(  # pragma: no cover — excluded by the seam intervals
                f"clip of line {k} ({sf * FRAME_MS}–{ef * FRAME_MS} ms) does not "
                f"contain its own text ({first_char}–{last_char} ms)"
            )
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


# The forced alignment is paid for per minute of audio and describes exactly
# one audio file, so it lives beside that file as ``<key>.fa.json`` (the raw
# service response). A cached paragraph without one is aligned on first use.
def alignment_load(key: str) -> dict | None:
    f = CACHE_DIR / f"{key}.fa.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def alignment_store(key: str, fa: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{key}.fa.json").write_text(json.dumps(fa, ensure_ascii=False))
    except Exception as e:  # a cache problem must never fail a run
        logger.warning(f"ElevenLabs alignment cache write failed: {e}")


# A rejected attempt is thrown away by design — bad audio must never reach the
# cache. But that left nothing to diagnose WITH: on 2026-09-12 five paragraphs
# were rejected, and by the time anyone looked, the audio was gone (the temp file
# is overwritten by the retry) and the model's alignment with it. The cause had
# to be reconstructed from the paragraphs that SUCCEEDED, which is how a wrong
# explanation got proposed first. So the rejected attempt is kept now.
#
# Kept by default, not behind a flag: these failures are unpredictable, and an
# env var you have to set in advance is useless the first time one happens.
# The forced alignment is the part that matters most — the cut is derived from
# it — and ``voice_segments`` is kept alongside for comparison.
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
    alignment: dict | None = None,
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
                    "forced_alignment": alignment,
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

    def _is_cached(chunk: _Chunk) -> bool:
        k = cache_key(chunk, model_id, output_format, stability)
        return (CACHE_DIR / f"{k}.mp3").exists() and (CACHE_DIR / f"{k}.json").exists()

    chunks = plan_chunks(texts, voice_ids, max_lines, max_chars, cached=_is_cached)
    n_cached = sum(1 for c in chunks if _is_cached(c))
    fresh_chars = sum(len(t) for c in chunks if not _is_cached(c) for t in c.texts)
    logger.info(
        f"  ElevenLabs {model_id}: {len(texts)} lines in {len(chunks)} paragraph "
        f"request(s), concurrency {concurrency}; {n_cached} cached, "
        f"{len(chunks) - n_cached} to synthesise (~{fresh_chars} credits)"
    )

    results: dict[int, list[AudioSegment]] = {}
    session = requests.Session()

    def _align(audio_path: Path, chunk: _Chunk, key: str | None) -> tuple[dict, list[tuple[int, int]]]:
        """Alignment for this audio: from the cache sidecar when ``key`` is given
        and one exists, else one paid call (~18 credits / minute of audio)."""
        fa = alignment_load(key) if key else None
        label = f"(lines {chunk.indices[0]}–{chunk.indices[-1]})"
        if fa is None:
            secs = len(AudioSegment.from_file(audio_path, format="mp3")) / 1000
            logger.info(f"  aligning paragraph audio {label}: {secs:.0f} s, ~{secs * 0.3:.0f} credits")
            fa = forced_align(audio_path, chunk.texts, session, label)
        return fa, alignment_marks(fa, chunk.texts)

    def _provenance(chunk: _Chunk, bounds, marks, fa, clips, extra: dict) -> dict:
        return {
            "lines": chunk.indices,
            "model_boundaries_ms": bounds,
            "alignment_marks_ms": marks,
            "alignment_loss": fa.get("loss"),
            "clip_ms": [len(c) for c in clips],
            **extra,
        }

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
                audio.export(raw, format="mp3")
                fa, marks = _align(raw, chunk, key)
                results[ci] = cut_paragraph(audio, marks)
                alignment_store(key, fa)
                (work_dir / f"el_para_{ci:03d}.json").write_text(json.dumps(
                    _provenance(chunk, bounds, marks, fa, results[ci], {"cached": True}),
                    ensure_ascii=False))
                logger.info(f"  paragraph {ci} (lines {chunk.indices[0]}–{chunk.indices[-1]}) from cache, 0 synthesis credits")
                return
            except ElevenLabsError as e:
                logger.warning(f"  cached paragraph {ci} no longer cuts ({e}); regenerating")

        for attempt in range(1, MAX_REGENERATE + 2):
            resp = _post_dialogue(chunk, model_id, output_format, stability, seed, session)
            raw.write_bytes(base64.b64decode(resp["audio_base64"]))
            fa: dict | None = None
            try:
                bounds = boundaries_from_response(resp, chunk)
                audio = AudioSegment.from_file(raw, format="mp3")
                fa, marks = _align(raw, chunk, None)
                clips = cut_paragraph(audio, marks)
                cache_store(key, raw, bounds, chunk)
                alignment_store(key, fa)
            except ElevenLabsError as e:
                last_err = e
                kept = reject_store(raw, resp, chunk, ci, attempt, seed, e, alignment=fa)
                logger.warning(
                    f"⟳ ElevenLabs paragraph {ci} (lines "
                    f"{chunk.indices[0]}–{chunk.indices[-1]}) failed boundary "
                    f"verification, regenerating ({attempt}/{MAX_REGENERATE + 1}): {e}"
                    + (f"\n      rejected attempt kept at {kept}" if kept else "")
                )
                seed = random.randint(0, 2**31 - 1)
                continue
            # Provenance: which alignment produced which clip.
            (work_dir / f"el_para_{ci:03d}.json").write_text(json.dumps(
                _provenance(chunk, bounds, marks, fa, clips,
                            {"seed": seed, "character_cost": resp.get("character_cost")}),
                ensure_ascii=False))
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

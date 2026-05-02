"""
Transcription server: FastAPI + faster-whisper + Silero VAD.

General-purpose LRC generation version:
- Lazy model loading
- Single model resident at a time
- Optional initial_prompt from frontend
- word_timestamps=True
- Rebuilds LRC lines with general sentence-boundary rules
- mode=default | learning  (learning uses tighter line limits for echo-loop practice)
- transcription_lock: only one job transcribes at a time
- queue.Queue worker thread keeps the asyncio loop responsive
"""

import asyncio
import logging
import os
import queue
import re
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from faster_whisper import WhisperModel
from sse_starlette.sse import EventSourceResponse


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("transcribe")


# ---------- Config ----------

IDLE_RELEASE_SECONDS = 10 * 60
JOB_RETENTION_SECONDS = 60 * 60

DEVICE = "cuda"
COMPUTE_TYPE = "int8_float16"

# Tunable subtitle segmentation parameters.
SOFT_GAP_SECONDS = 0.75
LONG_GAP_SECONDS = 3.0

# Default mode: general listening (YouTube, podcasts, lectures, game commentary).
MAX_LINE_DURATION_DEFAULT = 6.8
MAX_LINE_CHARS_EN_DEFAULT = 95
MAX_LINE_CHARS_JA_DEFAULT = 42

# Learning mode: tighter lines for echo-loop / shadowing practice.
MAX_LINE_DURATION_LEARNING = 4.5
MAX_LINE_CHARS_EN_LEARNING = 70
MAX_LINE_CHARS_JA_LEARNING = 28

SHORT_TAIL_MAX_WORDS_EN = 7

# Reading-speed cap. ~17-20 CPS is the typical pro subtitle ceiling.
MAX_CPS_EN = 18.0

MODEL_REPOS = {
    ("en", "large-v3"): "Systran/faster-whisper-large-v3",
    ("ja", "kotoba-v2"): "kotoba-tech/kotoba-whisper-v2.0-faster",
}


@dataclass(frozen=True)
class SegmentationConfig:
    max_duration: float
    max_chars_en: int
    max_chars_ja: int


def get_seg_config(mode: str) -> SegmentationConfig:
    if mode == "learning":
        return SegmentationConfig(
            max_duration=MAX_LINE_DURATION_LEARNING,
            max_chars_en=MAX_LINE_CHARS_EN_LEARNING,
            max_chars_ja=MAX_LINE_CHARS_JA_LEARNING,
        )
    return SegmentationConfig(
        max_duration=MAX_LINE_DURATION_DEFAULT,
        max_chars_en=MAX_LINE_CHARS_EN_DEFAULT,
        max_chars_ja=MAX_LINE_CHARS_JA_DEFAULT,
    )


# ---------- Job state ----------

class JobStatus(str, Enum):
    PENDING = "pending"
    LOADING_MODEL = "loading_model"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Job:
    id: str
    lang: str
    model_key: str
    audio_path: str
    duration: float
    initial_prompt: Optional[str] = None
    mode: str = "default"

    status: JobStatus = JobStatus.PENDING
    progress: float = 0.0
    current_time: float = 0.0
    segments: list = field(default_factory=list)
    lrc: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    event_queue: asyncio.Queue = field(default_factory=asyncio.Queue)


# ---------- Model manager ----------

class ModelManager:
    def __init__(self):
        self.model: Optional[WhisperModel] = None
        self.current_key: Optional[tuple[str, str]] = None
        self.last_used: float = time.time()
        self.active_users: int = 0
        self.lock = asyncio.Lock()

    async def acquire(self, lang: str, model_choice: str) -> WhisperModel:
        async with self.lock:
            key = (lang, model_choice)

            if key not in MODEL_REPOS:
                raise ValueError(f"Unsupported model combination: {key}")

            if self.current_key != key or self.model is None:
                if self.model is not None:
                    log.info(f"Unloading {self.current_key}")
                    self._unload_locked()

                repo = MODEL_REPOS[key]
                log.info(f"Loading {repo} on {DEVICE} ({COMPUTE_TYPE})")

                self.model = WhisperModel(
                    repo,
                    device=DEVICE,
                    compute_type=COMPUTE_TYPE,
                )
                self.current_key = key
                log.info(f"Model {key} ready")

            self.active_users += 1
            self.last_used = time.time()
            return self.model

    async def release(self) -> None:
        async with self.lock:
            self.active_users = max(self.active_users - 1, 0)
            self.last_used = time.time()

    def _unload_locked(self) -> None:
        if self.model is not None:
            del self.model

        self.model = None
        self.current_key = None

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()

    async def maybe_release(self) -> None:
        async with self.lock:
            if self.model is None or self.active_users:
                return

            if time.time() - self.last_used > IDLE_RELEASE_SECONDS:
                log.info(f"Idle timeout, releasing {self.current_key}")
                self._unload_locked()

    async def force_release(self) -> None:
        async with self.lock:
            if self.active_users:
                raise RuntimeError(
                    "Cannot release model while transcription is active"
                )

            if self.model is not None:
                log.info(f"Force release {self.current_key}")
                self._unload_locked()


model_mgr = ModelManager()
transcription_lock = asyncio.Lock()
jobs: dict[str, Job] = {}


# ---------- Background tasks ----------

async def idle_releaser():
    while True:
        await asyncio.sleep(60)
        try:
            await model_mgr.maybe_release()
        except Exception as e:
            log.exception(f"Idle releaser error: {e}")


async def job_cleaner():
    while True:
        await asyncio.sleep(300)

        now = time.time()
        to_delete = [
            jid for jid, j in jobs.items()
            if j.finished_at and now - j.finished_at > JOB_RETENTION_SECONDS
        ]

        for jid in to_delete:
            jobs.pop(jid, None)

        if to_delete:
            log.info(f"Cleaned {len(to_delete)} expired jobs")


# ---------- Audio probing ----------

def probe_duration(path: str) -> float:
    import subprocess

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    return float(result.stdout.strip())


# ---------- LRC helpers ----------

EN_SENTENCE_END = re.compile(r'[.!?]["\']?$')
JA_SENTENCE_END = re.compile(r'[。！？]["」』）)]?$')

EN_WEAK_END_WORDS = {
    # Articles / determiners
    "the", "a", "an", "this", "these", "those",

    # Prepositions
    "to", "of", "in", "on", "at", "for", "with",
    "as", "from", "by", "about", "towards", "toward",
    "onto", "into", "beneath", "inside", "outside",
    "through", "throughout", "around", "between", "under", "over",

    # Conjunctions / clause connectors
    "and", "or", "but", "if", "that", "because", "although", "while",
    "when", "where", "which", "who", "whom", "whose",

    # Be verbs / auxiliaries / modals
    "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did",
    "have", "has", "had",
    "can", "could", "would", "should", "will", "may", "might", "must",

    # Possessives / pronouns that often need a noun or complement
    "his", "her", "their", "our", "your", "my", "its",

    # Degree words / modifiers
    "very", "really", "quite", "fairly", "slightly", "much", "more", "most",
}

EN_TAIL_START_WORDS = {
    "to", "of", "at", "in", "on", "for", "with", "from", "by",
    "into", "onto", "inside", "outside", "beneath", "under", "over",
    "through", "throughout", "around", "between",
}

# Japanese particles that almost never make a good line ending.
# Order matters only insofar as multi-char particles must come first
# if we ever switch to startswith-style matching; endswith handles them fine.
JA_WEAK_END_PARTICLES = (
    # multi-char (compound particles / clause connectors)
    "から", "まで", "より", "けど", "けれど", "ので", "のに", "たり",
    "だけ", "しか", "ばかり", "でも", "こそ",
    # single-char (case / topic / adverbial)
    "は", "が", "を", "に", "で", "と", "へ", "の", "や", "か",
    "も", "し", "て",
)

# て-form / で-form: 行って / 飲んで / 乗って — connective, not a line break.
JA_TE_DE_FORM = re.compile(r"[っん]?[てで]$")


def format_lrc_timestamp(seconds: float) -> str:
    minutes = int(seconds // 60)
    secs = seconds - minutes * 60
    return f"[{minutes:02d}:{secs:05.2f}]"


def normalize_lrc_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text


def get_first_word(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"^[^\w'-]+", "", text)
    parts = text.split()
    return parts[0] if parts else ""


def get_last_word(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w'-]+$", "", text)
    parts = text.split()
    return parts[-1] if parts else ""


def has_sentence_end(text: str, lang: str) -> bool:
    text = normalize_lrc_text(text)

    if lang == "en":
        return bool(EN_SENTENCE_END.search(text))

    if lang == "ja":
        return bool(JA_SENTENCE_END.search(text))

    return False


def is_weak_line_end(text: str, lang: str) -> bool:
    """
    A line ending with a function word / connective is usually not a good
    subtitle boundary. Examples:
        EN:  at his / of the / because / while / looking very
        JA:  私は / 学校に / 食べて / 行くから
    """
    text = normalize_lrc_text(text)

    if not text:
        return True

    # Sentence-ending punctuation makes even a function word like "but..."
    # a complete boundary, not an incomplete weak tail.
    if has_sentence_end(text, lang):
        return False

    if lang == "en":
        last_word = get_last_word(text)

        if last_word in EN_WEAK_END_WORDS:
            return True

        # Possessive 's usually expects a noun after, e.g. "the company's".
        if re.search(r"\b\w+'s$", text.lower()):
            return True

        return False

    if lang == "ja":
        # Strip trailing whitespace already done by normalize. JA has no spaces.
        for particle in JA_WEAK_END_PARTICLES:
            if text.endswith(particle):
                return True

        if JA_TE_DE_FORM.search(text):
            return True

        return False

    return False


def is_too_short_fragment(text: str, lang: str) -> bool:
    """
    Detect obvious subtitle fragments.
    Conservative on purpose — short complete sentences ("It works.") still pass.
    """
    text = normalize_lrc_text(text)

    if not text:
        return True

    if lang == "en":
        words = text.split()

        if len(words) <= 3 and not has_sentence_end(text, lang):
            return True

        if len(text) < 14 and not has_sentence_end(text, lang):
            return True

    if lang == "ja":
        if len(text) <= 5 and not has_sentence_end(text, lang):
            return True

    return False


def is_probably_hallucination(text: str, start: float, end: float) -> bool:
    """
    Conservative hallucination filter.
    Avoid being too aggressive — short valid captions must not be lost.
    """
    cleaned = normalize_lrc_text(text).lower()
    duration = max(end - start, 0.01)

    if not cleaned:
        return True

    if cleaned in {"uh", "um", "hmm", "mmm"}:
        return True

    # Suspicious one-word micro captions often come from background audio.
    if cleaned in {"true", "right", "okay", "ok"} and duration < 1.5:
        return True

    bad_phrases = {
        "thank you for watching",
        "thanks for watching",
        "please subscribe",
        "subscribe to my channel",
    }

    if cleaned in bad_phrases:
        return True

    words = cleaned.split()

    if len(words) >= 8:
        unique_ratio = len(set(words)) / len(words)
        if unique_ratio < 0.35:
            return True

    # Very short text lasting a long time is usually suspicious.
    if len(words) <= 3 and duration >= 8.0:
        return True

    return False


def should_cut_line(
    text: str,
    lang: str,
    duration: float,
    char_count: int,
    gap: float,
    config: SegmentationConfig,
) -> bool:
    text = normalize_lrc_text(text)

    if not text:
        return False

    # Strong punctuation is the best signal.
    if has_sentence_end(text, lang):
        return True

    weak = is_weak_line_end(text, lang)

    # A pause can end a line, but avoid ending at a weak word.
    if gap >= SOFT_GAP_SECONDS and char_count >= 25 and not weak:
        return True

    # Reading-speed cap: long line crammed into short duration.
    if lang == "en" and char_count >= 30 and duration > 0:
        cps = char_count / duration
        if cps >= MAX_CPS_EN and not weak:
            return True

    # Avoid overly long subtitle duration / text length.
    if lang == "en":
        if duration >= config.max_duration and not weak:
            return True
        if char_count >= config.max_chars_en and not weak:
            return True

    if lang == "ja":
        # JA still respects weak ends here — it matters more, not less.
        if duration >= config.max_duration and not weak:
            return True
        if char_count >= config.max_chars_ja and not weak:
            return True

    return False


def append_rebuilt_segment(
    rebuilt: list,
    start: float,
    end: float,
    text: str,
    lang: str,
):
    text = normalize_lrc_text(text)

    if not text:
        return

    if is_probably_hallucination(text, start, end):
        return

    if is_too_short_fragment(text, lang):
        return

    rebuilt.append({
        "start": start,
        "end": end,
        "text": text,
    })


def rebuild_segments_by_words(
    raw_segments: list,
    lang: str,
    config: SegmentationConfig,
) -> list:
    """
    Rebuild faster-whisper segments into LRC-friendly lines.

    General strategy:
    - Use word timestamps.
    - Cut by punctuation, pause, max duration, max text length, CPS.
    - Avoid breaking at weak function-word endings.
    - Hold "incomplete tail" fragments and merge them into the next line.
    - Flush any pending fragment at end-of-stream so we never silently drop it.
    """
    rebuilt = []

    current_words: list[str] = []
    current_start: Optional[float] = None
    current_end: Optional[float] = None
    last_word_end: Optional[float] = None

    pending_fragment: Optional[dict] = None

    def flush_current():
        nonlocal current_words
        nonlocal current_start
        nonlocal current_end
        nonlocal last_word_end
        nonlocal pending_fragment

        if not current_words or current_start is None or current_end is None:
            return

        text = normalize_lrc_text("".join(current_words))

        if not text:
            current_words = []
            current_start = None
            current_end = None
            last_word_end = None
            return

        if is_too_short_fragment(text, lang) or is_weak_line_end(text, lang):
            pending_fragment = {
                "start": current_start,
                "end": current_end,
                "text": text,
            }
        else:
            append_rebuilt_segment(
                rebuilt=rebuilt,
                start=current_start,
                end=current_end,
                text=text,
                lang=lang,
            )

        current_words = []
        current_start = None
        current_end = None
        last_word_end = None

    for seg in raw_segments:
        words = seg.get("words") or []

        # Fallback if word timestamps are missing for this segment.
        if not words:
            text = normalize_lrc_text(seg.get("text", ""))
            start = float(seg.get("start", 0.0))
            end = float(seg.get("end", start))
            append_rebuilt_segment(rebuilt, start, end, text, lang)
            continue

        for w in words:
            word_text = w.get("word", "")
            word_start = w.get("start")
            word_end = w.get("end")

            if word_start is None or word_end is None:
                continue

            word_start = float(word_start)
            word_end = float(word_end)

            # Merge pending short fragment into the next line.
            if pending_fragment is not None and current_start is None:
                current_words = [pending_fragment["text"], word_text]
                current_start = pending_fragment["start"]
                current_end = word_end
                last_word_end = word_end
                pending_fragment = None
                continue

            if current_start is None:
                current_start = word_start

            gap = 0.0
            if last_word_end is not None:
                gap = word_start - last_word_end

            # Very long silence: flush current line first.
            if gap >= LONG_GAP_SECONDS and current_words:
                flush_current()

                if pending_fragment is not None:
                    current_words = [pending_fragment["text"], word_text]
                    current_start = pending_fragment["start"]
                    current_end = word_end
                    last_word_end = word_end
                    pending_fragment = None
                    continue

                current_start = word_start

            current_words.append(word_text)
            current_end = word_end

            current_text = normalize_lrc_text("".join(current_words))
            duration = current_end - current_start
            char_count = len(current_text)

            if should_cut_line(
                text=current_text,
                lang=lang,
                duration=duration,
                char_count=char_count,
                gap=gap,
                config=config,
            ):
                flush_current()
            else:
                last_word_end = word_end

    flush_current()

    # Emit any leftover pending fragment so we never silently drop the tail.
    # If it really is too short (e.g. one filler word at end), the
    # hallucination + short-fragment filters inside append_rebuilt_segment
    # will still reject it.
    if pending_fragment is not None:
        append_rebuilt_segment(
            rebuilt=rebuilt,
            start=pending_fragment["start"],
            end=pending_fragment["end"],
            text=pending_fragment["text"],
            lang=lang,
        )
        pending_fragment = None

    return rebuilt


def should_merge_with_previous(prev_text: str, current_text: str, lang: str) -> bool:
    """
    General short-tail merge rule (English only).
    Examples:
      prev: a clear look at his    / cur: face.
      prev: bears a resemblance    / cur: to another character.
      prev: buried beneath the rubble / cur: of Raccoon City.
    """
    if lang != "en":
        return False

    prev = normalize_lrc_text(prev_text)
    cur = normalize_lrc_text(current_text)

    if not prev or not cur:
        return False

    cur_words = cur.split()

    if len(cur_words) > SHORT_TAIL_MAX_WORDS_EN:
        return False

    first_word = get_first_word(cur)
    prev_last = get_last_word(prev)

    if first_word in EN_TAIL_START_WORDS:
        return True

    if (
        prev_last in EN_WEAK_END_WORDS
        and len(cur_words) <= 6
        and not has_sentence_end(prev, lang)
    ):
        return True

    if len(cur_words) <= 3 and not has_sentence_end(prev, lang):
        return True

    return False


def merge_short_tail_segments(segments: list, lang: str) -> list:
    if not segments:
        return []

    merged = []

    for seg in segments:
        if (
            merged
            and should_merge_with_previous(
                merged[-1]["text"],
                seg["text"],
                lang,
            )
        ):
            merged[-1]["text"] = normalize_lrc_text(
                merged[-1]["text"] + " " + seg["text"]
            )
            merged[-1]["end"] = seg["end"]
        else:
            merged.append(seg)

    return merged


def build_lrc(segments: list, lang: str, config: SegmentationConfig) -> str:
    rebuilt = rebuild_segments_by_words(segments, lang, config)
    rebuilt = merge_short_tail_segments(rebuilt, lang)

    lines = []

    for seg in rebuilt:
        ts = format_lrc_timestamp(seg["start"])
        text = seg["text"].strip()

        if text:
            lines.append(f"{ts}{text}")

    return "\n".join(lines) + "\n"


# ---------- Transcription core ----------

async def _set_failed(job: Job, exc: Exception) -> None:
    log.exception(f"Job {job.id} failed: {exc}")
    job.status = JobStatus.FAILED
    job.error = str(exc)
    job.finished_at = time.time()
    await job.event_queue.put({"event": "error", "data": str(exc)})


async def run_transcription(job: Job) -> None:
    acquired = False
    try:
        # Only one transcription runs at a time, so the model can't be
        # swapped out underneath an active job.
        async with transcription_lock:
            await job.event_queue.put({"event": "status", "data": "loading_model"})
            job.status = JobStatus.LOADING_MODEL

            model = await model_mgr.acquire(job.lang, job.model_key)
            acquired = True

            await job.event_queue.put({"event": "status", "data": "processing"})
            job.status = JobStatus.PROCESSING

            loop = asyncio.get_running_loop()
            segment_queue: queue.Queue[tuple[str, Any]] = queue.Queue()

            initial_prompt = (
                job.initial_prompt.strip() if job.initial_prompt else None
            )
            log.info(
                "Transcription options: "
                f"lang={job.lang}, model={job.model_key}, mode={job.mode}, "
                f"initial_prompt={'yes' if initial_prompt else 'no'}"
            )

            def transcribe_worker() -> None:
                try:
                    segments_iter, _info = model.transcribe(
                        job.audio_path,
                        language=job.lang,

                        vad_filter=True,
                        vad_parameters=dict(
                            min_silence_duration_ms=700,
                            speech_pad_ms=300,
                            threshold=0.35,
                        ),

                        beam_size=5,
                        word_timestamps=True,

                        # English benefits from previous-text context.
                        # Japanese (kotoba) hallucinates more with it.
                        condition_on_previous_text=(job.lang == "en"),

                        initial_prompt=initial_prompt,

                        # Conservative hallucination controls.
                        no_speech_threshold=0.6,
                        log_prob_threshold=-1.0,
                        compression_ratio_threshold=2.4,
                    )

                    for seg in segments_iter:
                        words = []
                        for w in (seg.words or []):
                            if w.start is None or w.end is None:
                                continue
                            words.append({
                                "start": float(w.start),
                                "end": float(w.end),
                                "word": w.word,
                            })

                        segment_queue.put((
                            "segment",
                            {
                                "start": float(seg.start),
                                "end": float(seg.end),
                                "text": seg.text,
                                "words": words,
                            },
                        ))

                    segment_queue.put(("done", None))

                except Exception as exc:
                    segment_queue.put(("error", exc))

            worker = loop.run_in_executor(None, transcribe_worker)

            while True:
                event_type, payload = await loop.run_in_executor(
                    None, segment_queue.get
                )

                if event_type == "done":
                    break
                if event_type == "error":
                    raise payload

                seg_dict = payload
                job.segments.append(seg_dict)
                job.current_time = seg_dict["end"]
                job.progress = (
                    min(seg_dict["end"] / job.duration, 1.0)
                    if job.duration
                    else 0.0
                )

                await job.event_queue.put({
                    "event": "progress",
                    "data": (
                        f"{seg_dict['end']:.2f}|{job.duration:.2f}|"
                        f"{seg_dict['text'].strip()}"
                    ),
                })

                await asyncio.sleep(0)

            # Make sure the worker is fully cleaned up before we exit
            # the transcription_lock critical section.
            await worker

            seg_config = get_seg_config(job.mode)
            job.lrc = build_lrc(job.segments, job.lang, seg_config)
            job.status = JobStatus.DONE
            job.finished_at = time.time()

            await job.event_queue.put({"event": "done", "data": "ok"})

    except Exception as exc:
        await _set_failed(job, exc)

    finally:
        if acquired:
            await model_mgr.release()

        try:
            if os.path.exists(job.audio_path):
                os.unlink(job.audio_path)

            tmp_dir = os.path.dirname(job.audio_path)
            if os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


# ---------- App lifecycle ----------

# Stale temp dir sweep: a process that gets SIGKILL'd / OOM-killed / power-cut
# leaves transcribe_* directories behind in $TMPDIR. The 6h cutoff is well
# beyond any realistic single-job duration, so we don't risk wiping a dir
# belonging to a job that's still running (single-process server, but the
# safety margin is cheap).
STALE_TEMP_DIR_CUTOFF_SECONDS = 6 * 60 * 60


def cleanup_stale_temp_dirs() -> None:
    tmp_root = Path(tempfile.gettempdir())
    cutoff = time.time() - STALE_TEMP_DIR_CUTOFF_SECONDS

    swept = 0
    for d in tmp_root.glob("transcribe_*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                swept += 1
        except Exception as exc:
            log.warning(f"Failed to inspect {d}: {exc}")

    if swept:
        log.info(f"Swept {swept} stale temp dirs from previous runs")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_stale_temp_dirs()

    log.info("Starting background tasks")

    t1 = asyncio.create_task(idle_releaser())
    t2 = asyncio.create_task(job_cleaner())

    try:
        yield
    finally:
        t1.cancel()
        t2.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)

        try:
            await model_mgr.force_release()
        except RuntimeError:
            log.warning("Server shutdown while transcription was active")


app = FastAPI(lifespan=lifespan)


# ---------- Endpoints ----------

@app.post("/jobs")
async def create_job(
    audio: UploadFile = File(...),
    lang: str = Form(...),
    model: str = Form("default"),
    initial_prompt: Optional[str] = Form(None),
    mode: str = Form("default"),
):
    if lang == "en":
        model_key = "large-v3" if model == "default" else model
    elif lang == "ja":
        model_key = "kotoba-v2"
    else:
        raise HTTPException(400, f"Unsupported lang: {lang}")

    if (lang, model_key) not in MODEL_REPOS:
        raise HTTPException(400, f"Unsupported model: {lang}/{model_key}")

    if mode not in ("default", "learning"):
        raise HTTPException(400, f"Unsupported mode: {mode}")

    cleaned_initial_prompt = (
        initial_prompt.strip() if initial_prompt and initial_prompt.strip() else None
    )

    tmp_dir = tempfile.mkdtemp(prefix="transcribe_")
    suffix = Path(audio.filename or "audio").suffix or ".bin"
    tmp_path = os.path.join(tmp_dir, f"input{suffix}")

    with open(tmp_path, "wb") as f:
        shutil.copyfileobj(audio.file, f)

    try:
        duration = probe_duration(tmp_path)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(400, f"Cannot probe audio: {e}")

    job = Job(
        id=str(uuid.uuid4()),
        lang=lang,
        model_key=model_key,
        audio_path=tmp_path,
        duration=duration,
        initial_prompt=cleaned_initial_prompt,
        mode=mode,
    )

    jobs[job.id] = job
    asyncio.create_task(run_transcription(job))

    log.info(
        f"Job {job.id} created: "
        f"{audio.filename} ({duration:.1f}s, {lang}/{model_key}, "
        f"mode={mode}, "
        f"initial_prompt={'yes' if cleaned_initial_prompt else 'no'})"
    )

    return {
        "job_id": job.id,
        "duration": duration,
    }


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: str):
    job = jobs.get(job_id)

    if not job:
        raise HTTPException(404, "Job not found")

    async def generator():
        yield {
            "event": "status",
            "data": job.status.value,
        }

        if job.status in (JobStatus.DONE, JobStatus.FAILED):
            yield {
                "event": job.status.value,
                "data": job.error or "ok",
            }
            return

        while True:
            try:
                evt = await asyncio.wait_for(job.event_queue.get(), timeout=30)
                yield evt

                if evt["event"] in ("done", "error"):
                    return

            except asyncio.TimeoutError:
                yield {
                    "event": "ping",
                    "data": "",
                }

    return EventSourceResponse(generator())


@app.get("/jobs/{job_id}/result")
async def job_result(job_id: str):
    job = jobs.get(job_id)

    if not job:
        raise HTTPException(404, "Job not found")

    if job.status == JobStatus.FAILED:
        raise HTTPException(500, f"Job failed: {job.error}")

    if job.status != JobStatus.DONE:
        raise HTTPException(409, f"Job not done: {job.status.value}")

    return {
        "job_id": job.id,
        "lrc": job.lrc,
        "duration": job.duration,
    }


@app.get("/jobs/{job_id}/segments")
async def job_segments(job_id: str):
    """
    Debug endpoint.
    Inspect raw faster-whisper segments and word timestamps.
    """
    job = jobs.get(job_id)

    if not job:
        raise HTTPException(404, "Job not found")

    return {
        "job_id": job.id,
        "status": job.status.value,
        "lang": job.lang,
        "model_key": job.model_key,
        "mode": job.mode,
        "duration": job.duration,
        "initial_prompt": job.initial_prompt,
        "segments": job.segments,
    }


@app.post("/release")
async def release():
    try:
        await model_mgr.force_release()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.get("/health")
async def health():
    return {
        "ok": True,
        "loaded_model": model_mgr.current_key,
        "active_jobs": sum(
            1 for j in jobs.values()
            if j.status in (
                JobStatus.PENDING,
                JobStatus.LOADING_MODEL,
                JobStatus.PROCESSING,
            )
        ),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )

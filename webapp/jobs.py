"""Job manager for the Echo Loop web UI.

Wraps the existing CLI pipeline. Each submitted job:

  1. Writes the pasted text to ``OUTPUT_DIR/<job_id>/<slug>.txt`` (the user's
     name, sanitised), so the output stem is meaningful and unique per run.
  2. Builds a ``config`` dict from ``config.yaml`` plus the request overrides.
  3. Calls ``run_text_mode`` / ``run_interview_mode`` from ``main.py`` — the same
     code path the CLI uses, so output lands in the job folder with a ``_tst``
     (or ``_tnt``/``_echo``) suffix. A dual (EN+JA) text job runs the pipeline
     twice from a trilingual script, emitting ``<slug>_en_*`` and ``<slug>_ja_*``.

A single background worker thread drains a FIFO queue. Running jobs serially is
deliberate: the pipeline relies on global folder-log handlers
(``attach_folder_log`` / ``detach_folder_log``) and serial execution also avoids
hammering the TTS backends.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import queue
import re
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from main import (
    load_config,
    run_text_mode,
    run_interview_mode,
    _apply_target_language_preset,
    _apply_interview_language_preset,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = os.environ.get("ECHO_CONFIG", str(PROJECT_ROOT / "config.yaml"))
OUTPUT_DIR = Path(os.environ.get("ECHO_OUTPUT_DIR", str(PROJECT_ROOT / "outputs"))).resolve()

# Optional retention. Both default to 0 = keep everything forever (no cleanup).
#   ECHO_RETENTION_DAYS — delete finished jobs older than N days
#   ECHO_MAX_JOBS       — keep only the newest N finished jobs
RETENTION_DAYS = float(os.environ.get("ECHO_RETENTION_DAYS", "0") or 0)
MAX_JOBS = int(os.environ.get("ECHO_MAX_JOBS", "0") or 0)

VALID_MODES = ("text", "interview")
VALID_ENGINES = ("google", "edge", "openai")
VALID_LANGS = ("ja", "en")
VALID_VARIANTS = ("full", "progressive", "shadow")

# Optional leading role marker on an interview line (e.g. "Q:", "Answer：").
# Used when splitting a trilingual interview script so the prefix can be
# re-attached to BOTH the English and Japanese single-language outputs.
_ROLE_PREFIX = re.compile(
    r"^\s*((?:Q|A|Question|Answer|Interviewer|Candidate|Interviewee)\s*[:：]\s*)(.*)$",
    re.IGNORECASE,
)

# File suffixes we treat as user-facing job outputs.
AUDIO_EXTS = (".m4a", ".mp3", ".wav", ".flac", ".ogg", ".aac")

# Characters unsafe in file names / rclone remote paths.
_SLUG_STRIP = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _slugify(name: str) -> str:
    """Turn a user-provided name into a safe file/folder stem.

    Keeps CJK and most printable characters, drops path separators and control
    chars, collapses whitespace, trims trailing dots/spaces, and caps the
    length. Raises ``ValueError`` if nothing usable remains so the API returns a
    400 instead of silently producing ``output_*`` files.
    """
    cleaned = _SLUG_STRIP.sub("", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned[:60].strip(" .")
    if not cleaned:
        raise ValueError("name is empty after sanitising")
    return cleaned


@dataclass
class Job:
    id: str
    mode: str
    title: str
    status: str = "queued"  # queued | running | done | error
    created_at: str = ""
    finished_at: str | None = None
    files: list[str] = field(default_factory=list)  # output file names (relative to job dir)
    error: str | None = None

    # Request payload kept so the job can be re-run / inspected. Not all of it is
    # surfaced to the UI.
    request: dict = field(default_factory=dict)

    @property
    def dir(self) -> Path:
        return OUTPUT_DIR / self.id

    def to_public(self) -> dict:
        """Trimmed view sent to the browser."""
        return {
            "id": self.id,
            "mode": self.mode,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "files": self.files,
            "error": self.error,
            "engine": self.request.get("engine"),
            "lang": self.request.get("lang"),
        }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._worker: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self._rebuild_from_disk()
        self._cleanup()
        self._worker = threading.Thread(
            target=self._worker_loop, name="echo-job-worker", daemon=True
        )
        self._worker.start()
        # Re-queue any jobs that were mid-flight when the server stopped.
        with self._lock:
            stale = [j.id for j in self._jobs.values() if j.status in ("queued", "running")]
        for job_id in stale:
            with self._lock:
                self._jobs[job_id].status = "queued"
                self._save(self._jobs[job_id])
            self._queue.put(job_id)

    def _rebuild_from_disk(self) -> None:
        if not OUTPUT_DIR.exists():
            return
        for child in OUTPUT_DIR.iterdir():
            meta = child / "job.json"
            if not meta.is_file():
                continue
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                job = Job(
                    id=data["id"],
                    mode=data.get("mode", "text"),
                    title=data.get("title", ""),
                    status=data.get("status", "done"),
                    created_at=data.get("created_at", ""),
                    finished_at=data.get("finished_at"),
                    files=data.get("files", []),
                    error=data.get("error"),
                    request=data.get("request", {}),
                )
                with self._lock:
                    self._jobs[job.id] = job
            except Exception:
                logger.warning("Could not load job metadata at %s", meta, exc_info=True)

    # -- public API --------------------------------------------------------

    def create(self, mode: str, content: str, request: dict) -> Job:
        if mode not in VALID_MODES:
            raise ValueError(f"unsupported mode: {mode}")
        content = (content or "").strip()
        if not content:
            raise ValueError("content is empty")

        name = (request.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        slug = _slugify(name)          # may raise ValueError -> 400
        request["slug"] = slug

        job_id = uuid.uuid4().hex[:12]
        job = Job(
            id=job_id,
            mode=mode,
            title=name,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            request=request,
        )
        job.dir.mkdir(parents=True, exist_ok=True)

        # The input filename becomes the output stem (slug_echo / slug_tst …),
        # which is what makes each run's synced files uniquely named.
        (job.dir / f"{slug}.txt").write_text(content, encoding="utf-8")

        with self._lock:
            self._jobs[job_id] = job
            self._save(job)
        self._queue.put(job_id)
        return job

    def list(self) -> list[dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return [j.to_public() for j in jobs]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def status_payload(self, job_id: str) -> dict | None:
        job = self.get(job_id)
        if job is None:
            return None
        payload = job.to_public()
        payload["log_tail"] = _tail_folder_log(job.dir) if job.status == "running" else []
        return payload

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        import shutil

        shutil.rmtree(job.dir, ignore_errors=True)
        return True

    def resolve_file(self, job_id: str, name: str) -> Path | None:
        """Return a safe absolute path for a file inside a job dir, or None."""
        job = self.get(job_id)
        if job is None:
            return None
        # Prevent path traversal: name must be a bare file name.
        candidate = (job.dir / name).resolve()
        try:
            candidate.relative_to(job.dir.resolve())
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        return candidate

    def _cleanup(self) -> None:
        """Apply optional retention. No-op unless ECHO_RETENTION_DAYS / ECHO_MAX_JOBS set.

        Only finished (done/error) jobs are eligible — never deletes a queued or
        running job.
        """
        if RETENTION_DAYS <= 0 and MAX_JOBS <= 0:
            return

        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        finished = [j for j in jobs if j.status in ("done", "error")]

        doomed: set[str] = set()
        if RETENTION_DAYS > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
            for j in finished:
                try:
                    created = datetime.fromisoformat(j.created_at)
                except ValueError:
                    continue
                if created < cutoff:
                    doomed.add(j.id)
        if MAX_JOBS > 0:
            for j in finished[MAX_JOBS:]:
                doomed.add(j.id)

        for job_id in doomed:
            logger.info("Retention cleanup: removing job %s", job_id)
            self.delete(job_id)

    # -- worker ------------------------------------------------------------

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._process(job_id)
            except Exception:
                logger.exception("Worker crashed processing job %s", job_id)
            finally:
                self._queue.task_done()

    def _process(self, job_id: str) -> None:
        job = self.get(job_id)
        if job is None:
            return

        self._update(job, status="running")
        start = time.monotonic()
        try:
            config = self._build_config(job)
            slug = job.request.get("slug") or "output"
            input_path = job.dir / f"{slug}.txt"

            if job.mode == "interview":
                if job.request.get("dual"):
                    self._run_dual(job, config, input_path, slug, mode="interview")
                else:
                    config["paths"]["interview"] = str(input_path)
                    run_interview_mode(config)
            elif job.request.get("dual"):
                self._run_dual(job, config, input_path, slug, mode="text")
            else:
                config["paths"]["text"] = str(input_path)
                run_text_mode(config)

            files = _discover_outputs(job.dir, config)
            if not files:
                raise RuntimeError("pipeline produced no audio output")
            elapsed = time.monotonic() - start
            logger.info("Job %s finished in %.1fs (%d files)", job.id, elapsed, len(files))
            self._update(
                job,
                status="done",
                files=files,
                finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                error=None,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced to the UI
            tb = traceback.format_exc()
            logger.error("Job %s failed: %s", job.id, exc)
            (job.dir / "error.log").write_text(tb, encoding="utf-8")
            self._update(
                job,
                status="error",
                error=str(exc),
                finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        self._cleanup()

    # -- config building ---------------------------------------------------

    def _build_config(self, job: Job) -> dict:
        """Start from config.yaml defaults, then apply request overrides.

        Mirrors the precedence rules in ``main.apply_cli_overrides``.
        """
        req = job.request
        config = load_config(CONFIG_PATH)

        engine = req.get("engine")
        if engine in VALID_ENGINES:
            config["tts"]["engine"] = engine

        lang = req.get("lang")
        if lang in VALID_LANGS:
            _apply_target_language_preset(config, lang)
            _apply_interview_language_preset(config, lang)

        loop = req.get("loop") or {}
        variant = loop.get("variant")
        if variant in VALID_VARIANTS:
            config["loop"]["variant"] = variant
            config["loop"]["tnt_repeats"] = None
            config["loop"]["tst_repeats"] = None
        if loop.get("tnt") is not None:
            config["loop"]["tnt_repeats"] = max(0, int(loop["tnt"]))
        if loop.get("tst") is not None:
            config["loop"]["tst_repeats"] = max(0, int(loop["tst"]))
        if loop.get("split") is not None:
            config["loop"]["split_outputs"] = bool(loop["split"])

        timing = req.get("timing") or {}
        for key in ("after_first_target", "after_native", "after_second_target"):
            if timing.get(key) is not None:
                config["timing"][key] = float(timing[key])

        if req.get("gain") is not None:
            config["tts"]["gain"] = float(req["gain"])
            config["tts"]["normalize"] = None

        # The chosen name doubles as the sync folder label, so a run's files
        # (incl. the EN + JA outputs of a dual run) land in YYYY-MM-DD_<name>.
        slug = req.get("slug")
        if slug:
            config.setdefault("sync", {})
            config["sync"]["label"] = slug

        return config

    def _run_dual(
        self, job: Job, base_config: dict, input_path: Path, slug: str, mode: str = "text"
    ) -> None:
        """Generate both English and Japanese audio from one trilingual script.

        Input lines are ``English|||Japanese|||Chinese``. Each is split into two
        2-column files — ``<slug>_en.txt`` (en|||zh) and ``<slug>_ja.txt``
        (ja|||zh) — and the pipeline for ``mode`` runs once per language with that
        language's voice preset applied. If no trilingual line is found we fall
        back to a single run on the original content.

        In interview mode the input is ``Q:en|||ja|||zh``; the leading role
        marker (``Q:`` / ``A:`` …) is preserved on BOTH single-language files so
        the interview parser still recognises the role on the Japanese side.
        """
        interview = mode == "interview"
        runner = run_interview_mode if interview else run_text_mode
        apply_preset = (
            _apply_interview_language_preset if interview else _apply_target_language_preset
        )
        path_key = "interview" if interview else "text"

        raw = input_path.read_text(encoding="utf-8")
        en_lines: list[str] = []
        ja_lines: list[str] = []
        found = False
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                en_lines.append(line)
                ja_lines.append(line)
                continue
            parts = [p.strip() for p in stripped.split("|||")]
            if len(parts) >= 3 and parts[0] and parts[1] and parts[2]:
                en, ja, zh = parts[0], parts[1], parts[2]
                if interview:
                    # Keep the Q:/A: role marker on the Japanese line too.
                    match = _ROLE_PREFIX.match(en)
                    if match:
                        ja = f"{match.group(1)}{ja}"
                en_lines.append(f"{en}|||{zh}")
                ja_lines.append(f"{ja}|||{zh}")
                found = True
            else:
                # Not trilingual — pass the line through unchanged to both runs.
                en_lines.append(line)
                ja_lines.append(line)

        if not found:
            base_config["paths"][path_key] = str(input_path)
            runner(base_config)
            return

        en_path = job.dir / f"{slug}_en.txt"
        ja_path = job.dir / f"{slug}_ja.txt"
        en_path.write_text("\n".join(en_lines) + "\n", encoding="utf-8")
        ja_path.write_text("\n".join(ja_lines) + "\n", encoding="utf-8")

        for lang, path in (("en", en_path), ("ja", ja_path)):
            cfg = copy.deepcopy(base_config)
            apply_preset(cfg, lang)
            cfg["paths"][path_key] = str(path)
            runner(cfg)

    # -- internal helpers --------------------------------------------------

    def _update(self, job: Job, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(job, key, value)
            self._save(job)

    def _save(self, job: Job) -> None:
        """Persist job metadata. Caller must hold the lock."""
        data = asdict(job)
        try:
            (job.dir).mkdir(parents=True, exist_ok=True)
            (job.dir / "job.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            logger.warning("Could not persist job %s", job.id, exc_info=True)


def _discover_outputs(job_dir: Path, config: dict) -> list[str]:
    """List generated audio + matching LRC files, audio first."""
    audio: list[str] = []
    lrc: list[str] = []
    for child in sorted(job_dir.iterdir()):
        if not child.is_file():
            continue
        suffix = child.suffix.lower()
        if suffix in AUDIO_EXTS:
            audio.append(child.name)
        elif suffix == ".lrc":
            lrc.append(child.name)
    return audio + lrc


def _tail_folder_log(job_dir: Path, n: int = 12) -> list[str]:
    """Return the last ``n`` lines of the per-folder run log, if present."""
    logs = sorted(job_dir.glob("echo_run_*.log"))
    if not logs:
        return []
    try:
        lines = logs[-1].read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    # Strip the leading "timestamp [LEVEL] " prefix for compactness.
    cleaned = []
    for line in lines[-n:]:
        if "] " in line:
            line = line.split("] ", 1)[1]
        cleaned.append(line)
    return cleaned


# Module-level singleton used by server.py
manager = JobManager()

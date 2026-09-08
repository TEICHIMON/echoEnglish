#!/usr/bin/env python
"""One-off spike for docs/elevenlabs_plan.md Phase 0. Does NOT touch audio/tts_generator.py.

Usage (run with the echo_env python from the repo root):
  python scripts/elevenlabs_spike.py probe          # subscription + models, no credits
  python scripts/elevenlabs_spike.py add-voices     # add JA voices from shared library (3 free slots)
  python scripts/elevenlabs_spike.py validate       # cheap 1-char probes: speed range, v3 voice_settings, stitching fields
  python scripts/elevenlabs_spike.py concurrency    # 3 parallel tiny requests on the free tier (limit 2)
  python scripts/elevenlabs_spike.py compare CONFIG [--lines N] [--voice ID]
  python scripts/elevenlabs_spike.py timestamps     # v3 via /with-timestamps on a paragraph
  python scripts/elevenlabs_spike.py google         # Chirp3-HD baseline, same lines
  python scripts/elevenlabs_spike.py report         # markdown tables from stats.jsonl

Every synth call is logged to outputs/elevenlabs_spike/calls.jsonl with the
subscription character_count before/after, so credit cost is measured, not guessed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from parser.lrc_parser import strip_furigana  # noqa: E402

BASE = "https://api.elevenlabs.io/v1"
H = {"xi-api-key": os.environ.get("ELEVENLABS_API_KEY", "")}
OUT = ROOT / "outputs" / "elevenlabs_spike"
OUT.mkdir(parents=True, exist_ok=True)
CALLS = OUT / "calls.jsonl"
STATS = OUT / "stats.jsonl"
MATERIAL = ROOT / "outputs" / "e0b8f339eaba" / "moneyForward 面试准备_ja.txt"
SEED = 4242
CONCURRENCY = 2

# Japanese voices from the shared library (Phase 0 candidates). owner id is needed to add them.
JA_VOICES = {
    "otani": ("3JDquces8E8bkmvbh6Bc", "71601d75e93fe200443db77c032bb8757638b7881c4b6949a1f8e88ba960ff2e"),   # narration, most cloned
    "asahi": ("GKDaBI8TKSBJVhsCLD6n", "b2ad93fb9e7f6c33e86e8d513b321eb12b332eb7c7411ff162a9736efe0f4129"),   # conversational young male
    "morioki": ("8EkOjt4xTPGMclNlh1pk", "406d402b53d85a3e1a24d894719ae131d4563c800acf5f78397d5842f6419a1f"), # conversational female
}
# Premade (English) voices: the only ones the free tier may call via the API
# (library voices return 402 paid_plan_required). Accent is baked in, so JA output
# from these is NOT representative -- use them only for voice-independent probes.
PREMADE = {
    "sarah": "EXAVITQu4vr4xnSDxMaL",
    "george": "JBFqnCBsd6RMkjVDRZzb",
    "river": "SAz9YHcvj6GT2YYXdXww",
}
DEFAULT_VOICE = "sarah"

CONFIGS = {
    # name: (model_id, stitching, extra body)
    "v3": ("eleven_v3", False, {}),
    "v3_stitch": ("eleven_v3", True, {}),
    "mv2_stitch": ("eleven_multilingual_v2", True, {}),
    "mv2": ("eleven_multilingual_v2", False, {}),
    "flash": ("eleven_flash_v2_5", True, {"language_code": "ja"}),
}


# ---------------------------------------------------------------- helpers
def subscription() -> dict:
    for attempt in range(6):
        r = requests.get(f"{BASE}/user/subscription", headers=H, timeout=30)
        if r.status_code == 429:  # polled too fast; back off
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("subscription endpoint kept returning 429")


def char_count() -> int:
    return int(subscription()["character_count"])


def load_lines(n: int) -> list[dict]:
    """First n lines of the material: role, raw target text, TTS text (furigana stripped)."""
    out = []
    for i, raw in enumerate(MATERIAL.read_text(encoding="utf-8").splitlines()):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        m = re.match(r"^([QA]):\s*(.*)$", raw)
        role, body = (m.group(1), m.group(2)) if m else ("", raw)
        target = body.split("|||")[0].strip()
        out.append({"i": len(out), "role": role, "raw": target, "tts": strip_furigana(target)})
        if len(out) >= n:
            break
    return out


def synth(
    text: str,
    *,
    model_id: str,
    voice_id: str,
    out_path: Path | None,
    previous_text: str | None = None,
    next_text: str | None = None,
    voice_settings: dict | None = None,
    extra: dict | None = None,
    tag: str = "",
    endpoint: str = "",
    measure_credits: bool = False,
) -> dict:
    body: dict = {"text": text, "model_id": model_id, "seed": SEED}
    if previous_text is not None:
        body["previous_text"] = previous_text
    if next_text is not None:
        body["next_text"] = next_text
    if voice_settings is not None:
        body["voice_settings"] = voice_settings
    if extra:
        body.update(extra)
    url = f"{BASE}/text-to-speech/{voice_id}{endpoint}"
    before = char_count() if measure_credits else None
    t0 = time.time()
    r = requests.post(url, headers=H, params={"output_format": "mp3_44100_128"}, json=body, timeout=120)
    elapsed = time.time() - t0
    after = char_count() if measure_credits else None
    hdr = {k.lower(): v for k, v in r.headers.items()}
    interesting = {k: v for k, v in hdr.items()
                   if any(s in k for s in ("character", "cost", "credit", "request-id", "history", "ratelimit", "retry"))}
    rec = {
        "tag": tag, "model": model_id, "voice": voice_id, "status": r.status_code,
        "chars": len(text), "prev_chars": len(previous_text or ""), "next_chars": len(next_text or ""),
        "elapsed_s": round(elapsed, 2), "headers": interesting,
        "credits_before": before, "credits_after": after,
        "credits_delta": (after - before) if measure_credits else None,
        "body_keys": sorted(k for k in body if k not in ("text",)),
    }
    if r.status_code == 200:
        if endpoint:  # json response (with-timestamps)
            rec["json"] = r.json()
        elif out_path is not None:
            out_path.write_bytes(r.content)
            rec["file"] = str(out_path.relative_to(ROOT))
            rec["bytes"] = len(r.content)
    else:
        try:
            rec["error"] = r.json()
        except Exception:
            rec["error"] = r.text[:500]
    with CALLS.open("a", encoding="utf-8") as f:
        f.write(json.dumps({k: v for k, v in rec.items() if k != "json"}, ensure_ascii=False) + "\n")
    return rec


def clip_stats(path: Path) -> dict:
    from pydub import AudioSegment
    from pydub.silence import detect_leading_silence
    a = AudioSegment.from_file(path)
    lead = detect_leading_silence(a, silence_threshold=a.dBFS - 25 if a.dBFS > -60 else -50)
    trail = detect_leading_silence(a.reverse(), silence_threshold=a.dBFS - 25 if a.dBFS > -60 else -50)
    return {"dur_ms": len(a), "dbfs": round(a.dBFS, 1), "lead_ms": lead, "trail_ms": trail,
            "speech_ms": max(len(a) - lead - trail, 0)}


def voice_id_of(name_or_id: str) -> str:
    if name_or_id in JA_VOICES:
        return JA_VOICES[name_or_id][0]
    if name_or_id in PREMADE:
        return PREMADE[name_or_id]
    return name_or_id


def show(rec: dict) -> None:
    keep = {k: rec[k] for k in ("tag", "status", "chars", "credits_delta", "elapsed_s", "headers") if k in rec}
    if "error" in rec:
        keep["error"] = rec["error"]
    print(json.dumps(keep, ensure_ascii=False))


# ---------------------------------------------------------------- commands
def cmd_probe() -> None:
    s = subscription()
    print(json.dumps({k: s.get(k) for k in ("tier", "character_count", "character_limit", "voice_limit", "max_voice_add_edits")}))
    for v in requests.get(f"{BASE}/voices", headers=H, timeout=30).json()["voices"]:
        if v.get("category") != "premade":
            print("voice:", v["voice_id"], v["name"], v.get("category"), v.get("labels", {}).get("language"))


def cmd_add_voices() -> None:
    have = {v["voice_id"] for v in requests.get(f"{BASE}/voices", headers=H, timeout=30).json()["voices"]}
    for name, (vid, owner) in JA_VOICES.items():
        if vid in have:
            print("already added:", name, vid)
            continue
        r = requests.post(f"{BASE}/voices/add/{owner}/{vid}", headers=H, json={"new_name": f"spike-{name}"}, timeout=30)
        print("add", name, vid, r.status_code, r.text[:200])


def cmd_validate() -> None:
    """Schema probes. A 422 costs nothing; a 200 on a 1-char text costs ~1 credit."""
    vid = voice_id_of(DEFAULT_VOICE)
    t = "は"
    probes = [
        ("speed_3.0_mv2", "eleven_multilingual_v2", dict(voice_settings={"speed": 3.0})),
        ("speed_0.5_mv2", "eleven_multilingual_v2", dict(voice_settings={"speed": 0.5})),
        ("speed_1.3_mv2", "eleven_multilingual_v2", dict(voice_settings={"speed": 1.3})),
        ("speed_0.6_mv2", "eleven_multilingual_v2", dict(voice_settings={"speed": 0.6})),
        ("v3_full_voice_settings", "eleven_v3", dict(voice_settings={"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True, "speed": 1.0})),
        ("v3_stability_0.3", "eleven_v3", dict(voice_settings={"stability": 0.3})),
        ("v3_prev_next", "eleven_v3", dict(previous_text="こんにちは。", next_text="よろしくお願いします。")),
        ("v3_language_code", "eleven_v3", dict(extra={"language_code": "ja"})),
        ("mv2_language_code", "eleven_multilingual_v2", dict(extra={"language_code": "ja"})),
        ("mv2_bogus_field", "eleven_multilingual_v2", dict(extra={"definitely_not_a_field": 1})),
    ]
    for tag, model, kw in probes:
        rec = synth(t, model_id=model, voice_id=vid, out_path=OUT / f"validate_{tag}.mp3", tag=tag, measure_credits=True, **kw)
        show(rec)
        time.sleep(1.5)


def cmd_concurrency() -> None:
    vid = voice_id_of(DEFAULT_VOICE)
    before = char_count()

    def one(i: int) -> dict:
        return synth("こんにちは。", model_id="eleven_multilingual_v2", voice_id=vid,
                     out_path=OUT / f"conc_{i}.mp3", tag=f"conc_{i}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        recs = [f.result() for f in as_completed(pool.submit(one, i) for i in range(4))]
    for r in sorted(recs, key=lambda x: x["tag"]):
        show(r)
    print("credits used by 4 x 6-char requests:", char_count() - before)


def cmd_compare(config: str, n_lines: int, voice: str) -> None:
    model_id, stitching, extra = CONFIGS[config]
    vid = voice_id_of(voice)
    lines = load_lines(n_lines)
    tag = f"{config}_{voice}"
    d = OUT / tag
    d.mkdir(exist_ok=True)
    before = char_count()

    def one(k: int) -> dict:
        ln = lines[k]
        prev = lines[k - 1]["tts"] if stitching and k > 0 else None
        nxt = lines[k + 1]["tts"] if stitching and k + 1 < len(lines) else None
        rec = synth(ln["tts"], model_id=model_id, voice_id=vid, out_path=d / f"{k:03d}.mp3",
                    previous_text=prev, next_text=nxt, extra=extra or None, tag=f"{tag}/{k:03d}")
        rec["k"] = k
        return rec

    recs: list[dict] = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for f in as_completed(pool.submit(one, k) for k in range(len(lines))):
            recs.append(f.result())
    recs.sort(key=lambda r: r["k"])
    used = char_count() - before
    total_chars = sum(len(l["tts"]) for l in lines)
    ctx_chars = sum(r["prev_chars"] + r["next_chars"] for r in recs)
    header_cost = sum(int(r["headers"].get("character-cost", 0)) for r in recs if r["status"] == 200)
    fails = [r for r in recs if r["status"] != 200]
    for r in fails:
        show(r)
    rows = []
    for r in recs:
        if r["status"] != 200:
            continue
        st = clip_stats(ROOT / r["file"])
        st.update({"config": config, "voice": voice, "k": r["k"], "chars": r["chars"],
                   "cps": round(r["chars"] / (st["speech_ms"] / 1000), 2) if st["speech_ms"] else None,
                   "elapsed_s": r["elapsed_s"], "file": r["file"]})
        rows.append(st)
    with STATS.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {"config": config, "voice": voice, "lines": len(lines), "ok": len(rows), "failed": len(fails),
               "text_chars": total_chars, "context_chars": ctx_chars, "credits_used": used,
               "header_cost": header_cost,
               "credits_per_text_char": round(header_cost / total_chars, 3) if total_chars else None}
    print(json.dumps(summary, ensure_ascii=False))
    with (OUT / "summaries.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")


def cmd_timestamps(n_lines: int, voice: str, model_id: str) -> None:
    """One paragraph request via /with-timestamps; cut per line using the alignment."""
    lines = load_lines(n_lines)
    text = "".join(l["tts"] for l in lines)  # JA sentences end with 。 already
    vid = voice_id_of(voice)
    rec = synth(text, model_id=model_id, voice_id=vid, out_path=None, endpoint="/with-timestamps",
                tag=f"timestamps_{model_id}", measure_credits=True)
    show(rec)
    if rec["status"] != 200:
        return
    import base64
    from pydub import AudioSegment
    js = rec["json"]
    al = js.get("alignment") or js.get("normalized_alignment")
    d = OUT / f"timestamps_{model_id}_{voice}"
    d.mkdir(exist_ok=True)
    audio_path = d / "paragraph.mp3"
    audio_path.write_bytes(base64.b64decode(js["audio_base64"]))
    (d / "alignment.json").write_text(json.dumps(al, ensure_ascii=False))
    chars, starts, ends = al["characters"], al["character_start_times_seconds"], al["character_end_times_seconds"]
    print("alignment chars:", len(chars), "text chars:", len(text), "audio:", audio_path)
    a = AudioSegment.from_file(audio_path)
    pos = 0
    for k, l in enumerate(lines):
        n = len(l["tts"])
        seg_chars = "".join(chars[pos:pos + n])
        s_ms, e_ms = int(starts[pos] * 1000), int(ends[pos + n - 1] * 1000)
        a[s_ms:e_ms].export(d / f"{k:03d}.mp3", format="mp3")
        print(f"  {k:03d} {s_ms:6d}-{e_ms:6d}ms  match={'ok' if seg_chars == l['tts'] else 'MISMATCH'}  {l['tts'][:30]}")
        pos += n


def cmd_google(n_lines: int) -> None:
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", str(ROOT / "google-credentials.json"))
    from audio.tts_generator import _get_google_client, _google_generate_single
    client = _get_google_client()
    lines = load_lines(n_lines)
    d = OUT / "google_charon"
    d.mkdir(exist_ok=True)
    rows = []
    for k, l in enumerate(lines):
        p = d / f"{k:03d}.mp3"
        t0 = time.time()
        _google_generate_single(client, l["tts"], p, "ja-JP-Chirp3-HD-Charon", 1.0, 0.0)
        st = clip_stats(p)
        st.update({"config": "google", "voice": "charon", "k": k, "chars": len(l["tts"]),
                   "cps": round(len(l["tts"]) / (st["speech_ms"] / 1000), 2) if st["speech_ms"] else None,
                   "elapsed_s": round(time.time() - t0, 2), "file": str(p.relative_to(ROOT))})
        rows.append(st)
    with STATS.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"config": "google", "ok": len(rows)}))


def cmd_report() -> None:
    import statistics as S
    rows = [json.loads(x) for x in STATS.read_text(encoding="utf-8").splitlines() if x.strip()]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(f"{r['config']}/{r['voice']}", []).append(r)
    print("| config/voice | clips | speech s | chars/s mean ± sd | cv | dBFS mean ± sd | lead ms med | trail ms med | latency s med |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for g, rs in groups.items():
        cps = [r["cps"] for r in rs if r["cps"]]
        db = [r["dbfs"] for r in rs]
        sd = S.pstdev(cps) if len(cps) > 1 else 0.0
        print(f"| {g} | {len(rs)} | {sum(r['speech_ms'] for r in rs)/1000:.1f} | {S.mean(cps):.2f} ± {sd:.2f} | {sd/S.mean(cps):.3f} | "
              f"{S.mean(db):.1f} ± {S.pstdev(db):.1f} | {S.median(r['lead_ms'] for r in rs):.0f} | {S.median(r['trail_ms'] for r in rs):.0f} | "
              f"{S.median(r['elapsed_s'] for r in rs):.2f} |")
    sp = OUT / "summaries.jsonl"
    if sp.exists():
        print("\n| config/voice | text chars | context chars | sum character-cost | credits / text char |")
        print("|---|---:|---:|---:|---:|")
        for x in sp.read_text(encoding="utf-8").splitlines():
            s = json.loads(x)
            print(f"| {s['config']}/{s['voice']} | {s['text_chars']} | {s['context_chars']} | {s.get('header_cost')} | {s['credits_per_text_char']} |")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("config", nargs="?")
    ap.add_argument("--lines", type=int, default=20)
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--model", default="eleven_v3")
    a = ap.parse_args()
    if not H["xi-api-key"]:
        sys.exit("ELEVENLABS_API_KEY missing in .env")
    {
        "probe": lambda: cmd_probe(),
        "add-voices": lambda: cmd_add_voices(),
        "validate": lambda: cmd_validate(),
        "concurrency": lambda: cmd_concurrency(),
        "compare": lambda: cmd_compare(a.config, a.lines, a.voice),
        "timestamps": lambda: cmd_timestamps(a.lines, a.voice, a.model),
        "google": lambda: cmd_google(a.lines),
        "report": lambda: cmd_report(),
    }[a.cmd]()

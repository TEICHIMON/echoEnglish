# Echo Loop Generator

Generate **T•N•T** (Target → Native → Target) Echo Loop audio files for language learning, with audio-extraction, text-only TTS, and batch modes.

Based on *"Echo: Rebuilding the Natural Reflex of Language"* by H. Reeve.

## What is an Echo Loop?

The Echo Loop follows the **T-S-N-S-T-S** pattern:

```
[Target Audio] → [silence] → [Native TTS] → [silence] → [Target Audio] → [silence]
```

This structure uses the reflex energy of your native language to ignite comprehension of the target language — not through translation, but through resonance.

## Three TTS Engines

The generator supports three TTS engines, selectable per run via config or CLI:

| Engine | Cost | Strengths | Flag |
|---|---|---|---|
| **Google Cloud TTS** | Paid | Stable, high-quality Neural2 / Chirp3-HD voices, broad language coverage | `--engine google` |
| **edge-tts** | Free | Many voices, no API key — but occasional 503s on long batches | `--engine edge` |
| **OpenAI gpt-4o-mini-tts** | Paid | Reads math formulas naturally, semantic understanding, instruction-tunable | `--engine openai` |

**Google Cloud TTS** is the default — most stable for long batch runs. Switch to OpenAI when your content includes math/technical notation, or to edge-tts when you don't want any cloud cost.

### Google Cloud TTS Setup

1. Install the SDK (already in `requirements.txt`):
   ```bash
   pip install google-cloud-texttospeech
   ```

2. Get a service account JSON key from the Google Cloud Console (project → IAM & Admin → Service Accounts → Keys → Add Key → JSON). The service account needs the **Cloud Text-to-Speech User** role.

3. Save the file at the project root as `google-credentials.json` (already gitignored), then point the SDK at it:
   ```bash
   # In .env (loaded automatically via python-dotenv)
   GOOGLE_APPLICATION_CREDENTIALS=./google-credentials.json
   ```

4. Default voices are configured in `config.yaml` under `tts.google`:
   ```yaml
   tts:
     engine: "google"
     google:
       target_voice: "ja-JP-Neural2-B"          # Japanese male Neural2
       native_voice: "cmn-CN-Chirp3-HD-Kore"    # Chinese female Chirp3-HD
       speaking_rate: 1.0
       pitch: 0.0                                # ignored by Chirp3-HD
   ```

5. CLI overrides:
   ```bash
   python main.py --text phrases.txt \
       --engine google \
       --google-voice cmn-CN-Chirp3-HD-Kore \
       --google-target-voice ja-JP-Neural2-B
   ```

> **Note:** Chirp3-HD voices ignore the `pitch` parameter — set `pitch: 0.0` (the default) when using them. Neural2 / Wavenet / Standard voices accept pitch normally.

### OpenAI Engine Setup

1. Install the SDK (already in `requirements.txt`):
   ```bash
   pip install openai
   ```
2. Set your API key:
   ```bash
   export OPENAI_API_KEY="sk-..."
   ```
   Or add it to a `.env` file in the project root — the generator loads it automatically via `python-dotenv`.

3. Use it:
   ```bash
   # CLI
   python main.py --text math_phrases.txt --engine openai

   # Or set in config.yaml
   # tts:
   #   engine: "openai"
   ```

### OpenAI Voice & Instructions

The OpenAI engine exposes two powerful controls:

- **voice** — choose from: `alloy`, `ash`, `ballad`, `cedar`, `coral`, `echo`, `fable`, `nova`, `onyx`, `sage`, `shimmer`, `verse`
- **instructions** — a natural language prompt that controls *how* the model speaks. This is where math reading happens:

```yaml
tts:
  engine: "openai"
  openai:
    voice: "coral"
    speed: 1.0
    instructions: "用中文自然地朗读，数学表达式要读成口语形式，比如2ⁿ读作2的n次方"
```

```bash
# CLI overrides
python main.py --text formulas.txt \
    --engine openai \
    --openai-voice nova \
    --openai-instructions "Read mathematical expressions naturally in English"
```

## Three Modes

### Audio Mode

You supply a source audio file and an LRC subtitle file. Target audio is extracted from the recording; native audio is generated via TTS.

```
source.mp3 + source.lrc  →  source_echo.m4a + source_echo.lrc
```

### Text-Only Mode

You supply a plain text file with bilingual entries. Both target and native audio are generated entirely via TTS. No source recording needed.

```
phrases.txt  →  phrases_echo.m4a + phrases_echo.lrc
```

### Batch Mode

You point at a folder. The scanner finds all audio+LRC pairs and/or text files and processes them one by one. Output files are written to the same folder with an `_echo` suffix.

```
lessons/
  ├── lesson01.mp3
  ├── lesson01.lrc
  ├── lesson02.mp3
  ├── lesson02.lrc
  └── vocab.txt
→
  ├── lesson01_echo.m4a + lesson01_echo.lrc
  ├── lesson02_echo.m4a + lesson02_echo.lrc
  └── vocab_echo.m4a    + vocab_echo.lrc
```

## Requirements

- Python 3.10+
- ffmpeg (for m4a export)
- ffprobe (for the optional transcription API; usually installed with ffmpeg)
- Internet connection (for edge-tts or OpenAI API)

## Installation

```bash
pip install -r requirements.txt
```

## Optional Transcription API

`transcription_server.py` adds a FastAPI service that accepts uploaded audio,
transcribes it with faster-whisper + Silero VAD, streams progress over SSE, and
returns generated LRC text.

Run it:

```bash
uvicorn transcription_server:app --host 0.0.0.0 --port 8000
```

Submit a job:

```bash
curl -F "audio=@lesson01.mp3" -F "lang=ja" http://localhost:8000/jobs
```

Watch progress:

```bash
curl -N http://localhost:8000/jobs/<job_id>/events
```

Fetch the LRC result:

```bash
curl http://localhost:8000/jobs/<job_id>/result
```

Supported transcription models:

| Language | Model form value | faster-whisper repo |
|---|---|---|
| `en` | `default` / `large-v3-turbo` | `Systran/faster-whisper-large-v3-turbo` |
| `en` | `large-v3` | `Systran/faster-whisper-large-v3` |
| `ja` | `default` | `kotoba-tech/kotoba-whisper-v2.0-faster` |

Environment overrides:

| Variable | Default |
|---|---|
| `TRANSCRIBE_DEVICE` | `cuda` |
| `TRANSCRIBE_COMPUTE_TYPE` | `int8_float16` |
| `TRANSCRIBE_IDLE_RELEASE_SECONDS` | `600` |
| `TRANSCRIBE_JOB_RETENTION_SECONDS` | `3600` |

## Quick Start

```bash
# Audio mode — positional arguments
python main.py lesson01.mp3 lesson01.lrc

# Text-only mode
python main.py --text phrases.txt

# Text-only mode with OpenAI TTS (for math content)
python main.py --text math_formulas.txt --engine openai

# Batch mode — scan a whole folder
python main.py --scan /path/to/lessons

# Config-only — all paths set in config.yaml
python main.py
```

---

## CLI Reference

### Synopsis

```
python main.py [audio] [lrc]           # audio mode (positional)
python main.py --text FILE             # text-only mode
python main.py --scan DIR              # batch mode
python main.py                         # config-only (paths from config.yaml)
```

### Input Arguments

| Flag / Positional | Description |
|---|---|
| `audio` | *(positional, optional)* Source audio file (mp3, wav, m4a, etc.) — audio mode |
| `lrc` | *(positional, optional)* LRC subtitle file with bilingual content — audio mode |
| `--text, -t FILE` | Bilingual text file — text-only mode |
| `--scan, -s DIR` | Folder to scan — batch mode |

### Mode Control

| Flag | Description |
|---|---|
| `--mode {audio,text}` | Force mode. Overrides config `mode:` and auto-detection. In batch mode, acts as a **filter**: `audio` = only audio+LRC pairs, `text` = only .txt files, omitted = both |

### Output Paths

| Flag | Description |
|---|---|
| `-o, --output PATH` | Output audio file path *(single-file mode only; ignored in batch)* |
| `--output-lrc PATH` | Output LRC file path *(single-file mode only; ignored in batch)* |

In single-file modes, if omitted, defaults to `<input_stem>_echo.<format>` in the same directory as the input. In batch mode, output is always `<stem>_echo.<format>` next to the source file.

### Config

| Flag | Default | Description |
|---|---|---|
| `-c, --config FILE` | `config.yaml` | Config file path |

### Timing Overrides

| Flag | Default | Description |
|---|---|---|
| `--after-first-target` | `0.8` | Silence after 1st target phrase (seconds) |
| `--after-native` | `0.5` | Silence after native phrase (seconds) |
| `--after-second-target` | `1.2` | Silence after 2nd target phrase / loop gap (seconds) |

### TTS Engine & Voice Overrides

| Flag | Default | Description |
|---|---|---|
| `--engine {google,edge,openai}` | `google` | TTS engine selection |
| `--target-voice` | `ja-JP-NanamiNeural` | Target language voice (text-only mode, edge-tts) |
| `--native-voice` | `zh-CN-XiaoxiaoNeural` | Native language voice (edge-tts) |
| `--voice` | — | Alias for `--native-voice` (backward compatible) |
| `--rate` | `+0%` | Speech rate (e.g., `+10%`, `-20%`) — edge-tts only |
| `--google-voice` | `cmn-CN-Chirp3-HD-Kore` | Google native voice — google engine only |
| `--google-target-voice` | `ja-JP-Neural2-B` | Google target voice — google engine only |
| `--openai-voice` | `coral` | OpenAI TTS voice — openai engine only |
| `--openai-instructions` | — | OpenAI TTS instructions prompt — openai engine only |

### TTS Volume Overrides

| Flag | Default | Description |
|---|---|---|
| `--gain` | `0` | Fixed dB gain applied to every TTS clip (e.g., `-6` to reduce, `+3` to boost) |
| `--normalize` | — | Normalize each TTS clip to this dBFS level (e.g., `-20`). Overrides `--gain` when set |

### LRC / Text Parsing Overrides

| Flag | Default | Description |
|---|---|---|
| `--delimiter` | `\|\|\|` | Delimiter between target and native text. Older files using `-` can keep working by setting `delimiter: "-"` in config — but `\|\|\|` is far safer when content contains hyphens. |
| `--split-strategy` | `last` | `first` or `last` delimiter occurrence |

---

## Mode Resolution

Settings are resolved in this order (first wins):

1. **CLI arguments** — always highest priority
2. **config.yaml** — used when CLI args are not provided
3. **Built-in defaults** — fallback

Mode detection follows this priority chain:

1. **`--scan` path set** → batch mode (overrides everything)
2. **`--mode` CLI flag** or **`mode:` in config.yaml** → forced audio or text
3. **Auto-detect from paths** — audio+lrc present → audio; text present → text; both → audio wins

In batch mode, the `--mode` flag (or config `mode:`) acts as a **scan filter**, not the top-level mode:

| `--mode` | Scanner behavior |
|---|---|
| *(omitted)* | Finds audio+LRC pairs **and** .txt files |
| `audio` | Only audio+LRC pairs |
| `text` | Only .txt files |

---

## Configuration (config.yaml)

```yaml
# Mode: "audio" or "text" (optional)
# If omitted, auto-detected from paths.
# In batch mode, acts as a scan filter.
# mode: "audio"

# File paths (CLI arguments override these)
paths:
  scan: ""               # folder path — triggers batch mode
  audio: ""              # source audio file — audio mode
  lrc: ""                # LRC subtitle file — audio mode
  text: ""               # bilingual text file — text-only mode
  output: ""             # output audio file (default: <input_name>_echo.<format>)
  output_lrc: ""         # output LRC file (default: same as output with .lrc)

timing:
  after_first_target: 0.8
  after_native: 0.5
  after_second_target: 1.2

tts:
  # Engine: "google" (default), "edge" (free), or "openai" (math-aware)
  engine: "google"

  # --- edge-tts settings ---
  target_voice: "ja-JP-NanamiNeural"
  native_voice: "zh-CN-XiaoxiaoNeural"
  rate: "+0%"
  pitch: "+0Hz"

  # --- Google Cloud TTS settings (used when engine: "google") ---
  google:
    target_voice: "ja-JP-Neural2-B"
    native_voice: "cmn-CN-Chirp3-HD-Kore"
    speaking_rate: 1.0
    pitch: 0.0

  # --- OpenAI TTS settings (used when engine: "openai") ---
  openai:
    model: "gpt-4o-mini-tts"
    voice: "coral"
    speed: 1.0
    instructions: "用中文自然地朗读，数学表达式要读成口语形式，比如2ⁿ读作2的n次方"

  # --- Volume control (applies to all engines) ---
  gain: -6              # fixed dB adjustment (0 = no change)
  normalize:            # target dBFS (e.g., -20). Overrides gain when set.

output:
  format: "m4a"
  bitrate: "192k"
  sample_rate: 44100

lrc:
  delimiter: "|||"
  split_strategy: "last"
```

---

## Input File Formats

### Text File Format

One bilingual entry per line. Target text and native text separated by the delimiter (default `|||`). Blank lines and `#` comments are ignored.

```
# Japanese → Chinese
一度の接種でハシカのMMRワクチンについて|||关于一次接种即可预防麻疹的MMR疫苗
厚生労働省の専門家部会は了承しました|||厚生劳动省的专家委员会已批准

# English → Chinese
The vaccine requires only one dose|||该疫苗只需接种一次
```

Format: `<target_text><delimiter><native_text>`

> **Why `|||`?** Earlier versions used `-`, but that breaks on content containing hyphens (e.g. `red-black tree`, `2-3 tree`, `state-of-the-art`) — the parser would split at the wrong dash and corrupt both target and native text. `|||` is essentially impossible to find in natural-language content, so the split is always clean.

The default split strategy `last` splits on the **last** occurrence of the delimiter, avoiding issues when the delimiter happens to appear within the text itself.

### LRC File Format

Standard LRC with bilingual content separated by the same delimiter:

```
[00:00.39]一度の接種でハシカ...MMRワクチンについて|||关于一次接种即可预防...的MMR疫苗
[00:06.74]厚生労働省の専門家部会は...了承しました|||厚生劳动省的专家委员会已批准...
```

Format: `[mm:ss.xx]<target_text><delimiter><native_text>`

### Batch Folder Structure

The scanner recognizes audio files by extension (mp3, m4a, wav, flac, ogg, aac, wma) and pairs them with a `.lrc` file of the same stem. Text files are any `.txt` not already claimed by an audio pair.

```
lessons/
  lesson01.mp3    ← paired with lesson01.lrc → audio mode
  lesson01.lrc
  lesson02.wav    ← paired with lesson02.lrc → audio mode
  lesson02.lrc
  vocab.txt       ← standalone → text-only mode
  notes.mp3       ← no notes.lrc → skipped with warning
```

---

## Examples

### Audio Mode

```bash
# Basic
python main.py lesson01.mp3 lesson01.lrc

# Custom output paths
python main.py lesson01.mp3 lesson01.lrc \
    -o out/echo.m4a \
    --output-lrc out/echo.lrc

# Custom timing
python main.py lesson01.mp3 lesson01.lrc \
    --after-first-target 1.0 \
    --after-native 0.6 \
    --after-second-target 1.5

# Override native voice
python main.py lesson01.mp3 lesson01.lrc \
    --native-voice zh-CN-YunxiNeural

# Use OpenAI for native TTS (math-heavy content)
python main.py lesson01.mp3 lesson01.lrc --engine openai
```

### Text-Only Mode

```bash
# Basic
python main.py --text phrases.txt

# Custom voices (English → Chinese)
python main.py --text phrases.txt \
    --target-voice en-US-JennyNeural \
    --native-voice zh-CN-XiaoxiaoNeural

# Slower TTS
python main.py --text phrases.txt --rate "-10%"

# OpenAI engine for math formulas
python main.py --text math.txt \
    --engine openai \
    --openai-voice sage \
    --openai-instructions "Read all math expressions naturally in spoken Chinese"

# Reduce TTS volume by 6 dB
python main.py --text phrases.txt --gain -6

# Normalize all TTS clips to -20 dBFS
python main.py --text phrases.txt --normalize -20
```

### Batch Mode

```bash
# Scan a folder — process all audio+LRC pairs and text files
python main.py --scan /path/to/lessons

# Only audio+LRC pairs
python main.py --scan /path/to/lessons --mode audio

# Only text files
python main.py --scan /path/to/lessons --mode text

# Batch with OpenAI engine and custom timing
python main.py --scan /path/to/lessons \
    --engine openai \
    --after-second-target 1.5
```

### Config-Only

```bash
# All paths set in config.yaml
python main.py

# Custom config file
python main.py -c my_lesson.yaml

# Config paths + CLI engine override
python main.py --engine openai
```

---

## TTS Volume Control

Volume adjustment applies to every generated TTS clip, regardless of engine. Two modes are available:

**Fixed gain** — shift all clips by a constant dB amount:
```bash
python main.py --text phrases.txt --gain -6    # quieter
python main.py --text phrases.txt --gain 3     # louder
```

**Normalization** — scale each clip individually so its average loudness hits a target dBFS. Overrides `--gain` when both are set:
```bash
python main.py --text phrases.txt --normalize -20
```

Typical spoken audio sits around -18 to -24 dBFS. Set in config as `tts.gain` and `tts.normalize`.

---

## Available Voices

### edge-tts Voices

Any edge-tts voice can be used. Common ones:

**Chinese:** `zh-CN-XiaoxiaoNeural` (F), `zh-CN-YunxiNeural` (M), `zh-CN-XiaoyiNeural` (F), `zh-CN-YunjianNeural` (M)

**Japanese:** `ja-JP-NanamiNeural` (F), `ja-JP-KeitaNeural` (M)

**English:** `en-US-JennyNeural` (F), `en-US-GuyNeural` (M), `en-US-AriaNeural` (F), `en-GB-SoniaNeural` (F), `en-GB-RyanNeural` (M)

**Korean:** `ko-KR-SunHiNeural` (F), `ko-KR-InJoonNeural` (M)

**French:** `fr-FR-DeniseNeural` (F), `fr-FR-HenriNeural` (M)

Browse all:
```bash
edge-tts --list-voices
edge-tts --list-voices | grep en-
```

### OpenAI Voices

`alloy`, `ash`, `ballad`, `cedar`, `coral`, `echo`, `fable`, `nova`, `onyx`, `sage`, `shimmer`, `verse`

The OpenAI engine uses a single voice for both target and native audio. Language is determined by the input text and `instructions` prompt — no separate voice-per-language selection is needed.

---

## Project Structure

```
echo-loop-generator/
├── config.yaml              # Default configuration
├── main.py                  # CLI entry point (audio + text + batch modes)
├── requirements.txt
├── README.md
├── .env                     # OPENAI_API_KEY (git-ignored)
├── parser/
│   ├── __init__.py
│   ├── lrc_parser.py        # LRC file parser
│   └── text_parser.py       # Plain text file parser (text-only mode)
├── audio/
│   ├── __init__.py
│   ├── splitter.py          # Audio segment extraction
│   ├── tts_generator.py     # TTS generation (edge-tts + OpenAI, volume control)
│   └── assembler.py         # Echo Loop assembly (T-S-N-S-T-S)
├── scanner/
│   ├── __init__.py
│   └── scanner.py           # Folder scanner for batch mode
└── export/
    ├── __init__.py
    ├── exporter.py           # Final audio export
    └── lrc_writer.py         # Echo Loop LRC generation
```

## License

MIT

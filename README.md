# Echo Loop Generator

Generate **T•N•T** (Target → Native → Target) Echo Loop audio files for language learning, with audio-extraction, text-only TTS, and batch modes.

Based on *"Echo: Rebuilding the Natural Reflex of Language"* by H. Reeve.

## What is an Echo Loop?

The Echo Loop follows the **T-S-N-S-T-S** pattern:

```
[Target Audio] → [silence] → [Native TTS] → [silence] → [Target Audio] → [silence]
```

This structure uses the reflex energy of your native language to ignite comprehension of the target language — not through translation, but through resonance.

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
- Internet connection (for edge-tts)

## Installation

```bash
pip install -r requirements.txt
```

## Quick Start

```bash
# Audio mode — positional arguments
python main.py lesson01.mp3 lesson01.lrc

# Text-only mode
python main.py --text phrases.txt

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

### TTS Overrides

| Flag | Default | Description |
|---|---|---|
| `--target-voice` | `ja-JP-NanamiNeural` | Target language TTS voice (text-only mode) |
| `--native-voice` | `zh-CN-XiaoxiaoNeural` | Native language TTS voice |
| `--voice` | — | Alias for `--native-voice` (backward compatible) |
| `--rate` | `+0%` | TTS speech rate (e.g., `+10%`, `-20%`) |

### LRC / Text Parsing Overrides

| Flag | Default | Description |
|---|---|---|
| `--delimiter` | `-` | Delimiter between target and native text |
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
  target_voice: "ja-JP-NanamiNeural"
  native_voice: "zh-CN-XiaoxiaoNeural"
  rate: "+0%"
  pitch: "+0Hz"

output:
  format: "m4a"
  bitrate: "192k"
  sample_rate: 44100

lrc:
  delimiter: "-"
  split_strategy: "last"
```

---

## Input File Formats

### Text File Format

One bilingual entry per line. Target text and native text separated by the delimiter (default `-`). Blank lines and `#` comments are ignored.

```
# Japanese → Chinese
一度の接種でハシカのMMRワクチンについて-关于一次接种即可预防麻疹的MMR疫苗
厚生労働省の専門家部会は了承しました-厚生劳动省的专家委员会已批准

# English → Chinese
The vaccine requires only one dose-该疫苗只需接种一次
```

Format: `<target_text><delimiter><native_text>`

The default split strategy `last` splits on the **last** occurrence of the delimiter, avoiding issues when the delimiter appears within the text itself.

### LRC File Format

Standard LRC with bilingual content separated by the same delimiter:

```
[00:00.39]一度の接種でハシカ...MMRワクチンについて-关于一次接种即可预防...的MMR疫苗
[00:06.74]厚生労働省の専門家部会は...了承しました-厚生劳动省的专家委员会已批准...
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
```

### Text-Only Mode

```bash
# Basic
python main.py --text phrases.txt

# Custom voices (English → Chinese)
python main.py --text phrases.txt \
    --target-voice en-US-JennyNeural \
    --native-voice zh-CN-XiaoxiaoNeural

# Japanese target, English native
python main.py --text phrases.txt \
    --target-voice ja-JP-NanamiNeural \
    --native-voice en-US-GuyNeural

# Slower TTS
python main.py --text phrases.txt --rate "-10%"

# Custom output path
python main.py --text phrases.txt -o output/phrases_echo.m4a
```

### Batch Mode

```bash
# Scan a folder — process all audio+LRC pairs and text files
python main.py --scan /path/to/lessons

# Scan but only process audio+LRC pairs
python main.py --scan /path/to/lessons --mode audio

# Scan but only process text files
python main.py --scan /path/to/lessons --mode text

# Batch with custom voices and timing
python main.py --scan /path/to/lessons \
    --native-voice zh-CN-YunxiNeural \
    --target-voice en-US-JennyNeural \
    --after-second-target 1.5
```

### Config-Only

```bash
# All paths set in config.yaml
python main.py

# Custom config file
python main.py -c my_lesson.yaml

# Config paths + CLI voice override
python main.py --native-voice zh-CN-YunxiNeural
```

---

## Available Voices (edge-tts)

Any edge-tts voice can be used for either target or native. Common ones:

### Chinese

| Voice | Gender | Description |
|---|---|---|
| `zh-CN-XiaoxiaoNeural` | Female | Standard, warm |
| `zh-CN-YunxiNeural` | Male | Standard, calm |
| `zh-CN-XiaoyiNeural` | Female | Lively |
| `zh-CN-YunjianNeural` | Male | Authoritative |

### Japanese

| Voice | Gender | Description |
|---|---|---|
| `ja-JP-NanamiNeural` | Female | Standard, clear |
| `ja-JP-KeitaNeural` | Male | Standard, calm |

### English

| Voice | Gender | Accent |
|---|---|---|
| `en-US-JennyNeural` | Female | American |
| `en-US-GuyNeural` | Male | American |
| `en-US-AriaNeural` | Female | American |
| `en-GB-SoniaNeural` | Female | British |
| `en-GB-RyanNeural` | Male | British |
| `en-AU-NatashaNeural` | Female | Australian |

### Korean

| Voice | Gender | Description |
|---|---|---|
| `ko-KR-SunHiNeural` | Female | Standard |
| `ko-KR-InJoonNeural` | Male | Standard |

### French

| Voice | Gender | Description |
|---|---|---|
| `fr-FR-DeniseNeural` | Female | Standard |
| `fr-FR-HenriNeural` | Male | Standard |

Browse all available voices:

```bash
edge-tts --list-voices
edge-tts --list-voices | grep en-    # filter by language
```

---

## Project Structure

```
echo-loop-generator/
├── config.yaml              # Default configuration
├── main.py                  # CLI entry point (audio + text + batch modes)
├── requirements.txt
├── README.md
├── parser/
│   ├── __init__.py
│   ├── lrc_parser.py        # LRC file parser
│   └── text_parser.py       # Plain text file parser (text-only mode)
├── audio/
│   ├── __init__.py
│   ├── splitter.py          # Audio segment extraction
│   ├── tts_generator.py     # TTS generation (target + native)
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
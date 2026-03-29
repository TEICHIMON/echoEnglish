# Echo Loop Generator

Generate **T•N•T** (Target → Native → Target) Echo Loop audio files for language learning, with both audio-extraction and text-only TTS modes.

Based on *"Echo: Rebuilding the Natural Reflex of Language"* by H. Reeve.

## What is an Echo Loop?

The Echo Loop follows the **T-S-N-S-T-S** pattern:

```
[Target Audio] → [silence] → [Native TTS] → [silence] → [Target Audio] → [silence]
```

This structure uses the reflex energy of your native language to ignite comprehension of the target language — not through translation, but through resonance.

## Two Modes

**Audio mode** — you supply a source audio file and an LRC subtitle file. Target audio is extracted from the recording; native audio is generated via TTS.

```
source.mp3 + source.lrc  →  echo_loop.m4a + echo_loop.lrc
```

**Text-only mode** — you supply a plain text file with bilingual entries. Both target and native audio are generated entirely via TTS. No source recording needed.

```
phrases.txt  →  echo_loop.m4a + echo_loop.lrc
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

### Audio Mode

```bash
python main.py lesson01.mp3 lesson01.lrc
```

### Text-Only Mode

```bash
python main.py --text phrases.txt
```

### Config-Only (no CLI args needed)

Set all paths in `config.yaml` and just run:

```bash
python main.py
```

## Configuration (config.yaml)

All settings can be specified in `config.yaml`. CLI arguments always take priority over config values.

```yaml
# Mode: "audio" or "text" (optional)
# If omitted, auto-detected from paths.
# If both audio+lrc and text paths exist, defaults to audio.
# mode: "audio"

# File paths (CLI arguments override these)
paths:
  audio: ""              # source audio file — audio mode
  lrc: ""                # LRC subtitle file — audio mode
  text: ""               # bilingual text file — text-only mode
  output: ""             # output audio file (default: <input_name>_echo.<format>)
  output_lrc: ""         # output LRC file (default: same as output with .lrc)

timing:
  after_first_target: 0.8    # silence after 1st target (seconds)
  after_native: 0.5          # silence after native TTS (seconds)
  after_second_target: 1.2   # silence after 2nd target (seconds)

tts:
  target_voice: "ja-JP-NanamiNeural"    # target language voice (text-only mode)
  native_voice: "zh-CN-XiaoxiaoNeural"  # native language voice
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

### Config Examples

Audio mode via config only:

```yaml
mode: "audio"
paths:
  audio: "lessons/lesson01.mp3"
  lrc: "lessons/lesson01.lrc"
  output: "output/lesson01_echo.m4a"
  output_lrc: "output/lesson01_echo.lrc"
```

Text-only mode via config only:

```yaml
mode: "text"
paths:
  text: "data/phrases.txt"
  output: "output/phrases_echo.m4a"
tts:
  target_voice: "en-US-JennyNeural"
  native_voice: "zh-CN-XiaoxiaoNeural"
```

### Priority Rules

Settings are resolved in this order (first wins):

1. **CLI arguments** — always highest priority
2. **config.yaml** — used when CLI args are not provided
3. **Built-in defaults** — fallback

Mode detection follows the same priority:

1. `--mode` CLI flag
2. `mode:` in config.yaml
3. Auto-detect from paths — if both `audio`+`lrc` and `text` are set, audio mode wins

## Text File Format

One bilingual entry per line. Target text and native text are separated by a delimiter (default `-`). Blank lines and lines starting with `#` are ignored.

```
# Japanese → Chinese
一度の接種でハシカのMMRワクチンについて-关于一次接种即可预防麻疹的MMR疫苗
厚生労働省の専門家部会は了承しました-厚生劳动省的专家委员会已批准

# English → Chinese
The vaccine requires only one dose-该疫苗只需接种一次
The committee approved the proposal-委员会批准了该提案

# Japanese → English
一度の接種でハシカのMMRワクチンについて-About the MMR vaccine that prevents measles with one dose
```

Format: `<target_text><delimiter><native_text>`

The split strategy defaults to `last`, meaning the text is split on the **last** occurrence of the delimiter. This avoids issues when the delimiter character appears within the text itself.

## LRC File Format

Standard LRC with bilingual content separated by the same delimiter:

```
[00:00.39]一度の接種でハシカ...MMRワクチンについて-关于一次接种即可预防...的MMR疫苗
[00:06.74]厚生労働省の専門家部会は...了承しました-厚生劳动省的专家委员会已批准...
```

Format: `[timestamp]<target_text><delimiter><native_text>`

## CLI Reference

### Positional Arguments (Audio Mode)

| Argument | Description |
|----------|-------------|
| `audio` | Source audio file (mp3, wav, m4a, etc.) |
| `lrc` | LRC subtitle file with bilingual content |

### Options

| Flag | Description |
|------|-------------|
| `--text, -t FILE` | Text file for text-only mode |
| `--mode` | Force mode: `audio` or `text` (overrides config + auto-detection) |
| `-o, --output PATH` | Output audio file path |
| `--output-lrc PATH` | Output LRC file path |
| `-c, --config FILE` | Config file path (default: `config.yaml`) |

### Timing Overrides

| Flag | Default | Description |
|------|---------|-------------|
| `--after-first-target` | 0.8 | Silence after first target phrase (seconds) |
| `--after-native` | 0.5 | Silence after native phrase (seconds) |
| `--after-second-target` | 1.2 | Silence after second target phrase (seconds) |

### TTS Overrides

| Flag | Default | Description |
|------|---------|-------------|
| `--target-voice` | ja-JP-NanamiNeural | Target language TTS voice (text-only mode) |
| `--native-voice` | zh-CN-XiaoxiaoNeural | Native language TTS voice |
| `--voice` | — | Alias for `--native-voice` (backward compatible) |
| `--rate` | +0% | TTS speech rate (e.g., `+10%`, `-20%`) |

### LRC / Text Parsing Overrides

| Flag | Default | Description |
|------|---------|-------------|
| `--delimiter` | `-` | Delimiter between target and native text |
| `--split-strategy` | last | `first` or `last` delimiter occurrence |

## Examples

```bash
# Audio mode — basic
python main.py lesson01.mp3 lesson01.lrc

# Audio mode — custom output paths
python main.py lesson01.mp3 lesson01.lrc -o out/echo.m4a --output-lrc out/echo.lrc

# Audio mode — custom timing
python main.py lesson01.mp3 lesson01.lrc \
    --after-first-target 1.0 \
    --after-native 0.6 \
    --after-second-target 1.5

# Text-only mode — basic
python main.py --text phrases.txt

# Text-only mode — custom voices
python main.py --text phrases.txt \
    --target-voice en-US-JennyNeural \
    --native-voice zh-CN-XiaoxiaoNeural

# Text-only mode — Japanese target, English native
python main.py --text phrases.txt \
    --target-voice ja-JP-NanamiNeural \
    --native-voice en-US-GuyNeural

# Slower TTS
python main.py --text phrases.txt --rate "-10%"

# Config-only — all paths set in config.yaml
python main.py

# Config-only with a custom config file
python main.py -c my_lesson.yaml

# CLI overrides config — use config paths but override voice
python main.py --native-voice zh-CN-YunxiNeural
```

## Available Voices (edge-tts)

Any edge-tts voice can be used for either target or native. Here are some common ones:

### Chinese

| Voice | Gender | Description |
|-------|--------|-------------|
| zh-CN-XiaoxiaoNeural | Female | Standard, warm |
| zh-CN-YunxiNeural | Male | Standard, calm |
| zh-CN-XiaoyiNeural | Female | Lively |
| zh-CN-YunjianNeural | Male | Authoritative |

### Japanese

| Voice | Gender | Description |
|-------|--------|-------------|
| ja-JP-NanamiNeural | Female | Standard, clear |
| ja-JP-KeitaNeural | Male | Standard, calm |

### English

| Voice | Gender | Accent |
|-------|--------|--------|
| en-US-JennyNeural | Female | American |
| en-US-GuyNeural | Male | American |
| en-US-AriaNeural | Female | American |
| en-GB-SoniaNeural | Female | British |
| en-GB-RyanNeural | Male | British |
| en-AU-NatashaNeural | Female | Australian |

### Korean

| Voice | Gender | Description |
|-------|--------|-------------|
| ko-KR-SunHiNeural | Female | Standard |
| ko-KR-InJoonNeural | Male | Standard |

### French

| Voice | Gender | Description |
|-------|--------|-------------|
| fr-FR-DeniseNeural | Female | Standard |
| fr-FR-HenriNeural | Male | Standard |

Browse all available voices:

```bash
edge-tts --list-voices
edge-tts --list-voices | grep en-    # filter by language
```

## Project Structure

```
echo-loop-generator/
├── config.yaml              # Default configuration (paths, timing, TTS, output)
├── main.py                  # CLI entry point (audio + text modes)
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
└── export/
    ├── __init__.py
    ├── exporter.py           # Final audio export
    └── lrc_writer.py         # Echo Loop LRC generation
```

## License

MIT
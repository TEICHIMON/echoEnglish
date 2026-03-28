# Echo Loop Generator

Generate **T•N•T** (Target → Native → Target) Echo Loop audio files for language learning.

Based on *"Echo: Rebuilding the Natural Reflex of Language"* by H. Reeve.

## What is an Echo Loop?

The Echo Loop follows the **T-S-N-S-T-S** pattern:

```
[Target Audio] → [0.8s silence] → [Chinese TTS] → [0.5s silence] → [Target Audio] → [1.2s silence]
```

This structure uses the reflex energy of your native language to "ignite" comprehension
of the target language — not through translation, but through resonance.

## Requirements

- Python 3.10+
- ffmpeg (for m4a export)
- Internet connection (for edge-tts)

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Basic

```bash
python main.py lesson01.mp3 lesson01.lrc
```

This produces `lesson01_echo.m4a` in the same directory.

### Custom output path

```bash
python main.py lesson01.mp3 lesson01.lrc -o output/echo_lesson01.m4a
```

### Custom timing

```bash
python main.py lesson01.mp3 lesson01.lrc \
    --after-first-target 1.0 \
    --after-native 0.6 \
    --after-second-target 1.5
```

### Different TTS voice

```bash
# Male Chinese voice
python main.py lesson01.mp3 lesson01.lrc --voice zh-CN-YunxiNeural

# Slower TTS
python main.py lesson01.mp3 lesson01.lrc --rate "-10%"
```

### Custom config file

```bash
python main.py lesson01.mp3 lesson01.lrc -c my_config.yaml
```

## LRC Format

The LRC file should contain bilingual content separated by a delimiter (default: `-`):

```
[00:00.39]一度の接種でハシカ...MMRワクチンについて-关于一次接种即可预防...的MMR疫苗
[00:06.74]厚生労働省の専門家部会は...了承しました-厚生劳动省的专家委员会已批准...
```

Format: `[timestamp]<target_text><delimiter><native_text>`

## Configuration (config.yaml)

```yaml
timing:
  after_first_target: 0.8    # silence after 1st target (seconds)
  after_native: 0.5          # silence after native TTS (seconds)
  after_second_target: 1.2   # silence after 2nd target (seconds)

tts:
  voice: "zh-CN-XiaoxiaoNeural"
  rate: "+0%"
  pitch: "+0Hz"

output:
  format: "m4a"
  bitrate: "192k"
  sample_rate: 44100

lrc:
  delimiter: "-"
  split_strategy: "last"     # split on last "-" to avoid issues
```

## Available Chinese Voices (edge-tts)

| Voice | Gender | Description |
|-------|--------|-------------|
| zh-CN-XiaoxiaoNeural | Female | Standard, warm |
| zh-CN-YunxiNeural | Male | Standard, calm |
| zh-CN-XiaoyiNeural | Female | Lively |
| zh-CN-YunjianNeural | Male | Authoritative |
| zh-CN-XiaochenNeural | Female | Gentle |

List all voices: `edge-tts --list-voices | grep zh-CN`

## Project Structure

```
echo-loop-generator/
├── config.yaml              # Default configuration
├── main.py                  # CLI entry point
├── requirements.txt
├── README.md
├── parser/
│   ├── __init__.py
│   └── lrc_parser.py        # LRC file parser
├── audio/
│   ├── __init__.py
│   ├── splitter.py          # Audio segment extraction
│   ├── tts_generator.py     # Chinese TTS via edge-tts
│   └── assembler.py         # Echo Loop assembly (T-S-N-S-T-S)
└── export/
    ├── __init__.py
    └── exporter.py           # Final audio export
```

## License

MIT

# Shell 脚本工具集

Echo Loop Generator 周边的 shell 脚本工具，用于自动化从字幕生成到音频处理的完整流程。

---

## 脚本一览

| 脚本 | 用途 | TTS 引擎 |
|------|------|----------|
| `echo_pipeline` | 字幕生成 → Echo Loop 音频（主力脚本） | edge-tts |
| `echo_pipeline_openai` | 同上，支持 OpenAI TTS | edge-tts / OpenAI |
| `extract_audio` | 从视频批量提取音频为 m4a | — |

---

## echo_pipeline

日常使用的主力脚本。完整流水线：运行 subtitle-automation 生成字幕，检测新文件夹，自动调用 echoEnglish 生成 Echo Loop 音频。

**适用场景：** 日常语言学习内容，纯文本对话和句子，不含数学公式或技术符号。

```bash
# 完整流水线：字幕 → Echo Loop
echo_pipeline

# 跳过字幕步骤，直接处理指定文件夹
echo_pipeline --echo /path/to/folder
```

脚本会根据文件夹名自动选择 target voice：
- 文件夹名含 "English" → `en-US-JennyNeural`
- 文件夹名含 "Japanese" → `ja-JP-NanamiNeural`
- 其他 → `en-US-JennyNeural`（默认）

---

## echo_pipeline_openai

echo_pipeline 的扩展版，增加了 OpenAI TTS 支持。不加 `--openai` 时行为和原版完全一样。

**适用场景：** 学习素材包含数学公式（`x²+y²=r²`）、化学式、技术符号等 edge-tts 无法自然朗读的内容。OpenAI 的 gpt-4o-mini-tts 能理解语义，把公式读成口语。

```bash
# 用 edge-tts（和原版一样）
echo_pipeline_openai

# 用 OpenAI TTS
echo_pipeline_openai --openai

# 指定 OpenAI 语音
echo_pipeline_openai --openai --openai-voice sage

# 跳过字幕 + OpenAI
echo_pipeline_openai --echo /path/to/folder --openai
```

需要提前在 `echoEnglish/.env` 中设置 `OPENAI_API_KEY`。

**如何选择：**

```
素材包含数学/技术符号？
    ├─ 否 → echo_pipeline
    └─ 是 → echo_pipeline_openai --openai
```

---

## extract_audio

独立的视频音频提取工具。扫描指定文件夹下的所有视频文件，用 ffmpeg 提取音频转为 m4a 格式，输出到同一文件夹。

**适用场景：** 拿到一批视频文件（mp4、mkv 等），需要先提取出音频再送入 Echo Loop 流水线处理。已存在同名 .m4a 的会自动跳过。

```bash
# 基本用法
extract_audio /path/to/videos

# 自定义比特率
extract_audio /path/to/videos --bitrate 256k

# 自定义采样率
extract_audio /path/to/videos --sample-rate 48000

# 覆盖已存在的文件
extract_audio /path/to/videos --overwrite
```

支持的视频格式：mp4、mkv、avi、mov、wmv、flv、webm、ts、m2ts、mpg、mpeg、3gp。

---

## 安装

```bash
# 把脚本放到 PATH 中
cp echo_pipeline ~/bin/
cp echo_pipeline_openai ~/bin/
cp extract_audio ~/bin/
chmod +x ~/bin/echo_pipeline ~/bin/echo_pipeline_openai ~/bin/extract_audio

# 前置依赖
brew install ffmpeg
```

---

## 典型工作流

```
视频文件
  │
  ▼
extract_audio          ← 提取音频为 m4a
  │
  ▼
subtitle-automation    ← 生成双语字幕（LRC）
  │
  ▼
echo_pipeline          ← 生成 T-S-N-S-T-S Echo Loop 音频
  │
  ▼
_echo.m4a + _echo.lrc  ← 可直接用于听力练习的成品
```
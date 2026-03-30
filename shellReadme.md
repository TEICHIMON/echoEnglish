# Echo Pipeline 使用指南

## 概览

你有两个 shell 脚本，分别对应两条不同的 TTS 路径：

| 脚本 | 文件名建议 | TTS 引擎 | 适用场景 |
|------|-----------|----------|---------|
| **原版** | `echo_pipeline` | edge-tts（免费） | 日常语言学习内容，纯文本对话/句子 |
| **扩展版** | `echo_pipeline_openai` | edge-tts + OpenAI（按量付费） | 含数学公式、技术符号、需要语义理解的内容 |

两个脚本做的事情完全一样——只是生成 native 语音的引擎不同。流程都是：

```
subtitle-automation (生成字幕+音频)
        ↓
   检测新建的文件夹
        ↓
  echoEnglish --scan (生成 Echo Loop 音频)
```

---

## 脚本差异对照

```
                        原版                          扩展版
──────────────────────────────────────────────────────────────────────
TTS 引擎              edge-tts only                edge-tts (默认) / OpenAI (--openai)
Python 调用           python main.py               完整 conda 路径 ①
subtitle 命令         npm run dev:translate         npm run dev ②
voice 选择逻辑        folder 名 → target_voice      folder 名 → target_voice (edge)
                                                   folder 名 → instructions (openai)
API Key 要求          无                            --openai 时需要 OPENAI_API_KEY
CLI 参数              --echo DIR                    --echo DIR
                                                   --openai
                                                   --openai-voice <voice>
```

> ① 原版用 `python main.py`，依赖 shell 环境中 `python` 指向正确的 conda 环境。扩展版直接硬编码 `/opt/homebrew/anaconda3/envs/echo_env/bin/python`，不依赖 shell 的 PATH。如果你在 `.zshrc` 中已经 activate 了 `echo_env`，两者效果一样；如果没有，扩展版更可靠。
>
> ② 这是 subtitle-automation 项目的 npm script 名称差异，按你实际项目中 `package.json` 的定义为准。如果两个命令等价，可以统一。

---

## 什么时候用哪个

### 用原版 `echo_pipeline`

绝大多数时候用这个。适合：

- 日语 → 中文 的日常对话练习
- 英语 → 中文 的句型/词汇练习
- 任何不含特殊符号的纯文本内容
- 不想产生 API 费用的场景

edge-tts 是微软免费的 TTS 服务，质量已经很好，支持多语言多音色，并发生成速度快。

### 用扩展版 `echo_pipeline_openai`

当内容包含 edge-tts 读不好的东西时切换过来：

- **数学公式**：`2ⁿ`、`∑`、`∫`、`x² + y² = r²` — edge-tts 会逐字符读，OpenAI 能读成「x的平方加y的平方等于r的平方」
- **技术符号**：化学式、物理单位、编程表达式
- **需要语境理解的文本**：同一个符号在不同上下文有不同读法
- **你需要用 instructions 精确控制发音方式**

费用参考：OpenAI TTS 按字符计费，一般一次 batch 处理几十条句子花费很低（几美分级别）。

### 决策流程

```
你的学习素材包含数学/技术符号吗？
    │
    ├─ 否 → echo_pipeline（原版，免费）
    │
    └─ 是 → echo_pipeline_openai --openai
```

---

## 安装和前置条件

### 两个脚本都需要

```bash
# 1. echoEnglish 项目依赖
cd /Volumes/SP/code/python/echoEnglish
conda activate echo_env
pip install -r requirements.txt

# 2. subtitle-automation 项目依赖
cd /Volumes/SP/code/subtitle-automation
npm install

# 3. ffmpeg（m4a 导出需要）
brew install ffmpeg

# 4. 把脚本放到 PATH 中
cp echo_pipeline ~/bin/
cp echo_pipeline_openai ~/bin/
chmod +x ~/bin/echo_pipeline ~/bin/echo_pipeline_openai

# 5. subtitle-automation 的 .env 中必须定义 SYNC_DIR
#    脚本启动时会从这里读取同步目录路径
cat /Volumes/SP/code/subtitle-automation/.env
# SYNC_DIR=/path/to/your/sync/folder
```

### 仅扩展版额外需要

```bash
# OpenAI API Key — 二选一：

# 方式 A：写入 echoEnglish 项目的 .env（推荐，脚本会自动 source）
echo 'OPENAI_API_KEY=sk-...' >> /Volumes/SP/code/python/echoEnglish/.env

# 方式 B：export 到 shell 环境
export OPENAI_API_KEY="sk-..."
```

---

## 使用方法

### 原版 echo_pipeline

#### 完整流水线（最常用）

```bash
echo_pipeline
```

执行过程：
1. 进入 `subtitle-automation` 目录，运行 `npm run dev:translate`
2. 完成后，比对 SYNC_DIR 前后的文件夹列表，找出新建的文件夹
3. 对每个新文件夹运行 `echoEnglish --scan`，根据文件夹名自动选择 target voice：
   - 文件夹名含 "English" → `en-US-JennyNeural`
   - 文件夹名含 "Japanese" → `ja-JP-NanamiNeural`
   - 其他 → 默认 `en-US-JennyNeural`
4. 输出的 `_echo.m4a` 和 `_echo.lrc` 生成在源文件同目录

#### 跳过字幕步骤，直接处理指定文件夹

```bash
echo_pipeline --echo /path/to/folder
```

适用场景：
- subtitle-automation 已经跑过了，你只想重新生成 Echo Loop
- 手动准备的 audio+LRC 文件夹
- 调试某个特定文件夹的输出效果

### 扩展版 echo_pipeline_openai

#### 完整流水线 + OpenAI TTS

```bash
echo_pipeline_openai --openai
```

和原版流程完全一样，但 native TTS 用 OpenAI 引擎。脚本会：
- 根据文件夹名自动选择对应语言的 `instructions`：
  - "English" → 英文 instructions
  - "Japanese" → 日文 instructions
  - 其他 → 英文 instructions（默认）
- 使用默认 voice `coral`

#### 完整流水线 + edge-tts（不加 --openai）

```bash
echo_pipeline_openai
```

不加 `--openai` 时，行为和原版脚本一样，用 edge-tts。

#### 指定 OpenAI 语音

```bash
echo_pipeline_openai --openai --openai-voice sage
```

可选 voice：`alloy`、`ash`、`ballad`、`cedar`、`coral`、`echo`、`fable`、`nova`、`onyx`、`sage`、`shimmer`、`verse`

#### 跳过字幕 + OpenAI

```bash
echo_pipeline_openai --echo /path/to/folder --openai
echo_pipeline_openai --echo /path/to/folder --openai --openai-voice nova
```

---

## 实际操作举例

### 场景 1：日常英语听力练习

你下载了一个英语播客，已经有了音频和字幕。

```bash
# 方式 A：走完整流水线（subtitle-automation 会处理新内容）
echo_pipeline

# 方式 B：字幕已经准备好了，直接处理
echo_pipeline --echo /path/to/sync/2025-03-30_English_Podcast
```

结果：文件夹内每个 audio+LRC 对都生成对应的 `_echo.m4a` + `_echo.lrc`。

### 场景 2：日语新闻听力

```bash
echo_pipeline --echo /path/to/sync/2025-03-30_Japanese_News
```

脚本检测到文件夹名含 "Japanese"，自动选择 `ja-JP-NanamiNeural` 作为 target voice。

### 场景 3：数学课内容（含公式）

你有一份数学课的字幕，里面有 `x²+y²=r²`、`∑(n=1,∞)`。

```bash
# edge-tts 会读成 "x 上标 2 加 y 上标 2 等于 r 上标 2" → 不自然
# OpenAI 会读成 "x的平方加y的平方等于r的平方" → 自然

echo_pipeline_openai --echo /path/to/sync/2025-03-30_Math_Lecture --openai
```

### 场景 4：批量处理一整天的新内容

```bash
# 早上运行一次，处理所有新字幕
echo_pipeline

# 下午发现有数学相关的新内容需要重跑
echo_pipeline_openai --echo /path/to/sync/2025-03-30_Calculus --openai
```

### 场景 5：测试不同 OpenAI 语音效果

```bash
# 用 coral（默认，温暖女声）
echo_pipeline_openai --echo /path/to/test_folder --openai

# 用 sage（平静男声）
echo_pipeline_openai --echo /path/to/test_folder --openai --openai-voice sage

# 用 nova（活泼女声）
echo_pipeline_openai --echo /path/to/test_folder --openai --openai-voice nova
```

---

## 输出文件说明

无论用哪个脚本，输出格式一致：

```
源文件夹/
  lesson01.mp3          ← 原始音频
  lesson01.lrc          ← 原始字幕
  lesson01_echo.m4a     ← Echo Loop 音频（T-S-N-S-T-S）
  lesson01_echo.lrc     ← 重新计算时间戳的双语字幕
```

`_echo.lrc` 保留原始的双语文本格式（target + delimiter + native），只有时间戳是重新计算的。

---

## 故障排查

| 问题 | 原因 | 解决 |
|------|------|------|
| `SYNC_DIR not found` | subtitle-automation 的 `.env` 缺少 `SYNC_DIR=` | 在 `.env` 中添加 |
| `OPENAI_API_KEY not set` | 用了 `--openai` 但没设 key | 加到 `echoEnglish/.env` 或 `export` |
| `No new sync folders created` | subtitle-automation 没产出新内容 | 正常，说明没有新素材要处理 |
| `conda activate` 报错 | 非交互式 shell 不支持 conda activate | 扩展版已用完整路径解决；原版确保 PATH 中 python 指向 echo_env |
| edge-tts 报错 | 网络问题或微软服务临时不可用 | 重试；batch 模式下单个失败不影响其他文件 |
| OpenAI TTS 很慢 | OpenAI 请求是串行的（避免 rate limit） | 正常，等待即可 |

---

## 合并为单个脚本（可选）

如果你不想维护两个脚本，可以只保留扩展版并重命名为 `echo_pipeline`：

```bash
cp echo_pipeline_openai ~/bin/echo_pipeline
```

不加 `--openai` 时行为和原版完全一样。唯一需要确认的是：
1. 把 Python 路径统一为完整 conda 路径（更可靠）
2. 把 `npm run dev` 改成你实际用的命令（`dev` 还是 `dev:translate`）
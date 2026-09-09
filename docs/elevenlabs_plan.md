# ElevenLabs TTS 接入计划（日语 / 英语）

> 2026-09-08 定稿。当前状态：**未动代码，等 Phase 0 实测结论**。
>
> 这份文档是自包含的 —— 新会话不需要之前的对话记录，读这一份就能接着干。

## 目标

给 echoEnglish 加第四个 TTS 引擎 `elevenlabs`，用在**目标语言**（日语 / 英语）上。
动机是日语听力训练，Google Chirp3-HD 的日语够用但不够像真人。

**用户预算**：先用免费档（10,000 credits/月）验证；效果确实好再开 $6/月 Starter
（30,000 credits）。所以整个 Phase 0 必须在 10,000 credits 内跑完。

---

## Phase 0：先验证，再写代码

**这是新会话要做的事。** 之所以先验证，是因为下面「已知风险」那一节里有一条可能
直接否掉 v3，而它只需要一次 API 调用就能试出来。

### 前置

1. 用户自己去 https://elevenlabs.io 注册免费账号，从 dashboard 拿 API key
   （**免费档确实有 API**，见「已确认的事实」）
2. key 放进 `.env` 的 `ELEVENLABS_API_KEY`，不要写进代码或提交

### 要回答的问题（按优先级）

| # | 问题 | 为什么关键 |
|---|---|---|
| **1** | **v3 是否接受 `previous_text` / `next_text`？** | 决定 v3 能不能用。见「已知风险 A」 |
| 2 | v3 在 ~25 字符的孤立日语句上，输出稳不稳（音色/语速逐句漂移？） | 同上 |
| 3 | `previous_text` / `next_text` **是否计入 credits**？ | 计费的话成本翻 2–3 倍，直接改预算结论 |
| 4 | 日语盲听排名：v3 / v3+stitching / multilingual_v2+stitching / flash_v2_5 / Chirp3-HD（基线） | 决定值不值得付费，以及选哪个模型 |
| 5 | `speed` 实际可用范围（0.25–4.0 还是 0.7–1.2？拉到多少开始掉质量） | 决定要不要按引擎钳位，见「待确认的分歧」 |
| 6 | v3 的 `voice_settings` 到底支持哪几个字段 | 同上 |
| 7 | 免费档并发 2 的真实行为（超了返回什么错误码/消息） | 决定重试逻辑里匹配哪些字符串 |

### 素材与预算

用现成的真日语稿，别新写：

```
outputs/e0b8f339eaba/moneyForward 面试准备_ja.txt      110 行 / 2,505 日语字符
outputs/ad49ed25473f/anyball interview_ja.txt          174 行 / 5,150 日语字符
outputs/d0a60ca1b2ff/anyball casual interview_ja.txt    46 行 / 1,220 日语字符
```

取其中 20 行（约 500 日语字符）跑对比：

| 配置 | credits |
|---|---:|
| `eleven_v3` 裸跑 | 500 |
| `eleven_v3` + `previous_text`/`next_text` | 500 |
| `eleven_multilingual_v2` + stitching | 500 |
| `eleven_flash_v2_5`（0.5 credit/char） | 250 |
| `ja-JP-Chirp3-HD-Charon`（现有，做基线） | 0 |
| **合计** | **~1,750** |

剩下 8,000+ credits 够再整份跑一遍 casual 稿（1,220 字符）听长段落效果。

### spike 脚本要求

写成一次性脚本放 `scripts/` 或临时目录，**不要**改动 `audio/tts_generator.py`：

- 直接调 REST（`POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}`，
  header `xi-api-key`，query `output_format=mp3_44100_128`），或用 `elevenlabs` SDK
- 每条一个 mp3，文件名带配置标识，方便盲听
- **读响应头里的 character-cost**，用来回答问题 3（不要靠猜）
- 剥假名注音后再送 TTS：复用 `parser.lrc_parser.strip_furigana`，别自己写正则
- 并发定 2

### 输出

在本文件末尾追加一节「Phase 0 实测结果」，写清楚：每个问题的答案、盲听排名、
实际消耗的 credits、以及最终选定的 `model_id`。然后才进 Phase 1。

---

## 已确认的事实（2026-09-08 查证）

### 计费与档位

| 档位 | 价格 | credits/月 | multilingual v2 并发 | flash 并发 |
|---|---:|---:|---:|---:|
| Free | $0 | 10,000 | **2** | 4 |
| Starter | $6 | 30,000 | 3 | 6 |
| Creator | $22（首月 $11） | 121,000 | 5 | 10 |
| Pro | $99 | 600,000 | 10 | 20 |

- `eleven_v3` 和 `eleven_multilingual_v2` 都是 **1 credit / 字符**（同价）
- `eleven_flash_v2_5` / `eleven_turbo_v2_5` 走 API 是 **0.5 credit / 字符**
- **免费档有 API**：官方 models 文档为 Free 档单列了并发上限；help center 也写
  大部分 endpoint 对所有档位开放，生成 API key 时带 free tier。
  （有第三方聚合站说免费档没 API，是错的）
- 免费档**无商用授权且要求署名** —— 个人听力训练不受影响
- 免费档输出格式用 `mp3_44100_128`（192k 要 Creator+）；下游 pydub 解码后重编码成
  m4a，128k 源足够

### `eleven_v3` 状态

- 2026-02-02 离开 alpha，已 GA，**公开 API 可用**
- `model_id: "eleven_v3"`，走标准 `POST /v1/text-to-speech/{voice_id}`
- 74 种语言含日语，单请求 5,000 字符上限

### 日语支持

`eleven_v3` / `eleven_multilingual_v2`（29 语言）/ `eleven_flash_v2_5`（32 语言）
**都支持日语**。flash v2.5 是 v2 全部语言 + 匈牙利/挪威/越南。

---

## 实测：语速与成本（本项目自己的产物，不是网上估算）

`outputs/safie_hybrid_2026-08-17/tts_google/` 存着 279 个 Google Chirp3-HD 日语 clip，
`output_timeline.json` 里正好 279 行文本一一对应。ffprobe 直接量：

```
279 条 clip，总时长 1330.3s (22.2 min)，总字符 7307
→ 5.49 char/s = 330 字符/分钟
```

英语没有缓存 clip，用三份 en/ja 双语产物的总时长反解（中文旁白项在 ja/en 之间
完全相同，可消去）：658 / 706 / 769 字符/分钟，三份一致，取 **~710**。

> 这是 Google Chirp3-HD 在 speaking_rate 1.0 下的语速，拿来当 ElevenLabs 的代理值。
> EL 默认 speed 1.0 的节奏同量级，但真实值大概有 ±15% 出入。

### 换算

| | 纯日语 | 纯英语 |
|---|---:|---:|
| 免费 10,000 credits | ~30 分钟 | ~14 分钟 |
| **$6 / 30,000** | **~91 分钟** | ~42 分钟 |
| $22 / 121,000 | ~6.1 小时 | ~2.8 小时 |
| $6 + flash_v2_5（半价） | ~182 分钟 | ~84 分钟 |

**日语每 credit 买到的时长是英语的 2.2 倍**（汉字信息密度高）。这对本项目有利。

### 但「91 分钟」不是 Echo 成品时长

Echo 每句 T-S-T 重复播放，而 **TTS 只生成一次**（config.yaml 里写明
"TTS is generated once and shared between both files (no extra API cost)"）。
实测 moneyForward 那份：日语目标语音 2,505 字符 ≈ 7.6 分钟纯语音，
成品 `_ja_tst.m4a` 是 **25.8 分钟** —— 3.4 倍。

**$6 ≈ 91 分钟纯日语语音 ≈ 300 分钟 Echo 成品。**

### 按真实稿件算

| 稿件 | 行数 | 日语字符 | 英语字符 | 中文字符 | 日语平均/行 |
|---|---:|---:|---:|---:|---:|
| moneyForward 面试准备 | 110 | 2,505 | 5,150 | 1,794 | 22.8 |
| anyball interview | 174 | 5,150 | 9,601 | 3,821 | 29.6 |
| anyball casual interview | 46 | 1,220 | 2,206 | 862 | 26.5 |

30,000 credits 能做：

- **只有日语走 EL**（中文留 Google）：约 **12 份** moneyForward 量级 / 5–6 份 anyball 量级
- **日语+中文都走 EL**：约 7 份 / 3 份 —— 中文旁白吃掉 **42%** 额度

---

## 已知风险

### A. v3 要长文本，而 Echo 只喂它一句 —— 且 v3 关掉了正解

官方 v3 prompting 指南：很短的 prompt 更容易产生不一致的输出，建议 **250 字符以上**。

你的日语稿每行 22.8 / 29.6 / 26.5 字符，是推荐下限的 **1/10**。而且 Echo 逐句独立
请求，v3 的表现力恰恰建立在整段情感弧线上 —— 一句一请求正好把它最强的地方废掉。

标准解法是 **Request Stitching**：`text` 只放当前这一句（返回音频也只有这一句），
前后句放进 `previous_text` / `next_text` 当上下文。这既给了模型上下文，又保持
「一句一 clip」，不违反 CLAUDE.md 的 TTS/LRC 硬规则。

**但官方 cookbook 明写：`Request stitching is not available for the eleven_v3 model.`**

**留了一个口子**：那句话所在的页面讲的主要是 `previous_request_ids`。而
`previous_text` / `next_text` 是 convert 端点 schema 里独立的字段，v3 是否也一并
禁掉，文档没写死。→ **这就是 Phase 0 的问题 1，一次调用就能试出来。**

**不能用「几句合成一个请求再切」绕过。** CLAUDE.md 的 TTS/LRC 硬规则明确禁止：
「一条最终字幕必须一对一对应一个 target 音频片段」「严禁先生成多句/整段 TTS，
再按字符数、单词数、平均语速或比例估算句内时间轴」。

**如果口子堵死**：退回 `eleven_multilingual_v2` + stitching。价格完全一样
（1 credit/char），$6 的判断不变，引擎层代码也是同一套 —— 只是换个 `model_id`。

### B. 日语汉字误读没法修

multilingual_v2 不支持 SSML / phoneme 标签，读错的音**没有办法纠正**（Google 至少
还能用 SSML 兜）。

项目现在的 `strip_furigana`（`parser/lrc_parser.py:36`）是把 `漢字（かんじ）` 里的
假名剥掉、把**汉字**喂给 TTS。如果 EL 误读明显，一个现成的解法是反过来给 EL 喂假名
（`target_text` 里的注音本来就在）。**这是另一个改动，Phase 0 先量误读率，别提前动手。**

> 参见 CLAUDE.md 里 OP/ED 歌声那条的教训：拿到现象先验证原因，别假设一个就动手修。

### C. 声音的口音是烤进 voice_id 里的

Google Chirp3-HD 是 `<区域>-Chirp3-HD-<persona>`，所以 UI 只让选 persona、语言由预设
决定，音色永远不会和文本语言错配（见 `webapp/jobs.py:461` 那段注释）。

ElevenLabs 没有这个结构：voice_id 不透明（`21m00Tcm4TlvDq8ikWAM` 这种），且**口音是
烤进声音里的** —— 拿英语母语的声音说日语会带口音。所以 EL 的 voice 必须**按语言分开
配置**，UI 那套 persona 下拉对 EL 不适用。

---

## 待确认的分歧（Phase 0 一并验掉）

| 项 | 分歧 |
|---|---|
| `speed` 范围 | 官方 skills 仓库的 voice-settings 参考写 **REST API 是 0.25–4.0**，0.7–1.2 是 Agents Platform 的范围；convert 端点 schema 没写硬边界。**上一版计划里「必须按引擎钳位到 0.7–1.2」的要求可能不成立** |
| v3 的 `voice_settings` | skills 参考说 stability/similarity_boost/style/use_speaker_boost/speed 五个全支持；另有来源说 v3 不支持 `similarity_boost` 和 `use_speaker_boost` |
| `previous_text` 计费 | 未查到明确说法。**必须实测**（读响应头 character-cost），计费的话成本翻 2–3 倍 |

---

## Phase 1+：代码落地（Phase 0 通过后再做）

三个引擎（`google` / `edge` / `openai`）不是插件，是**散在各处的 if-elif 分支** ——
`grep '"edge"\|"openai"\|"google"'` 在 main.py + webapp + audio 里有 123 处。
加第四个引擎主要是把这些分支补齐，核心合成代码反而是最小的一块。

### 接入点

| 层 | 文件 | 要做什么 |
|---|---|---|
| 合成 | `audio/tts_generator.py` | 加 `_run_eleven_batch`，在 `generate_target_audio` / `generate_native_audio` 里加分支。约 100 行，照 `_run_google_batch` 的 ThreadPool + 退避重试写。**不需要** Google 那套 `_split_long_text`（EL 单请求 5,000 字符，一行远够） |
| 配置 | `main.py:230` `load_config` | `tts.elevenlabs` 默认段 + 合并分支 + engine 白名单（`main.py:299`、`main.py:624`） |
| 语言预设 | `main.py:67` `LANG_PRESETS` / `INTERVIEW_LANG_PRESETS` | 每语言一个 voice_id（原因见风险 C） |
| 语速 | `main.py:139` `apply_speaking_rate` | 现在一次性写三个引擎，要加第四个 |
| 面试角色 | `main.py:1347` `_config_for_interview_role` | Q/A 两个 voice_id |
| 交互向导 | main.py 约 1146/1176/1217/1234/1385/1540/1800/1830 | 8 处 engine 分支 + `_engine_label` |
| Web 后端 | `webapp/jobs.py:62` `VALID_ENGINES`、`_apply_voice_overrides`、`_apply_rate_overrides` | 音色覆盖逻辑现在写死了 Google persona |
| Web 前端 | `webapp/static/index.html:151`、`webapp/static/app.js:271` | 引擎下拉 + 音色选择器（`updateVoiceUI` 现在对非 Google 直接 disable） |
| 部署 | requirements×2、.env.example、docker-compose、docs/deploy_vps.md | `ELEVENLABS_API_KEY` 透传 |

**容易漏的坑**：`webapp/server.py:232` 的 `_text_default_lang` 是**从 voice 名字前缀猜
语言**（`ja-JP-Chirp3-HD-Charon` → `ja`）。EL 的 voice_id 不透明，这个函数会静默返回
空。要么反查 `lang_presets`，要么显式存 lang。

### 三个设计决策

**1. 中文旁白不走 ElevenLabs。** 现在 `engine` 是全局开关，`_tts_engine_kwargs`
（`main.py:1971`）target 和 native 用同一个。选了 EL 中文也走 EL —— 按上面实测，
中文吃掉 42% 额度，换来的是 Chirp3-HD 已经做得很好的中文。
→ **加 `tts.native_engine`，target 走 EL、native 留 Google。在 $6 预算下这不是优化，
是必需项。**

**2. `model_id` 做成配置项，不写死**（`tts.elevenlabs.model_id`）。这样 Phase 0 的
五个配置在同一份代码上就能切；而且不管最后选 v3 还是 multilingual_v2，引擎层代码是
同一套，以后 EL 出新模型也不用改代码。

**3. 并发做成配置项**（`tts.elevenlabs.concurrency`），默认 **2**（免费档
multilingual v2 的上限），不要照 Google 抄 3 —— 超了会吃
`too_many_concurrent_requests`。升档时改配置不改代码。
（174 行的稿子按并发 2 大概 3 分钟跑完，不影响使用。）

### 配置结构草案

```yaml
tts:
  engine: "elevenlabs"
  native_engine: "google"        # 决策 1：中文旁白留 Google
  elevenlabs:
    model_id: "eleven_multilingual_v2"   # 决策 2；Phase 0 定最终值
    concurrency: 2                        # 决策 3
    output_format: "mp3_44100_128"
    stitching: true                       # previous_text / next_text
    target_voice: ""                      # 由 lang_presets 填
    speed: 1.0
    stability: 0.5
    similarity_boost: 0.75
  lang_presets:
    ja:
      google: "ja-JP-Chirp3-HD-Charon"
      edge:   "ja-JP-NanamiNeural"
      elevenlabs: "<日语母语 voice_id>"    # 风险 C：按语言分开配
    en:
      google: "en-US-Chirp3-HD-Puck"
      edge:   "en-US-JennyNeural"
      elevenlabs: "<英语母语 voice_id>"
```

### 落地顺序

1. 合成层（`audio/tts_generator.py`）+ 单元测试（mock API）
2. 配置 / CLI / 语言预设 / 面试角色
3. Web 后端 + 前端
4. 部署（requirements、env、docker-compose、VPS 文档）

**部署那步注意**：`requirements-web.txt` 必须同步。2026-09-07 就因为 web 镜像缺
numpy 导致站点 502（`main.py` 启动时 import 一切）。推之前先在 requirements-web 的
干净 venv 里验 `import webapp.server`。

---

## 不需要同步 interview-notes

CLAUDE.md 顶上那条硬性同步规则管的是「audio 生成 **prompt** 的说明」。换 TTS 引擎
不碰 prompt 文案、也不碰脚本格式约定，**不触发**同步。

例外：如果因为风险 B（汉字误读）改了假名 / 注音的格式约定，那就触发了，
必须同步 `~/WebstormProjects/resume20260521/interview-notes/.claude/commands/audio.md`
和本项目 CLAUDE.md 的 `## AUDIO contract` 章节。

---

## 参考链接

- https://elevenlabs.io/docs/overview/models — 模型列表 + 分档并发表
- https://elevenlabs.io/docs/models — 各模型语言表
- https://elevenlabs.io/docs/best-practices/prompting/eleven-v3 — v3 prompting（250 字符那条）
- https://elevenlabs.io/docs/eleven-api/guides/cookbooks/text-to-speech/request-stitching — stitching（v3 不支持那条）
- https://elevenlabs.io/docs/api-reference/text-to-speech/convert — convert 端点 schema
- https://github.com/elevenlabs/skills/blob/main/text-to-speech/references/voice-settings.md — voice_settings 参考
- https://elevenlabs.io/pricing — 档位与价格

---

## Phase 0 实测结果（2026-09-08）

> 脚本：`scripts/elevenlabs_spike.py`（子命令 probe / add-voices / validate / concurrency /
> compare / timestamps / google / report）。产物在 `outputs/elevenlabs_spike/`：每次调用的
> 状态码、响应头和错误体在 `calls.jsonl`，每个 clip 的解码时长/响度/首尾静音在 `stats.jsonl`。
>
> **实际消耗：1,444 credits**（`character-cost` 响应头逐次求和 = dashboard 计数，两者一致）。

### 先说结论：免费档验不了核心问题

**免费档不能通过 API 调用 Voice Library 的声音。** 三个日语母语声音（Otani / Asahi /
Morioki）加进账号成功（`POST /v1/voices/add` 返回 200，占了 3 个免费槽位），但一调
TTS 就是 HTTP 402：

```
paid_plan_required: Free users cannot use library voices via the API.
```

免费档可调的只有 21 个 premade 声音，**全部是英语**（labels.language = en）。所以问题 4
「日语盲听排名」在免费档无法回答 —— 风险 C 已写明口音烤在声音里，拿英语声音说日语
不代表最终效果。要听母语声音必须开 Starter（$6，同时并发升到 3）。

下面所有 EL 结果都是用 premade 的 `Sarah`（`EXAVITQu4vr4xnSDxMaL`）跑日语得到的，
**只回答与声音无关的问题**（协议、计费、并发、语速漂移）。

### 七个问题的答案

| # | 问题 | 答案 |
|---|---|---|
| **1** | v3 接受 `previous_text` / `next_text`？ | **否。** HTTP 400 `unsupported_model`："Providing previous_text or next_text is not yet supported with the 'eleven_v3' model." 是硬拒绝，不是静默忽略。风险 A 的口子堵死 |
| 2 | v3 孤立短句（平均 26 字）输出稳不稳 | 语速没有异常漂移。和 Google Charon **同一行配对**的语速比：v3 均值 1.006、标准差 0.071；mv2+stitch 0.990 / 0.089；flash 1.091 / 0.100。响度标准差三者都在 0.7–0.8 dB。v3 并不比 mv2 抖 |
| 3 | `previous_text` / `next_text` 计费？ | **不计费。** mv2+stitching 20 行：正文 516 字符，上下文 996 字符，`character-cost` 合计 516。flash 合计 260（0.504/字符，按请求向上取整） |
| 4 | 日语盲听排名 | **免费档做不了**（见上）。已留 8 个样本在 `outputs/elevenlabs_spike/listen/`（第 3 行含 React/TypeScript/Java，第 19 行含 P95/数字），四个配置各一份，仅供感受模型差异 |
| 5 | `speed` 范围 | **0.7–1.2 是服务端硬校验**（mv2）：3.0 / 0.5 / 1.3 / 0.6 全部 400 `invalid_voice_settings`，错误信息原文 "expected to be greater or equal to 0.7 and less or equal to 1.2"。「待确认的分歧」第一条：**必须钳位**，上一版计划是对的 |
| 6 | v3 的 `voice_settings` 支持哪些字段 | stability / similarity_boost / style / use_speaker_boost / speed 五个一起发 → 200；stability=0.3 → 200。**但塞一个不存在的字段也返回 200**，所以「接受」不等于「生效」，这一条只能靠听 |
| 7 | 免费档并发 2 超了会怎样 | 4 个并发：2 个 200，2 个**立即** 429（0.2s 返回，不排队）。错误体 `detail.code = "concurrent_limit_exceeded"`、`detail.status = "too_many_concurrent_requests"`。重试逻辑匹配 `concurrent_limit_exceeded` 即可 |

顺带验掉的：

- `language_code: "ja"` 在 v3 和 mv2 上都返回 200（flash 一直带着跑，没问题）
- 校验错误（400）不扣 credits；成功请求按 `character-cost` 头扣，dashboard 的
  `character_count` 有几分钟滞后，**以响应头为准**
- `GET /v1/user/subscription` 连续轮询会 429，脚本已改成按批读

### 时延与首尾静音（20 行，中位数）

| 配置 | 每行时延 | 头部静音 | 尾部静音 |
|---|---:|---:|---:|
| v3 | 2.10 s | 40 ms | 30 ms |
| mv2 + stitch | 1.35 s | 50 ms | 0 ms |
| flash v2.5 | 0.40 s | 40 ms | 0 ms |
| Google Charon | 1.02 s | 355 ms | 235 ms |

EL 的 clip 几乎不带首尾静音，Google 每边带 200–350 ms。接入时注意 assembler 里句间
停顿是否依赖 clip 自带的留白，否则 EL 的 T-S-T 会显得更「顶」。

### v3 + `/with-timestamps`：技术上可行

一段 5 行 135 字符的文本走 `POST /v1/text-to-speech/{voice}/with-timestamps`，
`model_id=eleven_v3`：200，耗时 8.8 s，扣 135 credits（和普通端点同价）。返回逐字符的
起止时间，字符序列和送入文本一一对应，按行边界切出 5 个 clip 全部匹配。

一个特性：对齐是**连续无缝**的（上一行 end 恰好等于下一行 start），句间停顿被记在
某个字符（通常是「。」）名下。所以按对齐切出来的 clip 一端会带着整段停顿，得过一遍
`audio/splitter.py` 现成的「退到静音 + 200ms 留量」逻辑，不能直接用对齐边界当 clip 边界。

这条路的取舍没变：它偏离「一条字幕一个 TTS 请求」的字面规则。只在付费后盲听
v3 明显赢过 mv2 时才值得再议。

### 对计划的影响

1. **`model_id` 定为 `eleven_multilingual_v2` + stitching**（问题 1 堵死 v3 的
   逐句用法；stitching 不计费，所以开着没有成本）。
2. **`speed` 必须钳位到 0.7–1.2**，撤销「可能不成立」那条。
3. **重试逻辑**匹配 `concurrent_limit_exceeded`；超并发是立即拒绝，退避 1–2 s 重试即可。
4. **决策点交给用户**：是否开 $6 Starter 做真正的日语盲听。免费档剩余 8,556 credits
   在本月内只能用英语声音跑，对核心问题没有帮助。开了 Starter 之后按同一脚本
   `compare mv2_stitch --voice otani|asahi|morioki` 各跑 20 行（~1,550 credits），
   再加 v3 裸跑一个声音做对照。
5. 汉字误读率（风险 B）本次**没有量**：原计划用 whisper 转写算字错率，GPU 机当时没开，
   这项等付费后用母语声音的 clip 再做，在 GPU 机上跑。

### 补测：Starter 开通后的日语母语声音（2026-09-08 晚）

用户开通 Starter（额度 38,556 = 30,000 + 上月结转 8,556；并发 3；声音槽位 10）。
同一脚本、同 20 行，三个日语母语声音跑 mv2 + stitching，Otani 另跑一份 v3 裸跑。
本轮消耗 **1,075 credits**（4 × 260 + 定价核对 35）。

**定价变了，付费档比免费档便宜一半。** 免费档 mv2 / v3 是 1 credit/字符，Starter 上
同一模型、同一文本、同一 premade 声音（Sarah）变成 **0.5 credit/字符**，flash 变成
**0.25**（27 字符分别扣 14 和 7）。和声音无关，是档位决定的（可能是当前促销，官方
价目表没写，以后按响应头核对）。**按这个价，$6 ≈ 60,000 日语字符 ≈ 180 分钟纯语音**，
是前面「换算」表里估计的两倍。

| 配置 / 声音 | 语速 chars/s | 响度 dBFS | 尾部静音中位 | 每行时延中位 |
|---|---:|---:|---:|---:|
| mv2 + stitch / Otani（叙述，男） | 5.68 | −18.8 | 155 ms | 1.94 s |
| mv2 + stitch / Asahi（对话，男） | 7.99 | **−36.2** | **830 ms** | 1.61 s |
| mv2 + stitch / Morioki（对话，女） | 6.53 | −27.6 | 670 ms | 1.79 s |
| v3 裸跑 / Otani | 6.75 | −16.7 | 0 ms | 2.25 s |
| Google Charon（基线） | 6.77 | −20.1 | 235 ms | 1.02 s |

- **响度按声音差 17 dB**（Asahi 非常轻）。接入时 `normalize_target_dbfs` 必须开，
  不能像 Google 那样靠默认电平。
- 母语声音走 stitching 时尾部带 150–830 ms 静音（premade 声音是 0）；v3 裸跑首尾几乎
  没留白。assembler 的句间停顿要以自己加的为准，不要依赖 clip 自带的。
- Otani 在 mv2 下明显慢于 Google（5.68 vs 6.77），换成 v3 就回到同一水平。
- 语速漂移：三个母语声音配对标准差 0.2–0.28，Asahi 最抖。都在可接受范围，不是逐句
  换人那种漂移。

**盲听样本**：`outputs/elevenlabs_spike/listen/` 里第 3 行（含 React / TypeScript /
Java）和第 19 行（含 P95 / 250 / 120 数字）各有 Otani / Asahi / Morioki(mv2) +
Otani(v3) + Google Charon 五份。排名和最终声音由用户听后填写：

- 盲听排名：__（待用户填）__
- 汉字误读：__（待用户听后记录；量化留待 GPU 机的 whisper 服务）__
- 选定 `model_id`：`eleven_multilingual_v2`（v3 逐句用法被问题 1 堵死，除非用户听后
  认为 v3 裸跑明显更好）
- 选定日语 voice_id：__（Otani `3JDquces8E8bkmvbh6Bc` / Asahi `GKDaBI8TKSBJVhsCLD6n` /
  Morioki `8EkOjt4xTPGMclNlh1pk`）__

**范围限定（用户 2026-09-08 定）**：ElevenLabs 只用于日语目标语音。英语目标语音和
中文旁白继续走现有引擎不变。Phase 1 的 `lang_presets.en.elevenlabs` 暂不配。

### 补测：v3 还有没有路（2026-09-08 晚，用户问「还是想用 v3」）

再探三条绕路，花 40 credits：

| 试法 | 结果 |
|---|---|
| v3 + `previous_request_ids` | 400 `unsupported_model`："not yet supported with the 'eleven_v3' model" |
| `eleven_v3_conversational` + `previous_text` | 400，同样的拒绝 |
| **`POST /v1/text-to-dialogue/with-timestamps`，`model_id=eleven_v3`，多条 `inputs`** | **200**。返回整段音频 + 逐字符对齐 + `voice_segments`：每条输入的 `start_time_seconds` / `end_time_seconds` / 字符区间 / `dialogue_input_index`。两条输入可以各指定 voice_id（Q/A 两个声音一个请求）。计费 0.5/字符，和普通端点一样 |

所以 v3 的逐句 stitching 三条路全堵；**能用 v3 的唯一方式是整段请求，用模型返回的对齐切成逐句 clip**。
两个端点都行：`/text-to-speech/{voice}/with-timestamps`（单声音）或 `/text-to-dialogue/with-timestamps`（多声音，
边界直接从 `voice_segments` 读，不用数字符）。

**边界落点实测（本地量，不花 credits）**：

- 单声音段落（5 行，Sarah）：每行末尾的「。」在对齐里独占 560–800 ms，也就是句间停顿被整个记在句号名下。
  按对齐切，刀口前方有 520–750 ms 的静音，后方只有 10–90 ms 就进下一句的起音。
- 对白端点（2 条，Otani + Morioki）：边界 1.839 s 前 100 ms、后 1,080 ms 都是静音。

即刀口全部落在静音里，只是停顿归属不对称。直接用对齐边界当 clip 边界会让每个 clip 尾巴带着整段停顿、
头部没有留量，正好是 `audio/splitter.py` 已经解决的那类问题（退到静音 + 200 ms 只吃静音的留量）。
**走这条路必须过 splitter，不能裸切。**

**与 CLAUDE.md 硬规则的关系**（需要用户拍板）：规则禁止的是「整段 TTS 后按字符数 / 词数 / 语速 / 比例
估算时间轴」。这里的边界不是估算，是模型逐字符输出的对齐，且经过能量判据验证落在静音里；切出的每个 clip
仍是真实解码音频，LRC 仍按 clip 实际长度重建。机制上和对白流水线（整段录音 → 验证过的边界 → splitter 切片）
是同一件事，只是边界来源从 whisper 换成了 TTS 自己的对齐。但规则原文写的是「严禁先生成多句/整段 TTS」，
采用前要把那条改成明确允许「模型对齐 + 静音验证」的表述，并把「任何边界没落在静音里就整段失败重生成」
加进验证项。

### 决定：用 v3，整段生成 + 模型对齐切片（2026-09-09 用户拍板）

用户盲听 `line002_v3_otani` vs `line002_mv2_stitch_otani`：v3「明显更好，差距不是一星半点」。
CLAUDE.md 的 TTS/LRC 硬规则已加例外条款（允许模型对齐 + 静音验证的整段切片）。

**语速与音量实测（花 220 credits）**：

- **v3 不理 `speed`**。同一段 96 字符，`voice_settings.speed` = 0.8 / 1.0 / 1.2 → 时长
  14.06 / 13.81 / 14.28 s，全是噪声级差异。对白端点的 `settings.speed` 0.8 vs 1.2 同样无效
  （11.40 vs 11.31 s）。请求返回 200，是「接受但不生效」的又一例（问题 6 的注脚）。
- 所以 **v3 的语速只能本地做**：切成逐句 clip 之后，每个 clip 过 ffmpeg `atempo`（保音高变速，
  本机 ffmpeg 有 `atempo` 和 `rubberband`）。顺序必须是先切后变速，时长以变速后解码为准，
  对齐不用换算。可用范围建议 0.85–1.15，再大会有伪影。
- **音量**：三个声音相差 17 dB（Otani −18.8、Morioki −27.6、Asahi −36.2 dBFS），
  用现有 `normalize_target_dbfs` / `gain_db` 在本地统一，和引擎无关。

**Phase 1 设计相应调整**（覆盖上文「接入点」表里合成层那一行）：

- 合成层不是「一句一请求」，而是**按段请求**：把 segments 按 10 句左右 / ≤ 2,000 字符分组
  （远低于 5,000 上限，单段翻车重生成的代价小），走 `/text-to-dialogue/with-timestamps`
  （面试稿 Q/A 各一个 voice_id 一个请求里出；纯文本稿全部同一 voice）。
- 用 `voice_segments` 的起止秒数做初始切点 → `audio/splitter.py` 退静音 + 留量 → 逐 clip
  解码取真实时长 → 可选 `atempo` → 再解码取最终时长。
- 验证：`voice_segments` 数量 == 该段 segments 数量；字符区间与送入文本一致；每个切点通过
  静音判据；否则整段重生成，重试 2 次后失败。
- 并发按段而不是按句：Starter 并发 3，一段一请求，174 行的稿子约 18 个请求。

---

## Phase 1 落地（2026-09-09）

按「决定：用 v3」那节的设计写完，端到端跑通（4 行面试稿，一个请求，Q=Shohei 1.2 倍速，
A=Morioki，旁白 Google，归一 −16 dBFS，LRC 4 行按 clip 实际长度重建，日志无 warning）。

| 层 | 改动 |
|---|---|
| 合成 | 新文件 `audio/elevenlabs_tts.py`：分段（10 行 / 2,000 字符）→ `/text-to-dialogue/with-timestamps` → `voice_segments` 边界先吸附到最近的静音段（±500 ms）→ splitter 的 `resolve_start_ms` / `resolve_end_ms` → 每个切点验证在静音里，否则换 seed 重生成（最多 3 次）后**报错**，不退回静音填充 → 本地 `atempo` 变速 → 共用 `_adjust_volume`。每段落一个 `el_para_NNN.json` 记录模型边界、clip 长度、seed |
| 合成入口 | `audio/tts_generator.py`：`generate_target_audio` 加 `elevenlabs` 分支和逐句 `elevenlabs_voice_ids` / `elevenlabs_speeds`；`generate_native_audio` 遇到 elevenlabs 直接抛错（旁白永远不走它） |
| 配置 | `main.py`：`TTS_ENGINES` 白名单、`tts.elevenlabs` / `tts.native_engine` / `interview.elevenlabs` 默认段与合并、`LANG_PRESETS.ja.elevenlabs` = Morioki、`INTERVIEW_LANG_PRESETS.ja.elevenlabs` = Shohei/Morioki；预设应用时**覆盖而不是合并**（换到英语要清掉日语声音）；`_resolve_target_engine`：engine 是 elevenlabs 但该语言没有声音 → 目标语音退回 Google（dual 的英语那半自动生效）；`_native_engine`：旁白引擎；native 调用点全部改用 `_native_tts_engine_kwargs` |
| 语速 | `apply_speaking_rate` 同时写 `tts.elevenlabs.speed`；面试模式按 Q/A 分别给每句一个倍率，在切片后本地变速 |
| 面试 | `_generate_interview_target_audio`：elevenlabs 时不再按角色分两批，整份 Q/A 按顺序进同一批段落请求，每条输入带自己的 voice_id |
| Web | `webapp/jobs.py` `VALID_ENGINES`；`webapp/server.py` `_text_default_lang` 对 elevenlabs 反查 `lang_presets`；`index.html` 引擎下拉加 ElevenLabs、音色提示说明；`app.js` `engineLabel` |
| 部署 | `requirements*.txt` 显式加 `requests`；`.env.example` / `docker-compose.yml` 透传 `ELEVENLABS_API_KEY`；`docs/deploy_vps.md` 提一句 |
| 测试 | `tests/test_elevenlabs_tts.py`：合成音频验证两种停顿归属都切得干净、边界在语音里必须拒绝、响应交叉检查、分段规则、本地变速 |

**没做 / 待办**：

- 汉字误读率没量（等 GPU 机的 whisper）。
- 部署前要在 `requirements-web.txt` 的干净 venv 里验 `import webapp.server`（2026-09-07 的教训）。
- `text-to-dialogue` 的 `settings.stability` 是否生效未验证（v3 对 `speed` 就是 200 但无效）。

### 修正：切片改成「只认句间长停顿」（2026-09-09，用户反馈日语句尾被切掉）

第一版切法（模型边界 → 吸附到最近 ≥30 ms 的静音 → splitter 的 resolve）在真实 v3 输出上
切掉句尾，两个原因，都是量出来的：

1. **模型边界本身会偏早**。对白端点 2 句样本：最后一句 `voice_segments` 的结束是 5.36 s，
   而音频到 6.32 s 都还是 −22 到 −29 dB 的声音；4 行 e2e 样本：最后一句模型结束 14.64 s，
   音频到 15.76 s 仍有 −24 到 −28 dB。偏早约 1 s。
2. **30 ms 的静音门槛会吸到词内的塞音闭锁里**。促音、k/t 闭锁本来就是 30–80 ms 的无声，
   边界一吸就落在词中间。另外 splitter 的「局部峰值 −25 dB」判据对 TTS 太紧：ます/です
   这类清化句尾在峰值以下 35 dB，被当成静音削掉（而 v3 的底噪在 −85 dBFS，远低于它）。

现在的规则（`audio/elevenlabs_tts.py` 的 `cut_paragraph`）：

- 「有声」用绝对阈值 −55 dBFS（TTS 底噪 −85，清化句尾 −45 左右，都分得开）
- 只有 **≥ 200 ms** 的静音段算候选切口，这把塞音闭锁排除掉了；v3 句间停顿实测 500–1,000 ms
- 模型给的相邻两句交界时间只用来**挑选**最近的候选静音段（±1.5 s），并且各交界必须选到
  不同的、按顺序递增的静音段，否则整段作废重生成
- 句尾切在停顿开始后 200 ms，下一句起点切在停顿结束前 200 ms；留量只吃静音，按构造切点
  一定在静音里
- 段落首尾用开头/结尾的静音同样处理；**v3 返回的音频在最后一个字后面是硬截止**（没有
  尾部静音），所以最后一句一律取到音频末尾

4 行 e2e 复跑：模型边界 [0,1519]/[1519,5679]/[5679,11680]/[11680,14640]，找到的长停顿
1500–2690 / 6020–6890 / 11940–12850 ms，clip 1700 / 3720 / 5440 / 3100 ms，末句包含到 15760 ms。
`ELEVENLABS_KEEP_DIR=<dir>` 可以把每段原始音频和边界 JSON 留下来，方便这类核对。

---

## 英语接入 + 切片鲁棒性修正（2026-09-09 下午）

### 声音定案

用户按 Voice Library 的使用人数选定，spike 试验声音已从账号删除（槽位 10 个，现用 4 个）：

| 用途 | 声音 | voice_id | 使用人数 |
|---|---|---|---:|
| 日语文本 / 面试 A | Morioki（对话女声） | `8EkOjt4xTPGMclNlh1pk` | 36K |
| 日语面试 Q | Shohei（对话男声） | `8FuuqoKHuM48hIEwni5e` | 13K |
| 英语文本 | Mark - Natural Conversations | `UgBBYS2sOqTuMpoF3BR0` | 3.82M |
| （用户自己加的） | Haru | `a0MsDWokG5Xsuji8g8er` | — |

> **已回退（2026-09-09 晚）**：英语改回 Google，只有日语走 EL。下面这段保留作为记录，
> 声音都还在账号里，取消 `config.yaml` 里那两处注释就能恢复。

英语面试补了 **Cassidy - Crisp, Direct and Clear**英语面试补了 **Cassidy - Crisp, Direct and Clear**（`56AoDkrOh6qfVPDXZ7Pt`，118 万，conversational）
当提问者。四种组合全部走 EL：

| 语言 | 文本 | 面试 Q | 面试 A |
|---|---|---|---|
| 日语 | Morioki | Shohei | Morioki |
| 英语 | Mark | Cassidy | Mark |

**模式是「文本声音兼任回答者，另配一个提问者」** —— 回答的语体和讲课稿一致，提问换个人，
两边区分明显。四组 voice_id 现在同时写在 `main.py` 的预设常量和 `config.yaml` 的
`interview.presets` 里（后者之前漏了 elevenlabs 这一层，一并补上）。

### 第一次英文跑失败，烧掉 4,277 credits

`no pause >= 200 ms within 1500 ms of the seam between lines 5 and 6`。两个独立的问题：

1. **门槛按日语标定，英语不适用**。实测英语词内停顿（塞音闭锁）60–90 ms，日语促音 30–80 ms，
   而真实句间停顿两种语言都是 240–870 ms。原来的 200 ms 卡在英语的边缘上，把一部分真实
   句间停顿也排除了。→ 改成 **120 ms**，落在两者中间的空档里。
2. **贪心匹配会走死**。原来每个接缝取「最近的、还没用过的静音段」。真实段落里逗号停顿夹在
   句间停顿之间，早期一次错选会让后面全部失败。→ 换成**单调 DP 全局最小化**总距离
   （`assign_seams`），局部让一步换全局可行；窗口放宽到 2,500 ms。

用之前保存的 12 个英文段落 + 14 个日文段落回归验证：**26/26 全部切通**，日语没有回归。

### 更贵的教训：一个段落失败会丢掉整轮已付费的段落

那 4,277 credits 里，只有 2 个段落是真失败（各重试 3 次），其余 13 个成功了却随着整轮
异常一起丢弃。**加了段落级缓存**（`audio/elevenlabs_tts.py` 的 `cache_*`）：

- key = sha256(model_id, output_format, stability, voice_ids, texts)，取前 32 位
- 默认目录 `~/.cache/echoEnglish/elevenlabs`，可用 `ELEVENLABS_CACHE_DIR` 覆盖
- 命中就直接切，日志打 `from cache, 0 credits`；缓存音频切不干净就照常重新生成
- 缓存写失败只 warning，绝不让一轮生成失败

修完后把那 12 个付过费的段落灌进缓存，重跑英文版**只花了 558 credits**（补 2 个缺的段落），
而不是重付 3,872。

### 两份成品

| | 行数 | TTS 字符 | credits | 纯语音 | 成品时长 |
|---|---:|---:|---:|---:|---:|
| 日语（Morioki） | 133 | 3,730（剥注音省 40%） | 1,862 | 10.2 min | 36:27 |
| 英语（Mark） | 133 | 7,745 | 3,872 + 558 重跑 | ~7.7 min | 32:25 |

**英语每 credit 买到的语音是日语的一半**（同一份稿子 7,745 vs 3,730 字符），
和 Phase 0 换算表里「日语是英语的 2.2 倍」一致。日语这边注音括号剥离又额外省了 40%。

两份都已 rclone 同步到 `gdrive:echoEnglish/`。


---

## 英语断句错位：模型时钟会漂（2026-09-09 晚）

用户听英文成品发现「一个错了，剩下的全错了」。查下来是两个叠加的问题，第二个更严重。

### 一、模型的绝对时间戳在英语上会漂

同一套代码，**日语 133 个 clip 全对，英语 113 个里 11% 错位**。差别在时钟：

| | 音频实际长度 / 模型末尾时间 |
|---|---|
| 日语（14 段） | 1.000 ~ 1.000（中位 **1.000**） |
| 英语（12 段） | 1.000 ~ 1.122（中位 **1.042**） |

日语的 `voice_segments` 和返回音频完全对齐，英语的音频比模型自己说的最多长 12%。
按绝对时间找最近的停顿就会走到隔壁那个停顿上，而匹配是单调的，**错一个之后全部顺移** ——
正是用户听到的现象。

修法：匹配前把模型时钟按 `音频长度 / 模型总时长` 缩放。实测把英语错位从 12/113 降到
**1/113**，日语 0/133 不变。

### 二、真正的教训：验证只检查了「切在静音里」

错位的切点**也在静音里** —— 它只是把下一句的内容装进了这个 clip。所以原来的验证全部放行，
12 个错位 clip 一个都没拦住，用户是靠耳朵发现的。

加了独立的交叉验证：**每个 clip 的实际时长必须和模型给这一行的时长（缩放后）吻合**，
容差 `max(400ms, 35%)`。错位会让某个 clip 明显偏短、下一个明显偏长，立刻暴露。
不吻合就整段作废重生成。用剩下那 1 个英语坏段落验证：确实被拦下并触发重生成。

> 方法论重复了 CLAUDE.md 里 OP/ED 那条的教训：**验证判据必须能证伪你真正关心的东西**。
> 「切点在静音里」证明的是刀口干净，不是内容对位。两者不是一回事，而我用前者冒充了后者。

### 三、英语回退到 Google（用户决定，成本原因）

同一份稿子英语 7,745 字符、日语 3,730 字符，**英语贵 2.08 倍**。原因两条：

1. 日语汉字信息密度高，同样的意思字符数本来就少（Phase 0 换算表里的 2.2 倍，这里实测 2.08）
2. 日语稿的注音括号在送 TTS 前被 `strip_furigana` 剥掉，又省了 40%（6,280 → 3,730）

而 Chirp3-HD 的英语本来就够用，听力 gap 在日语。所以 `lang_presets.en` 和
`interview.presets.en` 的 elevenlabs 层都注释掉了，英语文本和面试都退回 Google。
Mark / Cassidy 仍留在 EL 账号里（槽位 10 用 5），取消注释即可恢复。


### 用缓存重建了正确的英文版（2026-09-09 晚）

段落缓存这时候救了一次：14 个英文段落都还在 `~/.cache/echoEnglish/elevenlabs`，
用修好的切法重切，**12 段直接可用，只有 2 段（0 和 7）过不了时长交叉验证需要重生成**。
重建整份英文版只花 **574 credits**，而不是重付 3,872。14/14 段现在全部通过验证。

顺带加了三个 CLI 开关，用来在不改 `config.yaml` 的前提下临时指定 ElevenLabs 声音
（voice id 不透明，所以直接收原始 id，不像 Google 那样只换 persona）：

```
--elevenlabs-target-voice / --elevenlabs-interviewer-voice / --elevenlabs-interviewee-voice
```

这次就是用 `--lang en --elevenlabs-target-voice UgBBYS2sOqTuMpoF3BR0` 跑的：
`lang_presets.en` 里故意没有 EL 声音（英语默认走 Google），CLI 覆盖让这一次走 EL。

Google Drive 上错位的那份 `2026-09-09_11-57-18_db_index_en` 已删除，
正确的是 `2026-09-09_12-54-35_db_index_en`。

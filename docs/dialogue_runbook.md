# 对白流水线操作手册

从一部电影 / 动漫跑到 `_tnt` / `_tst` 学习音频的**完整命令序列**。

**为什么这么设计** 见 `docs/dialogue_pipeline_claude_v2.md`；**GPU 机运维**见
`~/.claude/gpu-box.md`；**不要回退的结论**见 `CLAUDE.md`。这份只管怎么跑。

---

## 0. 开跑前必做的自检

```bash
ssh -i ~/.ssh/mdreader_deploy -p 2222 sudami@192.168.31.50 "echo SSH_OK" \
  && curl -s -m 8 -o /dev/null -w "port8000 http=%{http_code}\n" \
       http://192.168.31.50:8000/openapi.json
```

**两条都要通。** SSH 通不代表流水线能用——转写走的是局域网 8000 端口，和 SSH 是
两条独立的路。2026-08-30 就栽在这：机器内部 `curl 127.0.0.1:8000` 一直好用，真跑
流水线时 Mac 连不上 8000，整条链在第一步断掉。

`ping` 不能用来判断这台机器死没死，ICMP 被 Windows 防火墙挡着。

8000 不通 → 让用户双击 Windows 桌面的 `start-transcribe.bat`。

**顺手确认笔记本插着电。** 电池供电时 GPU 会被锁到 210 MHz（上限 3105），demucs
会慢 30 倍。判据见 gpu-box.md 第 ⑩ 条。

---

## 1. 抽音频（Mac）

```bash
ffmpeg -v error -y -ss <起点秒> -i "<影片.mkv>" -t <时长秒> \
  -vn -acodec libmp3lame -b:a 192k -ar 44100 <名字>.mp3
```

参数和 `extract_audio.py` 保持一致（192k / 44.1kHz）。不加 `-map` 时 ffmpeg 自己挑
默认音轨，5.1 会自动下混成立体声。

**超过 15 分钟必须切段**，见 gpu-box.md 第 ⑥ 条：demucs 把 4 个 stem 全以 float32
留在内存，90 分钟会被内核 OOM kill，而分离现在是硬依赖，它挂了整条任务作废。
切段按采样点做，别信 `ffmpeg -c copy` 的容器时长（第 ⑦ 条）。

---

## 2. 分离（GPU 机）

```bash
scp -i ~/.ssh/mdreader_deploy -P 2222 <名字>.mp3 sudami@192.168.31.50:~/dialogue-exp/

ssh -i ~/.ssh/mdreader_deploy -p 2222 sudami@192.168.31.50 \
  'cd ~/dialogue-exp && setsid nohup ~/audio-sep/bin/python -m demucs \
     --two-stems=vocals -d cuda --flac -o sep <名字>.mp3 > sep.log 2>&1 < /dev/null & disown'
```

`setsid nohup ... & disown` 是必须的（第 ⑤ 条）。**这条 ssh 命令会挂住不返回**——
后台进程占着通道。别等它，另开一个 ssh 查进度：

```bash
ssh ... 'tail -c 200 ~/dialogue-exp/sep.log | tr "\r" "\n" | tail -2'
```

查进程别用 `pgrep -f demucs`，它会匹配到你自己的命令行（第 ⑧ 条），
用 `pgrep -f "audio-sep/bin/python"`。

正常速度：10 分钟音频约 20 秒。明显慢于此 → 查 GPU 频率，不是查代码。

```bash
scp -i ~/.ssh/mdreader_deploy -P 2222 \
  'sudami@192.168.31.50:~/dialogue-exp/sep/htdemucs/<名字>/vocals.flac' ./
```

---

## 3. 剪辑（Mac）

```bash
/opt/homebrew/anaconda3/envs/echo_env/bin/python tools/dialogue_cut.py \
  --stem vocals.flac --out <输出目录> --name <名字>
```

只在两侧都验证到达本地底噪的地方下刀，每边留 30ms。够不到底噪就不剪。
压缩率随素材而变（英语电影约剪掉 23~28%，对白密集的动漫约 11%）——
**这是接受的代价，不要为了压缩率去放松底噪判据。**

产出 `<名字>.mp3` 和 `<名字>.timeline.json`（后者只供审计回溯，下游不读）。

> **顺序不能颠倒**：转写跑在剪辑**之后**，喂的就是这一步的输出。所以这里没有
> `--whisper` 参数，也没有时间戳重映射。

---

## 4. 投放 → 转写 + 分句 + 翻译 + Echo（一条命令）

把剪辑后的 mp3 放进输入目录：

```bash
cp <名字>.mp3 ~/Documents/autoTranscribe/Japanese/<YYYYMM>/
# 英语素材放 English/ 下；目标语音色按这个文件夹名选
```

**先确认目录里没有别的待处理文件**，否则会连带重跑：

```bash
cd ~/Documents/autoTranscribe/Japanese/<YYYYMM> && \
  for f in *.mp3; do [ -f "${f%.mp3}.lrc" ] || echo "缺 lrc: $f"; done
```

然后：

```bash
~/bin/echo_pipeline
```

这一条会依次跑：whisper 转写（GPU）→ codex 分句（agent 模式）→ codex 翻译 →
同步到 `SYNC_DIR` → Google TTS + Echo 生成。**会真花钱**（Google TTS 按字符、
codex 两趟）。

耗时参考（10 分钟素材 / 7.3 分钟对白）：转写 48 秒，codex 约 6 分钟，
Echo 生成 102 秒。

---

## 5. 产物

```
~/Library/Mobile Documents/iCloud~com~cubeTC~MP3/Documents/mp3music/audio/
  <时间戳>_<Japanese|English>/
    <名字>.mp3            剪辑后的对白源音频
    <名字>.lrc            双语字幕
    <名字>_tnt.m4a/.lrc   Echo 产物
    <名字>_tst.m4a/.lrc
    echo_run_*.log
```

---

## 6. 只重跑 Echo 那一步（改了 splitter / config 之后）

```bash
~/bin/echo_pipeline --echo "<上面那个产物目录>"
```

**坑：产物已存在时会直接跳过**（日志显示 `⏭ Skipped (already exists)`，
且 `✓ Succeeded: 0`）。要重新生成必须先删掉四个输出文件：

```bash
rm -f "<目录>"/<名字>_tnt.m4a "<目录>"/<名字>_tnt.lrc \
      "<目录>"/<名字>_tst.m4a "<目录>"/<名字>_tst.lrc
```

想做 A/B 就先把旧产物拷到别处再删。

---

## 7. 验收

```bash
cd ~/PycharmProjects/echoEnglish
for t in test_splitter_tail test_dialogue_cut test_audio_timing_contract \
         test_build_bilingual_transcript test_extract_epub_transcript test_loop_defaults; do
  printf "%-34s " $t
  /opt/homebrew/anaconda3/envs/echo_env/bin/python -m unittest tests.$t 2>&1 | tail -1
done
```

产物侧：全部可完整解码；LRC 行数与 segment 数一致；无 `estimated` timing。
听感：对白首尾未被切断、背景明显降低、剪接无爆音。

"use strict";

// ---------------------------------------------------------------------------
// State + helpers
// ---------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);
const TOKEN_KEY = "echo_token";

let currentMode = "text";
let appDefaults = null;
let splitTouched = false;
const pollers = new Map(); // jobId -> intervalId

function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

function authHeaders(extra = {}) {
  const t = getToken();
  return t ? { Authorization: "Bearer " + t, ...extra } : { ...extra };
}

function withToken(url) {
  const t = getToken();
  if (!t) return url;
  return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(t);
}

function fileUrl(jobId, name, download = false) {
  let url = `/api/files/${jobId}/${encodeURIComponent(name)}`;
  if (download) url += "?download=1";
  return withToken(url);
}

async function api(path, options = {}) {
  const opts = { ...options, headers: authHeaders(options.headers || {}) };
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString();
}

function isAudio(name) {
  return /\.(m4a|mp3|wav|flac|ogg|aac)$/i.test(name);
}

function showError(msg) {
  const el = $("error");
  el.textContent = msg;
  el.classList.remove("hidden");
}
function clearError() {
  $("error").classList.add("hidden");
}

function langLabel(value) {
  return { ja: "日语", en: "英语" }[value] || "配置";
}

function engineLabel(value) {
  return { google: "Google", edge: "edge-tts", openai: "OpenAI" }[value] || "配置";
}

function loopLabel(loop) {
  if (!loop) return "配置默认";
  const base = `${loop.tnt_repeats}×T-N-T + ${loop.tst_repeats}×T-S-T`;
  return loop.split_outputs ? `${base}，拆分输出` : base;
}

function isBlank(value) {
  return value === null || value === undefined || value === "";
}

function gainLabel(gain) {
  const value = Number(gain || 0);
  return `${value > 0 ? "+" : ""}${value} dB`;
}

// Gain applied to every clip. The Chinese narration overrides it when
// native_gain is set, so that case is spelled out separately.
function volumeLabel(tts) {
  if (!tts) return "配置默认";
  if (!isBlank(tts.normalize)) return `normalize ${tts.normalize} dBFS`;
  return gainLabel(tts.gain);
}

// Gain the Chinese narration actually gets: native_gain, or the global gain
// when it is unset.
function nativeVolumeLabel(tts) {
  if (!tts) return "配置默认";
  if (!isBlank(tts.normalize)) return `normalize ${tts.normalize} dBFS`;
  return gainLabel(isBlank(tts.native_gain) ? tts.gain : tts.native_gain);
}

// Placeholder for a speech-rate box: the configured value if there is one,
// else the value it falls back to, else plain engine speed.
function rateLabel(value, fallback) {
  const effective = value ?? fallback;
  return effective ? `配置默认：${effective}` : "1.0（正常语速）";
}

function timingLabel(timing) {
  if (!timing) return "配置默认";
  return `${timing.after_first_target}s / ${timing.after_native}s / ${timing.after_second_target}s`;
}

function syncLabel(sync) {
  if (!sync || !sync.enabled) return "关闭";
  if (!sync.ready) return "已开启，但目标为空";
  const lrc = sync.include_lrc ? "含 LRC" : "仅音频";
  return `${sync.method} -> ${sync.dest}，${sync.layout}，${lrc}`;
}

function setDefaultOption(selectId, text) {
  const option = $(selectId).querySelector('option[value=""]');
  if (option) option.textContent = text;
}

function currentDefaultLang() {
  if (!appDefaults || !appDefaults.lang) return "";
  return appDefaults.lang[currentMode] || "";
}

function updateDefaultLabels() {
  if (!appDefaults) return;

  const lang = currentDefaultLang();
  setDefaultOption("lang", lang ? `（配置默认：${langLabel(lang)}）` : "（用配置默认）");
  setDefaultOption("engine", `（配置默认：${engineLabel(appDefaults.engine)}）`);
  setDefaultOption("variant", `（配置默认：${loopLabel(appDefaults.loop)}）`);

  $("gain").placeholder = `配置默认：${volumeLabel(appDefaults.tts)}`;
  $("nativeGain").placeholder = `配置默认：${nativeVolumeLabel(appDefaults.tts)}`;
  $("rate").placeholder = rateLabel(appDefaults.tts && appDefaults.tts.target_rate);
  const interviewDefaults = appDefaults.interview || {};
  $("qRate").placeholder = rateLabel(
    interviewDefaults.interviewer_rate, appDefaults.tts && appDefaults.tts.target_rate,
  );
  $("aRate").placeholder = rateLabel(
    interviewDefaults.interviewee_rate, appDefaults.tts && appDefaults.tts.target_rate,
  );
  $("t1").placeholder = appDefaults.timing.after_first_target;
  $("t2").placeholder = appDefaults.timing.after_native;
  $("t3").placeholder = appDefaults.timing.after_second_target;

  $("defaultsSummary").innerHTML = [
    ["目标语言", lang ? langLabel(lang) : "配置"],
    ["TTS", engineLabel(appDefaults.engine)],
    ["循环", loopLabel(appDefaults.loop)],
    ["停顿", timingLabel(appDefaults.timing)],
    ["音量", `${volumeLabel(appDefaults.tts)}（中文 ${nativeVolumeLabel(appDefaults.tts)}）`],
    ["输出", `${appDefaults.output.format} / ${appDefaults.output.bitrate}`],
    ["同步", syncLabel(appDefaults.sync)],
  ]
    .map(([k, v]) => `<span><b>${escapeHtml(k)}</b>${escapeHtml(v)}</span>`)
    .join("");
}

function applyDefaults(defaults) {
  appDefaults = defaults;
  if (!appDefaults) return;

  if (appDefaults.loop) {
    $("split").checked = Boolean(appDefaults.loop.split_outputs);
    $("tnt").value = appDefaults.loop.tnt_repeats;
    $("tst").value = appDefaults.loop.tst_repeats;
  }
  splitTouched = false;
  updateDefaultLabels();
  updateVoiceUI();
  updatePrompt();
}

async function loadConfigDefaults() {
  try {
    const config = await api("/api/config");
    applyDefaults(config.defaults);
  } catch (err) {
    $("defaultsSummary").textContent = "默认配置读取失败：" + err.message;
  }
}

// ---------------------------------------------------------------------------
// Theme (light default, dark opt-in, persisted; index.html pre-applies it)
// ---------------------------------------------------------------------------

const THEME_KEY = "echoTheme";

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("themeBtn").textContent = theme === "dark" ? "☀️" : "🌙";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = theme === "dark" ? "#0f1117" : "#f5f6f8";
}

applyTheme(localStorage.getItem(THEME_KEY) === "dark" ? "dark" : "light");

$("themeBtn").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
});

// ---------------------------------------------------------------------------
// Tabs + options UI
// ---------------------------------------------------------------------------

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    currentMode = tab.dataset.mode;
    updateDualUI();
    updateContentPlaceholders();
    updateVoiceUI();
    updateDefaultLabels();
    syncPromptCountUI();
    updatePrompt();
  });
});

// Trilingual dual run (EN+JA audio from one en|||ja|||zh script) works in both
// text and interview modes. When on, the language is auto (both), so the manual
// target-language picker is locked.
function isDual() {
  return $("dual").checked && !$("dual").disabled;
}

function updateDualUI() {
  $("lang").disabled = isDual();
}

function updateContentPlaceholders() {
  if (currentMode === "interview" && isDual()) {
    $("content").placeholder =
      "Q:/A: 三语面试稿，每条四列：英语|||日语|||中文(对英)|||中文(对日)。例如：\nQ:Tell me about yourself.|||自己紹介（じこしょうかい）をお願（ねが）いします。|||请介绍一下你自己。|||请做一下自我介绍。\nA:I can explain it if you want.|||必要（ひつよう）なら説明（せつめい）します。|||如果你想听，我可以解释。|||有需要的话，我来解释。";
    $("formatHint").innerHTML =
      '格式：<code>Q:/A: 英语|||日语|||中文(对英)|||中文(对日)</code>，两列中文各自贴着英语 / 日语的语序，会同时生成英中、日中两个面试音频；旧的三列 <code>英语|||日语|||中文</code> 也能用，两边共用一列中文。支持 Q:/Question:/Interviewer: 与 A:/Answer:/Candidate:。';
  } else if (currentMode === "interview") {
    $("content").placeholder =
      "Q:/A: 面试稿，每条同样用 ||| 分隔中文。例如：\nQ:Tell me about yourself.|||请介绍一下你自己。\nA:Sure, I am a backend engineer.|||当然，我是一名后端工程师。";
    $("formatHint").innerHTML =
      '格式：<code>Q:/A: 目标语言|||中文</code>。支持 Q:/Question:/Interviewer: 与 A:/Answer:/Candidate:。';
  } else if (isDual()) {
    $("content").placeholder =
      "每行一条：英语|||日语|||中文(对英)|||中文(对日)\n例如：\nI'll call you when I get home.|||家（いえ）に着（つ）いたら電話（でんわ）します。|||我会给你打电话，等我到家。|||到家了就打电话。\nI'd like to pay by card.|||カードで払（はら）いたいです。|||我想用卡支付。|||用卡支付，可以吗。";
    $("formatHint").innerHTML =
      '格式：<code>英语|||日语|||中文(对英)|||中文(对日)</code>，每行一条，两列中文各自贴着英语 / 日语的语序；<code>#</code> 开头为注释。会同时生成英文和日文两个音频。旧的三列 <code>英语|||日语|||中文</code> 也能用，两边共用一列中文。';
  } else {
    $("content").placeholder =
      "每行一条：目标语言|||中文翻译\n例如：\nこれはテストです|||这是一个测试\n水を飲みます|||我喝水";
    $("formatHint").innerHTML =
      '格式：<code>目标语言|||中文</code>，每行一条，<code>#</code> 开头为注释。';
  }
}

// The effective TTS engine = explicit pick, else the config default.
function effectiveEngine() {
  return $("engine").value || (appDefaults && appDefaults.engine) || "google";
}

// Voice (Chirp3-HD persona) pickers. Text mode shows one voice; interview mode
// shows Q + A. Persona swapping only affects Google voices, so the selects are
// disabled (and a hint shown) for the edge / openai engines.
function updateVoiceUI() {
  const interview = currentMode === "interview";
  $("voiceFieldText").classList.toggle("hidden", interview);
  $("voiceFieldQ").classList.toggle("hidden", !interview);
  $("voiceFieldA").classList.toggle("hidden", !interview);

  // Speech rate follows the same text/interview split, but works on every
  // engine (unlike the Google-only persona pickers), so it is never disabled.
  $("rateFieldText").classList.toggle("hidden", interview);
  $("rateFieldQ").classList.toggle("hidden", !interview);
  $("rateFieldA").classList.toggle("hidden", !interview);

  const isGoogle = effectiveEngine() === "google";
  ["voice", "qVoice", "aVoice"].forEach((id) => {
    $(id).disabled = !isGoogle;
  });
  $("voiceHint").classList.toggle("hidden", isGoogle);
}

// ---------------------------------------------------------------------------
// AI prompt helper (copy a ready-made prompt to generate a script)
// ---------------------------------------------------------------------------

function langWord() {
  const l = $("lang").value || currentDefaultLang();
  return l === "ja" ? "日语" : l === "en" ? "英语" : "目标语言";
}

// Count shown in the copy-prompt helper. Each interview flavor remembers its
// own value; a chat transcript usually contains fewer distinct topics than a
// full mock interview, so discussion starts at 12 instead of 24.
// A lecture counts sections (10–20 lines each), not sentences: 6 sections is
// one chapter told end to end, about 60–120 lines.
const promptCounts = { sentences: 15, lecture: 6, general: 24, sysdesign: 24, discussion: 12 };

function promptCountKey(mode) {
  return mode === "interview" ? interviewStyle() : textStyle();
}

function promptCount(mode) {
  const key = promptCountKey(mode);
  const n = parseInt(promptCounts[key], 10);
  if (Number.isFinite(n) && n > 0) return n;
  return key === "discussion" ? 12 : key === "sentences" ? 15 : key === "lecture" ? 6 : 24;
}

function syncPromptCountUI() {
  const input = $("promptCount");
  const label = $("promptCountLabel");
  if (currentMode === "interview") {
    label.textContent = interviewStyle() === "discussion"
      ? "目标问题数量（素材不足时宁少勿编）"
      : "问题数量（Q 的个数，建议 ≥ 20；每个 Q 可配多条 A、含深挖追问）";
  } else {
    label.textContent = textStyle() === "lecture"
      ? "小节数量（每小节 10-20 行；建议 4-8 节，一章讲完）"
      : "句子数量";
  }
  input.value = promptCounts[promptCountKey(currentMode)];
  $("interviewStyleField").classList.toggle("hidden", currentMode !== "interview");
  $("textStyleField").classList.toggle("hidden", currentMode !== "text");
  // Spoken-Q only makes sense when a script is being written from scratch — in
  // 聊天整理 the questions come from a real transcript, not from the model.
  $("spokenQField").classList.toggle(
    "hidden", currentMode !== "interview" || interviewStyle() === "discussion",
  );
  $("promptHelp").textContent = currentMode === "interview" && interviewStyle() === "discussion"
    ? "把下面提示词直接发在原技术会话末尾；如果要在新会话中使用，请把聊天记录粘到提示词末尾。整理结果可以直接粘到下面输入框。"
    : currentMode === "text" && textStyle() === "lecture"
    ? "复制下面提示词，发给 ChatGPT / Claude，填上章节主题（可以把教材、文章或网页内容一起贴在后面当素材），把生成的讲课稿直接粘到下面输入框。讲完一章再用「面试稿 → 聊天整理」把它整理成问答，两边用词就是同一套。"
    : "复制下面提示词，发给 ChatGPT / Claude，填上主题，把它生成的结果直接粘到下面输入框即可。提示词会随上方标签页、是否勾选「三语稿」、「目标语言」以及下面的数量自动调整。";
}

// Text prompt flavor: independent sentences, or one continuous lecture.
function textStyle() {
  const selected = document.querySelector('input[name="textStyle"]:checked');
  return selected ? selected.value : "sentences";
}

// Interview prompt flavor: general Q&A, scenario-driven system design (FDE),
// or a faithful conversion of an existing technical chat.
function interviewStyle() {
  const selected = document.querySelector('input[name="interviewStyle"]:checked');
  return selected ? selected.value : "general";
}

// Real-speech register for the interviewer's lines. Ignored while the toggle is
// hidden (text mode, or 聊天整理 where the questions come from a real transcript).
function spokenQ() {
  return $("spokenQ").checked && !$("spokenQField").classList.contains("hidden");
}

function buildPrompt(mode) {
  const L = langWord();
  const langCode = $("lang").value || currentDefaultLang();
  const lenHint = langCode === "ja"
    ? "约 8-36 个字符"
    : "通常 6-16 个词，必要时最多 18 个词";
  const style = mode === "interview" ? interviewStyle() : "";
  const sysdesign = style === "sysdesign";
  const discussion = style === "discussion";
  const topicExample = mode === "interview"
    ? (sysdesign ? "设计一个订单实时状态跟踪系统" : "后端工程师，5 年经验")
    : `${L} 日常购物对话`;
  const jaFuriganaRule = `- 日语汉字和数字都要注音：在汉字 / 数字后用全角括号标注平假名读音，如 漢字（かんじ）、2024年（にせんにじゅうよねん）；纯假名和片假名外来语（カタカナ）不用注`;
  const jaFuriganaSingle = langCode === "ja" ? jaFuriganaRule : null;
  // Why two Chinese columns: English and Japanese order their clauses
  // differently (JA is SOV and hangs contrast / negation on the sentence end),
  // so one Chinese line translated from the English reads "off" against the
  // Japanese audio. Echo relies on the native line lighting up the SAME
  // position in the target line, so each language gets its own Chinese.
  const zhAlignDual = `- 第三列中文逐句贴着英语写，第四列中文逐句贴着日语写：分句顺序、转折词和否定词的位置、量词（a / one / another、一つの / 別の）都跟各自的原文走，不调换分句、不合并、不拆分，宁可中文略显生硬也不按中文习惯重排；两列中文意思相同，只是语序各自贴合原文，都要完整对应原文信息，不要额外扩写原文没有的内容`;
  const zhAlignSingle = `- 中文翻译逐句贴着${L}写：分句顺序、转折词和否定词的位置、量词都跟原文走，不调换分句、不合并、不拆分，宁可中文略显生硬也不按中文习惯重排；完整对应原文信息，不要额外扩写原文没有的内容`;
  // Synonym rotation is the model's default and the learner's enemy: three
  // words for one idea are three things to remember. Native speakers repeat.
  const enVocabRule = `- 英语固定用 use（不用 utilize / employ）、keep（不用 retain / preserve / maintain）、improve（不用 enhance / refine）、start（不用 initiate / launch）；非术语部分限在最常用的 2000 词（NGSL）以内`;
  const jaVocabRule = `- 日语固定用 使う（不用 利用する / 活用する）、保つ（不用 保持する / 維持する）、確認する（不用 検証する / 点検する）；非术语部分限在 JLPT N3 以内`;
  const vocabSection = (dual) => [
    ``,
    `用词一致（重要——学习者要记得住，不是要文采）：`,
    `- 同一个概念在整份稿子里固定用同一个词，最多两个说法，绝不为了避免重复换近义词；母语者说话本来就是重复的`,
    ...(dual ? [enVocabRule, jaVocabRule] : [langCode === "ja" ? jaVocabRule : enVocabRule]),
    `- 技术术语和工程师职场常用词（処理、保証、障害、実装、Kafka、JWT、replay 这类）不受词汇范围限制，照常使用，但同样全文只用一个说法`,
    `- 中文列同样一词一译：同一个术语全文只用一个中文说法，而且两个不同的术语不能共用同一个中文词（如 replica 固定译「副本」，copy 就译「拷贝」，不能都叫「副本」；partition 译「分区」，shard 译「分片」；cache 译「缓存」，buffer 译「缓冲」）`,
    `- 输出前自检：把全文用过的动词、名词和术语的中文译法各过一遍，同一概念出现第三种说法就统一成第一种，两个术语撞了同一个中文词就把后者改掉，再输出`,
  ];
  const acronymRule = `- 缩写词一律全大写，如 ID、API、SQL、URL、AWS，绝不要写成 id、api（小写会被 TTS 当成普通单词读错音）`;
  const jaNaturalRule = `- 日语必须是日本人日常会说的自然日语，严禁英日混杂：技术概念用日本工程师惯用的片假名或汉字说法（如 データベース、認証、負荷分散、排他制御），不要在日语句子里原样夹英文单词或英文短语；只有 ID、API、SQL 这类日本人口语中也直接使用的缩写可以保留`;
  const jaNaturalSingle = langCode === "ja" ? jaNaturalRule : null;
  const enInterviewRule = `- 每条 A 的英语通常 10-16 个词，必要时最多 18 个词，简单内容可以更短；每行保持一个主旨，可以带一个表示原因、条件、时间或转折的简单从句，但不要嵌套从句或长关系从句`;
  const jaInterviewRule = `- 每条 A 的日语使用自然口语的です・ます体，一句保持一个主旨；可以带一个表示原因、条件、时间或转折的简短表达，但不要使用嵌套从句或长连体修饰`;
  const enConnectorRule = `- 英语优先使用常见、自然、容易说出口的连接词，如 and、but、so、because、if、when、while、although、for example、first / second / third、that's why；这些是优先选择，不是硬性白名单`;
  const singleInterviewRule = langCode === "ja" ? jaInterviewRule : enInterviewRule;
  const enConnectorSingle = langCode === "en" ? enConnectorRule : null;
  const simpleClauseSingle = langCode === "ja"
    ? `- 允许用一个简短的原因、条件、时间或转折表达把意思说完整，但不要嵌套从句或使用长连体修饰`
    : `- 允许用一个简单的原因、条件、时间或转折从句把意思说完整，但不要嵌套从句或使用长关系从句`;
  if (mode === "interview") {
    const rounds = promptCount("interview");
    const topicLabel = sysdesign ? "设计题目/客户场景" : "岗位/主题";
    const roleWord = sysdesign
      ? "同时也是一位资深系统设计面试官（或提出需求的客户）"
      : "同时也是一位资深技术面试官";
    const taskWord = sysdesign ? "系统设计模拟对话" : "模拟面试问答";
    const sourceLines = discussion
      ? [
          `素材来源：`,
          `- 如果这条指令发在原技术会话中，以本指令之前的技术讨论为素材`,
          `- 如果在新会话中使用，以粘贴在本指令末尾【聊天记录】中的内容为素材`,
          `- 如果两处都没有可用的技术讨论，只回复「请粘贴技术聊天记录」，不要自行编题`,
        ]
      : [`${topicLabel}：【在这里填，例如：${topicExample}】`];
    const countLine = discussion
      ? `目标问题数量：${rounds}（素材足够时整理为 ${rounds} 个 Q；素材不足时可以更少，宁少勿编；每个 Q 可以配多条 A）`
      : `问题数量：${rounds}（一共 ${rounds} 个 Q，至少 ${rounds} 个，宁多勿少；每个 Q 可以配多条 A）`;
    const introDual = discussion
      ? `你是 Echo Loop 跟读训练材料生成助手，同时也是一位资深技术面试官。请把当前会话中已有的技术讨论，或我附在本指令后的聊天记录，忠实整理成适合 TTS 朗读和口头跟读的「英语 + 日语 + 中文」三语模拟面试问答。`
      : `你是 Echo Loop 跟读训练材料生成助手，${roleWord}。请围绕我给的${topicLabel}，生成适合 TTS 朗读和口头跟读的「英语 + 日语 + 中文」三语${taskWord}。`;
    const introSingle = discussion
      ? `你是 Echo Loop 跟读训练材料生成助手，同时也是一位资深技术面试官。请把当前会话中已有的技术讨论，或我附在本指令后的聊天记录，忠实整理成适合 TTS 朗读和口头跟读的${L}模拟面试问答。`
      : `你是 Echo Loop 跟读训练材料生成助手，${roleWord}。请围绕我给的${topicLabel}，生成适合 TTS 朗读和口头跟读的${L}${taskWord}。`;
    // The content-specific section swaps with the selected interview flavor;
    // register, output format, furigana, and acronym rules stay shared.
    const depthSection = discussion
      ? [
          `聊天整理原则（核心要求）：`,
          `- 把用户提出的技术问题改写成自然的面试官 Q，把讨论中已经形成的回答整理成候选人 A；追问要紧跟对应主题`,
          `- 合并重复问题和重复答案，并按「基础概念 → 工作原理 → 设计取舍 → 边界 / 故障场景」重新排序`,
          `- 以讨论中的最终结论为准；如果后文修正了前文，采用修正后的说法，不要同时保留互相冲突的版本`,
          `- 忠实保留用户真实提到的项目事实、案例和数字；可以修正明显的技术错误，但不确定时要保守表达或省略`,
          `- 严禁编造用户没有说过的个人经历、生产事故、项目数据、选型理由或技术细节`,
          `- 不要为了凑数量强行加入自我介绍、无关八股、系统设计题或素材之外的知识点`,
          `- 删除寒暄、重复确认和关于聊天过程本身的元话语，只保留可用于技术面试的内容`,
        ]
      : sysdesign
      ? [
          `场景与流程（核心要求——系统设计 / FDE mock）：`,
          `- 这是一场场景驱动的系统设计对话：Q 是面试官/客户，A 是候选人。第一个 Q 必须以客户口吻给出一个模糊的业务需求，不要一上来就把细节说全`,
          `- 按四阶段循序推进：① 需求澄清（功能与非功能需求、量级与约束估算）→ ② 核心实体与 API 设计 → ③ 高层架构与数据库设计（表结构、主键、索引、缓存、分片/读写分离的取舍）→ ④ deep dive 深挖（瓶颈在哪、量级扩大十倍会怎样、失败场景、监控与降级）`,
          `- 需求澄清阶段的 A 必须包含澄清性反问：候选人先确认需求再动手设计，例如问清「实时」是几秒还是毫秒、读写比例、峰值量级、可以容忍什么样的不一致`,
          `- 讨论选型时使用固定的 trade-off 句型并反复出现：It depends on…；The trade-off here is X versus Y；That works, but it breaks down when…；Let me make sure I understand: you need X and the constraint is Y`,
          `- 每个设计决策都要给理由和取舍，不要只报方案名；数据库设计要落到具体的表、主键、索引，并说明为什么这样建`,
        ]
      : [
          `面试深度与覆盖（核心要求）：`,
          `- 对标资深 / 高级工程师面试：考机制原理、设计选型、trade-off、复杂度、并发与一致性、容错、可观测性、真实生产经验，不要停留在「是什么」的层面`,
          `- 循序渐进、分阶段推进：自我介绍 / 项目背景 → 基础概念 → 设计与机制 → 取舍与选型 → 边界与失败场景 → 性能与扩展 → deep dive 深挖`,
          `- 后段必须包含「深挖」：针对前面的某个回答继续追问，例如「为什么这样选」「如果量级再大十倍会怎样」「线上踩过什么坑」「还有没有更好的方案」`,
          `- 问得深也要问得广：从基础到系统设计再到工程实践都要覆盖，避免反复在同一个点上打转`,
        ];
    // Asymmetric register. Q is what the learner must UNDERSTAND, so it can
    // carry real-speech features; A is what the learner must SAY, so it keeps
    // the plain short-sentence rules above. Off by default.
    const wantsSpokenQ = spokenQ();
    const spokenQFeatures = {
      ja: `- 日语 Q 用这些真实特征：接续不断句（〜んですけど / 〜まして / 〜ていて / 〜とか / あと / で / ということで）、句尾省略（〜と。／〜ということで。／〜みたいな。／〜んですけど。）、口语缩约（〜ている→〜てる、という→っていう、〜てしまう→〜ちゃう、〜なければ→〜なきゃ）、填充词（まあ / なんか / ちょっと / そうですね / えっと）`,
      en: `- 英语 Q 用这些真实特征：用 and / so / but 把小句串下去而不是逐句断开、句尾收在半空（…, or something like that. ／ …, right? ／ …, if that makes sense.）、口语缩读（gonna / kind of / a bit of）、填充词（so / I mean / you know / basically / actually）`,
    };
    const spokenQLength = (dual) => dual
      ? `- 每条 Q 的长度：英语约 25-60 个词，日语约 40-110 个字符；超出就拆成下一个 Q，不要写成一整段独白`
      : langCode === "ja"
      ? `- 每条 Q 的日语约 40-110 个字符；超出就拆成下一个 Q，不要写成一整段独白`
      : `- 每条 Q 的英语约 25-60 个词；超出就拆成下一个 Q，不要写成一整段独白`;
    const spokenQSection = (dual) => !wantsSpokenQ ? [] : [
      ``,
      `面试官口语实感（只作用于 Q 行；A 行仍严格遵守上面的语域红线，不要放松）：`,
      `- Q 要写成「真人边想边说」的样子，不是念稿：允许 2-4 个小句连缀成一条，中间不收尾，最后才落到问题上`,
      `- 常见节奏是「先铺垫、再抛问题」：先讲一段团队 / 项目 / 场景的背景，最后一句才是真正要问的那句`,
      `- 允许省略助词，允许把句尾的判断部分省掉、让句子停在半空`,
      `- 允许说到一半改口重启（自我修复），但整份稿子里只出现 2-3 次，不要每条都有`,
      `- 填充词适量：大约每 3 条 Q 有 1 条带填充词，不要条条都塞`,
      ...(dual ? [spokenQFeatures.en, spokenQFeatures.ja]
        : [langCode === "ja" ? spokenQFeatures.ja : spokenQFeatures.en]),
      spokenQLength(dual),
      dual
        ? `- 英语和日语各自用本语言自然的口语方式表达同一个意思，不要把填充词逐字对译；两列中文都必须干净完整、把话说清楚，作为听不懂时的对照，各自仍贴着自己那一列的分句顺序`
        : `- 中文那一列必须干净完整、把话说清楚，作为听不懂时的对照，不要跟着写成口语碎句`,
    ];
    const qFormatRule = wantsSpokenQ
      ? `- 每个 Q 按上面「面试官口语实感」一节来写；无论多长都必须写在同一行里，也要避免括号、斜杠和难读符号`
      : `- 每个 Q 控制在 1 句自然口语；必要时可以带一个简单逻辑从句，但不要嵌套，也要避免括号、斜杠和难读符号`;
    if (isDual()) {
      return [
        introDual,
        ``,
        ...sourceLines,
        countLine,
        ``,
        `严格按以下格式输出，每行一条（一个 Q 后面可以跟 1 到 5 条 A）：`,
        `Q:<面试官的英语问题>|||<对应日语问题>|||<贴着英语语序的简体中文>|||<贴着日语语序的简体中文>`,
        `A:<英语回答·要点1>|||<对应日语>|||<贴着英语语序的简体中文>|||<贴着日语语序的简体中文>`,
        `A:<英语回答·要点2（同一问题，可选）>|||<对应日语>|||<贴着英语语序的简体中文>|||<贴着日语语序的简体中文>`,
        ``,
        ...depthSection,
        ``,
        `一问多答（重要）：`,
        discussion
          ? `- 简单回答用 1-2 条 A；只在原讨论确实包含多个要点时拆成 3-5 条 A，不要用扩写来凑答案`
          : `- 简单问题用 1-2 条 A；需要解释机制、取舍或边界的复杂问题通常用 3-5 条 A 逐点答透，越是 deep dive / 系统设计越要把逻辑讲完整`,
        `- 每条 A 都必须独占一行、各自以 A: 开头、各自带完整的四列（英语|||日语|||中文(对英)|||中文(对日)）；绝不要把多个要点塞进同一行，也不要写没有 A: 前缀的续行`,
        `- 多条 A 之间要有逻辑推进（先结论、再原因 / 机制、再取舍、再边界 / 例子 / 生产经验），合起来内容资深、解释完整、语言简单`,
        ``,
        `语域红线（非母语跟读材料——最重要的一节，逐条遵守）：`,
        `- 这些句子是给非母语者跟读、并要在真实面试里亲口说出来的：写「非母语者紧张时也能说出口的句子」，不是母语播音稿；语言简单 ≠ 内容初级，资深感来自机制、取舍、数字和生产经验，不来自词汇`,
        enInterviewRule,
        jaInterviewRule,
        `- 词汇用最常用的高频词 + 真实技术术语（Kafka、JWT、API、replay、idempotency 这类照留）；禁止习语、谚语和书面修辞`,
        `- 禁用包装词：spearheaded、leveraged、mission-critical、cutting-edge、world-class、seamlessly、state-of-the-art`,
        enConnectorRule,
        ...vocabSection(true),
        ...spokenQSection(true),
        ``,
        `格式与朗读要求：`,
        `- 每行以 Q: 或 A: 开头；顺序是「一个 Q，紧跟它的若干条 A」，再下一个 Q`,
        `- 分隔符必须是三个竖线 |||，顺序固定为 英语|||日语|||中文(对英)|||中文(对日)，共四列`,
        `- 英语和日语互为翻译，两列中文与它们同义`,
        qFormatRule,
        `- 每条 A 控制在 1 句、只讲一个要点，短到适合逐句跟读；要答得全靠「多条 A」，而不是把单条写长`,
        `- 英语、日语都要适合朗读：口语化、节奏清楚`,
        jaFuriganaRule,
        jaNaturalRule,
        acronymRule,
        zhAlignDual,
        `- 每条内容必须独占一行，不要换行续写`,
        `- 任意一列内部都不能再出现 |||`,
        `- 只输出问答行，不要标题、表格、序号、项目符号、解释、Markdown 或代码块`,
        discussion ? `` : null,
        discussion ? `【聊天记录（在原会话中使用可留空；在新会话中请粘贴到这里）】` : null,
      ].filter((line) => line !== null).join("\n");
    }
    return [
      introSingle,
      ``,
      ...sourceLines,
      countLine,
      ``,
      `严格按以下格式输出，每行一条（一个 Q 后面可以跟 1 到 5 条 A）：`,
      `Q:<面试官的${L}问题>|||<简体中文翻译>`,
      `A:<${L}回答·要点1>|||<简体中文翻译>`,
      `A:<${L}回答·要点2（同一问题，可选）>|||<简体中文翻译>`,
      ``,
      ...depthSection,
      ``,
      `一问多答（重要）：`,
      discussion
        ? `- 简单回答用 1-2 条 A；只在原讨论确实包含多个要点时拆成 3-5 条 A，不要用扩写来凑答案`
        : `- 简单问题用 1-2 条 A；需要解释机制、取舍或边界的复杂问题通常用 3-5 条 A 逐点答透，越是 deep dive / 系统设计越要把逻辑讲完整`,
      `- 每条 A 都必须独占一行、各自以 A: 开头、各自带完整的两列（${L}|||中文）；绝不要把多个要点塞进同一行，也不要写没有 A: 前缀的续行`,
      `- 多条 A 之间要有逻辑推进（先结论、再原因 / 机制、再取舍、再边界 / 例子 / 生产经验），合起来内容资深、解释完整、语言简单`,
      ``,
      `语域红线（非母语跟读材料——最重要的一节，逐条遵守）：`,
      `- 这些句子是给非母语者跟读、并要在真实面试里亲口说出来的：写「非母语者紧张时也能说出口的句子」，不是母语播音稿；语言简单 ≠ 内容初级，资深感来自机制、取舍、数字和生产经验，不来自词汇`,
      singleInterviewRule,
      `- 词汇用最常用的高频词 + 真实技术术语（Kafka、JWT、API、replay、idempotency 这类照留）；禁止习语、谚语和书面修辞`,
      `- 禁用包装词：spearheaded、leveraged、mission-critical、cutting-edge、world-class、seamlessly、state-of-the-art`,
      enConnectorSingle,
      ...vocabSection(false),
      ...spokenQSection(false),
      ``,
      `格式与朗读要求：`,
      `- 每行以 Q: 或 A: 开头；顺序是「一个 Q，紧跟它的若干条 A」，再下一个 Q`,
      `- 分隔符必须是三个竖线 |||，左边${L}、右边简体中文`,
      qFormatRule,
      `- 每条 A 控制在 1 句、只讲一个要点，短到适合逐句跟读；要答得全靠「多条 A」，而不是把单条写长`,
      `- ${L}部分要适合朗读：口语化、节奏清楚`,
      jaFuriganaSingle,
      jaNaturalSingle,
      acronymRule,
      zhAlignSingle,
      `- 每条内容必须独占一行，不要换行续写`,
      `- 左右两边都不能包含 |||`,
      `- 只输出问答行，不要标题、表格、序号、项目符号、解释、Markdown 或代码块`,
      discussion ? `` : null,
      discussion ? `【聊天记录（在原会话中使用可留空；在新会话中请粘贴到这里）】` : null,
    ].filter((line) => line !== null).join("\n");
  }
  if (textStyle() === "lecture") {
    return buildLecturePrompt(isDual(), L, langCode, {
      zhAlignDual, zhAlignSingle, vocabSection, jaFuriganaRule, jaNaturalRule, acronymRule,
    });
  }
  if (isDual()) {
    return [
      `你是 Echo Loop 跟读训练材料生成助手。请围绕我给的主题，生成适合 TTS 朗读、复听和跟读的「英语 + 日语 + 中文」三语短句。`,
      ``,
      `主题/场景：【在这里填，例如：日常购物对话】`,
      `句子数量：${promptCount("text")}`,
      ``,
      `严格按以下格式输出，每行一条：`,
      `<英语句子>|||<日语句子>|||<贴着英语语序的简体中文>|||<贴着日语语序的简体中文>`,
      ``,
      `要求：`,
      `- 分隔符必须是三个竖线 |||，顺序固定为 英语|||日语|||中文(对英)|||中文(对日)，共四列`,
      `- 每行一个完整短句，必须独占一行，不要换行续写`,
      `- 英语和日语互为翻译、长度大致相当，两列中文与它们同义`,
      `- 英语、日语都要自然口语化，适合朗读和影子跟读`,
      jaFuriganaRule,
      jaNaturalRule,
      acronymRule,
      `- 每句保持适合跟读的长度：英语通常 6-16 个词，必要时最多 18 个词；日语约 8-36 个字符`,
      `- 允许用一个简单的原因、条件、时间或转折从句把意思说完整，但不要嵌套从句、写长关系从句或长连体修饰`,
      `- 任意一列内部都不能再出现 |||`,
      `- 难度循序渐进，优先高频表达，避免过长从句、生僻专名和难读符号`,
      zhAlignDual,
      ...vocabSection(true),
      ``,
      `- 只输出句子行，不要标题、表格、序号、项目符号、解释、Markdown 或代码块`,
    ].filter((line) => line !== null).join("\n");
  }
  return [
    `你是 Echo Loop 跟读训练材料生成助手。请围绕我给的主题，生成适合 TTS 朗读、复听和跟读的${L}双语短句。`,
    ``,
    `主题/场景：【在这里填，例如：${topicExample}】`,
    `句子数量：${promptCount("text")}`,
    ``,
    `严格按以下格式输出，每行一条：`,
    `<${L}句子>|||<对应简体中文>`,
    ``,
    `要求：`,
    `- 分隔符必须是三个竖线 |||，左边${L}、右边简体中文`,
    `- 每行一个完整短句，必须独占一行，不要换行续写`,
    `- 左右两边都不能包含 |||`,
    `- ${L}部分要自然口语化，适合朗读和影子跟读`,
    jaFuriganaSingle,
    jaNaturalSingle,
    acronymRule,
    `- 每句保持适合跟读的长度：${L}${lenHint}`,
    simpleClauseSingle,
    `- 难度循序渐进，优先高频表达，避免过长从句、生僻专名和难读符号`,
    zhAlignSingle,
    ...vocabSection(false),
    ``,
    `- 只输出句子行，不要标题、表格、序号、项目符号、解释、Markdown 或代码块`,
  ].filter((line) => line !== null).join("\n");
}

// One continuous lecture instead of independent lines. Q&A fragments a topic
// into retrieval cues; a lecture builds the mental model those cues need —
// real-life problem first, then concept, mechanism, why, trade-offs, edges,
// with analogies mapped back to the term and a recap in the same words. The
// line format is unchanged (text mode eats it as-is); coherence comes from
// discourse markers between lines and `#` section comments the parser skips.
function buildLecturePrompt(dual, L, langCode, rules) {
  const sections = promptCount("text");
  const columns = dual
    ? "英语|||日语|||中文(对英)|||中文(对日)"
    : `${L}|||简体中文`;
  const intro = dual
    ? `你是 Echo Loop 跟读训练材料生成助手，同时也是一位擅长把复杂技术讲明白的老师。请围绕我给的主题，写一段适合 TTS 朗读、复听和跟读的「英语 + 日语 + 中文」三语讲课稿：像老师在讲台上讲一个章节，由浅入深、循序渐进，用现实生活的例子把机制讲透。`
    : `你是 Echo Loop 跟读训练材料生成助手，同时也是一位擅长把复杂技术讲明白的老师。请围绕我给的主题，写一段适合 TTS 朗读、复听和跟读的${L}讲课稿（配简体中文对照）：像老师在讲台上讲一个章节，由浅入深、循序渐进，用现实生活的例子把机制讲透。`;
  const formatLine = dual
    ? `<英语句子>|||<日语句子>|||<贴着英语语序的简体中文>|||<贴着日语语序的简体中文>`
    : `<${L}句子>|||<贴着${L}语序的简体中文>`;
  const titleExample = dual
    ? `Now let's look at how Redis writes data to disk. / では、Redis がデータをディスクにどう書（か）くかを見（み）ていきましょう。`
    : langCode === "ja"
    ? `では、Redis がデータをディスクにどう書（か）くかを見（み）ていきましょう。`
    : `Now let's look at how Redis writes data to disk.`;
  const markers = dual
    ? `英语 so / now / for example / that's why / in other words / but here's the problem；日语 では / つまり / たとえば / だから / でも、ここで問題（もんだい）があります`
    : langCode === "ja"
    ? `では / つまり / たとえば / だから / でも、ここで問題（もんだい）があります`
    : `so / now / for example / that's why / in other words / but here's the problem`;
  const lengthRule = dual
    ? `- 每句英语通常 8-16 个词，必要时最多 18 个词；日语约 8-36 个字符；一句一个主旨，可以带一个表示原因、条件、时间或转折的简单从句，不要嵌套从句或长连体修饰`
    : langCode === "ja"
    ? `- 每句日语约 8-36 个字符；一句一个主旨，可以带一个表示原因、条件、时间或转折的简短表达，不要嵌套从句或长连体修饰`
    : `- 每句英语通常 8-16 个词，必要时最多 18 个词；一句一个主旨，可以带一个表示原因、条件、时间或转折的简单从句，不要嵌套从句或长关系从句`;
  return [
    intro,
    ``,
    `主题/章节：【在这里填，例如：Redis 的持久化机制；可以把教材、文章或网页内容贴在本指令末尾作为素材】`,
    `小节数量：${sections}（每小节 10-20 行，整篇约 ${sections * 10}-${sections * 20} 行，把这一章讲完）`,
    ``,
    `严格按以下格式输出，每行一条：`,
    formatLine,
    ``,
    `讲课结构（核心要求）：`,
    `- 这是一条连续的叙事，不是知识点清单，也不是问答：每一句都接着上一句说，后面的内容踩在前面已经讲过的内容上`,
    `- 开头用一个现实生活里的问题或场景引入，先让人明白「为什么需要这个东西」，再引出概念`,
    `- 主线按「现实问题 → 概念 → 机制怎么运作 → 为什么这样设计 → 取舍与代价 → 边界和失败场景」推进；每个小节只往前推一步，不要在一节里塞两个概念`,
    `- 每个小节的第一行是一句口语化的小节引入（如 ${titleExample}），最后一到两行用同样的词把这一节的要点复述一遍`,
    `- 整篇最后一个小节是回顾：用前面用过的原词，把这一章讲了什么按顺序再说一遍`,
    `- 用现实生活的类比讲机制：类比先出场（便签和档案柜、邮局的多个窗口、餐厅的点单本这类），紧接着用一句话把类比明确映射回技术术语；类比是帮助理解的桥，不能代替对机制本身的解释`,
    `- 用话语标记把句子串起来，让听的人随时知道讲到哪一步：${markers}`,
    `- 内容要资深：讲到取舍、数字、失败场景和生产经验，语言保持简单；如果我附了素材，以素材为准，不要编造素材里没有的事实`,
    ``,
    `语域红线（非母语跟读材料——逐条遵守）：`,
    `- 这些句子是给非母语者逐句跟读的：写「非母语者也能顺口说出来的句子」，不是播音稿；语言简单 ≠ 内容初级`,
    lengthRule,
    `- 词汇用最常用的高频词 + 真实技术术语（Redis、Kafka、JWT、API 这类照留）；禁止习语、谚语和书面修辞`,
    `- 禁用包装词：spearheaded、leveraged、mission-critical、cutting-edge、world-class、seamlessly、state-of-the-art`,
    ...rules.vocabSection(dual),
    ``,
    `格式与朗读要求：`,
    dual
      ? `- 分隔符必须是三个竖线 |||，顺序固定为 ${columns}，共四列`
      : `- 分隔符必须是三个竖线 |||，左边${L}、右边简体中文`,
    dual ? `- 英语和日语互为翻译，两列中文与它们同义` : null,
    `- 每行一个完整句子，必须独占一行，不要换行续写`,
    `- ${dual ? "英语、日语都要" : `${L}要`}口语化、节奏清楚，像在讲话，不是在念书`,
    dual || langCode === "ja" ? rules.jaFuriganaRule : null,
    dual || langCode === "ja" ? rules.jaNaturalRule : null,
    rules.acronymRule,
    dual ? rules.zhAlignDual : rules.zhAlignSingle,
    `- 小节之间用一行 # 开头的注释标出小节序号和中文标题（如 # 1. 为什么需要缓存）；这一行不会被朗读，只是给我看的；# 必须顶格写在行首`,
    `- 任意一列内部都不能再出现 |||`,
    `- 除了 # 小节注释，只输出句子行，不要标题、表格、序号、项目符号或解释`,
    `- 把全部内容放在一个 \`\`\`text 代码块里输出，代码块之外不要任何文字；这样聊天窗口不会把 # 行渲染成标题，我复制出来的是原文`,
  ].filter((line) => line !== null).join("\n");
}

function updatePrompt() {
  $("promptText").textContent = buildPrompt(currentMode);
}

$("lang").addEventListener("change", updatePrompt);
function bindStyleRadios(name) {
  const inputs = [...document.querySelectorAll(`input[name="${name}"]`)];
  inputs.forEach((input, index) => {
    input.addEventListener("change", () => {
      syncPromptCountUI();
      updatePrompt();
    });
    input.addEventListener("keydown", (event) => {
      const backwards = event.key === "ArrowLeft" || event.key === "ArrowUp";
      const forwards = event.key === "ArrowRight" || event.key === "ArrowDown";
      if (!backwards && !forwards) return;
      event.preventDefault();
      const offset = backwards ? -1 : 1;
      const next = inputs[(index + offset + inputs.length) % inputs.length];
      next.checked = true;
      next.focus();
      next.dispatchEvent(new Event("change", { bubbles: true }));
    });
  });
}
bindStyleRadios("interviewStyle");
bindStyleRadios("textStyle");
$("spokenQ").addEventListener("change", updatePrompt);
$("promptCount").addEventListener("input", () => {
  promptCounts[promptCountKey(currentMode)] = $("promptCount").value;
  updatePrompt();
});
$("dual").addEventListener("change", () => {
  updateDualUI();
  updateContentPlaceholders();
  updatePrompt();
});

async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
  }
}

function flashCopied(btn) {
  const old = btn.textContent;
  btn.textContent = "已复制 ✓";
  setTimeout(() => (btn.textContent = old), 1500);
}

$("copyPromptBtn").addEventListener("click", async () => {
  await copyToClipboard(buildPrompt(currentMode));
  flashCopied($("copyPromptBtn"));
});

// Exam prompt: a static snapshot of interview-notes' portable phone prompt.
let examPromptText = "";
async function loadExamPrompt() {
  try {
    const res = await fetch("/static/exam-prompt.md");
    if (!res.ok) throw new Error(res.statusText);
    examPromptText = (await res.text()).trim();
  } catch (_) {
    examPromptText = "";
  }
  $("examPromptText").textContent = examPromptText || "提示词加载失败，请刷新页面。";
}

$("copyExamPromptBtn").addEventListener("click", async () => {
  if (!examPromptText) return;
  await copyToClipboard(examPromptText);
  flashCopied($("copyExamPromptBtn"));
});

$("variant").addEventListener("change", (e) => {
  $("customRepeats").classList.toggle("hidden", e.target.value !== "custom");
});
$("engine").addEventListener("change", updateVoiceUI);
$("split").addEventListener("change", () => {
  splitTouched = true;
});

// Settings panel
$("settingsBtn").addEventListener("click", () => {
  $("settingsPanel").classList.toggle("hidden");
  $("tokenInput").value = getToken();
});
$("saveToken").addEventListener("click", () => {
  localStorage.setItem(TOKEN_KEY, $("tokenInput").value.trim());
  $("settingsPanel").classList.add("hidden");
  refreshHistory();
});

$("refreshBtn").addEventListener("click", refreshHistory);

// ---------------------------------------------------------------------------
// Submit
// ---------------------------------------------------------------------------

function collectRequest() {
  const req = { mode: currentMode, content: $("content").value, name: $("name").value.trim() };

  const dual = isDual();
  if (dual) req.dual = true;
  if (!dual && $("lang").value) req.lang = $("lang").value;
  if ($("engine").value) req.engine = $("engine").value;

  // Voice (Google Chirp3-HD persona). Skipped when the picker is disabled
  // (non-Google engine). Text mode -> one voice; interview -> Q + A.
  if (currentMode === "interview") {
    if (!$("qVoice").disabled && $("qVoice").value) req.q_voice = $("qVoice").value;
    if (!$("aVoice").disabled && $("aVoice").value) req.a_voice = $("aVoice").value;
    if ($("qRate").value !== "") req.q_rate = parseFloat($("qRate").value);
    if ($("aRate").value !== "") req.a_rate = parseFloat($("aRate").value);
  } else {
    if (!$("voice").disabled && $("voice").value) req.voice = $("voice").value;
    if ($("rate").value !== "") req.rate = parseFloat($("rate").value);
  }

  const loop = {};
  const variant = $("variant").value;
  if (variant === "custom") {
    loop.tnt = parseInt($("tnt").value, 10);
    loop.tst = parseInt($("tst").value, 10);
  } else if (variant) {
    loop.variant = variant;
  }
  if (splitTouched) loop.split = $("split").checked;
  req.loop = loop;

  const timing = {};
  if ($("t1").value !== "") timing.after_first_target = parseFloat($("t1").value);
  if ($("t2").value !== "") timing.after_native = parseFloat($("t2").value);
  if ($("t3").value !== "") timing.after_second_target = parseFloat($("t3").value);
  if (Object.keys(timing).length) req.timing = timing;

  if ($("gain").value !== "") req.gain = parseFloat($("gain").value);
  if ($("nativeGain").value !== "") req.native_gain = parseFloat($("nativeGain").value);

  return req;
}

$("generateBtn").addEventListener("click", async () => {
  clearError();
  const req = collectRequest();
  if (!req.name) {
    showError("请先填写名称（用于文件名和同步文件夹）。");
    return;
  }
  if (!req.content.trim()) {
    showError("请输入要生成的内容。");
    return;
  }
  const btn = $("generateBtn");
  btn.disabled = true;
  btn.textContent = "提交中…";
  try {
    const { job_id } = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
    await refreshHistory();
    startPolling(job_id);
  } catch (err) {
    showError("提交失败：" + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "生成音频";
  }
});

// ---------------------------------------------------------------------------
// Polling + rendering
// ---------------------------------------------------------------------------

function startPolling(jobId) {
  if (pollers.has(jobId)) return;
  const id = setInterval(async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      updateJobCard(job);
      if (job.status === "done" || job.status === "error") {
        clearInterval(id);
        pollers.delete(jobId);
      }
    } catch (err) {
      clearInterval(id);
      pollers.delete(jobId);
    }
  }, 1500);
  pollers.set(jobId, id);
}

async function refreshHistory() {
  const container = $("history");
  try {
    const { jobs } = await api("/api/jobs");
    if (!jobs.length) {
      container.innerHTML = '<div class="empty">还没有生成记录。</div>';
      return;
    }
    container.innerHTML = "";
    jobs.forEach((job) => {
      container.appendChild(renderJobCard(job));
      if (job.status === "queued" || job.status === "running") startPolling(job.id);
    });
  } catch (err) {
    container.innerHTML = `<div class="empty">加载失败：${err.message}</div>`;
  }
}

function badge(status) {
  const labels = { queued: "排队中", running: "生成中", done: "完成", error: "失败" };
  const spin = status === "running" || status === "queued" ? '<span class="spin"></span>' : "";
  return `<span class="badge ${status}">${spin}${labels[status] || status}</span>`;
}

function renderJobCard(job) {
  const card = document.createElement("div");
  card.className = "job";
  card.id = "job-" + job.id;
  card.innerHTML = jobCardInner(job);
  bindJobActions(card, job);
  return card;
}

function updateJobCard(job) {
  const card = $("job-" + job.id);
  if (!card) {
    refreshHistory();
    return;
  }
  card.innerHTML = jobCardInner(job);
  bindJobActions(card, job);
}

function jobCardInner(job) {
  const modeLabel = job.mode === "interview" ? "面试稿" : "文本";
  const meta = [modeLabel, job.engine || "", job.lang || "", fmtTime(job.created_at)]
    .filter(Boolean)
    .join(" · ");

  let body = "";

  if (job.status === "running" && job.log_tail && job.log_tail.length) {
    body += `<div class="logtail">${job.log_tail.map(escapeHtml).join("\n")}</div>`;
  }

  if (job.status === "error") {
    body += `<div class="error">${escapeHtml(job.error || "生成失败")}</div>`;
  }

  if (job.status === "done" && job.files && job.files.length) {
    const outputs = job.files
      .filter((f) => isAudio(f))
      .map((name) => {
        const lrc = job.files.find((f) => f.endsWith(".lrc") && sameStem(f, name));
        const lrcLink = lrc
          ? `<a href="${fileUrl(job.id, lrc, true)}">下载 LRC</a>`
          : "";
        return `
          <div class="output">
            <div class="name">${escapeHtml(name)}</div>
            <audio controls preload="none" src="${fileUrl(job.id, name)}"></audio>
            <div class="links">
              <a href="${fileUrl(job.id, name, true)}">下载音频</a>
              ${lrcLink}
            </div>
          </div>`;
      })
      .join("");
    body += `<div class="outputs">${outputs}</div>`;
  }

  return `
    <div class="job-head">
      <div>
        <div class="job-title">${escapeHtml(job.title || "(未命名)")}</div>
        <div class="job-meta">${escapeHtml(meta)}</div>
      </div>
      ${badge(job.status)}
    </div>
    ${body}
    <div class="job-actions">
      <button class="link-btn danger" data-action="delete">删除</button>
    </div>`;
}

function bindJobActions(card, job) {
  const del = card.querySelector('[data-action="delete"]');
  if (del) {
    del.addEventListener("click", async () => {
      if (!confirm("删除这条记录及其音频？")) return;
      try {
        await api(`/api/jobs/${job.id}`, { method: "DELETE" });
        if (pollers.has(job.id)) {
          clearInterval(pollers.get(job.id));
          pollers.delete(job.id);
        }
        card.remove();
        if (!$("history").children.length) refreshHistory();
      } catch (err) {
        showError("删除失败：" + err.message);
      }
    });
  }
}

function sameStem(lrcName, audioName) {
  const a = audioName.replace(/\.[^.]+$/, "");
  const l = lrcName.replace(/\.[^.]+$/, "");
  return a === l;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function init() {
  updateDualUI();
  updateContentPlaceholders();
  updateVoiceUI();
  syncPromptCountUI();
  updatePrompt();
  loadExamPrompt();
  await loadConfigDefaults();
  refreshHistory();
}

init();

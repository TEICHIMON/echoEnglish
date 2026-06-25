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

function volumeLabel(tts) {
  if (!tts) return "配置默认";
  if (tts.normalize !== null && tts.normalize !== undefined && tts.normalize !== "") {
    return `normalize ${tts.normalize} dBFS`;
  }
  const gain = Number(tts.gain || 0);
  return `${gain > 0 ? "+" : ""}${gain} dB`;
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
  $("t1").placeholder = appDefaults.timing.after_first_target;
  $("t2").placeholder = appDefaults.timing.after_native;
  $("t3").placeholder = appDefaults.timing.after_second_target;

  $("defaultsSummary").innerHTML = [
    ["目标语言", lang ? langLabel(lang) : "配置"],
    ["TTS", engineLabel(appDefaults.engine)],
    ["循环", loopLabel(appDefaults.loop)],
    ["停顿", timingLabel(appDefaults.timing)],
    ["音量", volumeLabel(appDefaults.tts)],
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
// Tabs + options UI
// ---------------------------------------------------------------------------

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    currentMode = tab.dataset.mode;
    if (currentMode === "interview") {
      $("content").placeholder =
        "Q:/A: 面试稿，每条同样用 ||| 分隔中文。例如：\nQ:Tell me about yourself.|||请介绍一下你自己。\nA:Sure, I am a backend engineer.|||当然，我是一名后端工程师。";
      $("formatHint").innerHTML =
        '格式：<code>Q:/A: 目标语言|||中文</code>。支持 Q:/Question:/Interviewer: 与 A:/Answer:/Candidate:。';
    } else {
      $("content").placeholder =
        "每行一条：目标语言|||中文翻译\n例如：\nこれはテストです|||这是一个测试\n水を飲みます|||我喝水";
      $("formatHint").innerHTML =
        '格式：<code>目标语言|||中文</code>，每行一条，<code>#</code> 开头为注释。';
    }
    updateDefaultLabels();
    updatePrompt();
  });
});

// ---------------------------------------------------------------------------
// AI prompt helper (copy a ready-made prompt to generate a script)
// ---------------------------------------------------------------------------

function langWord() {
  const l = $("lang").value || currentDefaultLang();
  return l === "ja" ? "日语" : l === "en" ? "英语" : "目标语言";
}

function buildPrompt(mode) {
  const L = langWord();
  const topicExample = mode === "interview" ? "后端工程师，5 年经验" : `${L} 日常购物对话`;
  if (mode === "interview") {
    return [
      `你是 Echo Loop 跟读训练材料生成助手。请围绕我给的岗位/主题，生成适合 TTS 朗读和口头跟读的${L}模拟面试问答。`,
      ``,
      `岗位/主题：【在这里填，例如：${topicExample}】`,
      `问答轮数：8（Q 与 A 交替）`,
      ``,
      `严格按以下格式输出，每行一条：`,
      `Q:<面试官的${L}问题>|||<简体中文翻译>`,
      `A:<应聘者的${L}回答>|||<简体中文翻译>`,
      ``,
      `要求：`,
      `- 每行以 Q: 或 A: 开头，Q、A 交替`,
      `- 分隔符必须是三个竖线 |||，左边${L}、右边简体中文`,
      `- 每个 Q 控制在 1 句，简短、自然、像真实面试官会问的话`,
      `- 每个 A 控制在 1-2 句，只讲一个重点，不要写成长段落`,
      `- ${L}部分要适合朗读：口语化、节奏清楚，避免复杂从句、括号、斜杠和难读符号`,
      `- 中文翻译要简洁自然，方便快速理解，不要扩写`,
      `- 每条内容必须独占一行，不要换行续写`,
      `- 左右两边都不能包含 |||`,
      `- 只输出问答行，不要标题、表格、序号、项目符号、解释、Markdown 或代码块`,
    ].join("\n");
  }
  return [
    `你是 Echo Loop 跟读训练材料生成助手。请围绕我给的主题，生成适合 TTS 朗读、复听和跟读的${L}双语短句。`,
    ``,
    `主题/场景：【在这里填，例如：${topicExample}】`,
    `句子数量：15`,
    ``,
    `严格按以下格式输出，每行一条：`,
    `<${L}句子>|||<对应简体中文>`,
    ``,
    `要求：`,
    `- 分隔符必须是三个竖线 |||，左边${L}、右边简体中文`,
    `- 每行一个完整短句，必须独占一行，不要换行续写`,
    `- 左右两边都不能包含 |||`,
    `- ${L}部分要自然口语化，适合朗读和影子跟读`,
    `- 每句尽量短：英语约 6-14 个词；日语约 8-28 个字符`,
    `- 难度循序渐进，优先高频表达，避免过长从句、生僻专名和难读符号`,
    `- 中文翻译要简洁自然，贴近原意，不要扩写`,
    `- 只输出句子行，不要标题、表格、序号、项目符号、解释、Markdown 或代码块`,
  ].join("\n");
}

function updatePrompt() {
  $("promptText").textContent = buildPrompt(currentMode);
}

$("lang").addEventListener("change", updatePrompt);

$("copyPromptBtn").addEventListener("click", async () => {
  const text = buildPrompt(currentMode);
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
  const btn = $("copyPromptBtn");
  const old = btn.textContent;
  btn.textContent = "已复制 ✓";
  setTimeout(() => (btn.textContent = old), 1500);
});

$("variant").addEventListener("change", (e) => {
  $("customRepeats").classList.toggle("hidden", e.target.value !== "custom");
});
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
  const req = { mode: currentMode, content: $("content").value };

  if ($("lang").value) req.lang = $("lang").value;
  if ($("engine").value) req.engine = $("engine").value;

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

  return req;
}

$("generateBtn").addEventListener("click", async () => {
  clearError();
  const req = collectRequest();
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
  updatePrompt();
  await loadConfigDefaults();
  refreshHistory();
}

init();

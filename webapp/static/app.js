"use strict";

// ---------------------------------------------------------------------------
// State + helpers
// ---------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);
const TOKEN_KEY = "echo_token";

let currentMode = "text";
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
  });
});

$("variant").addEventListener("change", (e) => {
  $("customRepeats").classList.toggle("hidden", e.target.value !== "custom");
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
  loop.split = $("split").checked;
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

refreshHistory();

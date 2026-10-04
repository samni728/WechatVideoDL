(() => {
  const $ = (id) => document.getElementById(id);
  const page = document.body?.dataset.page || "";
  const themeSelect = $("theme-select");

  function applyTheme(value) {
    const theme = ["light", "dark", "system"].includes(value) ? value : "system";
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("wvd-theme", theme);
    if (themeSelect) themeSelect.value = theme;
  }
  if (themeSelect) {
    applyTheme(localStorage.getItem("wvd-theme") || "system");
    themeSelect.addEventListener("change", () => applyTheme(themeSelect.value));
  }

  class ApiError extends Error {
    constructor(message, { status = 0, data = null, excerpt = "" } = {}) {
      super(message); this.name = "ApiError"; this.status = status; this.data = data; this.excerpt = excerpt;
    }
  }

  async function safeFetch(url, options = {}) {
    const response = await fetch(url, options);
    const contentType = (response.headers.get("content-type") || "").toLowerCase();
    const raw = await response.text();
    if (!contentType.includes("application/json")) {
      const excerpt = raw.replace(/\s+/g, " ").trim().slice(0, 500);
      throw new ApiError(`服务器返回了非 JSON 错误（HTTP ${response.status}）。可能是反向代理或上游服务超时。`, { status: response.status, excerpt });
    }
    let data;
    try { data = raw ? JSON.parse(raw) : {}; }
    catch (_) { throw new ApiError(`服务器返回了无法解析的 JSON（HTTP ${response.status}）。`, { status: response.status, excerpt: raw.slice(0, 500) }); }
    if (!response.ok || data.ok === false) {
      const message = data?.error?.message || `请求失败（HTTP ${response.status}）`;
      throw new ApiError(message, { status: response.status, data });
    }
    return data;
  }

  function escapeText(value) { return value == null ? "" : String(value); }
  function fmtDate(value) {
    if (!value) return "—";
    try { return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)); }
    catch (_) { return value; }
  }
  function setStatus(el, text, kind = "idle") {
    if (!el) return; el.textContent = text; el.className = `status-badge ${kind}`;
  }

  async function initWorkspace() {
    const videoUrl = $("video-url"), presetSelect = $("preset-select"), promptInput = $("prompt-input");
    const outputFormat = $("output-format"), downloadMp4 = $("download-mp4"), parseButton = $("parse-button");
    const resultEmpty = $("result-empty"), resultContent = $("result-content"), resultError = $("result-error");
    const resultStatus = $("result-status"), technicalJson = $("technical-json"), analysisContent = $("analysis-content");
    const retryNotice = $("retry-notice"), downloadLinks = $("download-links"), healthPill = $("health-pill");
    let presets = [];

    try {
      const data = await safeFetch("/api/presets"); presets = data.presets || [];
      presets.forEach((preset) => { const option = document.createElement("option"); option.value = preset.id; option.textContent = preset.name; presetSelect.appendChild(option); });
    } catch (_) {}
    presetSelect.addEventListener("change", () => {
      const preset = presets.find((item) => item.id === presetSelect.value);
      promptInput.value = preset ? preset.prompt : "";
      promptInput.focus();
    });

    try {
      const health = await safeFetch("/health");
      if (health.ok) { healthPill.classList.add("ok"); healthPill.querySelector("span:last-child").textContent = "服务在线"; }
    } catch (_) { healthPill.querySelector("span:last-child").textContent = "服务状态未知"; }

    parseButton.addEventListener("click", async () => {
      const url = videoUrl.value.trim();
      if (!url) { videoUrl.focus(); return; }
      parseButton.disabled = true; parseButton.querySelector("span:first-child").textContent = "解析中 · 自动重试已开启";
      resultEmpty.classList.add("hidden"); resultContent.classList.add("hidden"); resultError.classList.add("hidden");
      setStatus(resultStatus, "处理中", "running");
      try {
        const data = await safeFetch("/api/parse", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url, prompt: promptInput.value.trim() || null, preset_id: presetSelect.value || null, output_format: outputFormat.value, download: downloadMp4.checked })
        });
        resultContent.classList.remove("hidden"); setStatus(resultStatus, "已完成", "completed");
        $("result-id").textContent = data.id || "—"; $("result-attempts").textContent = `${data.attempts_used || 1} / ${data.max_attempts || 3}`; $("result-format").textContent = (data.output_format || "txt").toUpperCase();
        analysisContent.textContent = data.content || ""; technicalJson.textContent = JSON.stringify(data, null, 2);
        retryNotice.classList.toggle("hidden", !(data.retry_errors || []).length);
        retryNotice.textContent = (data.retry_errors || []).length ? `任务曾遇到 ${data.retry_errors.length} 次临时错误，后台已自动恢复。` : "";
        downloadLinks.replaceChildren();
        if (data.text?.download_path) downloadLinks.appendChild(downloadLink(data.text.download_path, `下载 ${(data.output_format || "txt").toUpperCase()}`));
        if (data.video?.download_path) downloadLinks.appendChild(downloadLink(data.video.download_path, "下载 MP4"));
      } catch (error) {
        setStatus(resultStatus, "失败", "failed"); resultError.classList.remove("hidden");
        $("error-message").textContent = error.message || "请求失败";
        const details = $("error-details"), excerpt = $("error-excerpt");
        const data = error.data || null; const text = error.excerpt || (data ? JSON.stringify(data, null, 2) : "");
        details.classList.toggle("hidden", !text); excerpt.textContent = text;
        if (data?.job) technicalJson.textContent = JSON.stringify(data.job, null, 2);
      } finally {
        parseButton.disabled = false; parseButton.querySelector("span:first-child").textContent = "开始解析";
      }
    });
    $("copy-content")?.addEventListener("click", async () => { if (analysisContent.textContent) await navigator.clipboard.writeText(analysisContent.textContent); });
  }

  function downloadLink(path, label) {
    const a = document.createElement("a"); a.className = "download-pill"; a.href = path; a.textContent = label; return a;
  }

  async function initHistory() {
    const root = $("history-list"), filter = $("history-filter"), refresh = $("refresh-history");
    let jobs = [];
    const render = () => {
      const q = (filter.value || "").trim().toLowerCase();
      const visible = jobs.filter((job) => !q || [job.id, job.input_url, job.prompt, job.preset_name].some((v) => String(v || "").toLowerCase().includes(q)));
      root.replaceChildren();
      if (!visible.length) { const empty = document.createElement("div"); empty.className = "history-empty panel"; empty.textContent = q ? "没有匹配的历史记录。" : "还没有历史记录，完成一次解析后会出现在这里。"; root.appendChild(empty); return; }
      visible.forEach((job) => root.appendChild(historyCard(job, load)));
    };
    const load = async () => {
      root.innerHTML = '<div class="history-empty panel">正在加载历史记录…</div>';
      try { const data = await safeFetch("/api/history"); jobs = data.jobs || []; render(); }
      catch (error) { root.replaceChildren(); const box = document.createElement("div"); box.className = "history-empty panel"; box.textContent = error.message; root.appendChild(box); }
    };
    filter.addEventListener("input", render); refresh.addEventListener("click", load); await load();
  }

  function historyCard(job, reload) {
    const card = document.createElement("article"); card.className = "history-card panel";
    const main = document.createElement("div"); main.className = "history-main";
    const top = document.createElement("div"); top.className = "history-topline";
    const id = document.createElement("span"); id.className = "history-id"; id.textContent = job.id;
    const badge = document.createElement("span"); setStatus(badge, job.status === "completed" ? "已完成" : job.status === "failed" ? "失败" : "进行中", job.status);
    const date = document.createElement("span"); date.className = "history-date"; date.textContent = fmtDate(job.created_at);
    top.append(id, badge, date);
    const source = document.createElement("span"); source.className = "history-source"; source.textContent = job.input_url || "";
    const prompt = document.createElement("p"); prompt.className = "history-prompt"; prompt.textContent = job.prompt || "未设置 Prompt";
    main.append(top, source, prompt);
    const actions = document.createElement("div"); actions.className = "history-actions";
    if (job.text?.download_path) actions.appendChild(downloadLink(job.text.download_path, `下载 ${(job.output_format || "txt").toUpperCase()}`));
    if (job.video?.download_path) actions.appendChild(downloadLink(job.video.download_path, "MP4"));
    const detailBtn = document.createElement("button"); detailBtn.className = "text-button"; detailBtn.type = "button"; detailBtn.textContent = "详情";
    const del = document.createElement("button"); del.className = "danger-button"; del.type = "button"; del.textContent = "删除";
    actions.append(detailBtn, del); card.append(main, actions);
    const detail = document.createElement("div"); detail.className = "history-detail hidden"; const pre = document.createElement("pre"); detail.appendChild(pre); card.appendChild(detail);
    detailBtn.addEventListener("click", async () => { if (!detail.classList.contains("hidden")) { detail.classList.add("hidden"); return; } try { const data = await safeFetch(`/api/history/${encodeURIComponent(job.id)}`); pre.textContent = data.job.content || data.job.error_message || "没有文本内容"; detail.classList.remove("hidden"); } catch (e) { pre.textContent = e.message; detail.classList.remove("hidden"); } });
    del.addEventListener("click", async () => { if (!window.confirm(`确定删除 ${job.id} 及其关联文件吗？此操作不可恢复。`)) return; del.disabled = true; try { await safeFetch(`/api/history/${encodeURIComponent(job.id)}`, { method: "DELETE" }); await reload(); } catch (e) { window.alert(e.message); del.disabled = false; } });
    return card;
  }

  if (page === "workspace") initWorkspace();
  if (page === "history") initHistory();
  window.WechatVideoDL = { safeFetch, applyTheme };
})();

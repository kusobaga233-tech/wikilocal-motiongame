const state = { sources: [] };

const api = async (path, options = {}) => {
  const response = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "本地服务请求失败");
  }
  return response.json();
};

const escapeHtml = (value) => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

const toast = (message, isError = false) => {
  const node = document.querySelector("#toast");
  node.textContent = message;
  node.classList.toggle("is-error", isError);
  node.classList.add("is-visible");
  window.setTimeout(() => node.classList.remove("is-visible"), 3200);
};

const renderSources = (sources) => {
  const list = document.querySelector("#source-list");
  list.innerHTML = sources.length ? sources.map((source) => {
    const metadata = source.metadata || {};
    const details = [metadata.chat_name, metadata.wiki_path, metadata.sent_at]
      .filter(Boolean).map(escapeHtml).join(" · ");
    return `<article class="source-item">
      <strong>${escapeHtml(source.title)}</strong>
      <small>${details || escapeHtml(source.source_type)}</small>
      <p>${escapeHtml(source.excerpt || "")}</p>
    </article>`;
  }).join("") : "<p class=\"muted\">暂无已同步来源</p>";
};

const refreshSources = async (query = "") => {
  const result = await api(`/sources?query=${encodeURIComponent(query)}`);
  state.sources = result.sources;
  renderSources(state.sources);
};

const refreshStatus = async () => {
  const result = await api("/sync/status");
  const show = (label, item) => `<div class="status-row"><strong>${label}</strong><span>新增 ${item.created} · 更新 ${item.changed} · 跳过 ${item.skipped} · 失败 ${item.failed}</span></div>`;
  document.querySelector("#sync-status").innerHTML = show("文档", result.documents) + show("聊天", result.chats);
};

const loadSettings = async () => {
  const settings = await api("/settings");
  document.querySelector("#daily-time").value = settings.daily_time;
  document.querySelector("#documents-enabled").checked = settings.documents_enabled;
  document.querySelector("#chats-enabled").checked = settings.chats_enabled;
  document.querySelector("#history-start").value = settings.chat_history_start || "";
};

const appendAnswer = (question, answer) => {
  const conversation = document.querySelector("#conversation");
  conversation.querySelector(".empty-state")?.remove();
  const citations = answer.citations.map((citation, index) => {
    const url = citation.metadata?.url;
    const title = escapeHtml(citation.title);
    const evidence = escapeHtml(citation.text_content);
    const link = url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${title}</a>` : title;
    return `<details class="citation"><summary>[${index + 1}] ${link}</summary><p>${evidence}</p></details>`;
  }).join("") || "<p class=\"muted\">未找到可引用的本地来源</p>";
  conversation.insertAdjacentHTML("beforeend", `<article class="turn user-turn"><p>${escapeHtml(question)}</p></article><article class="turn answer-turn"><p>${escapeHtml(answer.text)}</p><div class="citations">${citations}</div></article>`);
  conversation.scrollTop = conversation.scrollHeight;
};

document.querySelector("#question-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.querySelector("#question");
  const question = input.value.trim();
  if (!question) return;
  const button = document.querySelector("#ask-button");
  button.disabled = true;
  try {
    appendAnswer(question, await api("/answer", { method: "POST", body: JSON.stringify({ question }) }));
    input.value = "";
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    input.focus();
  }
});

document.querySelectorAll("[data-sync]").forEach((button) => button.addEventListener("click", async () => {
  button.disabled = true;
  try {
    const result = await api(`/sync/${button.dataset.sync}`, { method: "POST" });
    toast(`同步完成：新增 ${result.created}，更新 ${result.changed}`);
    await Promise.all([refreshStatus(), refreshSources()]);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}));

document.querySelector("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/settings", {
      method: "PUT",
      body: JSON.stringify({
        daily_time: document.querySelector("#daily-time").value,
        documents_enabled: document.querySelector("#documents-enabled").checked,
        chats_enabled: document.querySelector("#chats-enabled").checked,
        chat_history_start: document.querySelector("#history-start").value || null,
      }),
    });
    toast("设置已保存");
  } catch (error) {
    toast(error.message, true);
  }
});

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((node) => node.classList.toggle("is-active", node === tab));
  document.querySelectorAll(".panel").forEach((node) => node.classList.toggle("is-active", node.dataset.panelContent === tab.dataset.panel));
}));

let searchTimer;
document.querySelector("#source-search").addEventListener("input", (event) => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => refreshSources(event.target.value).catch((error) => toast(error.message, true)), 200);
});

Promise.all([api("/health"), refreshSources(), refreshStatus(), loadSettings()])
  .then(([health]) => { document.querySelector("#model-status").textContent = `本地模型：${health.models.answer}`; })
  .catch((error) => { document.querySelector("#model-status").textContent = "本地服务不可用"; toast(error.message, true); });

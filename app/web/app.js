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
  const show = (label, item) => {
    const error = item.error ? `<small class="sync-error">${escapeHtml(item.error)}</small>` : "";
    return `<div class="status-row"><strong>${label}</strong><span>新增 ${item.created} · 更新 ${item.changed} · 跳过 ${item.skipped} · 失败 ${item.failed}</span>${error}</div>`;
  };
  document.querySelector("#sync-status").innerHTML = show("文档", result.documents) + show("聊天", result.chats);
};

const loadSettings = async () => {
  const settings = await api("/settings");
  document.querySelector("#daily-time").value = settings.daily_time;
  document.querySelector("#documents-enabled").checked = settings.documents_enabled;
  document.querySelector("#chats-enabled").checked = settings.chats_enabled;
  document.querySelector("#history-start").value = settings.chat_history_start || "";
};

const safeUrl = (value) => {
  try {
    const url = new URL(String(value));
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : "";
  } catch {
    return "";
  }
};

const citationMarkup = (citation, index) => {
  const metadata = citation.metadata || {};
  const url = safeUrl(metadata.url);
  const title = escapeHtml(citation.title);
  const sender = metadata.sender ? `<span>${escapeHtml(metadata.sender)}</span>` : "";
  const timestamp = metadata.sent_at ? `<time>${escapeHtml(metadata.sent_at)}</time>` : "";
  const evidence = escapeHtml(citation.text_content);
  const link = url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${title}</a>` : title;
  return `<details class="citation"><summary>[${index + 1}] ${link}</summary><div class="citation-meta">${sender}${timestamp}</div><p>${evidence}</p></details>`;
};

const citationsMarkup = (citations) => citations.map(citationMarkup).join("")
  || "<p class=\"muted\">未找到可引用的本地来源</p>";

const scrollConversation = () => {
  const conversation = document.querySelector("#conversation");
  conversation.scrollTop = conversation.scrollHeight;
};

const appendAnswer = (question, answer) => {
  const conversation = document.querySelector("#conversation");
  conversation.querySelector(".empty-state")?.remove();
  conversation.insertAdjacentHTML("beforeend", `<article class="turn user-turn"><p>${escapeHtml(question)}</p></article><article class="turn answer-turn"><p class="answer-text">${escapeHtml(answer.text)}</p><div class="citations">${citationsMarkup(answer.citations || [])}</div></article>`);
  scrollConversation();
};

const createPendingAnswer = (question) => {
  const conversation = document.querySelector("#conversation");
  conversation.querySelector(".empty-state")?.remove();
  conversation.insertAdjacentHTML("beforeend", `<article class="turn user-turn"><p>${escapeHtml(question)}</p></article><article class="turn answer-turn is-streaming"><p class="answer-text"></p><div class="citations" hidden></div></article>`);
  const answerTurn = conversation.lastElementChild;
  return { userTurn: answerTurn.previousElementSibling, answerTurn };
};

const loadConversation = async () => {
  const result = await api("/conversations");
  result.turns.forEach((turn) => appendAnswer(turn.question, turn));
};

const streamAnswer = async (question, pending) => {
  const response = await fetch("/api/answer/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (response.status === 404 || !response.body) return false;
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "本地服务请求失败");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const answerText = pending.answerTurn.querySelector(".answer-text");
  const citations = pending.answerTurn.querySelector(".citations");
  let remainder = "";
  while (true) {
    const { done, value } = await reader.read();
    remainder += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = remainder.split("\n");
    remainder = lines.pop();
    for (const line of lines) {
      if (!line) continue;
      const event = JSON.parse(line);
      if (event.type === "delta") {
        answerText.textContent += event.text;
        scrollConversation();
      } else if (event.type === "answer") {
        citations.innerHTML = citationsMarkup(event.citations || []);
        citations.hidden = false;
        pending.answerTurn.classList.remove("is-streaming");
      } else if (event.type === "error") {
        throw new Error(event.detail || "本地模型生成失败");
      }
    }
    if (done) break;
  }
  return true;
};

document.querySelector("#question-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.querySelector("#question");
  const question = input.value.trim();
  if (!question) return;
  const button = document.querySelector("#ask-button");
  button.disabled = true;
  try {
    const pending = createPendingAnswer(question);
    const streamed = await streamAnswer(question, pending);
    if (!streamed) {
      pending.userTurn.remove();
      pending.answerTurn.remove();
      appendAnswer(question, await api("/answer", { method: "POST", body: JSON.stringify({ question }) }));
    }
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

Promise.all([api("/health"), refreshSources(), refreshStatus(), loadSettings(), loadConversation()])
  .then(([health]) => {
    document.querySelector("#model-status").textContent = health.model_available
      ? `本地模型：${health.models.answer.name}`
      : "本地模型不可用";
  })
  .catch((error) => { document.querySelector("#model-status").textContent = "本地服务不可用"; toast(error.message, true); });

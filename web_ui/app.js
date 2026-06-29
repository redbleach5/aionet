// Aionet Web-UI — vanilla JS чат-клиент.
// Подключается к scripts/web_ui.py (HTTP :8080) + avatar_bridge (WS :8765).

const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const healthStatus = document.getElementById("health-status");
const sessionIdEl = document.getElementById("session-id");
const lastMetaEl = document.getElementById("last-meta");
const avatarEl = document.getElementById("avatar");
const mouthEl = document.getElementById("mouth");
const wsDot = document.getElementById("ws-dot");
const avatarState = document.getElementById("avatar-state");
const emotionDisplay = document.getElementById("emotion-display");

let sessionId = null;
let busy = false;
let ws = null;

// =============================================================================
// Health check
// =============================================================================
async function checkHealth() {
  try {
    const resp = await fetch("/api/health");
    const data = await resp.json();
    healthStatus.textContent = "online";
    healthStatus.className = "status ok";
    inputEl.disabled = false;
    sendBtn.disabled = false;
    inputEl.focus();
    // Подключаемся к avatar WS
    const wsUrl = data.ws_endpoint;
    connectWS(wsUrl);
  } catch (e) {
    healthStatus.textContent = "offline";
    healthStatus.className = "status err";
    console.error("health check failed:", e);
  }
}

// =============================================================================
// WebSocket — для аватара
// =============================================================================
function connectWS(url) {
  wsDot.className = "dot connecting";
  avatarState.textContent = "connecting...";
  try {
    ws = new WebSocket(url);
    ws.onopen = () => {
      wsDot.className = "dot online";
      avatarState.textContent = "online";
    };
    ws.onclose = () => {
      wsDot.className = "dot offline";
      avatarState.textContent = "offline";
      // retry через 3с
      setTimeout(() => connectWS(url), 3000);
    };
    ws.onerror = () => {
      wsDot.className = "dot offline";
      avatarState.textContent = "error";
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        handleAvatarCommand(msg);
      } catch { /* ignore */ }
    };
  } catch (e) {
    wsDot.className = "dot offline";
    avatarState.textContent = "n/a";
  }
}

function handleAvatarCommand(msg) {
  if (msg.type !== "command") return;
  if (msg.action === "speak") {
    avatarEl.classList.add("speaking");
    mouthEl.classList.add("speaking");
    avatarState.textContent = "speaking";
    // Анимация ~65ms на символ
    const dur = Math.min(12000, Math.max(1500, (msg.text || "").length * 65));
    setTimeout(() => {
      avatarEl.classList.remove("speaking");
      mouthEl.classList.remove("speaking");
      avatarState.textContent = "online";
    }, dur);
  } else if (msg.action === "emote") {
    const emotion = msg.emotion || "neutral";
    emotionDisplay.textContent = emotion;
    // Меняем цвет аватара
    const colors = {
      neutral: "#7c5cff",
      happy: "#4ade80",
      sad: "#60a5fa",
      thinking: "#fbbf24",
      angry: "#f87171",
    };
    avatarEl.style.background = `linear-gradient(135deg, ${colors[emotion] || colors.neutral} 0%, #5b3fd6 100%)`;
  } else if (msg.action === "idle") {
    avatarEl.classList.remove("speaking", "thinking");
    mouthEl.classList.remove("speaking");
    avatarState.textContent = "idle";
    emotionDisplay.textContent = "нейтральное";
  }
}

// =============================================================================
// Chat
// =============================================================================
function appendMessage(role, text, isError = false) {
  const div = document.createElement("div");
  div.className = `msg ${role}${isError ? " error" : ""}`;
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = role === "user" ? "Вы" : "Aionet";
  div.appendChild(meta);
  div.appendChild(document.createTextNode(text));
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function appendToolTrace(tools) {
  if (!tools || tools.length === 0) return;
  for (const t of tools) {
    const div = document.createElement("div");
    div.className = "tool-trace";
    const ok = t.ok;
    div.innerHTML = `
      <span class="${ok ? "ok" : "err"}">${ok ? "✓" : "✗"} ${t.tool_name}</span>
      <span style="color: var(--fg-muted)"> (${t.duration_ms}мс)</span>
      <pre>args: ${escapeHtml(t.arguments || "")}\nres:  ${escapeHtml((t.result || "").slice(0, 300))}</pre>
    `;
    messagesEl.appendChild(div);
  }
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function showTyping() {
  const div = document.createElement("div");
  div.className = "typing-indicator";
  div.id = "typing";
  div.innerHTML = "<span></span><span></span><span></span>";
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function hideTyping() {
  const el = document.getElementById("typing");
  if (el) el.remove();
}

async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || busy) return;

  inputEl.value = "";
  inputEl.style.height = "auto";
  busy = true;
  sendBtn.disabled = true;
  avatarEl.classList.add("thinking");
  avatarState.textContent = "thinking...";

  appendMessage("user", text);
  showTyping();

  const t0 = Date.now();
  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        session_id: sessionId,
      }),
    });
    const data = await resp.json();
    hideTyping();

    if (!data.ok) {
      appendMessage("assistant", `Ошибка: ${data.error}`, true);
      if (data.traceback) {
        console.error(data.traceback);
      }
    } else {
      sessionId = data.session_id;
      sessionIdEl.textContent = sessionId.slice(0, 16) + "...";
      appendMessage("assistant", data.final_text || "(пустой ответ)");
      appendToolTrace(data.tool_calls);
      const dt = data.duration_ms || (Date.now() - t0);
      const tokens = data.tokens_used || 0;
      lastMetaEl.textContent = `${dt}мс · ${tokens} токенов`;
    }
  } catch (e) {
    hideTyping();
    appendMessage("assistant", `Сетевая ошибка: ${e.message}`, true);
  } finally {
    busy = false;
    sendBtn.disabled = false;
    avatarEl.classList.remove("thinking");
    avatarState.textContent = ws && ws.readyState === 1 ? "online" : "idle";
    inputEl.focus();
  }
}

// =============================================================================
// Events
// =============================================================================
sendBtn.addEventListener("click", sendMessage);

inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

// Auto-resize textarea
inputEl.addEventListener("input", () => {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(160, inputEl.scrollHeight) + "px";
});

// =============================================================================
// Init
// =============================================================================
checkHealth();
// Периодический health-check каждые 30с
setInterval(checkHealth, 30000);

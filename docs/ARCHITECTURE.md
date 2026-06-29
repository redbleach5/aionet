# Architecture — Aionet

Каноническая схема компонентов, потоков данных и контрактов.
Если в других документах что-то расходится с этим — **этот файл прав**.

---

## 1. Высокоуровневая диаграмма

```
┌──────────────────────────────────────────────────────────────────────┐
│                    Tauri Desktop App (Rust + React)                   │
│  ┌──────────────────┐   ┌─────────────────────────────────────────┐  │
│  │  React/Three.js  │   │  Rust backend                            │  │
│  │  - Chat history  │   │  - ZMQ REQ → agent_core (:5550)          │  │
│  │  - 3D Avatar     │◄──┤  - IPC: send_message, health_check       │  │
│  │  - Tool traces   │   │  - WS client → avatar_bridge (:8765)      │  │
│  └────────┬─────────┘   └────────────────┬─────────────────────────┘  │
└───────────┼──────────────────────────────┼───────────────────────────┘
            │ WebSocket                    │ ZMQ REQ
            │ ws://127.0.0.1:8765          │ tcp://127.0.0.1:5550
            ▼                              ▼
┌────────────────────────────┐   ┌─────────────────────────────────────────┐
│   avatar_bridge (Python)   │   │         agent_core (Python)             │
│                            │   │                                         │
│  ZMQ SUB :5554 (cmd in)    │   │  Цикл plan→act→respond:                  │
│  ZMQ PUB :5555 (evt out)   │   │  1. classify_complexity(user_text)       │
│  WS :8765 (Tauri clients)  │   │  2. retrieve(memory) → context           │
│                            │   │  3. build prompt (static + dynamic)      │
│  Ring buffer: 256 msgs     │   │  4. LLM call ↔ tool calls (max_iter)     │
│                            │   │  5. LoopDetector.check() after each tool │
│                            │   │  6. store(memory, Q+A)                   │
│                            │   │  7. avatar_speak(final_text)             │
└────────────────────────────┘   └──┬──────────┬──────────┬──────────┬────┘
                                    │ REQ      │ REQ      │ REQ      │ PUB
                                    ▼          ▼          ▼          ▼
                          ┌──────────────┐ ┌─────────┐ ┌────────┐ ┌────────┐
                          │ llm_engine   │ │ memory  │ │ tools  │ │ avatar │
                          │ :5551        │ │ :5552   │ │ :5553  │ │ bridge │
                          │              │ │         │ │        │ │ (PUB)  │
                          │ OllamaClient │ │ FAISS   │ │ MCPBroker│ │       │
                          │ + retry      │ │ +SQLite │ │ +retry │ │        │
                          │ + static/dyn │ │ +GC     │ │        │ │        │
                          └──────┬───────┘ └─────────┘ └───┬────┘ └────────┘
                                 │ HTTP                      │ stdio MCP
                                 ▼                           ▼
                          ┌──────────────┐     ┌─────────────────────────┐
                          │ Ollama       │     │  4 MCP-сервера:          │
                          │ :11434       │     │  ┌────────┐ ┌────────┐   │
                          │ (или mock)   │     │  │ shell  │ │  fs    │   │
                          │              │     │  │(Docker)│ │(Docker)│   │
                          └──────────────┘     │  └────────┘ └────────┘   │
                                               │  ┌────────┐ ┌────────┐   │
                                               │  │ winget │ │browser │   │
                                               │  │ (host) │ │ (host) │   │
                                               │  └────────┘ └────────┘   │
                                               └─────────────────────────┘
```

---

## 2. Сервисы и их ответственность

### 2.1 `agent_core` (Python, :5550 REP)
**Оркестратор** — единственная точка входа для UI.

| Компонент | Файл | Назначение |
|---|---|---|
| `AgentRuntime` | `agent_core/agent.py` | Цикл plan→act→respond,协调 memory/llm/tools/avatar |
| `LoopDetector` | `agent_core/loop_detector.py` | 3-сигнальный детектор зацикливания |
| `TaskComplexityClassifier` | `agent_core/task_complexity.py` | 5-уровневая классификация запроса |
| `SystemPromptBuilder` | `agent_core/prompt_builder.py` | Static prefix + dynamic suffix split |
| `make_sandbox()` | `agent_core/security.py` | Фабрика песочниц (docker/mxc/none) |

**Цикл обработки AgentRequest:**
```
1. classify_complexity(user_text) → max_iter, max_tokens, tools_enabled
2. retrieve(memory, session_id, user_text) → memory_context
3. build prompt: static_prefix + dynamic_suffix(complexity, memory, ...)
4. for iteration in range(max_iter):
     a. llm_call(messages, tools, static_prefix, dynamic_suffix)
     b. if no tool_calls → final_text, break
     c. for each tool_call:
        - call_tool(tools_broker, tool_name, args)
        - add tool result to messages
        - loop_detector.add(step)
        - if loop_detector.check() → break with diagnostic
5. store(memory, session_id, "User: ... Assistant: ...")
6. avatar_speak(final_text) via ZMQ PUB
7. return AgentResponse(final_text, tool_calls_trace, tokens_used)
```

### 2.2 `llm_engine` (Python, :5551 REP)
**LLM-инференс** — обёртка над Ollama API.

| Компонент | Файл | Назначение |
|---|---|---|
| `OllamaClient` | `llm_engine/ollama_client.py` | HTTP-клиент к Ollama `/api/chat` |
| `LLMEngineService` | `llm_engine/ollama_client.py` | ZMQ REP-обработчик LLMCall → LLMResult |

**Ключевые особенности:**
- Приоритетный список моделей: `mock:test-7b` → `aion-plan-1.0` → `mistral:7b` → `llama3.1:8b`
- Retry через `tenacity` (3 попытки, exponential backoff)
- **Static/dynamic prompt split**: если `LLMCall.static_prefix` задан — отправляет
  в Ollama 2 system-сообщения `[static, dynamic]` для KV-cache. Иначе — legacy
  режим (одно `system_prompt`).
- Поддержка tool_calls в формате Ollama

### 2.3 `memory` (Python, :5552 REP)
**Долговременная память** — FAISS + SQLite.

| Компонент | Файл | Назначение |
|---|---|---|
| `FaissMemoryStore` | `memory/faiss_memory.py` | Реализация `MemoryStore` ABC |

**Хранилище:**
- **FAISS** `IndexFlatIP` — векторы (384-dim, L2-нормализованные)
- **SQLite** — метаданные: `id, session_id, text, importance, created_at, last_accessed, access_count, metadata_json, faiss_idx, deleted, deleted_at`

**Многоканальное ранжирование:**
```
score = w_sem × semantic + w_rec × recency + w_freq × frequency

semantic = cosine_similarity(FAISS search)
recency  = exp(-days_since_access / half_life_days)
frequency = access_count / max_access_count_in_candidates
```

**Кривая забывания Эббингауза:**
```
effective_importance = importance₀ × exp(-age_days / half_life_days)
if effective_importance < min_importance → soft-delete
```

**Инкрементальный GC:**
1. `forget()` → soft-delete (помечает `deleted=1`, быстро, не трогает FAISS)
2. Если `soft_deleted_count >= gc_rebuild_threshold` (default 1000) →
   физически удалить из SQLite + перестроить FAISS (дорого, но редко)

**Изоляция сессий:**
SQL pre-filter: `WHERE faiss_idx IN (...) AND deleted=0 AND session_id=?`
— записи из другой сессии физически не попадают в кандидаты.

### 2.4 `tools` (Python, :5553 REP)
**Брокер MCP-инструментов** — ZMQ ↔ stdio-MCP мост.

| Компонент | Файл | Назначение |
|---|---|---|
| `MCPBroker` | `tools/broker.py` | Агрегатор MCP-серверов, retry, timeout |
| `StdioMCPClient` | `tools/broker.py` | Один stdio-MCP-сервер с lazy init |
| `BaseToolServer` | `tools/_base.py` | Базовый класс на FastMCP |
| `ShellServer` | `tools/shell_server.py` | shell_run через Sandbox |
| `FsServer` | `tools/fs_server.py` | fs_read/write/list/stat с allowed_roots |
| `WingetServer` | `tools/winget_server.py` | winget search/install/list |
| `BrowserServer` | `tools/browser_server.py` | browser_navigate/screenshot (Playwright) |

**Особенности:**
- Lazy init: MCP-сессия создаётся при первом `call_tool`/`list_tools`
- Event-loop в отдельном daemon-потоке
- Broken session помечается, следующий вызов пересоздаёт
- Retry: 3 попытки с exponential backoff (1с, 2с, 4с), только инфраструктурные ошибки

### 2.5 `avatar_bridge` (Python, :8765 WS + :5554 SUB + :5555 PUB)
**Мост ZMQ ↔ WebSocket** — для Tauri/Three.js фронта.

| Компонент | Файл | Назначение |
|---|---|---|
| `AvatarBridge` | `avatar/ws_bridge.py` | ZMQ SUB→WS, WS→ZMQ PUB |
| `HomunculusMCPBackend` | `avatar/homunculus_backend.py` | Stub для Desktop Homunculus |
| `_WSBridgeAdapter` | `avatar/homunculus_backend.py` | Адаптер AvatarBridge → AvatarBackend |

**Поток:**
```
agent_core ──ZMQ PUB :5554──► avatar_bridge ──WS──► Tauri/Three.js
Tauri/Three.js ──WS──► avatar_bridge ──ZMQ PUB :5555──► agent_core
```

**Ring buffer:** 256 сообщений — команды буферизуются если WS-клиент не подключён,
отправляются после reconnect (последние 16 при подключении).

### 2.6 Tauri Desktop (Rust + React)
**Десктоп-приложение** — UI + IPC к agent_core.

| Компонент | Файл | Назначение |
|---|---|---|
| `lib.rs` | `rust/src/lib.rs` | Tauri setup, команды `send_message`, `health_check` |
| `ipc.rs` | `rust/src/ipc.rs` | ZMQ REQ-клиент к agent_core (JSON-конверт) |
| `App.tsx` | `rust/frontend/src/App.tsx` | Chat + Three.js avatar, WS-клиент к :8765 |

**Tauri↔agent_core:** JSON-конверт (не protobuf — для простоты фронта).
Контракт: `{"trace_id", "span_id", "source", "target", "type", "payload"}`.

---

## 3. Контракты данных (Protobuf)

Все ZMQ-сообщения — `Envelope` (см. `proto/messages.proto`):

```protobuf
message Envelope {
  string trace_id   = 1;  // сквозная трассировка
  string span_id    = 2;
  string source     = 3;  // "agent_core", "llm_engine", ...
  string target     = 4;
  uint64 timestamp  = 5;
  bytes  payload    = 6;  // сериализованное тело одного из сообщений ниже
  PayloadType type  = 7;
}
```

Типы payload:
- `AgentRequest` / `AgentResponse` — UI ↔ agent_core
- `LLMCall` / `LLMResult` — agent_core ↔ llm_engine
- `MemoryOp` / `MemoryResult` — agent_core ↔ memory
- `ToolCallMessage` / `ToolResultMessage` — agent_core ↔ tools
- `AvatarCommand` / `AvatarEvent` — agent_core ↔ avatar_bridge
- `ErrorPayload` — универсальная ошибка

**Sprint 1 расширение LLMCall:**
```protobuf
message LLMCall {
  string model         = 1;
  string system_prompt = 2;   // legacy
  repeated ChatMessage messages = 3;
  float  temperature   = 4;
  uint32 max_tokens    = 5;
  repeated ToolSchema  tools = 6;
  bool   stream        = 7;
  string static_prefix = 8;   // ★ Sprint 1: KV-cache friendly
  string dynamic_suffix = 9;  // ★ Sprint 1: меняется между вызовами
}
```

---

## 4. Поток запроса (end-to-end)

Пример: пользователь отправляет "перечисли файлы в директории".

```
1. Tauri (Rust)
   │ IPC: send_message("перечисли файлы в директории")
   │ ZMQ REQ → agent_core :5550
   ▼
2. agent_core (AgentRuntime._run)
   │ classify_complexity("перечисли файлы...") → MODERATE
   │   → max_iter=3, max_tokens=2048, tools_enabled=True
   │ retrieve(memory, session_id, "перечисли файлы") → []
   │ build prompt: static_prefix + dynamic_suffix(complexity=moderate)
   │
   │ iteration 0:
   │   llm_call(messages, tools, static_prefix, dynamic_suffix)
   │     → llm_engine :5551 → Ollama /api/chat
   │     ← LLMResult{tool_calls: [filesystem/run{command:"fs_list"}]}
   │   call_tool("filesystem/run", {command:"fs_list", args:["."]})
   │     → tools :5553 → MCPBroker → fs_server (stdio MCP)
   │     ← ToolResult{ok:True, output:"...files..."}
   │   loop_detector.add(step) → check() → None (no loop)
   │
   │ iteration 1:
   │   llm_call(messages + tool_result, ...)
   │     ← LLMResult{content: "Вот файлы: ..."}
   │   no tool_calls → final_text, break
   │
   │ store(memory, session_id, "User: ... Assistant: Вот файлы: ...")
   │ avatar_speak("Вот файлы: ...") via ZMQ PUB :5554
   │
   │ return AgentResponse{final_text, tool_calls_trace, tokens_used}
   ▼
3. Tauri (Rust)
   │ ZMQ REP ← AgentResponse
   │ IPC: emit("agent-response", resp)
   ▼
4. React (App.tsx)
   │ setMessages([...messages, {role:"assistant", text:resp.final_text}])
   │
   │ Параллельно: avatar_bridge получил AvatarCommand(SPEAK)
   │   → WS → Three.js → анимация губ
```

---

## 5. Точки расширения

Все заменяемые подсистемы — ABC в `python/common/interfaces.py`:

| ABC | Методы | Реализации |
|---|---|---|
| `LLMClient` | `list_available_models()`, `call(...)` | `OllamaClient` |
| `MemoryStore` | `store()`, `retrieve()`, `forget()`, `stats()` | `FaissMemoryStore` |
| `Sandbox` | `run()`, `apply_policy()`, `policy` | `DockerSandbox`, `MXCSandbox`, `NoneSandbox` |
| `ToolRunner` | `list_tools()`, `call()`, `shutdown()` | `MCPBroker` |
| `AvatarBackend` | `speak()`, `emote()`, `gesture()`, `look_at()`, `idle()` | `_WSBridgeAdapter`, `HomunculusMCPBackend` |

**Dataclass'ы:**
- `ChatMessage`, `ToolSchema`, `ToolCall`, `LLMResult`
- `MemoryRecord`
- `SandboxResult`, `SandboxPolicy` (13 полей)
- `ToolExecutionResult`

Гайд по созданию новых реализаций — в [`docs/DEPLOYMENT.md`](DEPLOYMENT.md) § 5.

---

## 6. Безопасность

### Docker-песочница (для shell, fs)
- seccomp whitelist (`docker/seccomp-profile.json`)
- AppArmor (`docker/apparmor-profile` — deny network, deny системных путей)
- read-only rootfs + tmpfs /tmp
- непривилегированный user (UID 1000)
- `--network none` по умолчанию
- memory/cpu/pid limits

### SandboxPolicy (для тонкой настройки)
Через `Sandbox.apply_policy(SandboxPolicy)` можно задать:
- `seccomp_profile`, `apparmor_profile`, `yaml_policy` (для MXC/OpenShell)
- `network: list[str]` — список разрешённых хостов
- `fs_read: list[str]`, `fs_write: list[str]` — доп. mounts
- `syscalls_allow/deny`, `capabilities`
- `env: dict`, `memory_limit_mb`, `cpu_quota_percent`, `pid_limit`

### Browser (без песочницы)
Playwright требует chromium (~300MB) + сеть — несовместимо с `--network none`.
Запускается на хосте. Альтернативы: отдельный Docker-образ с сетью, browserless.io.

### WinGet (без песочницы)
Требует системных привилегий для установки пакетов. Запускается на хосте.

---

## 7. Логирование и трассировка

Каждое ZMQ-сообщение несёт `trace_id` и `span_id` в `Envelope`. Логгер
выводит их в каждую строку:

```
2026-06-29 14:00:53 | INFO | agent_core.agent | tid=abc123 sid=def456 | AgentRequest session=xyz
```

Контекст трассировки пробрасывается через `contextvars` — все логи внутри
обработки одного запроса имеют один `trace_id`.

Уровни: `DEBUG | INFO | WARNING | ERROR` (через `config.toml: [system].log_level`).

---

## 8. Масштабирование

### Текущие пределы (mock-ollama, измерено)
- Memory STORE: 879 ops/sec, p95=1.3ms
- Memory RETRIEVE: p95=1.2ms
- LLM Engine: 19 calls/sec (3 параллельных worker)
- Agent E2E: p95=58ms

### Bottlenecks
1. **LLM-инференс** — реальная Ollama с 7B моделью даёт 5-50× медленнее
2. **FAISS IndexFlatIP** — O(N) search, для миллионов записей нужен IVF/HNSW
3. **MCPBroker** — один процесс, но можно запустить несколько реплик
4. **ZMQ REQ/REP** — один запрос за раз, для параллельных нужен DEALER/ROUTER

### Пути масштабирования
- FAISS → `IndexIVFFlat` или `IndexHNSWFlat` (аппроксимация, быстрее)
- FAISS → GPU (`faiss.index_cpu_to_gpu`)
- Memory → Chroma/Qdrant/Milvus (через `MemoryStore` ABC)
- LLM → несколько реплик `llm_engine` на разных портах + пул клиентов
- ZMQ → ROUTER/DEALER для асинхронных запросов

Подробности — в [`docs/DEPLOYMENT.md`](DEPLOYMENT.md) § 7.

# Aionet — Local AI Agent

Локальный AI-агент с микросервисной архитектурой на ZeroMQ + Protobuf,
Tauri-десктопом и HTML5/Three.js-аватаром. Все «недоступные» компоненты
целевого стека (MXC, Atomic Agent, Aion 1.0, SuperLocalMemory, Desktop
Homunculus) заменены на ближайшие open-source аналоги **за абстрактными
интерфейсами** — возврат к оригиналам не требует переписывания системы.

Полный список допущений и точек замены — в [`ASSUMPTIONS.md`](./ASSUMPTIONS.md).

---

## Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│                    Tauri Desktop App (Rust)                       │
│  ┌──────────────────┐   ┌─────────────────────────────────────┐  │
│  │  React/Three.js  │   │  Rust backend (ZMQ REQ → agent_core)│  │
│  │  Avatar + Chat   │◄──┤  IPC commands: send_message, health │  │
│  └────────┬─────────┘   └─────────────────────────────────────┘  │
└───────────┼──────────────────────────────────────────────────────┘
            │ WS (ws://127.0.0.1:8765)
            ▼
┌─────────────────────────────────────────────────────────────────┐
│                avatar_bridge (python/avatar)                     │
│   ZMQ SUB (avatar_cmd) ←→ WebSocket → Tauri/Three.js             │
│   ZMQ PUB (avatar_evt) ←← WebSocket ← Tauri/Three.js             │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                       agent_core (Python)                        │
│   Цикл: retrieve(memory) → llm_call → tool_call → llm_call →    │
│         store(memory) → avatar_speak                             │
└───────┬───────────────┬───────────────┬───────────────┬─────────┘
        │ REQ           │ REQ           │ REQ           │ PUB
        ▼               ▼               ▼               ▼
   ┌─────────┐    ┌───────────┐   ┌───────────┐   ┌───────────┐
   │ memory  │    │ llm_engine│   │   tools   │   │  avatar   │
   │ (FAISS) │    │ (Ollama)  │   │ (MCP bkr) │   │  bridge   │
   └─────────┘    └───────────┘   └─────┬─────┘   └───────────┘
                                       │ stdio MCP
                   ┌───────────────────┼───────────────────┐
                   ▼                   ▼                   ▼
              shell_server       fs_server         winget/browser
              (in Docker         (in Docker         (host or Docker)
               sandbox)           sandbox)
```

Все стрелки — ZeroMQ (REQ/REP для синхронных, PUB/SUB для аватара) с Protobuf-
конвертами. Tauri↔avatar — WebSocket (Tauri-фронтенд не может в ZMQ напрямую).

---

## Структура проекта

```
local-ai-agent/
├── config.toml                 # единый конфиг всех сервисов
├── ASSUMPTIONS.md              # документированные замены компонентов
├── proto/
│   └── messages.proto          # каноническая protobuf-схема
├── python/
│   ├── common/                 # ZMQ-транспорт, protobuf-биндинги, config, logging
│   │   ├── interfaces.py       # ★ ABC для всех заменяемых подсистем
│   │   ├── zmq_transport.py    # ZMQServer / ZMQClient / ZMQPublisher / ZMQSubscriber
│   │   ├── proto.py            # ленивая загрузка messages_pb2
│   │   ├── config.py           # TOML-конфиг
│   │   └── logging.py          # логгер с trace_id-контекстом
│   ├── agent_core/
│   │   ├── agent.py            # цикл plan→act→respond
│   │   ├── security.py         # Sandbox: DockerSandbox / MXCSandbox / NoneSandbox
│   │   └── __main__.py         # точка входа ZMQ REP-сервера
│   ├── llm_engine/
│   │   ├── ollama_client.py    # OllamaClient (aion/mistral/llama)
│   │   └── __main__.py
│   ├── memory/
│   │   ├── faiss_memory.py     # FaissMemoryStore + Ebbinghaus forgetting
│   │   └── __main__.py
│   ├── tools/
│   │   ├── _base.py            # BaseToolServer (stdio-MCP)
│   │   ├── shell_server.py     # MCP-сервер shell
│   │   ├── fs_server.py        # MCP-сервер filesystem
│   │   ├── winget_server.py    # MCP-сервер winget (stub на Linux)
│   │   ├── browser_server.py   # MCP-сервер browser (Playwright)
│   │   └── broker.py           # MCPBroker — ZeroMQ ↔ stdio-MCP
│   ├── avatar/
│   │   └── ws_bridge.py        # ZeroMQ PUB/SUB ↔ WebSocket
│   └── requirements.txt
├── rust/                       # Tauri-десктоп
│   ├── Cargo.toml
│   ├── tauri.conf.json
│   ├── capabilities/default.json
│   ├── src/
│   │   ├── main.rs
│   │   ├── lib.rs              # Tauri setup + команда send_message
│   │   ├── ipc.rs              # ZMQ REQ-клиент к agent_core
│   │   └── proto.rs            # константы payload-type
│   └── frontend/               # React + Three.js
│       ├── package.json
│       ├── vite.config.ts
│       └── src/
│           ├── main.tsx
│           ├── App.tsx          # чат + аватар
│           └── styles.css
├── docker/
│   ├── Dockerfile.toolbox       # образ песочницы инструментов
│   ├── seccomp-profile.json     # seccomp-whitelist
│   └── apparmor-profile         # AppArmor-профиль
├── scripts/
│   ├── gen_proto.sh             # регенерация messages_pb2.py
│   ├── build_sandbox.sh         # сборка Docker-образа + AppArmor
│   └── start_all.sh             # запуск всех ZMQ-воркеров
└── tests/
    └── integration_test.py      # end-to-end smoke-тест
```

---

## Быстрый старт

### 0. Зависимости

```bash
# Python 3.11+
python -m venv .venv && source .venv/bin/activate
pip install -r python/requirements.txt

# Ollama + модель
ollama serve &
ollama pull mistral:7b-instruct   # или llama3.1:8b-instruct

# Docker (опционально, для песочницы инструментов)
docker --version

# Tauri (только для сборки десктоп-приложения)
# https://tauri.app/v2/guides/getting-started/prerequisites
cargo install tauri-cli --version "^2.0"
```

### 1. Сгенерировать protobuf-биндинги

```bash
./scripts/gen_proto.sh
```

> Если пропустить — `common/proto.py` сгенерирует их на лету при первом
> импорте (медленнее при старте).

### 2. Собрать Docker-образ песочницы (опционально)

```bash
./scripts/build_sandbox.sh
```

### 3. Запустить все ZeroMQ-воркеры

```bash
./scripts/start_all.sh
```

### 4. Запустить десктоп-приложение

```bash
cd rust
npm --prefix frontend install
cargo tauri dev
```

Для сборки установщика под Windows:

```bash
cd rust
cargo tauri build  # → rust/target/release/bundle/{msi,nsis}/
```

### 5. Интеграционный тест (без UI)

```bash
python tests/integration_test.py
```

---

## Ключевые проектные решения

### Слабая связность
Все сервисы общаются **только** через ZeroMQ + Protobuf. Ни один модуль не
импортирует другой напрямую (кроме `common` и точек расширения). Замена
любого сервиса на другую реализацию (или на другой язык) не требует
переписывания остальных.

### Точки расширения (`python/common/interfaces.py`)
- `LLMClient`     → `OllamaClient` (можно добавить `LlamaCppClient`, `OpenAICompatibleClient`)
- `MemoryStore`   → `FaissMemoryStore` (можно добавить `ChromaStore`)
- `Sandbox`       → `DockerSandbox` / `MXCSandbox` / `NoneSandbox`
- `ToolRunner`    → `MCPBroker` (можно добавить прямой `LangChainToolsRunner`)
- `AvatarBackend` → `WebSocketBridge` (можно добавить `HomunculusMCPBackend`)

### Безопасность
- Все tool-серверы с `sandbox = true` запускаются в Docker-контейнере с:
  - seccomp-профилем (whitelist syscall'ов)
  - AppArmor-профилем (deny network, deny системных путей на запись)
  - read-only rootfs + tmpfs /tmp
  - непривилегированный user (UID 1000)
  - memory/cpu limits
  - `--network none` по умолчанию
- `winget` запускается на хосте (требует системных привилегий).

### Память
- Эмбеддинги: `sentence-transformers/all-MiniLM-L6-v2` (384-dim, локально)
- Векторный индекс: FAISS `IndexFlatIP` (косинусное сходство через L2-норму)
- Метаданные: SQLite (id, session_id, importance, created_at, last_accessed, access_count, faiss_idx)
- Каналы ранжирования: `semantic` (FAISS), `recency` (exp(-days/half_life)), `frequency` (count/max_count)
- Итоговый score = взвешенная сумма каналов (веса в `config.memory.channel_weights`)
- Забывание: `importance(t) = importance_0 * exp(-t/half_life)`; при
  `importance < min` запись удаляется (GC каждые 60 минут)

### Логирование и трассировка
Каждое ZeroMQ-сообщение несёт `trace_id` и `span_id` в Envelope. Логгер
выводит их в каждую строку — можно отследить путь запроса через все
сервисы. Контекст трассировки пробрасывается через `contextvars`.

---

## Что НЕ вошло в текущую итерацию

1. **Потоковый стриминг LLM** — `LLMCall.stream` зарезервирован, но
   реализация требует перехода с REQ/REP на DEALER/ROUTER + chunked-сообщения.
2. **Multi-modal LLM** — только текст.
3. **Реальный TTS** — конфиг `avatar.tts_backend` есть, но интеграция
   с Coqui/Silero оставлена как TODO.
4. **Persisted sessions** — `session_id` передаётся, но отдельной
   сессии-менеджмент-сервис пока нет; сессия — это просто ключ в memory.
5. **Rust-side protobuf** — используется JSON-конверт; для бинарного
   протокола нужно подключить `prost-build` в `build.rs`.

Все эти пункты не блокируют базовый сценарий и могут быть добавлены
итеративно.

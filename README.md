# Aionet — Local AI Agent

[![status](https://img.shields.io/badge/status-active%20development-green)]()
[![tests](https://img.shields.io/badge/tests-42%2F42%20pass-brightgreen)]()
[![sprint](https://img.shields.io/badge/sprint-1%20done%2C%202%20planned-blue)]()

Локальный AI-агент с микросервисной архитектурой: ZeroMQ + Protobuf для
транспорта, Tauri + React/Three.js для десктопа, MCP-инструменты в
Docker-песочнице, FAISS-память с кривой забывания Эббингауза.

> **Статус:** Sprint 1 завершён, все 42 теста проходят. Sprint 2 (эмоции,
> личность, адаптивность к железу) запланирован — см. [`docs/SPRINT-2-PLAN.md`](docs/SPRINT-2-PLAN.md).

---

## 📋 Содержание

- [Возможности](#-возможности)
- [Быстрый старт](#-быстрый-старт)
- [Архитектура](#-архитектура)
- [Структура проекта](#-структура-проекта)
- [Конфигурация](#-конфигурация)
- [Тестирование](#-тестирование)
- [Документация](#-документация)
- [Дорожная карта](#-дорожная-карта)

---

## ✨ Возможности

### Ядро агента (Sprint 1)
- **Микросервисная архитектура** — 5 ZMQ-сервисов (agent_core, llm_engine, memory, tools, avatar) общаются через Protobuf-конверты
- **Цикл plan→act→respond** с поддержкой tool_calls (стандартный MCP-формат)
- **LoopDetector** — 3-сигнальный детектор зацикливания (pattern + empty + semantic через embeddings), с LLM-error filter (timeout ≠ цикл)
- **TaskComplexityClassifier** — 5-уровневая классификация запроса (trivial → research), определяет max_iter/max_tokens
- **Static/dynamic prompt split** — KV-cache friendly промпт для Ollama (ускорение 3-5× для повторных вызовов)

### Память
- **FAISS + SQLite** — векторный индекс + метаданные
- **Кривая забывания Эббингауза** — `importance(t) = importance₀ × exp(-t/half_life)`
- **Многоканальный ретрив** — semantic (FAISS) + recency (exp decay) + frequency (access_count), взвешенная сумма
- **Инкрементальный GC** — soft-delete (быстро) + физическая перестройка FAISS только при пороге (default 1000)
- **Архитектурная изоляция сессий** — SQL pre-filter по session_id, записи физически не попадают в чужие результаты

### Инструменты (MCP)
- **4 stdio-MCP-сервера**: shell (Docker-песочница), filesystem (проверка allowed_roots), winget (host), browser (Playwright)
- **MCPBroker** — ZMQ↔stdio мост, ленивая инициализация сессий, retry с exponential backoff
- **Docker-песочница** — seccomp whitelist + AppArmor + read-only rootfs + no-network + непривилегированный user
- **SandboxPolicy** — декларативные политики (готовы для MXC/OpenShell когда они выйдут)

### Безопасность
- Все tool-серверы с `sandbox = true` изолированы в Docker
- `apply_policy(SandboxPolicy)` — 13 параметров (seccomp, apparmor, network, fs_read/write, syscalls, caps, env, mem/cpu/pid limits)
- Browser запускается на хосте (требует Playwright + сеть — несовместимо с no-network песочницей)

### Десктоп UI
- **Tauri 2.0** — Rust backend с ZMQ REQ-клиентом к agent_core
- **React + Three.js** — чат с историей + 3D-аватар (icosahedron-гомункул с глазами/ртом)
- **WebSocket-мост** — ZMQ PUB/SUB ↔ WS для аватара (Tauri-фронт не может в ZMQ напрямую)

---

## 🚀 Быстрый старт

### Вариант A: Тестовое окружение (mock-LLM, без Ollama)

Самый быстрый способ проверить, что всё работает:

**Linux / macOS:**
```bash
# 1. Установить Python-зависимости
pip install pyzmq protobuf grpcio-tools requests faiss-cpu tenacity \
            mcp websockets pydantic numpy

# 2. Сгенерировать protobuf-биндинги
./scripts/gen_proto.sh

# 3. Запустить mock-Ollama + все сервисы
bash scripts/start_bg.sh

# 4. Прогнать тесты
PYTHONPATH=python:proto/_gen python tests/test_integration.py   # 9/9
PYTHONPATH=python:proto/_gen python tests/test_sprint1.py        # 21/21
PYTHONPATH=python:proto/_gen python tests/test_memory_unit.py    # 12/12

# 5. Остановить сервисы
bash scripts/stop_bg.sh
```

**Windows (PowerShell):**
```powershell
# 1. Установить Python-зависимости
pip install pyzmq protobuf grpcio-tools requests faiss-cpu tenacity `
            mcp websockets pydantic numpy

# 2. Сгенерировать protobuf-биндинги
python -m grpc_tools.protoc -Iproto --python_out=proto/_gen proto/messages.proto

# 3. Запустить mock-Ollama + все сервисы
.\scripts\start_bg.ps1

# 4. Прогнать тесты
$env:PYTHONPATH = "python;proto\_gen"
python tests\test_integration.py    # 9/9
python tests\test_sprint1.py        # 21/21
python tests\test_memory_unit.py    # 12/12

# 5. Остановить сервисы
.\scripts\stop_bg.ps1
```

> **PowerShell Execution Policy:** если скрипт блокируется, выполните:
> ```powershell
> Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
> ```

Mock-Ollama (`scripts/mock_ollama.py`) — rule-based HTTP-сервер, имитирует
`/api/chat`, `/api/tags`, `/api/show`. Эмитит tool_calls на "list files" / "calc".

### Вариант B: Полное окружение (реальная Ollama)

```bash
# 1. Установить Ollama + модель
ollama serve &
ollama pull mistral:7b-instruct   # или llama3.1:8b-instruct

# 2. Python-зависимости (включая sentence-transformers — ~2GB)
pip install -r python/requirements.txt

# 3. Опционально: Docker-песочница
./scripts/build_sandbox.sh            # Linux/macOS

# 4. В config.toml убрать "mock:test-7b" из candidate_models
#    (или оставить — Ollama-клиент выберет первую доступную)

# 5. Запуск сервисов
bash scripts/start_bg.sh               # Linux/macOS
.\scripts\start_bg.ps1 -SkipMockOllama # Windows (реальная Ollama уже работает)

# 6. Десктоп-приложение (требует Rust + Node.js)
cd rust && npm --prefix frontend install && cargo tauri dev
```

### Проверка здоровья

```bash
# Linux/macOS
curl -s http://127.0.0.1:11434/api/tags | jq '.models[].name'  # Ollama
ss -tln | grep -E ":555[0-5]|:8765"                            # ZMQ + WS

# Windows (PowerShell)
(Invoke-RestMethod http://127.0.0.1:11434/api/tags).models.name  # Ollama
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 5550..5555,8765 }
```

Все 7 портов должны слушать: 11434 (Ollama), 5550-5555 (ZMQ), 8765 (WS).

---

## 🏗 Архитектура

Каноническая схема в [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
Краткая диаграмма:

```
Tauri (Rust+React) ──WS──► avatar_bridge ──ZMQ PUB──► agent_core
       │                        ▲                      │
       │ ZMQ REQ                │ ZMQ SUB              │ REQ
       ▼                        │                      ▼
   agent_core ─────REQ────► llm_engine ──HTTP──► Ollama
       │                                              (или mock_ollama)
       │ REQ
       ▼
    memory (FAISS+SQLite)
       │
       │ REQ
       ▼
    tools broker ──stdio MCP──► shell / fs / winget / browser
```

**Ключевой принцип:** все сервисы слабо связаны, общаются только через ZeroMQ
+ Protobuf. Замена любого компонента = новый класс-наследник ABC + строка в
`config.toml`. См. [`ASSUMPTIONS.md`](ASSUMPTIONS.md) — таблица всех замен.

---

## 📁 Структура проекта

```
local-ai-agent/
├── config.toml                  # Единый конфиг всех сервисов
├── proto/messages.proto         # Protobuf-схема (Envelope + все сообщения)
├── python/
│   ├── common/                  # ZMQ-транспорт, protobuf, config, logging, interfaces
│   │   ├── interfaces.py        # ★ ABC: LLMClient, MemoryStore, Sandbox, ToolRunner, AvatarBackend
│   │   ├── zmq_transport.py     # ZMQServer/Client/Publisher/Subscriber
│   │   ├── embedder.py          # HashEmbedder (fallback для тестов без torch)
│   │   └── ...
│   ├── agent_core/
│   │   ├── agent.py             # AgentRuntime — цикл plan→act→respond
│   │   ├── loop_detector.py     # ★ Sprint 1: 3-сигнальный детектор зацикливания
│   │   ├── task_complexity.py   # ★ Sprint 1: классификатор trivial→research
│   │   ├── prompt_builder.py    # ★ Sprint 1: static/dynamic prompt split
│   │   └── security.py          # DockerSandbox + MXCSandbox + SandboxPolicy
│   ├── llm_engine/              # OllamaClient с retry + static/dynamic prompt
│   ├── memory/                  # FaissMemoryStore + инкрементальный GC
│   ├── tools/                   # MCPBroker + 4 MCP-сервера (FastMCP)
│   └── avatar/                  # WebSocketBridge + HomunculusMCPBackend (stub)
├── rust/                        # Tauri 2.0 десктоп
│   ├── src/                     # Rust backend (ZMQ REQ → agent_core)
│   └── frontend/                # React + Three.js (чат + аватар)
├── docker/                      # Dockerfile.toolbox + seccomp + AppArmor
├── scripts/                     # gen_proto.sh, start_bg.sh/.ps1, stop_bg.sh/.ps1, mock_ollama.py, build_sandbox.sh
├── tests/                       # test_sprint1, test_memory_unit, test_integration, test_load
└── docs/                        # ARCHITECTURE, DEPLOYMENT, TESTING, CHANGELOG, ...
```

---

## ⚙️ Конфигурация

Единый `config.toml` — источник правды для всех сервисов. Ключевые секции:

| Секция | Назначение |
|---|---|
| `[system]` | log_level, log_file |
| `[zmq]` | порты 5550-5555 + 8765 (WS) |
| `[llm]` | candidate_models, system_prompt_static (KV-cache), temperature, max_tokens |
| `[agent.loop_detector]` | pattern_limit, empty_limit, semantic_threshold |
| `[memory]` | FAISS paths, channel_weights, forgetting params, gc_rebuild_threshold |
| `[tools.servers]` | 4 MCP-сервера с флагом sandbox |
| `[security]` | backend (docker/mxc/none), seccomp, apparmor, run_as_user |
| `[avatar]` | backend (html5_threejs/homunculus), WS port, TTS |

Подробности и примеры dev/prod конфигов — в [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

---

## 🧪 Тестирование

| Тест | Что проверяет | Кол-во | Статус |
|---|---|---|---|
| `test_sprint1.py` | LoopDetector, TaskComplexity, PromptBuilder | 21 | ✅ 21/21 |
| `test_memory_unit.py` | Ранжирование, забывание, GC, session isolation | 12 | ✅ 12/12 |
| `test_integration.py` | End-to-end pipeline (mock-LLM) | 9 | ✅ 9/9 |
| `test_load.py` | Нагрузочные метрики ZeroMQ+FAISS | — | ✅ ran OK |

**Базовые метрики** (mock-ollama, без реальной LLM):
- Memory STORE: **879 ops/sec**, p95=1.3ms
- Memory RETRIEVE: p95=1.2ms
- LLM Engine: 19 calls/sec (3 workers)
- Agent E2E: p95=58ms

Подробности — в [`docs/TESTING.md`](docs/TESTING.md).

---

## 📚 Документация

| Документ | Назначение |
|---|---|
| [README.md](README.md) | Этот файл — обзор + quick-start |
| [ASSUMPTIONS.md](ASSUMPTIONS.md) | Таблица всех замен целевого стека |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Каноническая схема компонентов и потоков данных |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Порты, порядок запуска, dev/prod конфиги, гайд по замене компонентов |
| [docs/TESTING.md](docs/TESTING.md) | Как тестировать, как интерпретировать метрики |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | История изменений по спринтам |
| [docs/SPRINT-2-PLAN.md](docs/SPRINT-2-PLAN.md) | План Sprint 2 (эмоции, личность, адаптивность) |
| [docs/LIA-V2-ANALYSIS.md](docs/LIA-V2-ANALYSIS.md) | Анализ Lia-v2 — что переняли, что нет |

---

## 🗺 Дорожная карта

### ✅ Sprint 1 — Завершён
- LoopDetector (3 сигнала + LLM-error filter)
- TaskComplexityClassifier (5 уровней, regex)
- Static/dynamic prompt split (KV-cache для Ollama)
- 42 теста (21 sprint1 + 12 memory + 9 integration)

### 🔄 Sprint 2 — Запланирован
- EmotionEngine (5-осевая rule-based, без LLM)
- Personality + DisagreementAssessor (5-уровневый спектр несогласия)
- CapabilityProfiler (tier micro/standard/plus/max)
- CognitivePlanner (adaptive calls/deliberate/selfCheck)

### 🔮 Sprint 3+ — Будущее
- Episodes + FactExtractor (изоляция чатов)
- EmotionalMemoryStore (decay 180 дней)
- VRM-аватар (@pixiv/three-vrm)
- RL sidecar (PPO + ONNX, обучаемый стиль)
- Streaming LLM (DEALER/ROUTER вместо REQ/REP)

---

## 📄 Лицензия

Приватный проект. © 2026

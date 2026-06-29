# Допущения и замены компонентов

Этот документ фиксирует все отклонения от целевого стека, описанного в ТЗ.
Каждая замена реализована **за абстрактным интерфейсом** в
`python/common/interfaces.py` — возврат к оригинальному компоненту не требует
переписывания системы, только нового класса-наследника + строки в `config.toml`.

---

## Таблица замен

| # | Целевой компонент (ТЗ) | Доступен? | Реализация в проекте | Точка замены |
|---|---|---|---|---|
| 1 | Microsoft Execution Containers (MXC) | ❌ Публичных релизов нет | `DockerSandbox` + seccomp + AppArmor + read-only rootfs + no-network + непривилегированный user. `SandboxPolicy` с 13 полями готов для MXC YAML | `Sandbox` ABC, `DockerSandbox` / `MXCSandbox` / `NoneSandbox` |
| 2 | NVIDIA OpenShell | ❌ Публичных релизов нет | То же, что п.1. `SandboxPolicy.yaml_policy` готов принимать YAML OpenShell-стиля | `Sandbox` ABC |
| 3 | Atomic Agent (Python) | ❌ Публичной Python-библиотеки нет | Собственный `AgentRuntime` с циклом plan→act→respond + MCP-tool-calls. Sprint 1 добавил: LoopDetector, TaskComplexityClassifier, static/dynamic prompt | `AgentRuntime` в `agent_core/agent.py` |
| 4 | Microsoft Aion 1.0 Plan | ❌ Модель не опубликована | `OllamaClient` с приоритетным списком: `mock:test-7b` → `aion-plan-1.0` → `mistral:7b-instruct` → `llama3.1:8b-instruct`. Если Aion выйдет — положить GGUF в Ollama | `LLMClient` ABC, `OllamaClient` |
| 5 | SuperLocalMemory V3.3 | ❌ Библиотека не опубликована | `FaissMemoryStore`: FAISS IndexFlatIP + SQLite, 3-канальный ретрив (semantic/recency/frequency), кривая Эббингауза, инкрементальный GC (soft-delete + threshold rebuild) | `MemoryStore` ABC, `FaissMemoryStore` |
| 6 | Desktop Homunculus (Rust/Bevy) | ❌ Публичного MCP-сервера нет | `WebSocketBridge` → Three.js-аватар в Tauri. `HomunculusMCPBackend` (stub) готов для подключения через stdio-MCP когда Homunculus выйдет | `AvatarBackend` ABC, `WebSocketBridge` / `HomunculusMCPBackend` |
| 7 | MCP-сервер WinGet | ⚠️ Готового сервера под Linux нет | Собственный `WingetServer` (FastMCP): на Windows вызывает `winget.exe`, на Linux — mock с `pip list` | `python/tools/winget_server.py` |
| 8 | MCP-сервер браузера | ⚠️ Готового нет | `BrowserServer` (FastMCP) на Playwright. Запускается на хосте (не в песочнице — требует chromium + сеть) | `python/tools/browser_server.py` |

---

## Точки расширения (`python/common/interfaces.py`)

Все заменяемые подсистемы — ABC-классы. Конкретная реализация выбирается в
`config.toml` и инстанцируется фабрикой:

| ABC | Реализации | Фабрика |
|---|---|---|
| `LLMClient` | `OllamaClient` (можно добавить `LlamaCppClient`, `OpenAICompatibleClient`) | В `llm_engine/ollama_client.py` |
| `MemoryStore` | `FaissMemoryStore` (можно добавить `ChromaMemoryStore`) | В `memory/__main__.py` |
| `Sandbox` | `DockerSandbox`, `MXCSandbox` (stub), `NoneSandbox` | `make_sandbox()` в `agent_core/security.py` |
| `ToolRunner` | `MCPBroker` (можно добавить `LangChainToolRunner`) | В `tools/broker.py` |
| `AvatarBackend` | `WebSocketBridge` (через адаптер), `HomunculusMCPBackend` (stub) | `make_avatar_backend()` в `avatar/homunculus_backend.py` |

**Добавление нового компонента = 3 шага:**
1. Создать класс-наследник ABC
2. Добавить строку в `config.toml` (backend = "my_new_impl")
3. Обновить фабрику (if/elif по backend)

Гайд с примерами кода — в [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) § 5.

---

## Известные ограничения

### Архитектурные
1. **Tauri-сборка под Windows** требует Windows-хоста с Rust toolchain + Tauri CLI.
   В Linux-окружении разработки собирается только Python-часть и ZMQ-сервисы.
2. **Rust-side protobuf** — Tauri backend использует JSON-конверт вместо бинарного
   protobuf. Для бинарного протокола нужно подключить `prost-build` в `build.rs`.
3. **Streaming LLM** — `LLMCall.stream` зарезервирован в proto, но реализация
   требует перехода с REQ/REP на DEALER/ROUTER + chunked-сообщения.
4. **Multi-modal LLM** — только текст. Изображения/аудио не поддерживаются.

### Инфраструктурные
5. **Ollama** должна быть установлена и запущена отдельно (`ollama serve`).
   Для тестов без Ollama используйте `scripts/mock_ollama.py`.
6. **sentence-transformers** (~2GB с torch) — опциональна. Если не установлена,
   `HashEmbedder` даёт детерминированные хеш-векторы той же размерности (384),
   но без семантического качества. Pipeline работает, но semantic-search бесполезен.
7. **FAISS** — `pip install faiss-cpu`. На ARM-платформах может потребоваться
   conda-forge сборка.
8. **Docker** — опционален для песочницы. Без него `NoneSandbox` запускает
   инструменты напрямую (только dev-режим, небезопасно).

### Безопасность
9. **Docker-песочница** активна только для инструментов с `sandbox = true` в
   `config.toml`. `winget` и `browser` запускаются на хосте (требуют системных
   привилегий / сети).
10. **Browser isolation** — Playwright требует chromium (~300MB) + сеть, что
    несовместимо с `--network none`. Рекомендуется отдельный Docker-образ или
    browserless.io (см. `docs/DEPLOYMENT.md` § 6).
11. **Avatar через WebSocket** — при потере соединения команды буферизуются в
    кольцевой очереди (256 сообщений) и отправляются после переподключения.

### Функциональные
12. **Persisted sessions** — `session_id` передаётся, но отдельной
    сессии-менеджмент-сервис пока нет; сессия — это просто ключ в memory.
    Запланирован в Sprint 3 (Episodes).
13. **Реальный TTS** — конфиг `avatar.tts_backend` есть, но интеграция с
    Coqui/Silero оставлена как TODO.
14. **Эмоции и личность** — пока нет. Запланированы в Sprint 2
    (EmotionEngine + DisagreementAssessor).

---

## Что УЖЕ лучше, чем целевой стек

Чтобы не копировать вслепую — фиксируем свои преимущества:

1. **Микросервисная архитектура ZeroMQ** — целевой стек подразумевал монолит.
   Мы можем заменять компоненты независимо, языково-агностичны.
2. **MCP-стандарт** — целевой стек мог использовать кастомный tool-формат.
   Мы на стандарте MCP — совместимость с любой MCP-экосистемой.
3. **Кривая забывания Эббингауза в ядре** — применяется ко всей памяти,
   не только к эмоциональным якорям.
4. **Многоканальный ретрив** — 3 канала (semantic + recency + frequency)
   с взвешенной суммой, вместо одного semantic.
5. **trace_id/span_id** — сквозная трассировка через все сервисы в каждом
   ZMQ-сообщении.
6. **Инкрементальный GC** — soft-delete + threshold-based rebuild, не
   перестраивает FAISS на каждой уборке.
7. **LoopDetector** — 3-сигнальный детектор зацикливания с LLM-error filter
   (не предлагает "уточнить запрос" при инфраструктурных сбоях).

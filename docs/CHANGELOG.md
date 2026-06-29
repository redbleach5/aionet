# Changelog — Aionet

История изменений в обратном хронологическом порядке. Формат —
[Keep a Changelog](https://keepachangelog.com/), версии — SemVer.

Текущая версия: **0.2.0** (post-Sprint 1 + review fixes)

---

## [0.2.0] — 2026-06-29 — Sprint 1 + Code Review Fixes

### Added — Sprint 1 фичи
- **LoopDetector** (`python/agent_core/loop_detector.py`) — 3-сигнальный
  детектор зацикливания:
  - Pattern: одинаковые (tool+args) > 2 раз подряд
  - Empty: K подряд истинно пустых observation (null/none/[]/{}),
    НЕ короткие валидные ответы (OK/42/done)
  - Semantic: max pairwise cosine ≥ 0.85 последних 3 thought'ов
  - LLM-error filter: timeout/ECONNREFUSED/AI_APICallError не считаются циклом
- **TaskComplexityClassifier** (`python/agent_core/task_complexity.py`) —
  5-уровневая классификация запроса (trivial/simple/moderate/complex/research)
  на Cyrillic-safe regex. Возвращает defaults: max_iter/max_tokens/tools/web_search
- **SystemPromptBuilder** (`python/agent_core/prompt_builder.py`) —
  static prefix + dynamic suffix split для KV-cache Ollama (3-5× ускорение
  повторных вызовов). Legacy fallback если `system_prompt_static` пустой
- Proto: `LLMCall` += `static_prefix` (поле 8) + `dynamic_suffix` (поле 9)
- Config: секция `[agent.loop_detector]` с 5 параметрами,
  `[llm].system_prompt_static`

### Added — Code Review Fixes
- **SandboxPolicy** (`python/common/interfaces.py`) — декларативная политика
  с 13 полями (seccomp/apparmor/yaml/network/fs_read/write/syscalls/caps/
  env/mem/cpu/pid). `Sandbox.apply_policy()` + `policy` property в ABC.
  `DockerSandbox.apply_policy()` конвертирует в Docker security-opt.
  `MXCSandbox.apply_policy()` готов для YAML когда SDK появится.
- **MCPBroker retry** (`python/tools/broker.py`) — 3 попытки с exponential
  backoff (1с, 2с, 4с). Ретраит только инфраструктурные ошибки (timeout,
  broken pipe), логические ok=False — без retry.
- **HomunculusMCPBackend** (`python/avatar/homunculus_backend.py`) —
  полный AvatarBackend над MCP (speak/emote/gesture/look_at/idle) через
  stdio-MCP-сессию. `make_avatar_backend()` фабрика.
- **HashEmbedder** (`python/common/embedder.py`) — детерминированный
  хеш-эмбеддер 384-мерных векторов как fallback для sentence-transformers
  (тянет torch ~2GB). Семантического качества нет, но pipeline работает.
- **Memory инкрементальный GC** — soft-delete (быстро) сразу, физическая
  перестройка FAISS только при `soft_deleted >= gc_rebuild_threshold`
  (default 1000). Миграция для старых БД.
- **Memory stats** — `total`, `soft_deleted`, `gc_rebuild_threshold`,
  `gc_pending_rebuild`
- **Browser sandbox=false** — задокументировано, что Playwright требует
  chromium + сеть, несовместимо с `--network none`
- **Mock Ollama** (`scripts/mock_ollama.py`) — HTTP-сервер, имитирует
  Ollama API для тестов без реальной LLM
- **Web-UI** (`scripts/web_ui.py` + `web_ui/`) — простой чат в браузере
  через HTTP+WebSocket, не требует Rust/Node.js. Альтернатива Tauri-десктопу
  для быстрого тестирования backend'а.
- **Tauri dev-mode launcher** (`scripts/dev_tauri.sh` + `.ps1`) —
  кроссплатформенный запуск `cargo tauri dev` с проверкой зависимостей
  (Rust, Node.js, system libs) и health-check'ом backend'а.
- **Tauri icons** (`scripts/gen_icons.py` + `rust/icons/`) — генерация
  icon.png/icon.ico/icon_128.png для dev и build.
- **HashEmbedder fallback** в `memory/faiss_memory.py` и
  `agent_core/agent.py` (LoopDetector)

### Added — Документация и тесты
- `docs/DEPLOYMENT.md` — полная инструкция развёртывания
- `docs/ARCHITECTURE.md` — каноническая схема компонентов
- `docs/TESTING.md` — гайд по тестированию
- `docs/CHANGELOG.md` — этот файл
- `tests/test_sprint1.py` — 21 unit-тест (LoopDetector + Complexity + PromptBuilder)
- `tests/test_memory_unit.py` — 12 unit-тестов (ранжирование, забывание, GC, session isolation)
- `tests/test_integration.py` — 9 end-to-end сценариев
- `tests/test_load.py` — нагрузочные тесты ZeroMQ+FAISS
- `scripts/start_bg.sh` — bash-лаунчер всех сервисов (Linux/macOS)
- `scripts/start_bg.ps1` — PowerShell-лаунчер для Windows
- `scripts/stop_bg.sh` / `scripts/stop_bg.ps1` — остановка сервисов

### Fixed — Баги найденные в процессе
- **Cross-session leak в `retrieve()`** — `session_id` применялся только как
  `score *= 0.3` post-filter, а не SQL pre-filter. Записи из другой сессии
  могли попасть в top-k. Fix: session_id теперь в WHERE clause SQL.
- **`ZMQClient._ensure()`** ссылался на `_rcvtimeo_ms` вместо `_rcvtimeo` —
  AttributeError на каждом клиентском вызове
- **LLM Engine handler** вызывал `m.role.Name.lower()`, но protobuf enum
  это int — использован `ROLE_NAMES` dict
- **`mcp.Server`** не имеет `@tool()` декоратора в текущем SDK — переключились
  на `FastMCP`
- **MCPBroker блокировался** на последовательных `client.start()` —
  переписан с lazy init + отдельный event-loop thread + broken-session retry
- **`_base.py`** не ловил `BrokenPipeError` на shutdown — добавлен
  try/except + ExceptionGroup unwrap
- **HashEmbedder** возвращал `dim*4` элементов (uint8 vs float32) —
  AssertionError в `faiss.add()`. Переписан на стабильную реализацию
- **`common/__init__.py`** импортировал несуществующее имя `envelope` —
  AttributeError при первой загрузке proto

### Changed
- `AgentRuntime` — заменил жёсткий `MAX_ITER=6` на адаптивный из complexity
  defaults (cap `MAX_ITER_HARD_LIMIT=12`). `max_tokens` динамический per-call.
  Tools загружаются только если complexity разрешает.
- `OllamaClient.call()` — принимает `static_prefix`/`dynamic_suffix` kwargs.
  При static_prefix → отправляет 2 system-сообщения в Ollama.
- `config.toml` — `candidate_models` начинается с `mock:test-7b` для тестов
- `config.toml` — `gc_rebuild_threshold = 1000` (новый параметр)
- `config.toml` — `browser sandbox = false` с explanatory comment
- `rust/tauri.conf.json` — `bundle.targets = "all"` (кроссплатформенный),
  добавлены секции `linux` и `macOS`, исправлены пути иконок

### Removed
- `scripts/start_all.sh` — дубликат `scripts/start_bg.sh`
- `scripts/test_launch.py` — дубликат `scripts/start_bg.sh`
- `tests/integration_test.py` — дубликат `tests/test_integration.py`

### Test Results
- `test_sprint1.py`: **21/21 PASS**
- `test_memory_unit.py`: **12/12 PASS**
- `test_integration.py`: **9/9 PASS**
- `test_load.py`: ran OK, baselines captured

### Metrics (mock-ollama)
- Memory STORE: 879 ops/sec, p95=1.3ms
- Memory RETRIEVE: p95=1.2ms
- LLM Engine: 19 calls/sec (3 workers)
- Agent E2E: p95=58ms

---

## [0.1.0] — 2026-06-29 — Initial Release

### Added — Базовый каркас (Этапы 0-6)
- **Этап 0:** proto-схема `Envelope` с trace_id/span_id, ZeroMQ-обёртки
  (REQ/REP + PUB/SUB), TOML-конфиг, ABC-интерфейсы в `interfaces.py`
- **Этап 1:** Docker-песочница с seccomp + AppArmor + read-only rootfs +
  no-network. `MXCSandbox` stub. `SandboxPolicy` (позже в review-fixes).
- **Этап 2:** `OllamaClient` с приоритетным списком моделей (aion → mistral →
  llama) и retry через tenacity. `AgentRuntime` с циклом plan-act-respond.
- **Этап 3:** 4 stdio-MCP-сервера (shell/fs/winget/browser) на `FastMCP`.
  `MCPBroker` — ZMQ↔stdio мост.
- **Этап 4:** `FaissMemoryStore` — FAISS IndexFlatIP + SQLite, 3-канальный
  ретрив (semantic/recency/frequency), кривая Эббингауза, GC.
- **Этап 5:** Tauri 2.0 + React + Three.js-аватар. WebSocket-мост.
- **Этап 6:** `start_all.sh`, `integration_test.py`, README, ASSUMPTIONS.

### Added — Анализ Lia-v2
- `docs/LIA-V2-ANALYSIS.md` — анализ проекта Lia-v2, что перенять
- `docs/SPRINT-2-PLAN.md` — план внедрения эмоций + личности + адаптивности

### Architecture Decisions
- Микросервисы на ZeroMQ + Protobuf (не monolith)
- MCP-стандарт для инструментов (не кастомный формат)
- Docker-песочница с seccomp+AppArmor (не `child_process` без изоляции)
- Кривая Эббингауза на всю память (не только эмоциональные якоря)
- 3-канальный ретрив (не один semantic)
- `trace_id`/`span_id` в каждом ZMQ-сообщении

---

## Roadmap

### [0.3.0] — Sprint 2 (запланирован)
- **EmotionEngine** — 5-осевая модель (joy/curiosity/calm/irritation/sadness),
  rule-based perceive **без LLM**, exp decay к baseline
- **Personality + DisagreementAssessor** — 5-уровневый спектр несогласия
  (execute → reluctant → counterOffer → principledRefusal → ethicalBlock)
- **CapabilityProfiler** — tier micro/standard/plus/max через nvidia-smi + Ollama API
- **CognitivePlanner** — матрица `tier × complexity × mode` → adaptive
  calls/deliberate/selfCheck/maxTokens

### [0.4.0] — Sprint 3 (запланирован)
- Episodes + FactExtractor (изоляция чатов, GlobalFact/EpisodeFact)
- EmotionalMemoryStore (decay 180 дней, "не бередить раны")

### [0.5.0] — Sprint 4 (запланирован)
- VRM 3D-аватар через `@pixiv/three-vrm` (вместо icosahedron)
- Реальный TTS (Coqui/Silero)

### [0.6.0] — Sprint 5 (опционально)
- RL sidecar (PPO + ONNX, обучаемый стиль общения)
- Smart notifications о hardware-лимитах
- Streaming LLM (DEALER/ROUTER вместо REQ/REP)

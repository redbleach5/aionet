# Deployment Guide — Aionet

> **Связанные доки:** [`ARCHITECTURE.md`](ARCHITECTURE.md) (схема компонентов),
> [`TESTING.md`](TESTING.md) (тестирование), [`CHANGELOG.md`](CHANGELOG.md)
> (история версий), [`ASSUMPTIONS.md`](../ASSUMPTIONS.md) (замены компонентов).

Полная инструкция по развёртыванию: порты, порядок запуска, конфиги для
dev/prod, гайд по замене компонентов.

## 1. Схема развёртывания

```
                ┌──────────────────────────────────────────────────────┐
                │           Tauri Desktop App (Rust + React)            │
                │  ┌─────────────┐  ┌────────────────────────────────┐  │
                │  │ Three.js    │  │  Rust backend (ZMQ REQ)        │  │
                │  │ Avatar      │  │  IPC: send_message, health     │  │
                │  └──────┬──────┘  └──────────┬─────────────────────┘  │
                └─────────┼─────────────────────┼────────────────────────┘
                          │ WS                   │ ZMQ REQ
                          │ ws://127.0.0.1:8765  │ tcp://127.0.0.1:5550
                          ▼                      ▼
┌────────────────────────────┐   ┌─────────────────────────────────────────┐
│   avatar_bridge (Python)   │   │         agent_core (Python)             │
│   ZMQ SUB :5554 (cmd)      │   │   Orchestrator: plan-act-respond loop   │
│   ZMQ PUB :5555 (events)   │   │   Sprint 1: complexity + loop detector  │
│   WS :8765 (Tauri)         │   │   + static/dynamic prompt split          │
└────────────────────────────┘   └──┬──────────┬──────────┬──────────┬────┘
                                    │ REQ      │ REQ      │ REQ      │ PUB
                                    ▼          ▼          ▼          ▼
                          ┌──────────────┐ ┌─────────┐ ┌────────┐ ┌────────┐
                          │ llm_engine   │ │ memory  │ │ tools  │ │ avatar │
                          │ :5551        │ │ :5552   │ │ :5553  │ │ bridge │
                          │ Ollama       │ │ FAISS   │ │ MCP    │ │ (PUB)  │
                          │ :11434       │ │ +SQLite │ │ broker │ │        │
                          └──────────────┘ └─────────┘ └───┬────┘ └────────┘
                                                             │ stdio MCP
                          ┌──────────────────────────────────┼──────────┐
                          ▼                                  ▼          ▼
                    ┌──────────┐                      ┌──────────┐ ┌──────────┐
                    │ shell    │                      │ fs       │ │ winget   │
                    │ (Docker) │                      │ (Docker) │ │ (host)   │
                    └──────────┘                      └──────────┘ └──────────┘
                                                                          │
                                                                          ▼
                                                                    ┌──────────┐
                                                                    │ browser  │
                                                                    │ (host,   │
                                                                    │  net=on) │
                                                                    └──────────┘
```

## 2. Порты

| Порт | Протокол | Сервис        | Назначение                            |
|------|----------|---------------|---------------------------------------|
| 11434| HTTP     | Ollama/mock   | LLM API (`/api/chat`, `/api/tags`)    |
| 5550 | ZMQ REP  | agent_core    | Приём AgentRequest от Tauri/UI        |
| 5551 | ZMQ REP  | llm_engine    | LLM-инференс (LLMCall → LLMResult)    |
| 5552 | ZMQ REP  | memory        | Memory ops (STORE/RETRIEVE/STATS)     |
| 5553 | ZMQ REP  | tools broker  | Tool calls (MCP-агрегатор)            |
| 5554 | ZMQ PUB  | agent_core    | Avatar-команды (всем подписчикам)     |
| 5555 | ZMQ PUB  | avatar_bridge | Avatar-события (для agent_core)       |
| 8765 | WS       | avatar_bridge | WebSocket для Tauri/Three.js фронта   |

Все порты — только на `127.0.0.1` (localhost). Внешнего доступа нет.

## 3. Порядок запуска

**Жёсткий порядок** (зависимости сверху вниз):

```
1. mock_ollama (или реальная Ollama)   — должна слушать :11434
       ↓
2. memory                               — зависит только от FAISS+SQLite
       ↓
3. llm_engine                           — при старте опрашивает Ollama /api/tags
       ↓
4. tools broker                         — регистрирует MCP-серверы (lazy init)
       ↓
5. avatar_bridge                        — SUB к :5554, PUB на :5555, WS на :8765
       ↓
6. agent_core                           — REQ-клиенты к :5551/:5552/:5553,
                                          PUB на :5554
       ↓
7. Tauri app (опционально)              — REQ к :5550, WS к :8765
```

**Скрипт запуска:**

| Платформа | Запуск | Остановка |
|---|---|---|
| Linux / macOS | `bash scripts/start_bg.sh` | `bash scripts/stop_bg.sh` |
| Windows (PowerShell) | `.\scripts\start_bg.ps1` | `.\scripts\stop_bg.ps1` |
| Windows (реальная Ollama уже работает) | `.\scripts\start_bg.ps1 -SkipMockOllama` | `.\scripts\stop_bg.ps1` |

> **Windows note:** если PowerShell блокирует скрипты, выполните один раз:
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`
> (только для текущей сессии, безопасно).

Скрипты поднимают сервисы в правильном порядке, пишут логи в `logs/`,
сохраняют PID'ы в `logs/pids.txt` для последующей остановки.

**Проверка здоровья:**

```bash
# Linux/macOS
curl -s http://127.0.0.1:11434/api/tags | jq '.models[].name'    # Ollama
ss -tln | grep -E ":555[0-5]|:8765"                               # все ZMQ + WS

# Windows (PowerShell)
(Invoke-RestMethod http://127.0.0.1:11434/api/tags).models.name   # Ollama
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 5550..5555,8765 }
```

## 4. Конфиги для разных сред

### dev (по умолчанию, `config.toml`)
- LLM: `mock:test-7b` (или `mistral:7b-instruct` если есть Ollama)
- Песочница: `none` (без изоляции, fastest iteration)
- Логи: DEBUG в stderr
- Memory: SQLite + FAISS в `./data/`
- Embedder: HashEmbedder (если нет sentence-transformers)

```bash
# dev — Linux/macOS, без Ollama, mock-сервер
AIONET_CONFIG=config.toml bash scripts/start_bg.sh

# dev — Windows (PowerShell)
$env:AIONET_CONFIG = "config.toml"
.\scripts\start_bg.ps1
```

### test (интеграционное тестирование)
- LLM: `mock:test-7b` (rule-based, мгновенные ответы)
- Песочница: `none`
- Memory: временная SQLite в `./data/` (чистится между запусками)
- Embedder: HashEmbedder

```bash
# Linux/macOS
rm -f data/memory.faiss data/memory.sqlite
bash scripts/start_bg.sh
PYTHONPATH=python:proto/_gen python tests/test_integration.py

# Windows (PowerShell)
Remove-Item data\memory.faiss, data\memory.sqlite -ErrorAction SilentlyContinue
.\scripts\start_bg.ps1
$env:PYTHONPATH = "python;proto\_gen"
python tests\test_integration.py
```

### prod (требует реальную Ollama + Docker)
- LLM: `mistral:7b-instruct` или `llama3.1:8b-instruct`
- Песочница: `docker` (seccomp + AppArmor)
- Логи: INFO в файл `./logs/aionet.log`
- Memory: SQLite в `~/.local/share/aionet/memory.sqlite`
- Embedder: `sentence-transformers/all-MiniLM-L6-v2`

Создайте `config.prod.toml` рядом с `config.toml`:

```toml
[system]
environment = "prod"
log_level = "INFO"
log_file = "/var/log/aionet/aionet.log"

[llm]
candidate_models = ["mistral:7b-instruct", "llama3.1:8b-instruct"]
fallback_model = "mistral:7b-instruct"
ollama_host = "http://127.0.0.1:11434"

[security]
backend = "docker"
sandbox_image = "aionet-toolbox:latest"
seccomp_profile = "/etc/aionet/seccomp-profile.json"
apparmor_profile = "aionet-toolbox"
network_enabled = false
read_only_rootfs = true

[memory]
index_path = "/var/lib/aionet/memory.faiss"
meta_db_path = "/var/lib/aionet/memory.sqlite"
embedding_model = "all-MiniLM-L6-v2"
gc_rebuild_threshold = 5000  # прод-объёмы больше

[avatar]
backend = "html5_threejs"   # или "homunculus" когда станет доступен
```

```bash
# Linux/macOS
AIONET_CONFIG=config.prod.toml bash scripts/start_bg.sh

# Windows (PowerShell) — paths use Windows conventions
$env:AIONET_CONFIG = "config.prod.toml"
.\scripts\start_bg.ps1 -SkipMockOllama   # реальная Ollama уже работает
```

> **Windows prod paths:** замените Unix-пути в `config.prod.toml`:
> - `log_file = "C:/ProgramData/aionet/logs/aionet.log"`
> - `seccomp_profile = "C:/ProgramData/aionet/seccomp-profile.json"`
> - `index_path = "C:/ProgramData/aionet/data/memory.faiss"`
> - AppArmor на Windows не работает — оставьте `apparmor_profile = ""`
> - Docker Desktop на Windows поддерживает `--security-opt seccomp=...`

## 5. Гайд по замене компонентов

Все заменяемые компоненты изолированы за ABC-интерфейсами в
`python/common/interfaces.py`. Чтобы заменить компонент:

### 5.1. LLM (Ollama → llama.cpp / OpenAI-compatible)

1. Создать новый класс, наследующий `LLMClient`:
   ```python
   # python/llm_engine/llamacpp_client.py
   from common.interfaces import LLMClient, LLMResult, ChatMessage, ToolSchema

   class LlamaCppClient(LLMClient):
       def list_available_models(self) -> list[str]:
           return ["llama-3.1-8b-q4"]

       def call(self, *, model, system_prompt, messages, tools=None,
                temperature=0.3, max_tokens=2048, timeout_s=120,
                static_prefix=None, dynamic_suffix=None) -> LLMResult:
           # Реализация через llama_cpp.Llama(...).create_chat_completion(...)
           ...
   ```

2. В `config.toml` указать провайдера:
   ```toml
   [llm]
   provider = "llama_cpp"
   model_path = "/models/llama-3.1-8b-q4_k_m.gguf"
   ```

3. В `python/llm_engine/ollama_client.py` (или новом `__main__.py`) —
   добавить фабрику:
   ```python
   def make_llm_client(cfg):
       provider = cfg.llm.get("provider", "ollama")
       if provider == "llama_cpp":
           from .llamacpp_client import LlamaCppClient
           return LlamaCppClient(cfg)
       return OllamaClient(...)
   ```

### 5.2. Memory (FAISS → Chroma)

1. Создать `python/memory/chroma_memory.py`:
   ```python
   from common.interfaces import MemoryStore, MemoryRecord

   class ChromaMemoryStore(MemoryStore):
       def __init__(self, cfg):
           import chromadb
           self.client = chromadb.PersistentClient(path=cfg.memory["meta_db_path"])
           self.collection = self.client.get_or_create_collection("aionet")
           # ... реализовать store/retrieve/forget/stats
   ```

2. В `config.toml`:
   ```toml
   [memory]
   backend = "chroma"
   ```

3. В `python/memory/__main__.py` — фабрика по `cfg.memory["backend"]`.

### 5.3. Sandbox (Docker → MXC/OpenShell)

Контракт уже подготовлен (см. `SandboxPolicy` + `apply_policy`):

1. Создать `python/agent_core/mxc_sandbox.py`:
   ```python
   from common.interfaces import Sandbox, SandboxResult, SandboxPolicy

   class RealMXCSandbox(Sandbox):
       def __init__(self, cfg):
           # Инициализация MXC SDK
           import mxc_sdk  # когда станет доступен
           self.client = mxc_sdk.Client()

       def apply_policy(self, policy: SandboxPolicy) -> None:
           # Конвертируем SandboxPolicy → MXC YAML
           if policy.yaml_policy:
               self._mxc_policy = policy.yaml_policy  # путь к файлу
           else:
               import yaml
               self._mxc_policy = yaml.dump(policy.to_dict())

       def run(self, *, command, workspace=None, network=False, ...):
           # mxc_client.run_with_policy(self._mxc_policy, command, workspace)
           ...
   ```

2. В `config.toml`:
   ```toml
   [security]
   backend = "mxc"
   yaml_policy = "/etc/aionet/policy.yaml"  # опционально
   ```

3. В `make_sandbox()` — заменить заглушку `MXCSandbox` на `RealMXCSandbox`.

### 5.4. Avatar (Three.js → Desktop Homunculus)

1. Убедиться, что `HomunculusMCPBackend` (`python/avatar/homunculus_backend.py`)
   уже реализован — это готовая точка расширения.

2. Установить Homunculus (когда выйдет):
   ```bash
   # Гипотетически
   wget https://github.com/homunculus/homunculus/releases/latest/homunculus_mcp
   chmod +x homunculus_mcp
   ```

3. В `config.toml`:
   ```toml
   [avatar]
   backend = "homunculus"
   homunculus_mcp_command = ["/opt/homunculus/bin/homunculus_mcp"]
   homunculus_env = { RUST_LOG = "info" }
   ```

4. Перезапустить `avatar_bridge` — `make_avatar_backend()` подхватит новый
   backend автоматически.

### 5.5. Tools (MCP → LangChain)

Если нужно использовать LangChain-инструменты вместо MCP:

1. Создать `python/tools/langchain_runner.py`:
   ```python
   from common.interfaces import ToolRunner, ToolSchema, ToolExecutionResult

   class LangChainToolRunner(ToolRunner):
       def __init__(self, cfg):
           from langchain.tools import Tool
           self.tools = [
               Tool(name="search", func=...),
               # ...
           ]

       def list_tools(self) -> list[ToolSchema]:
           return [ToolSchema(name=t.name, description=t.description,
                              parameters_json=...) for t in self.tools]

       def call(self, *, tool_name, arguments, timeout_ms=30000):
           tool = next((t for t in self.tools if t.name == tool_name), None)
           if not tool:
               return ToolExecutionResult(ok=False, output=None, error="not found")
           result = tool.run(arguments)
           return ToolExecutionResult(ok=True, output=result)
   ```

2. В `python/tools/broker.py` — фабрика по `cfg.tools.get("backend", "mcp")`.

## 6. Browser isolation trade-offs

Browser-tool (`tools/browser_server.py` на Playwright) имеет особенности:

| Режим | Песочница | Сеть | Playwright | Комментарий |
|---|---|---|---|---|
| Текущий (host) | ❌ | ✅ | ✅ | Browser запускается на хосте. Небезопасно, но работает. |
| Docker toolbox | ✅ | ❌ | ❌ | sandbox=true ломает browser: нет chromium, нет сети |
| Отдельный контейнер | ✅ | ✅ | ✅ | Отдельный образ `aionet-browser:latest` с Playwright+chromium |
| Browserless.io | ✅ | ✅ | ✅ | SaaS, MCP-over-HTTP |

**Рекомендация для prod:** собрать отдельный Docker-образ с Playwright+chromium
(на базе `mcr.microsoft.com/playwright:v1.40.0-jammy`), включить сеть, и
запускать browser_server в нём. Не использовать общий `aionet-toolbox` образ.

## 7. Масштабирование

### 7.1. MCPBroker — уже отдельный сервис
MCPBroker (`python -m tools`) запускается как **отдельный процесс** на :5553.
Это позволяет:
- Масштабировать его независимо (больше CPU для tool-выполнения)
- Перезапускать без остановки agent_core
- В будущем — запускать несколько реплик с балансировкой (через ZMQ ROUTER/DEALER)

### 7.2. Memory — горизонтальное масштабирование
FAISS `IndexFlatIP` — один процесс. Для продакшена с миллионами записей:
- `IndexIVFFlat` или `IndexHNSWFlat` (быстрее, аппроксимация)
- `faiss.index_cpu_to_gpu(...)` если есть GPU
- Или перейти на Chroma/Qdrant/Milvus (через `MemoryStore` ABC)

### 7.3. LLM — несколько реплик
Сейчас один `llm_engine` процесс. Для параллельных запросов:
- Запустить несколько `llm_engine` процессов на разных портах
- В `agent_core` — пул ZMQ-клиентов с round-robin
- Или ZMQ ROUTER/DEALER вместо REQ/REP (нужна доработка)

## 8. UI — Tauri desktop app (основной) и Web-UI (быстрый fallback)

### 8.1 Tauri dev-режим (нативное окно, hot-reload)

**Системные требования (один раз):**

| Платформа | Установить |
|---|---|
| Linux (Debian/Ubuntu) | `sudo apt install libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev patchelf libssl-dev` + [Rust](https://rustup.rs) + Node.js 18+ |
| macOS | `brew install node rustup-init` (webkit входит в систему) + Xcode Command Line Tools |
| Windows | [Rust](https://win.rustup.rs/x86_64) + [Node.js LTS](https://nodejs.org) + [VS Build Tools 2022](https://visualstudio.microsoft.com/visual-cpp-build-tools/) (workload "Desktop development with C++") |

**Запуск:**
```bash
bash scripts/dev_tauri.sh        # Linux/macOS
.\scripts\dev_tauri.ps1          # Windows (PowerShell)
```

Скрипт делает:
1. Проверяет что Rust, Node.js, backend-сервисы — на месте
2. `npm install` если `frontend/node_modules` нет
3. `cargo install tauri-cli` если `cargo tauri` не доступен
4. `cargo tauri dev` — поднимает Vite dev-server :5173, компилирует Rust
   backend, открывает нативное окно приложения

**Что увидишь:**
- Нативное окно "Aionet — Local AI Agent" (1280×800)
- Левая колонка: 3D аватар (Three.js icosahedron с глазами/ртом)
- Правая колонка: чат с историей, tool-call traces
- WebSocket к `avatar_bridge` (:8765) для эмоций аватара
- ZMQ REQ к `agent_core` (:5550) через Rust IPC layer
- Hot-reload: правка `App.tsx`/`styles.css` обновляет UI без перезапуска

**Сборка установщика (опционально):**
```bash
cd rust && cargo tauri build
# Linux:   rust/target/release/bundle/deb/aionet_0.1.0_amd64.deb
# macOS:   rust/target/release/bundle/dmg/Aionet_0.1.0.dmg
# Windows: rust/target/release/bundle/msi/Aionet_0.1.0_x64.msi
#          rust/target/release/bundle/nsis/Aionet_0.1.0_x64-setup.exe
```

### 8.2 Web-UI (быстрый fallback без Rust)

Если Rust не установлен и нужен просто чат в браузере:

```bash
# Linux/macOS
PYTHONPATH=python:proto/_gen python scripts/web_ui.py

# Windows (PowerShell)
$env:PYTHONPATH = "python;proto\_gen"
python scripts\web_ui.py
```

Открой **http://127.0.0.1:8080** в браузере.

**Что умеет Web-UI** (упрощённая версия Tauri-фронтенда):
- Чат с историей сообщений (user/assistant)
- Отображение tool-call traces (имя, аргументы, результат, длительность)
- Простой CSS-аватар с анимацией (speaking/thinking/idle)
- WebSocket-подключение к `avatar_bridge` (:8765) для эмоций
- Session ID сохраняется между запросами
- Health-check каждые 30с

**Архитектура Web-UI:**
```
Browser ──HTTP──► web_ui.py (:8080) ──ZMQ REQ──► agent_core (:5550)
     │                                              │
     └──WS──► avatar_bridge (:8765) ◄──ZMQ PUB────┘
```

**Когда что использовать:**
| Сценарий | Tauri | Web-UI |
|---|---|---|
| Разработка UI (hot-reload) | ✅ | ✅ |
| 3D VRM-аватар (будущее) | ✅ | ❌ |
| Нативные уведомления, tray | ✅ | ❌ |
| Быстрый smoke-test backend | ❌ (долго собирать) | ✅ |
| CI/CD без Rust | ❌ | ✅ |
| Production для конечного пользователя | ✅ (установщик) | ❌ |

---

## 9. Мониторинг

### Healthcheck
```bash
# Каждый сервис должен отвечать на /health (TODO: не реализовано,
# сейчас health через logs/pids.txt)
curl http://127.0.0.1:11434/api/tags  # Ollama alive?
ss -tln | grep ":555[0-5]"             # ZMQ services listening?
```

### Логи
- Каждый сервис пишет в `logs/<service>.log`
- Формат: `timestamp | LEVEL | logger | tid=xxx sid=xxx | message`
- `trace_id` (tid) и `span_id` (sid) — сквозная трассировка через все сервисы
- Уровень: `config.toml: [system].log_level = "DEBUG" | "INFO" | "WARNING" | "ERROR"`

### Memory stats
```bash
PYTHONPATH=python:proto/_gen python -c "
from common.config import load_config
from common.proto import build_payload, PayloadType
from common.zmq_transport import ZMQClient
cfg = load_config()
c = ZMQClient(endpoint=cfg.zmq['memory_endpoint'], service_name='mon')
res = c.call(target='memory', payload_type=PayloadType.MEMORY_OP,
             payload=build_payload(PayloadType.MEMORY_OP, op=3))
print(dict(res.stats))
"
# Вывод: {'total': '42', 'soft_deleted': '3', 'faiss_total': '42',
#         'gc_rebuild_threshold': '1000', 'gc_pending_rebuild': '997', ...}
```

# Допущения и замены компонентов

Этот документ фиксирует все отклонения от целевого стека, описанного в ТЗ.
Каждая замена реализована за абстрактным интерфейсом — возврат к оригинальному
компоненту не требует переписывания системы, только новой реализации интерфейса.

| # | Целевой компонент (ТЗ) | Доступен? | Реализация в проекте | Точка замены (интерфейс) |
|---|---|---|---|---|
| 1 | Microsoft Execution Containers (MXC) | ❌ Публичных релизов нет | Docker + seccomp + AppArmor, непривилегированный user, read-only rootfs, no-network | `python/agent_core/security.py → class Sandbox` |
| 2 | NVIDIA OpenShell | ❌ Публичных релизов нет | То же, что и п.1 | `python/agent_core/security.py → class Sandbox` |
| 3 | Atomic Agent (Python) | ❌ Публичной Python-библиотеки с заявленным API нет | LangChain-совместимый agent-loop, обёрнутый в `AtomicAgentCompat` с тем же циклом plan→act→respond и поддержкой MCP | `python/agent_core/agent.py → class AgentRuntime` |
| 4 | Microsoft Aion 1.0 Plan | ❌ Модель не опубликована | Ollama-обёртка с приоритетным списком моделей: `aion-plan-1.0` → `mistral:7b-instruct` → `llama3.1:8b-instruct`. Если появится Aion — достаточно положить GGUF в Ollama и обновить `candidate_models` | `python/llm_engine/ollama_client.py → class LLMClient` |
| 5 | SuperLocalMemory V3.3 | ❌ Библиотека не опубликована | FAISS + SQLite + Ebbinghaus-forgetting + многоканальный ретрив (semantic/recency/frequency) | `python/memory/faiss_memory.py → class MemoryStore` |
| 6 | Desktop Homunculus (Rust/Bevy) | ❌ Публичного MCP-сервера нет | HTML5-аватар на Three.js внутри Tauri-окна, управляемый через WebSocket-мост. Сигнатура команд совпадает с целевой (`AvatarCommand` proto), поэтому при появлении Homunculus его MCP-сервер подключается вместо WS-моста | `python/avatar/ws_bridge.py → class AvatarBackend` |
| 7 | MCP-сервер WinGet | ⚠️ Готового сервера под Linux нет | Реализован свой stub-сервер с тем же интерфейсом; на Windows вызывает реальный `winget.exe`, на Linux — возвращает диагностическое сообщение | `python/tools/winget_server.py` |
| 8 | MCP-сервер браузера | ⚠️ Используется `mcp-server-browserbase`-like stub | Своя реализация на Playwright (опционально) или заглушка | `python/tools/browser_server.py` |

## Архитектурные интерфейсы для замены

Все точки расширения реализованы как абстрактные базовые классы (`abc.ABC`)
в `python/common/interfaces.py`. Конкретная реализация выбирается в `config.toml`
и инстанцируется фабрикой. Добавление нового компонента = новый класс-наследник
+ строка в конфиге.

## Известные ограничения текущей реализации

1. **Tauri-сборка под Windows** требует Windows-хоста с установленным
   Rust toolchain + Tauri CLI. В Linux-окружении разработки собирается только
   Python-часть и ZeroMQ-сервисы.
2. **Ollama** должна быть установлена и запущена отдельно (`ollama serve`).
   Модели подтягиваются через `ollama pull mistral:7b-instruct`.
3. **FAISS** требует `pip install faiss-cpu`. На ARM-платформах может
   потребоваться `faiss-cpu` из conda-forge.
4. **Docker-песочница** активна только для инструментов с `sandbox = true`.
   `winget` запускается на хосте, т.к. требует доступа к системному пакетному
   менеджеру.
5. **Avatar через WebSocket**: при потере соединения команды буферизуются в
   кольцевой очереди (256 сообщений) и отправляются после переподключения.

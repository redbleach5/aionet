# Анализ Lia-v2 — что перенять в Aionet

> Источник: https://github.com/redbleach5/Lia-v2.git
> Анализ выполнен при подготовке второго коммита в репозиторий Aionet.
> Lia-v2 — Next.js + Prisma + sqlite-vec + Python-sidecar (PPO). Локальный
> AI-компаньон с эмоциями, эпизодической памятью и RL-обучаемым стилем.

## 1. Карта возможностей Lia-v2 (по слоям)

| Слой | Что у Lia-v2 | Что у Aionet сейчас | Зазор |
|---|---|---|---|
| Память: эпизодическая | ✅ Episode + EpisodeFact (CRUD, изоляция по episode_id) | ⚠️ только session_id без UI-менеджмента | Нужен Episodes-менеджер |
| Память: факты | ✅ GlobalFact + EpisodeFact, LLM-extraction с эвристикой | ❌ нет | Нужен FactExtractor |
| Память: эмоциональная | ✅ EmotionalAnchor с decay 180д, "не бередить раны" | ❌ нет | Нужен EmotionalMemoryStore |
| Память: векторная | ✅ sqlite-vec с pre-filter по episode_id | ✅ FAISS (post-filter session_id с понижением score) | Можно ужесточить pre-filter |
| Эмоции | ✅ 5-осевая модель (joy/curiosity/calm/irritation/sadness), rule-based perceive, экспоненциальный decay | ❌ нет | Нужен EmotionEngine |
| Личность | ✅ Personality const (values, taste, signaturePhrases), DisagreementLevel (5 ступеней) | ❌ только system_prompt в config.toml | Нужен PersonalityProfile + DisagreementAssessor |
| Агентский цикл | ✅ ReAct с PLAN→EXECUTE→SYNTHESIZE, checkpointing, ask_user, cancellation | ⚠️ plan-act без явных фаз PLAN/SYNTHESIZE, без ask_user, без checkpoint | Доработать AgentRuntime |
| Loop detection | ✅ 3 сигнала: pattern + empty + semantic (embedding cosine ≥0.85) | ⚠️ только MAX_ITER=6 | Нужен LoopDetector |
| RL-обучение | ✅ PyTorch PPO sidecar, ONNX export, 9 действий (WAIT/WARM/BUSINESS/ASK/...) | ❌ нет | Опционально — большой пласт |
| Capability profile | ✅ Tier micro/standard/plus/max + авто-detect через nvidia-smi/Ollama API | ❌ нет | Нужен CapabilityProfiler |
| Cognitive depth | ✅ Адаптивный план (calls=1..4, deliberate, selfCheck, maxTokens) по tier × complexity | ❌ нет | Нужен CognitivePlanner |
| Task complexity | ✅ Классификатор trivial/simple/moderate/complex/research (regex-паттерны) | ❌ нет | Нужен TaskComplexityClassifier |
| Smart notifications | ✅ Hardware-limit warnings (web_search → reliable domains → user) | ❌ нет | Опционально |
| System prompt | ✅ Static prefix (KV-cache friendly) + dynamic suffix | ⚠️ один статичный system_prompt | Реструктурировать |
| Avatar | ✅ VRM (3D, @pixiv/three-vrm) + Live2D (PixiJS) + SVG fallback | ⚠️ Three.js icosahedron | Можно добавить VRM |
| Архитектура | Next.js monolith + Python sidecar | ✅ Микросервисы ZeroMQ | Наш сильнее по модульности |

## 2. Что точно стоит перенять (приоритет 1 — большая ценность, низкая стоимость)

### 2.1. **LoopDetector** — 3-сигнальный детектор зацикливания

Сейчас у нас `MAX_ITER = 6` — тупая отсечка. Lia-v2 делает умнее:

1. **Pattern loop** — если один и тот же `tool + arguments` повторяется > 2 раз подряд → стоп.
2. **Empty loop** — 3 подряд пустых или `< 20 символов` observation → стоп.
   С важной оговоркой: **LLM-таймауты не считаются** (это инфраструктура, не цикл).
3. **Semantic loop** — embedding последних 3 thought'ов; если max pairwise cosine ≥ 0.85 → стоп.

**Где реализовать:** `python/agent_core/loop_detector.py` (новый модуль).
Вызывается в `AgentRuntime._run()` после каждой итерации. При срабатывании —
пауза + ask_user (если есть UI) или synthesize по текущим шагам.

### 2.2. **EmotionEngine** — 5-осевая модель БЕЗ LLM

Принципиально важная находка: **эмоции на rule-based regex**, не на LLM.
Lia-v1 классифицировала эмоции LLM-вызовом → получала "купи молоко = rudeness"
и загрязняла состояние. v2 чинит это детерминированными триггерами.

Реализация:
- 5 осей: `joy`, `curiosity`, `calm`, `irritation`, `sadness` (0..1)
- Triggers (Cyrillic-safe regex): `warmth`, `rudeness`, `sadTopic`, `enthusiasm`,
  `curiosity`, `deepQuestion`, `disagreement`, `task`, `trivial`
- Каждому триггеру — детерминированный delta-вектор
- Decay к baseline по экспоненте (2%/мин): `factor = exp(-0.02 * dt_min)`,
  `current = current * factor + baseline * (1 - factor)`
- `emotionToText()` для инъекции в system prompt
- `dominantEmotion()` для аватара (→ эмоция-команда)

**Где реализовать:** `python/agent_core/emotion.py` + новая таблица в
`config.toml: [emotion] baseline = {joy=0.55, curiosity=0.75, ...}`.
Подключается в `AgentRuntime._run()`: perceive(input) → decay → emotionToText
→ добавить в system_prompt → avatar.emote(dominantEmotion()).

### 2.3. **Personality + DisagreementAssessor** — ценности и спектр несогласия

5-уровневый спектр: `execute → reluctant → counterOffer → principledRefusal → ethicalBlock`.
Не бинарный "согласен/отказ", а интонационная модуляция.

Реализация:
- `PersonalityProfile` в `python/common/interfaces.py` — константный объект:
  name, role, backstory, manners, signaturePhrases, baselineEmotion, values[]
- Каждое value: `{name, description, weight, violationPatterns[], tasteConflictPatterns[]}`
- `assessDisagreement(user_message)` →DisagreementAssessment{level, reason, triggeredValue}
- 5 инструкций для system_prompt — по одной на уровень

**Где реализовать:** `python/agent_core/personality.py` + расширить `AgentRequest`
proto-полем `disagreement_level` для UI-индикации.

### 2.4. **TaskComplexityClassifier** — тривиально/просто/умеренно/сложно/исследовательски

Regex-классификатор сложности запроса. Дешёвый, без LLM. Используется:
- В `CognitivePlanner` для выбора числа LLM-вызовов
- В UI для индикатора сложности
- В логах для аналитики

### 2.5. **CapabilityProfiler** — авто-детект tier (micro/standard/plus/max)

Lia-v2 детектит:
- `nvidia-smi` (Linux/Windows) → gpuCount + vramGb
- `system_profiler` (macOS) → Apple Silicon (vram = RAM/2)
- `ollama /api/show` → parameter_size + quantization

И классифицирует в tier:
- `micro` — ≤4B или CPU или <8GB VRAM
- `standard` — 5-13B, 8-24GB VRAM
- `plus` — 14-32B, 24-80GB VRAM
- `max` — 33B+, multi-GPU или 80GB+

Кеш 1 час в БД Setting. На основе tier выбирается `CognitiveParams`:
calls/deliberate/selfCheck/maxTokens/agentMaxSteps.

**Где реализовать:** `python/llm_engine/capability.py`. Профиль публикуется
через новый ZeroMQ-канал `capability_endpoint` или возвращается по
`healthcheck_endpoint`. `AgentRuntime` при старте опрашивает его и
корректирует `max_iter`, `max_tokens`, `temperature`.

### 2.6. **CognitivePlanner** — адаптивный pipeline

На основе `tier × complexity × mode`:
- `mode = auto | fast | standard | deep | agent`
- Возвращает: `{calls, deliberate, selfCheck, maxTokens, toolsEnabled, autoWebSearch}`

Например, на `micro` + `complex`: 1 call, webSearch=true, smartNotification=true
(маленькая модель честно предупреждается о лимите).
На `max` + `research`: 4 calls (deliberate → respond → self-check → revise),
maxTokens=16384, webSearch=true.

**Где реализовать:** `python/agent_core/cognitive.py`. В `AgentRuntime._run()`
вместо жёстких `MAX_ITER` и `max_tokens` — берём из плана.

## 3. Что перенять во вторую очередь (приоритет 2 — заметно улучшает UX)

### 3.1. **Episodes + Facts** — изоляция чатов и structured facts

Сейчас у нас `session_id` — это неявная сущность. Lia-v2 делает Episodes
первоклассной сущностью с CRUD, и разделяет память:
- `GlobalFact` (user.name, user.profession) — переживают смену чата
- `EpisodeFact` (current.project, current.topic) — только в этом чате

Это решает утечки контекста между чатами архитектурно.

**Где реализовать:**
- Новая таблица `episodes` в SQLite (рядом с `memories`)
- Расширить `MemoryOp` proto: `op = EPISODE_CREATE / EPISODE_LIST / EPISODE_SWITCH`
- Фронтенд: левая колонка со списком эпизодов (как в Lia-v2)
- `FactExtractor`: после каждого ответа — LLM-вызов с эвристикой
  (только если message > 200 символов или содержит триггеры "меня зовут", "я работаю"...)

### 3.2. **EmotionalMemory** — эмоциональные якоря

Запоминает не ЧТО было, а КАК себя чувствовал пользователь. Позволяет:
- "В прошлый раз, когда мы обсуждали X, ты был раздражён. Сейчас ты
   выглядишь спокойнее — могу я вернуться к той теме?"
- Анти-паттерн "не бередить раны": если прошлый эпизод экстремально
  интенсивный (≥0.8 после decay) и текущий тон нейтральный → warning
  в system_prompt: "не упоминай прямо, будь мягче"

Decay: halfTime 180 дней (медленнее, чем обычная память — эмоции живут дольше).

**Где реализовать:** расширить `MemoryStore` методом `store_emotional_anchor()`
+ `retrieve_emotional_anchors()`. Отдельная FAISS-коллекция или `metadata.type='emotional'`.

### 3.3. **Loop-detector LLM-error filter**

Подмножество 2.1, но заслуживает отдельного упоминания: ошибки LLM (timeout,
ECONNREFUSED, AI_APICallError) **не считаются** "пустым результатом". Это
инфраструктурная проблема, не цикл. Если детектор наивно посчитает 3 LLM-
таймаута подряд как empty-loop и предложит пользователю уточнить запрос —
это UX-бага. Lia-v2 явно фильтрует по маркерам в observation.

### 3.4. **Static prefix + dynamic suffix** — KV-cache optimization

Lia-v2 разбивает system_prompt на:
1. **Static prefix** (~600 токенов): личность, правила, инструменты.
   Ollama кэширует KV-prefix, следующие вызовы в 3-5× быстрее.
2. **Dynamic suffix**: эмоция, контекст, факты, tier-инструкции.

Сейчас у нас один сплошной `system_prompt_plan` в `config.toml` — каждое
изменение эмоции/фактов инвалидирует весь KV-кеш.

**Где реализовать:** в `LLMEngineService.handle()` — разделить `system_prompt`
на 2 сообщения: `role=system, content=static_prefix` + `role=system,
content=dynamic_suffix`. В `OllamaClient.call()` — отправлять как 2 записи
в `messages[]`. Контракт `LLMCall` proto добавить полем `static_prefix`.

## 4. Что можно перенять опционально (приоритет 3 — большие пласты)

### 4.1. **RL sidecar (PPO + ONNX)** — обучаемый стиль общения

Lia-v2 имеет отдельный Python-сервис (FastAPI:8765) с PyTorch PPO:
- 9 действий: WAIT/WARM_RESPONSE/BUSINESS_RESPONSE/ASK_QUESTION/OFFER_HELP/
  SHARE_THOUGHT/CRACK_JOKE/BE_CONCISE/BE_DETAILED
- State = 13-dim: 5 эмоций + 4 drives (curiosity/social/safety/rest) + 4 context
- Recorder сохраняет `(state, action, reward)` в SQLite при каждом ответе пользователя
- PPO обучает policy network, экспорт в ONNX
- Inference в Next.js через `onnxruntime-node` — **без HTTP-раундтрипов**

Это большой пласт, но архитектурно у нас есть куда вставить: новый
микросервис `python/rl/` с REP-сокетом `rl_endpoint`, который возвращает
RLAction для текущего state. `AgentRuntime` перед LLM-вызовом опрашивает
RL-сервис → добавляет `rlActionInstruction` в system_prompt.

### 4.2. **VRM-аватар (3D)** — @pixiv/three-vrm

Lia-v2 поддерживает VRM-модели с blendshapes для эмоций, дыханием, морганием,
lip-sync. Это качественный скачок от нашего icosahedron-гомункула.

**Где реализовать:** в `rust/frontend/src/App.tsx` — заменить `<Homunculus>`
на VRM-компонент. Управление через те же WS-команды (`speak`, `emote`).
В `config.toml: [avatar] backend = "vrm" | "html5_threejs"`.

### 4.3. **Smart notifications** — hardware-limit warnings

Если пользователь на 4B-модели просит доказать теорему Гёделя — фоновый
web_search "what LLM model size needed for [task type]" → фильтр по
надёжным доменам (arxiv, huggingface, anthropic...) → inline-уведомление
"для таких задач обычно нужна модель побольше".

Нишевая фича, но хорошо демонстрирует подход "честность о своих лимитах".

## 5. Что у нас УЖЕ лучше, чем у Lia-v2

Чтобы не копировать вслепую — фиксируем свои преимущества:

1. **Микросервисная архитектура ZeroMQ** — у Lia-v2 Next.js monolith +
   Python sidecar. Мы можем заменять компоненты независимо, языково-агностичны.
2. **MCP-протокол** — Lia-v2 использует кастомный tool-формат AI SDK.
   Мы на стандарте MCP — совместимость с любой MCP-экосистемой.
3. **Docker-песочница с seccomp+AppArmor** — Lia-v2 запускает code-run
   напрямую через `child_process` без изоляции.
4. **Кривая забывания Эббингауза в ядре** — Lia-v2 применяет decay только к
   emotional anchors (180д). У нас — к всей памяти (7д half-life).
5. **Многоканальный ретрив (semantic+recency+frequency)** — Lia-v2 использует
   только semantic с pre-filter по episode_id. Мы комбинируем 3 канала.
6. **trace_id/span_id в каждом сообщении** — сквозная трассировка через все
   сервисы. Lia-v2 использует локальный logger без трейсинга.

## 6. Рекомендованный план внедрения

### Спринт 1 (1-2 дня) — высокий ROI, низкий риск
- ✅ LoopDetector (3 сигнала + LLM-error filter) — `python/agent_core/loop_detector.py`
- ✅ TaskComplexityClassifier — `python/agent_core/task_complexity.py`
- ✅ Static prefix + dynamic suffix в system_prompt — `LLMEngineService` + proto

### Спринт 2 (2-3 дня) — эмоциональный слой
- ✅ EmotionEngine (5-осевая + decay) — `python/agent_core/emotion.py`
- ✅ Personality + DisagreementAssessor — `python/agent_core/personality.py`
- ✅ Avatar emote() интеграция с dominantEmotion()
- ✅ UI: индикатор эмоций + disagreement-level бейдж

### Спринт 3 (2-3 дня) — адаптивность
- ✅ CapabilityProfiler (nvidia-smi + Ollama API) — `python/llm_engine/capability.py`
- ✅ CognitivePlanner (tier × complexity × mode) — `python/agent_core/cognitive.py`
- ✅ Заменить `MAX_ITER=6` на `plan.calls` + `plan.agentMaxSteps`

### Спринт 4 (3-5 дней) — структурированная память
- ✅ Episodes как first-class entity (CRUD через MemoryOp)
- ✅ GlobalFact / EpisodeFact + FactExtractor с эвристикой
- ✅ EmotionalMemoryStore с decay 180д + "не бередить раны"
- ✅ UI: список эпизодов в левой колонке

### Спринт 5 (опционально, 5-7 дней) — большие пластины
- ⚠️ RL sidecar (PPO + ONNX) — `python/rl/` микросервис
- ⚠️ VRM-аватар через @pixiv/three-vrm
- ⚠️ Smart notifications о hardware-лимитах

## 7. Чего НЕ стоит переносить

1. **sqlite-vec** — у нас FAISS, который для этого use-case мощнее (IndexFlatIP
   нормализованных эмбеддингов = точный cosine similarity). sqlite-vec хорош
   для тех, у кого Prisma; у нас его нет.
2. **Next.js monolith** — наш Tauri + ZeroMQ архитектурно чище для десктопа.
3. **Bun runtime** — мы на Python+Rust, Bun тут не нужен.
4. **Vercel AI SDK streamText** — у нас свой LLM-engine; AI SDK — лишняя
   прослойка.
5. **Qwen2.5 как дефолт** — наш приоритетный список `aion → mistral → llama`
   ближе к ТЗ. Можно добавить qwen как 4-й fallback, но не как дефолт.

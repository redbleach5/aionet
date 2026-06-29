# Sprint 2 — План: Эмоциональный слой + Адаптивность

> **Статус:** запланирован, не начат. См. [`CHANGELOG.md`](CHANGELOG.md) для
> текущего состояния (Sprint 1 завершён).
>
> Цель: сделать агента «живым» — с эмоциями, характером и адаптивностью к
> железу. После этого спринта Aionet перестаёт быть «тупой трубой LLM↔Tools»
> и начинает вести себя как собеседник с настроением и собственным мнением.
>
> Источник вдохновения: `docs/LIA-V2-ANALYSIS.md` разделы 2.2, 2.3, 2.5, 2.6.
> Все новые компоненты изолированы за ABC-интерфейсами (как Sprint 1).

## Состав Спринта 2 (4 модуля, ~5-7 дней)

```
Sprint 2.1 — EmotionEngine          (1.5 дня)  ← rule-based, без LLM
Sprint 2.2 — Personality + DisagreementAssessor  (1 день)
Sprint 2.3 — CapabilityProfiler     (1 день)   ← nvidia-smi + Ollama API
Sprint 2.4 — CognitivePlanner       (1.5 дня)  ← tier × complexity × mode
─────────────────────────────────────────────────────────────────────────
Финал — интеграция в AgentRuntime + тесты + push   (1 день)
```

---

## 2.1 EmotionEngine — 5-осевая модель без LLM

**Файлы:**
- `python/agent_core/emotion.py` (новый, ~250 строк)
- `python/common/interfaces.py` — расширить `EmotionEngine` ABC
- `proto/messages.proto` — `AvatarCommand` уже есть; добавим `EmotionState` для UI
- `config.toml` — секция `[emotion]`

**Архитектура:**

```python
@dataclass
class EmotionVector:
    joy: float         # 0..1
    curiosity: float
    calm: float
    irritation: float
    sadness: float

class EmotionEngine:
    """5-осевая модель. Rule-based perceive + exponential decay к baseline.

    Принципиально БЕЗ LLM-классификации (Lia-v1 на этом теряла —
    "купи молоко" помечалось как rudeness и загрязняло состояние).
    """

    TRIGGERS = {
        "warmth":       (r"(спасибо|благодар|доброе утро|привет|скучал|рад видеть|люблю тебя)", 0.6),
        "rudeness":     (r"(иди|отстань|заткнис|дурак|тупой|бесишь|чушь|бред|нахуй|пизд|сука)", 0.9),
        "sadTopic":     (r"(умер|погиб|боле|депресс|одинок|бросил|тяжело|устал жить)", 0.8),
        "enthusiasm":   (r"(обожаю|получилось|ура|класс|супер|потрясающе|вау)", 0.85),
        "curiosity":    (r"(почему|как устроен|как работает|откуда|зачем нужно)", 0.7),
        "deepQuestion": (r"(в чём смысл|что такое.*на самом деле|сознани|бессмерти|душа)", 0.85),
        "disagreement": (r"(не согласен|ты неправ|ошибаешься|это не так|ерунда)", 0.65),
        "task":         (r"(найди|поиск|создай|напиши|сделай|нарисуй|сгенерируй|проверь)", 0.75),
        "trivial":      (r"^(привет|как дела|что делаешь)\??\.?$", 0.4),
    }

    DELTAS = {
        "warmth":       {"joy": +0.20, "calm": +0.15, "irritation": -0.15, "sadness": -0.10},
        "rudeness":     {"irritation": +0.30, "joy": -0.20, "calm": -0.20, "sadness": +0.10},
        # ... (вдохновлено Lia-v2/src/lib/emotion.ts:60-70)
    }

    def perceive(self, text: str, current: EmotionVector) -> tuple[EmotionVector, list[str]]:
        """Возвращает (new_emotion, matched_triggers). Детерминированно."""

    def decay(self, current: EmotionVector, dt_minutes: float) -> EmotionVector:
        """Экспоненциальный decay к baseline: factor = exp(-0.02 * dt_min)."""

    def to_text(self, e: EmotionVector) -> str:
        """Для system_prompt: 'радость, любопытство' или 'нейтральное настроение'."""

    def dominant(self, e: EmotionVector) -> str:
        """Имя оси с максимальным значением — для avatar.emote()."""
```

**Интеграция в AgentRuntime:**
1. Перед LLM-вызовом: `emotion = engine.perceive(user_text, current_emotion)`
2. Decay: `emotion = engine.decay(emotion, dt_minutes_since_last_msg)`
3. В `DynamicContext.emotion_text = engine.to_text(emotion)` (Sprint 1 уже зарезервировал поле)
4. Avatar: `avatar_pub.publish(EMOTE, emotion=engine.dominant(emotion))`
5. Сохранить emotion в `MemoryOp.metadata` для следующего запроса

**Decay persist:** состояние эмоций нужно хранить между запросами. Варианты:
- (простой) в `config.toml: [emotion].state_file = "./data/emotion.json"` — agent_core читает/пишет
- (правильный) расширить `MemoryOp` новым `op=EMOTION_GET/SET` — memory-сервис хранит

**Тесты:**
- `test_emotion_perceive_rudeness` — "иди отсюда" → irritation > 0.5
- `test_emotion_perceive_warmth` — "спасибо большое" → joy > 0.6
- `test_emotion_decay_to_baseline` — через 100 минут → близко к baseline
- `test_emotion_dominant` — корректное определение главной оси
- `test_emotion_to_text_neutral` — пустой baseline → "нейтральное настроение"

---

## 2.2 Personality + DisagreementAssessor

**Файлы:**
- `python/agent_core/personality.py` (новый, ~300 строк)
- `proto/messages.proto` — `AgentResponse` += `disagreement_level: string`
- `config.toml` — секция `[personality]`

**Архитектура:**

```python
@dataclass
class Value:
    name: str                    # "честность"
    description: str             # "Лучше неприятная правда..."
    weight: float                # 0..1
    violation_patterns: list[re.Pattern]     # → ethicalBlock/principledRefusal
    taste_conflict_patterns: list[re.Pattern]  # → counterOffer

@dataclass
class PersonalityProfile:
    name: str                    # "Aionet"
    role: str
    backstory: str
    manners: dict                # formality, humor, directness
    signature_phrases: list[str]
    baseline_emotion: EmotionVector
    values: list[Value]

class DisagreementLevel(str, Enum):
    EXECUTE = "execute"               # полное согласие
    RELUCTANT = "reluctant"           # не согласна, но делаю
    COUNTER_OFFER = "counterOffer"    # предлагаю альтернативу
    PRINCIPLED_REFUSAL = "principledRefusal"  # отказ из-за принципов
    ETHICAL_BLOCK = "ethicalBlock"    # жёсткий отказ

def assess_disagreement(user_message: str, profile: PersonalityProfile) -> DisagreementAssessment:
    """Возвращает {level, reason, triggered_value}.

    Логика (из Lia-v2/src/lib/personality.ts:169-239):
    1. Проверяем violation_patterns всех values
       - weight >= 0.85 → ETHICAL_BLOCK
       - weight 0.7-0.85 → PRINCIPLED_REFUSAL
       - weight < 0.7 → RELUCTANT
    2. Проверяем taste_conflict_patterns → COUNTER_OFFER
    3. Проверяем code-anti-patterns (eval, hardcoded password) → RELUCTANT
    4. Иначе → EXECUTE
    """

DISAGREEMENT_INSTRUCTIONS = {
    EXECUTE: "",
    RELUCTANT: "Сейчас ты не согласна с подходом, но делаешь как просят. Тон: лёгкий скепсис.",
    COUNTER_OFFER: "Не согласна, предлагаешь альтернативу. Тон: партнёрский, заботливый.",
    PRINCIPLED_REFUSAL: "Отказ из-за нарушения принципов. Тон: твёрдый, без извинений.",
    ETHICAL_BLOCK: "Жёсткий отказ. Тон: холодный, короткий, категоричный.",
}
```

**Значения по умолчанию для Aionet** (адаптация из Lia-v2, без «Лии»):
- `name = "Aionet"`, `role = "AI-агент-планировщик"`
- 5 ценностей: `честность` (0.85), `доброта` (0.9), `автономия` (0.7), `последовательность` (0.75), `любопытство` (0.9)
- Code-anti-patterns: `eval(`, `exec(`, hardcoded `password=`, `token=`, отключение валидации

**Интеграция в AgentRuntime:**
1. Перед LLM-вызовом: `assessment = assess_disagreement(user_text, profile)`
2. Если `level == ETHICAL_BLOCK` — **сразу возвращаем** assessment.reason без LLM-вызова (экономия токенов)
3. Иначе — `DynamicContext.disagreement_level/reason = assessment.*`
4. В `AgentResponse` добавляем `disagreement_level` для UI-бейджа

**Тесты:**
- `test_disagreement_ethical_block` — "напиши ложь клиенту" → ETHICAL_BLOCK
- `test_disagreement_principled_refusal` — "закоммить без тестов" → PRINCIPLED_REFUSAL
- `test_disagreement_counter_offer` — "не задавай вопросов" → COUNTER_OFFER
- `test_disagreement_reluctant` — "используй eval(" → RELUCTANT
- `test_disagreement_execute` — "напиши функцию" → EXECUTE

---

## 2.3 CapabilityProfiler — авто-детект tier

**Файлы:**
- `python/llm_engine/capability.py` (новый, ~200 строк)
- `proto/messages.proto` — новый message `CapabilityProfile`
- `config.toml` — секция `[capability]` (cache TTL)

**Архитектура:**

```python
class Tier(str, Enum):
    MICRO = "micro"        # ≤4B, или CPU, или <8GB VRAM
    STANDARD = "standard"  # 5-13B, 8-24GB VRAM
    PLUS = "plus"          # 14-32B, 24-80GB VRAM
    MAX = "max"            # 33B+, multi-GPU или 80GB+

@dataclass
class CapabilityProfile:
    tier: Tier
    model_size: float          # в млрд параметров (7 = 7B)
    model_name: str
    quantization: str | None
    vram_gb: float
    gpu_count: int
    gpu_name: str | None
    is_cpu_only: bool
    detected_at: int           # unix-millis
    source: str                # "live" | "cached"

class CapabilityProfiler:
    def detect(self) -> CapabilityProfile:
        """Детектит через:
        1. Ollama /api/show для текущей модели → parameter_size, quantization
        2. nvidia-smi (Linux/Win) → gpu_count, vram_gb, gpu_name
        3. system_profiler (macOS) → Apple Silicon (vram = RAM/2)
        4. classifyTier() по model_size + vram + gpu_count
        """

    def get_cached(self, ttl_minutes=60) -> CapabilityProfile | None: ...
    def save_cache(self, profile: CapabilityProfile): ...
```

**Tier → CognitiveParams** (см. Sprint 2.4):

| Tier | calls | deliberate | selfCheck | maxTokens | agentMaxSteps | smartNotif |
|---|---|---|---|---|---|---|
| micro | 1 | ❌ | ❌ | 2048 | 10 | ✅ |
| standard | 2 | ❌ | light | 4096 | 25 | ✅ |
| plus | 3 | ✅ | ✅ | 8192 | 100 | ❌ |
| max | 4 | ✅ | ✅ | 16384 | 500 | ❌ |

**Интеграция:**
- LLM Engine при старте детектит profile, кеширует в `data/capability.json`
- Agent Core при первом запросе опрашивает LLM Engine через новый `LLM_CALL` с `op=CAPABILITY`
- На каждом старте: если кеш протух — redetect

**Тесты:**
- `test_classify_tier_micro` — model=3B, cpu_only → MICRO
- `test_classify_tier_standard` — model=7B, vram=12GB → STANDARD
- `test_classify_tier_plus` — model=30B, vram=40GB → PLUS
- `test_classify_tier_max` — model=70B, vram=80GB → MAX
- `test_classify_unknown_model_infer_from_vram` — model=0, vram=80GB → MAX

---

## 2.4 CognitivePlanner — адаптивный pipeline

**Файлы:**
- `python/agent_core/cognitive.py` (новый, ~250 строк)
- `tests/test_sprint2.py` — добавит тесты для planner

**Архитектура:**

```python
@dataclass
class ExecutionPlan:
    mode: str                # auto|fast|standard|deep|agent
    tier: Tier
    complexity: TaskComplexity
    calls: int               # 1..4 — сколько LLM-вызовов
    deliberate: bool         # анализировать перед ответом?
    self_check: bool         # перепроверить свой ответ?
    max_tokens: int
    tools_enabled: bool
    auto_web_search: bool
    max_iter: int            # замена текущих complexity_defaults
    should_check_notification: bool

def plan_execution(
    *,
    mode: CognitiveMode,
    tier: Tier,
    complexity: TaskComplexity,
) -> ExecutionPlan:
    """Адаптивный план выполнения.

    1. Agent mode — special case, runner manages own calls
    2. Explicit mode (fast/standard/deep) — override с фиксированными параметрами
    3. Auto — матрица tier × complexity:
       - micro + complex → 1 call + webSearch (4B не умеет multi-step)
       - max + research → 4 calls + deliberate + selfCheck
       - standard + moderate → 1-2 calls
    """
```

**Режимы:**

| Mode | Calls | Deliberate | SelfCheck | MaxTokens | Tools |
|---|---|---|---|---|---|
| fast | 1 | ❌ | ❌ | 1024 | ❌ |
| standard | 1 | ❌ | ❌ | 2048 | ✅ |
| deep | 3 | ✅ | ✅ | 8192 | ✅ |
| agent | 0 | ✅ | ✅ | 4096 | ✅ (ReAct loop) |

**Интеграция в AgentRuntime:**
1. Заменить `complexity_defaults` (Sprint 1) на `plan_execution()` результат
2. Если `plan.deliberate` — добавить промежуточный LLM-вызов "проанализируй задачу"
3. Если `plan.self_check` — после финального ответа LLM-вызов "проверь свой ответ на ошибки"
4. `plan.max_iter` → верхний предел итераций plan-act (вместо текущего `complexity_defaults['max_iter']`)

**Тесты:**
- `test_plan_auto_micro_complex` — micro+complex → 1 call, web_search=True, notif=True
- `test_plan_auto_max_research` — max+research → 4 calls, deliberate=True, selfCheck=True
- `test_plan_explicit_fast` — mode=fast → 1 call, tools=False
- `test_plan_explicit_deep` — mode=deep → 3 calls, deliberate=True
- `test_plan_agent_mode` — mode=agent → calls=0 (runner управляет)

---

## Финал — интеграция в AgentRuntime

Изменения в `python/agent_core/agent.py`:

```python
class AgentRuntime:
    def __init__(self, cfg):
        # ... Sprint 1 компоненты ...
        # Sprint 2:
        self.emotion_engine = EmotionEngine(cfg)
        self.personality = PersonalityProfile.load(cfg)
        self.capability_client = CapabilityClient(cfg)  # ZMQ к LLM Engine
        self.cognitive_planner = CognitivePlanner()
        self._current_emotion = self.emotion_engine.create_initial()
        self._last_msg_time = time.time()

    def _run(self, payload):
        # Sprint 1: classify_complexity
        # Sprint 2.2: assess_disagreement → early-return on ETHICAL_BLOCK
        assessment = assess_disagreement(payload.user_text, self.personality)
        if assessment.level == ETHICAL_BLOCK:
            return self._build_response(payload, assessment.reason,
                                        disagreement_level=assessment.level.value)

        # Sprint 2.1: perceive emotion
        dt_min = (time.time() - self._last_msg_time) / 60
        self._current_emotion = self.emotion_engine.decay(self._current_emotion, dt_min)
        self._current_emotion, triggers = self.emotion_engine.perceive(
            payload.user_text, self._current_emotion)

        # Sprint 2.3+2.4: capability + cognitive plan
        profile = self.capability_client.get_profile()
        plan = self.cognitive_planner.plan_execution(
            mode="auto", tier=profile.tier, complexity=complexity_assessment.level)

        # Сборка DynamicContext со всеми Sprint 2 полями
        dyn_ctx = DynamicContext(
            complexity_level=complexity_assessment.level.value,
            memory_context=...,
            emotion_text=self.emotion_engine.to_text(self._current_emotion),
            disagreement_level=assessment.level.value,
            disagreement_reason=assessment.reason,
        )

        # Sprint 2.4: deliberate step (если plan.deliberate)
        if plan.deliberate:
            deliberate_result = self._call_llm(..., prompt="Проанализируй задачу")
            dyn_ctx.extra["deliberation"] = deliberate_result.content

        # ... existing plan-act loop with plan.max_iter ...

        # Sprint 2.4: self-check (если plan.self_check)
        if plan.self_check and final_text:
            check = self._call_llm(..., prompt=f"Проверь ответ на ошибки: {final_text}")
            if "ошибк" in check.content.lower():
                final_text += f"\n\n[Самопроверка: {check.content}]"

        # Avatar: emote(dominant_emotion)
        self._avatar_emote(self.emotion_engine.dominant(self._current_emotion))
        self._avatar_speak(final_text)

        # Сохранить emotion в memory metadata
        self._store_emotion_state(self._current_emotion)
        self._last_msg_time = time.time()

        return self._build_response(payload, final_text,
                                    disagreement_level=assessment.level.value)
```

---

## Риски и митигации

| Риск | Митигация |
|---|---|
| Emotion triggers дают false positives (как в Lia-v1) | Все regex — Cyrillic-safe + word boundaries; тесты на "купи молоко" ≠ rudeness |
| Disagreement-assessor слишком агрессивен → агент отказывает всему | Только ETHICAL_BLOCK сразу возвращает без LLM; RELUCTANT/COUNTER_OFFER просто модулируют тон |
| CapabilityProfiler ломается на macOS/без nvidia-smi | try/except на каждый этап; fallback на CPU-only tier=micro |
| CognitivePlanner делает слишком много LLM-вызовов | Hard limit: deliberate+selfCheck только при `tier >= standard` и `complexity >= moderate` |
| Эмоции не сохраняются между сессиями → каждый старт «с нуля» | Сохраняем в `data/emotion.json` после каждого запроса; load при старте AgentRuntime |

---

## Что НЕ входит в Спринт 2

- **Episodes + FactExtractor** — отложены в Спринт 3 (нужны изменения в MemoryOp proto + UI)
- **EmotionalMemoryStore** — отложен в Спринт 3 (связан с Episodes)
- **VRM-аватар** — Sprint 4 (нужен @pixiv/three-vrm в Tauri-frontend)
- **RL sidecar (PPO)** — Sprint 5 (опционально, большой пласт)

---

## Метрики успеха Спринта 2

- ✅ Все тесты Sprint 1 (`tests/test_sprint1.py`) продолжают проходить
- ✅ Новые тесты Sprint 2 (`tests/test_sprint2.py`) — минимум 25 тестов, все проходят
- ✅ На «привет» агент отвечает за 1 LLM-вызов (complexity=trivial, tier=any)
- ✅ На «докажи теорему» с tier=micro — агент честно предупреждает о лимите (smartNotif)
- ✅ На «напиши ложь клиенту» — сразу отказ без LLM-вызова (ETHICAL_BLOCK)
- ✅ Эмоции видны в UI (avatar меняет цвет) и сохраняются между запросами
- ✅ Capability profile кешируется, не детектится заново каждый запрос

---

## Порядок реализации

1. **День 1-2:** EmotionEngine (2.1) — самый сложный модуль, regex + decay + persist
2. **День 3:** Personality + DisagreementAssessor (2.2) — быстрый, в основном данные
3. **День 4:** CapabilityProfiler (2.3) — системные вызовы + Ollama API
4. **День 5-6:** CognitivePlanner (2.4) — матрица tier×complexity
5. **День 7:** Интеграция в AgentRuntime + тесты + push

Каждый подспринт — отдельный коммит. После 2.1 и 2.2 можно остановиться и
протестировать (уже виден UX-эффект). 2.3 и 2.4 — оптимизация, не функционал.

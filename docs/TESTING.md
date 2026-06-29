# Testing — Aionet

Гайд по тестированию: что проверяет каждый тест-сьют, как запускать, как
интерпретировать метрики, как добавить новый тест.

---

## 1. Обзор тест-сьютов

| Тест | Тип | Кол-во | Что проверяет | Зависимости |
|---|---|---|---|---|
| `tests/test_sprint1.py` | Unit | 21 | LoopDetector, TaskComplexity, PromptBuilder | Только Python |
| `tests/test_memory_unit.py` | Unit | 12 | Ранжирование, забывание, GC, session isolation | FAISS + SQLite |
| `tests/test_integration.py` | Integration | 9 | End-to-end pipeline через ZMQ | Все сервисы подняты |
| `tests/test_load.py` | Load | — | Throughput + latency метрики | Все сервисы подняты |

**Итого: 42 теста, все проходят.**

---

## 2. Запуск тестов

### 2.1 Unit-тесты (без поднятых сервисов)

```bash
cd /home/z/my-project/local-ai-agent

# Sprint 1 фичи: LoopDetector + TaskComplexity + PromptBuilder
PYTHONPATH=python:proto/_gen python tests/test_sprint1.py

# Memory unit: ранжирование + забывание + GC
PYTHONPATH=python:proto/_gen python tests/test_memory_unit.py
```

Эти тесты **не требуют** поднятых ZMQ-сервисов. Они тестируют классы напрямую.
Для `test_memory_unit.py` нужен только FAISS + SQLite (создаются во временной
директории через `tempfile.mkdtemp()`).

### 2.2 Integration-тесты (требуют поднятых сервисов)

```bash
# 1. Поднять все сервисы
bash scripts/start_bg.sh

# 2. Запустить интеграционные тесты
PYTHONPATH=python:proto/_gen python tests/test_integration.py
```

Эти тесты отправляют реальные ZMQ-запросы к `agent_core` и проверяют
end-to-end pipeline. Используют `mock_ollama.py` — реальная LLM не нужна.

### 2.3 Нагрузочные тесты

```bash
bash scripts/start_bg.sh
PYTHONPATH=python:proto/_gen python tests/test_load.py
```

Измеряет throughput и latency: memory store/retrieve, LLM parallel, agent E2E.

### 2.4 Все тесты одной командой

```bash
cd /home/z/my-project/local-ai-agent
bash scripts/start_bg.sh && \
PYTHONPATH=python:proto/_gen python tests/test_sprint1.py && \
PYTHONPATH=python:proto/_gen python tests/test_memory_unit.py && \
PYTHONPATH=python:proto/_gen python tests/test_integration.py && \
PYTHONPATH=python:proto/_gen python tests/test_load.py
```

---

## 3. Что проверяет каждый тест-сьют

### 3.1 `test_sprint1.py` (21 тест)

**TaskComplexityClassifier (6 тестов):**
- `test_complexity_trivial` — "привет", "спасибо", "ок" → TRIVIAL
- `test_complexity_research` — "найди информацию", "последняя версия" → RESEARCH
- `test_complexity_complex` — "проанализируй", "сравни", "рефакторинг" → COMPLEX
- `test_complexity_moderate` — "напиши функцию", "создай docker-compose" → MODERATE
- `test_complexity_simple_question` — "Что такое HTTP?" → SIMPLE
- `test_complexity_defaults` — max_iter/max_tokens/tools/web_search per level

**LoopDetector (9 тестов):**
- `test_pattern_loop_detected` — 3× одинаковый (tool+args) → PATTERN signal
- `test_pattern_loop_not_triggered_different_args` — разные args → нет сигнала
- `test_pattern_loop_not_triggered_different_tools` — разные tools → нет сигнала
- `test_empty_loop_detected` — 3× null/none/[] → EMPTY signal
- `test_empty_loop_NOT_triggered_by_short_valid_results` — "OK"/"42"/"done" → нет сигнала
- `test_empty_loop_NOT_triggered_by_llm_errors` — timeout/ECONNREFUSED → нет сигнала
- `test_empty_loop_partial_llm_error_no_signal` — 2 пустых + 1 LLM-ошибка → нет сигнала
- `test_semantic_loop_detected` — 3 похожих thought (cosine ≥ 0.85) → SEMANTIC signal
- `test_semantic_loop_no_embed_fn_skipped` — без embed_fn → semantic пропускается
- `test_semantic_loop_different_thoughts_no_signal` — ортогональные thought → нет сигнала
- `test_detector_summary` — корректный summary для логов
- `test_detector_reset` — очистка истории

**SystemPromptBuilder (3 теста):**
- `test_prompt_builder_legacy_fallback` — пустой `system_prompt_static` → legacy
- `test_prompt_builder_split_mode` — задан static → split-режим
- `test_prompt_builder_empty_context` — пустой DynamicContext → None suffix

### 3.2 `test_memory_unit.py` (12 тестов)

**Базовый store/retrieve (3 теста):**
- `test_store_returns_id` — store возвращает непустой id
- `test_retrieve_returns_stored_text` — retrieve находит сохранённое
- `test_retrieve_empty_store` — пустой store возвращает []

**Кривая забывания Эббингауза (3 теста):**
- `test_forgetting_curve_decay` — старая запись с low importance → forget()
- `test_forgetting_keeps_recent_important` — свежая важная выживает
- `test_high_importance_survives_long_age` — importance=10 через 30 дней выживает
  (eff_imp = 10 × exp(-30/7) = 0.137 > 0.05)

**Инкрементальный GC (3 теста):**
- `test_soft_delete_doesnt_rebuild_immediately` — soft-delete без rebuild (< threshold)
- `test_threshold_triggers_rebuild` — при threshold → физическая перестройка
- `test_stats_includes_soft_deleted` — stats показывает soft_deleted + pending_rebuild

**Многоканальное ранжирование (3 теста):**
- `test_retrieve_returns_scored_records` — все score > 0, отсортированы по убыванию
- `test_session_isolation` — SQL pre-filter по session_id, записи из другой сессии не попадают
- `test_access_count_incremented` — retrieve увеличивает access_count

### 3.3 `test_integration.py` (9 сценариев)

End-to-end pipeline через ZMQ, mock-ollama:

1. **Simple greeting** — "привет" → 0 tool_calls, 30 tokens
2. **Tool call filesystem** — "перечисли файлы" → mock-LLM эмитит fs tool_call
3. **Tool call shell** — "посчитай 2+2" → mock-LLM эмитит shell tool_call
4. **Memory store + retrieve** — STORE ok, SQLite verified, STATS работает
5. **LLM Engine direct** — static_prefix + dynamic_suffix split
6. **Tools broker direct** — fs_list возвращает test.txt + config.json
7. **Complex full-pipeline** — memory→LLM→tools→avatar, 182ms
8. **Complexity classification** (через логи) — trivial + moderate видны
9. **LoopDetector initialization** (через логи) — HashEmbedder fallback активен

### 3.4 `test_load.py` (нагрузочные)

4 сценария с измерением throughput и latency (p50/p95/p99/max):

1. **Memory STORE throughput** — 100 ops, измеряет ops/sec + latency
2. **Memory RETRIEVE latency** — 50 retrieves после seeding 50 records
3. **LLM Engine parallel throughput** — 15 calls, 3 параллельных worker
4. **Agent Core E2E latency** — 10 requests через весь pipeline

---

## 4. Базовые метрики (mock-ollama)

Эти числа — **baseline** для тестового окружения без реальной LLM.
В проде с Ollama + 7B моделью ожидается 5-50× медленнее LLM-вызовы.

| Метрика | Значение | Комментарий |
|---|---|---|
| Memory STORE throughput | 879 ops/sec | HashEmbedder (без torch) |
| Memory STORE p95 | 1.3 ms | Включая embedding + FAISS add + SQLite insert |
| Memory RETRIEVE p95 | 1.2 ms | Включая embedding + FAISS search + SQL + ranking |
| LLM Engine throughput | 19 calls/sec | 3 параллельных worker, mock-ollama |
| LLM Engine p95 | 208 ms | Mock-ollama искусственная задержка |
| Agent E2E p95 | 58 ms | Полный pipeline: classify→memory→LLM→store→avatar |

### Что считать "нормальным"

- **Memory p95 < 5ms** — отлично. Если > 50ms — проверьте FAISS dim и
  количество записей (IndexFlatIP — O(N) search).
- **LLM p95 < 500ms** — mock-ollama. Реальная 7B модель: 2-10 сек на запрос.
- **Agent E2E p95 < 100ms** — mock-ollama. Реальная LLM: 5-30 сек.

### Что считать "плохо"

- **Memory p95 > 100ms** — возможно, SQLite заблокирован (check_same_thread)
  или FAISS перестраивается (проверьте `soft_deleted` в stats)
- **LLM timeout** — Ollama не отвечает. Проверьте `curl http://127.0.0.1:11434/api/tags`
- **Agent E2E > 30 сек** — LoopDetector должен был сработать. Проверьте логи.

---

## 5. Как интерпретировать результаты

### 5.1 Если тест упал

```bash
# 1. Посмотреть детали
PYTHONPATH=python:proto/_gen python tests/test_sprint1.py 2>&1 | tail -30

# 2. Проверить логи сервисов (для integration)
tail -50 logs/agent_core.log
tail -50 logs/llm_engine.log
tail -50 logs/tools.log

# 3. Проверить порты
ss -tln | grep -E ":555[0-5]|:8765|:11434"

# 4. Перезапустить сервисы
pkill -f "python -m"
bash scripts/start_bg.sh
```

### 5.2 Если метрики деградировали

Сравните с baseline из этого файла. Если деградация > 2×:

1. **Memory store медленнее** — проверьте `data/memory.sqlite` размер,
   возможно пора rebuild (проверьте `stats()` → `soft_deleted` vs `gc_rebuild_threshold`)
2. **Memory retrieve медленнее** — проверьте `index.ntotal`. Для > 100k
   записей нужен `IndexIVFFlat` вместо `IndexFlatIP`
3. **LLM медленнее** — проверьте `nvidia-smi` (GPU утилизация), возможно
   модель не влезает в VRAM и работает на CPU
4. **Agent E2E медленнее** — проверьте LoopDetector в логах, возможно
   агент делает больше итераций чем ожидалось

---

## 6. Как добавить новый тест

### 6.1 Unit-тест (без сервисов)

Добавьте функцию в соответствующий `tests/test_*.py`:

```python
def test_my_new_feature(tmp_path):
    """Что проверяет этот тест."""
    # setup
    store = make_store(tmp_path)
    # action
    result = store.do_something(...)
    # assertions
    assert result.is_ok, f"expected ok, got {result}"
    print(f"  ✓ my new feature works: {result}")
```

Не забудьте добавить в `_run_all()` список:

```python
tests = [
    ...,
    ("my_new_feature", lambda: test_my_new_feature(Path(tempfile.mkdtemp()))),
]
```

### 6.2 Integration-тест (с сервисами)

Добавьте функцию в `tests/test_integration.py`:

```python
def test_my_scenario(cfg) -> bool:
    header("TEST N: My scenario")
    client = make_agent_client(cfg)
    try:
        resp = send_request(client, "my test query")
        ok(f"response: {resp.final_text[:80]!r}")
        assert resp.final_text, "empty response"
        ok("ALL ASSERTIONS PASSED")
        return True
    except Exception as e:
        fail(f"FAILED: {e}")
        return False
    finally:
        client.close()
```

Добавьте в `tests` список в `main()`.

### 6.3 Нагрузочный тест

Добавьте функцию в `tests/test_load.py` по образцу `test_memory_store_throughput`:

```python
def test_my_metric(cfg, n: int = 100):
    header(f"MY METRIC (n={n})")
    client = ZMQClient(endpoint=cfg.zmq["..."], service_name="load")
    latencies = []
    t0 = time.time()
    for i in range(n):
        ts = time.time()
        # ... вызов
        latencies.append((time.time() - ts) * 1000)
    dt = time.time() - t0
    print(f"  Total: {n} ops in {dt:.2f}s → {n/dt:.1f} ops/sec")
    print(f"  Latency (ms): p50={percentile(latencies,0.5):.1f} "
          f"p95={percentile(latencies,0.95):.1f}")
    return n/dt
```

---

## 7. CI/CD рекомендации

Пока нет CI, но для будущей GitHub Actions:

```yaml
# .github/workflows/test.yml
name: Tests
on: [push, pull_request]
jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install pyzmq protobuf grpcio-tools requests faiss-cpu tenacity mcp websockets pydantic numpy
      - run: ./scripts/gen_proto.sh
      - run: PYTHONPATH=python:proto/_gen python tests/test_sprint1.py
      - run: PYTHONPATH=python:proto/_gen python tests/test_memory_unit.py

  integration:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install pyzmq protobuf grpcio-tools requests faiss-cpu tenacity mcp websockets pydantic numpy
      - run: ./scripts/gen_proto.sh
      - run: bash scripts/start_bg.sh
      - run: PYTHONPATH=python:proto/_gen python tests/test_integration.py
```

---

## 8. Известные ограничения тестов

1. **HashEmbedder не даёт семантического сходства** — тесты памяти проверяют
   структуру (store/retrieve/GC/session isolation), но не качество поиска.
   Для семантических тестов установите `sentence-transformers`.
2. **Mock-ollama rule-based** — эмитит tool_calls по ключевым словам, не
   понимает контекст. Для тестов reasoning нужен реальный LLM.
3. **Tauri-фронт не тестируется** — нет headless-браузерных тестов для UI.
   Тестируется только backend pipeline.
4. **Docker-песочница не тестируется** — в CI без Docker. `NoneSandbox`
   используется в тестах.
5. **Нагрузочные тесты не assertion-based** — они выводят метрики, но не
   падают если метрики деградировали. Сравнение с baseline — ручное.

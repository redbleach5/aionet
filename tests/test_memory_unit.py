"""Unit-тесты для FaissMemoryStore: ранжирование, кривая забывания, инкрементальный GC.

Покрывает:
  * Многоканальное ранжирование (semantic + recency + frequency)
  * Кривая забывания Эббингауза (importance * exp(-days / half_life))
  * Инкрементальный GC (soft-delete + threshold-based rebuild)
  * Статистику (total / soft_deleted / gc_pending_rebuild)
  * Изоляцию сессий (session_id filter)

Запуск:
    cd /home/z/my-project/local-ai-agent
    PYTHONPATH=python:proto/_gen python tests/test_memory_unit.py
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "proto" / "_gen"))
os.environ.setdefault("AIONET_CONFIG", str(ROOT / "config.toml"))

from common.config import Config, load_config
from common.interfaces import MemoryRecord
from memory.faiss_memory import FaissMemoryStore


# =============================================================================
# Helper — создаёт временный Config с тестовыми путями
# =============================================================================
def make_test_config(tmp_dir: Path, **overrides) -> Config:
    """Создаёт Config с временной SQLite + FAISS в tmp_dir."""
    cfg = load_config()
    cfg.raw["memory"]["index_path"] = str(tmp_dir / "memory.faiss")
    cfg.raw["memory"]["meta_db_path"] = str(tmp_dir / "memory.sqlite")
    cfg.raw["memory"]["gc_rebuild_threshold"] = overrides.get("gc_rebuild_threshold", 1000)
    cfg.raw["memory"]["forgetting_half_life_days"] = overrides.get(
        "forgetting_half_life_days", 7.0)
    cfg.raw["memory"]["forgetting_min_importance"] = overrides.get(
        "forgetting_min_importance", 0.05)
    cfg.raw["memory"]["gc_interval_minutes"] = overrides.get("gc_interval_minutes", 60)
    return cfg


def make_store(tmp_dir: Path, **overrides) -> FaissMemoryStore:
    cfg = make_test_config(tmp_dir, **overrides)
    # Чистим старые файлы
    for f in [tmp_dir / "memory.faiss", tmp_dir / "memory.sqlite"]:
        if f.exists():
            f.unlink()
    return FaissMemoryStore(cfg)


# =============================================================================
# Тесты: базовый store/retrieve
# =============================================================================
def test_store_returns_id(tmp_path):
    store = make_store(tmp_path)
    rid = store.store(session_id="s1", text="hello world")
    assert rid, "store should return non-empty id"
    print(f"  ✓ store returned id: {rid[:16]}")


def test_retrieve_returns_stored_text(tmp_path):
    store = make_store(tmp_path)
    store.store(session_id="s1", text="привет мир")
    results = store.retrieve(session_id="s1", text="привет мир", top_k=1)
    assert len(results) > 0, "should retrieve at least 1 record"
    assert "привет мир" in results[0].text
    print(f"  ✓ retrieved: {results[0].text!r}")


def test_retrieve_empty_store(tmp_path):
    store = make_store(tmp_path)
    results = store.retrieve(session_id="s1", text="anything", top_k=5)
    assert results == [], "empty store should return []"
    print("  ✓ empty store returns []")


# =============================================================================
# Тесты: кривая забывания Эббингауза
# =============================================================================
def test_forgetting_curve_decay(tmp_path):
    """Записи с малым importance + старой created_at → forget()."""
    store = make_store(tmp_path, forgetting_half_life_days=0.001,  # 1.4 минуты
                       forgetting_min_importance=0.5)
    # Создаём запись с importance=1.0, но "старая" (created_at в прошлом)
    rid = store.store(session_id="s1", text="old memory", importance=1.0)
    # Подделываем created_at в БД — 1 час назад
    one_hour_ago_ms = int((time.time() - 3600) * 1000)
    store._db.execute(
        "UPDATE memories SET created_at=? WHERE id=?",
        (one_hour_ago_ms, rid),
    )
    store._db.commit()
    # Запускаем GC
    deleted = store.forget(session_id="s1")
    assert deleted == 1, f"expected 1 deleted, got {deleted}"
    print(f"  ✓ forgotten: {deleted} record (importance decayed below threshold)")


def test_forgetting_keeps_recent_important(tmp_path):
    """Свежие важные записи не удаляются."""
    store = make_store(tmp_path, forgetting_half_life_days=7.0,
                       forgetting_min_importance=0.05)
    rid = store.store(session_id="s1", text="important recent", importance=1.0)
    # GC сразу — запись свежая, importance=1.0, должна выжить
    deleted = store.forget()
    assert deleted == 0, f"expected 0 deleted, got {deleted}"
    print(f"  ✓ kept: important recent record survived GC")


def test_forkeeping_high_importance_survives_long_age(tmp_path):
    """Запись с importance=10.0 должна выжить даже через 30 дней (half_life=7)."""
    store = make_store(tmp_path, forgetting_half_life_days=7.0,
                       forgetting_min_importance=0.05)
    rid = store.store(session_id="s1", text="very important old",
                      importance=10.0)
    # 30 дней назад
    thirty_days_ago_ms = int((time.time() - 30 * 86400) * 1000)
    store._db.execute("UPDATE memories SET created_at=? WHERE id=?",
                      (thirty_days_ago_ms, rid))
    store._db.commit()
    # eff_importance = 10.0 * exp(-30/7) = 10.0 * 0.0137 = 0.137 > 0.05
    deleted = store.forget()
    assert deleted == 0, f"expected 0 deleted (eff_imp=0.137>0.05), got {deleted}"
    print(f"  ✓ high-importance record survived 30 days (eff_imp=0.137 > 0.05)")


# =============================================================================
# Тесты: инкрементальный GC (soft-delete + threshold)
# =============================================================================
def test_soft_delete_doesnt_rebuild_immediately(tmp_path):
    """Soft-delete помечает deleted=1, но НЕ перестраивает FAISS (если < threshold).

    ВАЖНО: вызываем forget() БЕЗ session_id — иначе срабатывает forced-rebuild
    (по дизайну: forget(session_id) — это принудительная очистка сессии).
    """
    store = make_store(tmp_path, gc_rebuild_threshold=1000,
                       forgetting_half_life_days=0.001,
                       forgetting_min_importance=0.5)
    rid = store.store(session_id="s1", text="to be forgotten", importance=1.0)
    # Делаем запись "старой"
    one_hour_ago = int((time.time() - 3600) * 1000)
    store._db.execute("UPDATE memories SET created_at=? WHERE id=?",
                      (one_hour_ago, rid))
    store._db.commit()
    ntotal_before = store._index.ntotal
    # forget() без session_id — мягкий GC без forced rebuild
    deleted = store.forget()
    assert deleted == 1
    # FAISS не перестроен — вектор всё ещё там
    assert store._index.ntotal == ntotal_before, "FAISS should not be rebuilt below threshold"
    # Но в SQLite запись помечена deleted=1
    row = store._db.execute(
        "SELECT deleted FROM memories WHERE id=?", (rid,)
    ).fetchone()
    assert row[0] == 1, "record should be soft-deleted"
    print(f"  ✓ soft-deleted (FAISS ntotal={store._index.ntotal}, SQLite deleted=1)")


def test_threshold_triggers_rebuild(tmp_path):
    """При достижении gc_rebuild_threshold — физическая перестройка FAISS."""
    store = make_store(tmp_path, gc_rebuild_threshold=2,  # низкий порог для теста
                       forgetting_half_life_days=0.001,
                       forgetting_min_importance=0.5)
    # Создаём 3 записи, все "старые"
    for i in range(3):
        rid = store.store(session_id="s1", text=f"old {i}", importance=1.0)
        one_hour_ago = int((time.time() - 3600) * 1000)
        store._db.execute("UPDATE memories SET created_at=? WHERE id=?",
                          (one_hour_ago, rid))
    store._db.commit()
    ntotal_before = store._index.ntotal
    assert ntotal_before == 3
    # GC: soft-deletes 3, threshold=2 → rebuild
    deleted = store.forget()
    assert deleted == 3
    # После rebuild: FAISS пуст, все SQLite записи физически удалены
    assert store._index.ntotal == 0, f"expected 0 vectors after rebuild, got {store._index.ntotal}"
    count = store._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert count == 0, f"expected 0 records in SQLite, got {count}"
    assert store._deleted_count == 0, "deleted_count should be reset after rebuild"
    print(f"  ✓ threshold triggered rebuild (FAISS 3→0, SQLite 3→0, counter reset)")


def test_stats_includes_soft_deleted(tmp_path):
    """Stats показывает soft_deleted и gc_pending_rebuild."""
    store = make_store(tmp_path, gc_rebuild_threshold=100,
                       forgetting_half_life_days=0.001,
                       forgetting_min_importance=0.5)
    # Создаём 5 записей, 2 делаем "старыми"
    for i in range(5):
        rid = store.store(session_id="s1", text=f"record {i}", importance=1.0)
        if i < 2:
            one_hour_ago = int((time.time() - 3600) * 1000)
            store._db.execute("UPDATE memories SET created_at=? WHERE id=?",
                              (one_hour_ago, rid))
    store._db.commit()
    store.forget()  # soft-deletes 2
    stats = store.stats()
    assert stats["total"] == "3", f"expected 3 active, got {stats['total']}"
    assert stats["soft_deleted"] == "2", f"expected 2 soft-deleted, got {stats['soft_deleted']}"
    assert stats["gc_rebuild_threshold"] == "100"
    assert stats["gc_pending_rebuild"] == "98", f"expected 98 pending, got {stats['gc_pending_rebuild']}"
    print(f"  ✓ stats: active={stats['total']}, soft_deleted={stats['soft_deleted']}, "
          f"pending_rebuild={stats['gc_pending_rebuild']}")


# =============================================================================
# Тесты: многоканальное ранжирование
# =============================================================================
def test_retrieve_returns_scored_records(tmp_path):
    """Все возвращаемые записи имеют score > 0 (ранжирование отработало)."""
    store = make_store(tmp_path)
    for i in range(5):
        store.store(session_id="s1", text=f"document number {i} about Python")
    results = store.retrieve(session_id="s1", text="Python document", top_k=3)
    assert len(results) == 3
    for r in results:
        assert r.score > 0, f"record {r.id} has score={r.score}"
    # Проверим что они отсортированы по убыванию score
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True), f"not sorted desc: {scores}"
    print(f"  ✓ {len(results)} records retrieved, scores: {[f'{s:.3f}' for s in scores]}")


def test_session_isolation(tmp_path):
    """Retrieve для s1 возвращает только s1 записи (SQL pre-filter по session_id).

    ВАЖНО: с HashEmbedder (тестовый fallback без sentence-transformers) хеш-
    векторы могут давать semantic-сходство между разными текстами. Но SQL
    pre-filter по session_id гарантирует, что записи из s2 физически не
    попадут в результаты для s1 — даже если бы их векторы были близки.

    Проверяем через SQL, а не через semantic-сходство.
    """
    store = make_store(tmp_path)
    store.store(session_id="s1", text="unique alpha content for session one")
    store.store(session_id="s2", text="completely different beta gamma for two")
    # Retrieve для s1
    results = store.retrieve(session_id="s1", text="unique alpha", top_k=5)
    # Все возвращённые записи должны быть из s1 (SQL pre-filter)
    for r in results:
        # Проверяем в БД, что эта запись действительно из s1
        row = store._db.execute(
            "SELECT session_id FROM memories WHERE id=?", (r.id,)
        ).fetchone()
        assert row is not None, f"record {r.id} not found in DB"
        assert row[0] == "s1", f"cross-session leak: record {r.id} is from session {row[0]}, expected s1"
    print(f"  ✓ session isolation: {len(results)} records for s1, all from s1 (SQL pre-filter)")


def test_access_count_incremented(tmp_path):
    """Retrieve увеличивает access_count у возвращённых записей."""
    store = make_store(tmp_path)
    rid = store.store(session_id="s1", text="accessed record", importance=1.0)
    # Retrieve 3 раза
    for _ in range(3):
        store.retrieve(session_id="s1", text="accessed record", top_k=1)
    row = store._db.execute(
        "SELECT access_count FROM memories WHERE id=?", (rid,)
    ).fetchone()
    assert row[0] >= 3, f"expected access_count >= 3, got {row[0]}"
    print(f"  ✓ access_count incremented to {row[0]} after 3 retrieves")


# =============================================================================
# Запуск
# =============================================================================
def _run_all():
    tests = [
        ("store_returns_id",                    lambda: test_store_returns_id(Path(tempfile.mkdtemp()))),
        ("retrieve_returns_stored_text",        lambda: test_retrieve_returns_stored_text(Path(tempfile.mkdtemp()))),
        ("retrieve_empty_store",                lambda: test_retrieve_empty_store(Path(tempfile.mkdtemp()))),
        ("forgetting_curve_decay",              lambda: test_forgetting_curve_decay(Path(tempfile.mkdtemp()))),
        ("forgetting_keeps_recent_important",   lambda: test_forgetting_keeps_recent_important(Path(tempfile.mkdtemp()))),
        ("high_importance_survives_long_age",   lambda: test_forkeeping_high_importance_survives_long_age(Path(tempfile.mkdtemp()))),
        ("soft_delete_doesnt_rebuild",          lambda: test_soft_delete_doesnt_rebuild_immediately(Path(tempfile.mkdtemp()))),
        ("threshold_triggers_rebuild",          lambda: test_threshold_triggers_rebuild(Path(tempfile.mkdtemp()))),
        ("stats_includes_soft_deleted",         lambda: test_stats_includes_soft_deleted(Path(tempfile.mkdtemp()))),
        ("retrieve_returns_scored_records",     lambda: test_retrieve_returns_scored_records(Path(tempfile.mkdtemp()))),
        ("session_isolation",                   lambda: test_session_isolation(Path(tempfile.mkdtemp()))),
        ("access_count_incremented",            lambda: test_access_count_incremented(Path(tempfile.mkdtemp()))),
    ]
    passed = 0
    failed = 0
    for name, fn in tests:
        print(f"\n[+] {name}")
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"  ✗ FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ CRASH: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n{'='*60}")
    print(f"  Memory unit tests: PASSED {passed}/{passed+failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_all())

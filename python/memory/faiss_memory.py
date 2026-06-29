"""aionet.memory — FAISS + SQLite + многоканальный ретрив + забывание Эббингауза.

Реализация MemoryStore с тремя каналами ранжирования:
  * semantic   — косинусное сходство эмбеддингов (FAISS IndexFlatIP)
  * recency    — убывает по экспоненте от last_accessed (период half_life_days)
  * frequency  — нормированный access_count

Итоговый score = w_sem·semantic + w_rec·recency + w_freq·frequency.
Каналы суммируются; top_k берётся по итоговому скору.

Кривая забывания: importance(t) = importance_0 * exp(-days / half_life_days).
При importance < forgetting_min_importance запись удаляется (GC).
"""
from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np

from common.config import Config
from common.interfaces import MemoryRecord, MemoryStore
from common.logging import get_logger

log = get_logger(__name__)


class FaissMemoryStore(MemoryStore):
    """Долговременная память с векторным поиском и кривой забывания."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        mcfg = cfg.memory
        self.index_path = Path(mcfg.get("index_path", "./data/memory.faiss"))
        self.meta_db_path = Path(mcfg.get("meta_db_path", "./data/memory.sqlite"))
        self.embedding_model_name = mcfg.get("embedding_model", "all-MiniLM-L6-v2")
        self.embedding_dim = int(mcfg.get("embedding_dim", 384))
        self.top_k = int(mcfg.get("top_k", 5))
        self.channel_weights = mcfg.get("channel_weights", {
            "semantic": 0.6, "recency": 0.25, "frequency": 0.15,
        })
        self.half_life_days = float(mcfg.get("forgetting_half_life_days", 7.0))
        self.min_importance = float(mcfg.get("forgetting_min_importance", 0.05))

        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._embedder = None
        self._faiss = None
        self._init_faiss()
        self._init_db()
        self._last_gc = 0.0
        self._gc_interval = float(mcfg.get("gc_interval_minutes", 60)) * 60
        # Инкрементальный GC: soft-delete сразу, физическая перестройка индекса
        # только при достижении порога удалённых записей.
        # Это снижает нагрузку на больших объёмах (десятки/сотни тысяч записей).
        self._gc_rebuild_threshold = int(mcfg.get("gc_rebuild_threshold", 1000))
        self._deleted_count = 0  # кэш количества soft-deleted

    # ------------------------------------------------------------------
    # Инициализация
    # ------------------------------------------------------------------
    def _init_faiss(self):
        import faiss
        self._faiss = faiss
        if self.index_path.exists():
            self._index = faiss.read_index(str(self.index_path))
            log.info("FAISS index loaded: %d vectors", self._index.ntotal)
        else:
            self._index = faiss.IndexFlatIP(self.embedding_dim)
            log.info("FAISS index created (dim=%d)", self.embedding_dim)

    def _init_db(self):
        self._db = sqlite3.connect(str(self.meta_db_path), check_same_thread=False)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id           TEXT PRIMARY KEY,
                session_id   TEXT NOT NULL,
                text         TEXT NOT NULL,
                importance   REAL NOT NULL,
                created_at   INTEGER NOT NULL,
                last_accessed INTEGER NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                faiss_idx    INTEGER NOT NULL,
                deleted      INTEGER NOT NULL DEFAULT 0,
                deleted_at   INTEGER
            )
        """)
        # Миграция: добавляем колонки deleted/deleted_at если их нет (старые БД)
        try:
            self._db.execute("ALTER TABLE memories ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # колонка уже есть
        try:
            self._db.execute("ALTER TABLE memories ADD COLUMN deleted_at INTEGER")
        except sqlite3.OperationalError:
            pass
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_session ON memories(session_id)
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_deleted ON memories(deleted)
        """)
        self._db.commit()
        # Считаем soft-deleted для инкрементального GC
        row = self._db.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted=1"
        ).fetchone()
        self._deleted_count = row[0] if row else 0

    def _get_embedder(self):
        if self._embedder is None:
            # Сначала пытаемся sentence-transformers (прод), fallback на HashEmbedder (тесты)
            try:
                from sentence_transformers import SentenceTransformer
                self._embedder = SentenceTransformer(self.embedding_model_name)
                dim = self._embedder.get_sentence_embedding_dimension()
            except ImportError:
                from common.embedder import HashEmbedder
                log.warning("sentence-transformers unavailable; using HashEmbedder (test mode)")
                self._embedder = HashEmbedder(self.embedding_model_name)
                dim = self._embedder.get_sentence_embedding_dimension()
            if dim != self.embedding_dim:
                log.warning("embedding model dim=%d, config dim=%d; updating config",
                            dim, self.embedding_dim)
                self.embedding_dim = dim
        return self._embedder

    def _embed(self, text: str) -> np.ndarray:
        emb = self._get_embedder().encode(text, normalize_embeddings=True)
        return np.array([emb], dtype=np.float32)

    # ------------------------------------------------------------------
    # STORE
    # ------------------------------------------------------------------
    def store(self, *, session_id: str, text: str,
              metadata: Mapping[str, str] | None = None,
              importance: float = 1.0) -> str:
        with self._lock:
            rec_id = uuid.uuid4().hex
            now = int(time.time() * 1000)
            emb = self._embed(text)
            self._index.add(emb)
            faiss_idx = self._index.ntotal - 1
            self._db.execute(
                "INSERT INTO memories "
                "(id, session_id, text, importance, created_at, last_accessed, "
                " access_count, metadata_json, faiss_idx) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (rec_id, session_id, text, float(importance),
                 now, now, 0,
                 __import__("json").dumps(dict(metadata or {}), ensure_ascii=False),
                 faiss_idx),
            )
            self._db.commit()
            self._maybe_save_index()
            log.debug("stored id=%s session=%s faiss_idx=%d",
                      rec_id, session_id, faiss_idx)
            return rec_id

    # ------------------------------------------------------------------
    # RETRIEVE (многоканальный)
    # ------------------------------------------------------------------
    def retrieve(self, *, session_id: str | None, text: str,
                 top_k: int = 5,
                 channels: Iterable[str] | None = None) -> list[MemoryRecord]:
        with self._lock:
            if self._index.ntotal == 0:
                return []
            chans = list(channels) if channels else list(self.channel_weights.keys())
            k = min(max(top_k, self.top_k), self._index.ntotal)

            # 1) SEMANTIC: FAISS search
            emb = self._embed(text)
            D, I = self._index.search(emb, k)
            semantic_scores: dict[int, float] = {}
            for score, idx in zip(D[0].tolist(), I[0].tolist()):
                if idx >= 0:
                    semantic_scores[idx] = float(score)

            # 2) Грузим метаданные всех кандидатов (по faiss_idx)
            #    Фильтруем: deleted=0 (не soft-deleted) И session_id (если задан).
            #    SQL pre-filter гарантирует, что записи из другой сессии
            #    физически не попадут в результаты — даже если бы их векторы
            #    были близки. Это архитектурная изоляция чатов.
            if not semantic_scores:
                return []
            placeholders = ",".join("?" * len(semantic_scores))
            params: list = list(semantic_scores.keys())
            session_clause = ""
            if session_id is not None:
                session_clause = " AND session_id=?"
                params.append(session_id)
            rows = self._db.execute(
                f"SELECT id, session_id, text, importance, created_at, "
                f"last_accessed, access_count, metadata_json, faiss_idx, deleted "
                f"FROM memories WHERE faiss_idx IN ({placeholders}) "
                f"AND deleted=0{session_clause}",
                tuple(params),
            ).fetchall()

            now_ms = int(time.time() * 1000)
            max_count = max((r[6] for r in rows), default=1) or 1

            records: list[MemoryRecord] = []
            for r in rows:
                (rid, sid, rtext, importance, created, last_acc,
                 count, meta_json, faiss_idx, _deleted) = r
                # SEMANTIC
                s_sem = semantic_scores.get(faiss_idx, 0.0)
                # RECENCY: exp(-days / half_life)
                days = max((now_ms - last_acc) / 86_400_000.0, 0.0)
                s_rec = math.exp(-days / max(self.half_life_days, 1e-3))
                # FREQUENCY: нормированный access_count
                s_freq = count / max_count
                # Итог
                score = 0.0
                if "semantic" in chans:
                    score += self.channel_weights.get("semantic", 0.6) * s_sem
                if "recency" in chans:
                    score += self.channel_weights.get("recency", 0.25) * s_rec
                if "frequency" in chans:
                    score += self.channel_weights.get("frequency", 0.15) * s_freq
                # Session_id уже отфильтрован в SQL pre-filter выше —
                # здесь все записи гарантированно из нужной сессии (если задана).
                records.append(MemoryRecord(
                    id=rid, text=rtext, score=score,
                    importance=importance, created_at=created,
                    last_accessed=last_acc, access_count=count,
                    metadata=__import__("json").loads(meta_json),
                ))

            records.sort(key=lambda r: r.score, reverse=True)
            top = records[:top_k]

            # Обновляем last_accessed и access_count для выданных записей.
            for r in top:
                self._db.execute(
                    "UPDATE memories SET last_accessed=?, access_count=access_count+1 "
                    "WHERE id=?",
                    (now_ms, r.id),
                )
                r.last_accessed = now_ms
                r.access_count += 1
            self._db.commit()

            # Фоновый GC, если пора.
            self._maybe_gc(now_ms)
            return top

    # ------------------------------------------------------------------
    # FORGET / GC
    # ------------------------------------------------------------------
    def forget(self, *, session_id: str | None = None) -> int:
        with self._lock:
            now_ms = int(time.time() * 1000)
            return self._gc(now_ms, session_id)

    def _maybe_gc(self, now_ms: int) -> None:
        """Периодически запускает GC (soft-delete). Перестройка индекса —
        только при достижении порога _gc_rebuild_threshold удалённых записей.
        """
        if now_ms - self._last_gc < self._gc_interval:
            return
        self._last_gc = now_ms
        try:
            self._gc(now_ms, None)
        except Exception:
            log.exception("GC failed")

    def _gc(self, now_ms: int, session_id: str | None) -> int:
        """Инкрементальный GC:

        1) Soft-delete: помечаем записи с importance < min как deleted=1
           (быстро, O(N) SQL, не трогает FAISS)
        2) Если количество soft-deleted >= threshold — физически удаляем
           из SQLite и перестраиваем FAISS-индекс (дорого, но редко)
        3) Если session_id задан — GC только для этой сессии (принудительный)
        """
        # 1) Находим кандидатов на удаление (только не удалённые)
        rows = self._db.execute(
            "SELECT id, importance, created_at FROM memories "
            "WHERE deleted=0"
            + (" AND session_id=?" if session_id else ""),
            ((session_id,) if session_id else ()),
        ).fetchall()
        to_soft_delete: list[str] = []
        for rid, imp, created in rows:
            days = max((now_ms - created) / 86_400_000.0, 0.0)
            eff_imp = float(imp) * math.exp(-days / max(self.half_life_days, 1e-3))
            if eff_imp < self.min_importance:
                to_soft_delete.append(rid)
        if not to_soft_delete:
            return 0
        # Soft-delete: помечаем deleted=1, ставим deleted_at
        placeholders = ",".join("?" * len(to_soft_delete))
        self._db.execute(
            f"UPDATE memories SET deleted=1, deleted_at=? WHERE id IN ({placeholders})",
            (now_ms, *to_soft_delete),
        )
        self._db.commit()
        self._deleted_count += len(to_soft_delete)
        log.info("GC soft-deleted %d memories (total soft-deleted: %d, threshold: %d)",
                 len(to_soft_delete), self._deleted_count, self._gc_rebuild_threshold)

        # 2) Если накопилось много soft-deleted — физическая перестройка
        if self._deleted_count >= self._gc_rebuild_threshold:
            log.info("GC threshold reached (%d >= %d), rebuilding FAISS index...",
                     self._deleted_count, self._gc_rebuild_threshold)
            self._rebuild_index()
        # Принудительный GC по session_id — тоже перестраивает (маловероятно, но безопасно)
        elif session_id is not None and len(to_soft_delete) > 0:
            # Если пользователь явно попросил forget(session_id) — удаляем физически
            log.info("forced GC for session %s, rebuilding index", session_id[:16])
            self._rebuild_index()
        return len(to_soft_delete)

    def _rebuild_index(self) -> None:
        """Перестраивает индекс из активных (не deleted) записей.

        IndexFlatIP не поддерживает удаление векторов, поэтому:
          1) Удаляем все deleted=1 записи из SQLite
          2) Перестраиваем FAISS из оставшихся (deleted=0)
          3) Сбрасываем счётчик _deleted_count
        """
        # Физически удаляем soft-deleted
        deleted_count = self._db.execute(
            "DELETE FROM memories WHERE deleted=1"
        ).rowcount
        self._db.commit()
        log.info("physically removed %d soft-deleted records", deleted_count)

        # Перестраиваем FAISS из активных записей
        rows = self._db.execute(
            "SELECT id, text FROM memories WHERE deleted=0 ORDER BY faiss_idx"
        ).fetchall()
        if not rows:
            self._index = self._faiss.IndexFlatIP(self.embedding_dim)
        else:
            embs = np.vstack([self._embed(text)[0] for _, text in rows])
            self._index = self._faiss.IndexFlatIP(self.embedding_dim)
            self._index.add(embs)
            # Обновляем faiss_idx в БД.
            for new_idx, (rid, _) in enumerate(rows):
                self._db.execute(
                    "UPDATE memories SET faiss_idx=? WHERE id=?",
                    (new_idx, rid),
                )
            self._db.commit()
        self._deleted_count = 0  # сбрасываем счётчик после rebuild
        self._maybe_save_index()

    def _maybe_save_index(self) -> None:
        try:
            self._faiss.write_index(self._index, str(self.index_path))
        except Exception:
            log.exception("FAISS write_index failed")

    # ------------------------------------------------------------------
    # STATS
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, str]:
        with self._lock:
            n_active = self._db.execute(
                "SELECT COUNT(*) FROM memories WHERE deleted=0"
            ).fetchone()[0]
            n_deleted = self._db.execute(
                "SELECT COUNT(*) FROM memories WHERE deleted=1"
            ).fetchone()[0]
            by_session = self._db.execute(
                "SELECT session_id, COUNT(*) FROM memories WHERE deleted=0 "
                "GROUP BY session_id"
            ).fetchall()
            return {
                "total": str(n_active),
                "soft_deleted": str(n_deleted),
                "faiss_total": str(self._index.ntotal),
                "gc_rebuild_threshold": str(self._gc_rebuild_threshold),
                "gc_pending_rebuild": str(max(0, self._gc_rebuild_threshold - n_deleted)),
                "sessions": ";".join(f"{sid}:{cnt}" for sid, cnt in by_session),
                "embedding_dim": str(self.embedding_dim),
                "embedding_model": self.embedding_model_name,
            }

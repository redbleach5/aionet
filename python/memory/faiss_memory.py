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
                faiss_idx    INTEGER NOT NULL
            )
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_session ON memories(session_id)
        """)
        self._db.commit()

    def _get_embedder(self):
        if self._embedder is None:
            from sentence_transformers import SentenceTransformer
            self._embedder = SentenceTransformer(self.embedding_model_name)
            # Проверяем размерность.
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
            if not semantic_scores:
                return []
            placeholders = ",".join("?" * len(semantic_scores))
            rows = self._db.execute(
                f"SELECT id, session_id, text, importance, created_at, "
                f"last_accessed, access_count, metadata_json, faiss_idx "
                f"FROM memories WHERE faiss_idx IN ({placeholders})",
                tuple(semantic_scores.keys()),
            ).fetchall()

            now_ms = int(time.time() * 1000)
            max_count = max((r[6] for r in rows), default=1) or 1

            records: list[MemoryRecord] = []
            for r in rows:
                (rid, sid, rtext, importance, created, last_acc,
                 count, meta_json, faiss_idx) = r
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
                # Фильтр по session_id (если задан)
                if session_id is not None and sid != session_id:
                    # Можно ослабить: понижать score, но не исключать.
                    score *= 0.3
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
        if now_ms - self._last_gc < self._gc_interval:
            return
        self._last_gc = now_ms
        try:
            self._gc(now_ms, None)
        except Exception:
            log.exception("GC failed")

    def _gc(self, now_ms: int, session_id: str | None) -> int:
        """Удаляет записи с importance < min после применения кривой забывания."""
        rows = self._db.execute(
            "SELECT id, importance, created_at, faiss_idx FROM memories"
            + (" WHERE session_id=?" if session_id else ""),
            ((session_id,) if session_id else ()),
        ).fetchall()
        to_delete: list[str] = []
        for rid, imp, created, faiss_idx in rows:
            days = max((now_ms - created) / 86_400_000.0, 0.0)
            eff_imp = float(imp) * math.exp(-days / max(self.half_life_days, 1e-3))
            if eff_imp < self.min_importance:
                to_delete.append(rid)
        if not to_delete:
            return 0
        placeholders = ",".join("?" * len(to_delete))
        self._db.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})",
            tuple(to_delete),
        )
        self._db.commit()
        log.info("GC deleted %d memories", len(to_delete))
        # Перестраиваем FAISS-индекс без удалённых векторов.
        self._rebuild_index()
        return len(to_delete)

    def _rebuild_index(self) -> None:
        """Перестраивает индекс из оставшихся записей (IndexFlatIP не поддерживает удаление)."""
        rows = self._db.execute(
            "SELECT id, text FROM memories ORDER BY faiss_idx"
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
            n = self._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            by_session = self._db.execute(
                "SELECT session_id, COUNT(*) FROM memories GROUP BY session_id"
            ).fetchall()
            return {
                "total": str(n),
                "faiss_total": str(self._index.ntotal),
                "sessions": ";".join(f"{sid}:{cnt}" for sid, cnt in by_session),
                "embedding_dim": str(self.embedding_dim),
                "embedding_model": self.embedding_model_name,
            }

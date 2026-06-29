"""Лёгкий embedding-провайдер для тестового окружения без sentence-transformers.

В проде используется `sentence_transformers.SentenceTransformer('all-MiniLM-L6-v2')`
(384-мерные нормализованные векторы). Для тестов мы используем детерминированный
хеш-эмбеддер на numpy — он не даёт семантического сходства, но:
  * имеет фиксированную размерность (384, как у MiniLM)
  * детерминирован (одинаковый текст → одинаковый вектор)
  * нормализован (L2=1, совместим с IndexFlatIP)
  * НЕ требует torch

Это позволяет прогнать весь pipeline без установки тяжёлых зависимостей.
Семантическое качество теряется, но структурно всё работает.
"""
from __future__ import annotations

import hashlib
import numpy as np


EMBED_DIM = 384  # совпадает с all-MiniLM-L6-v2


def hash_embed(text: str, dim: int = EMBED_DIM) -> np.ndarray:
    """Детерминированный хеш-эмбеддинг: text → dim-мерный L2-нормализованный вектор.

    Использует SHA-256 для генерации enough-энтропии, потом растягивает в dim.
    Одинаковый текст → одинаковый вектор. Разные тексты → разные (но не семантически) векторы.
    """
    if not text:
        return np.zeros(dim, dtype=np.float32)
    # Нам нужны dim float32 значений = dim*4 байта. Генерим столько хеш-байтов.
    needed_bytes = dim * 4
    h = b""
    counter = 0
    while len(h) < needed_bytes:
        h += hashlib.sha256(f"{text}#{counter}".encode("utf-8")).digest()
        counter += 1
    # Интерпретируем первые dim*4 байтов КАК dim float32 значений
    arr = np.frombuffer(h[:needed_bytes], dtype=np.float32).copy()
    # На случай если dim не подошёл (маловато байтов) — берём первые dim
    if len(arr) != dim:
        # fallback: интерпретируем как uint8 и берём dim элементов
        arr = np.frombuffer(h[:needed_bytes], dtype=np.uint8).astype(np.float32)[:dim]
    # Нормализуем: subtract mean, divide by L2 (для совместимости с IndexFlatIP)
    arr = arr - arr.mean()
    norm = np.linalg.norm(arr)
    if norm < 1e-9:
        return np.zeros(dim, dtype=np.float32)
    arr = arr / norm
    return arr.astype(np.float32)


class HashEmbedder:
    """Drop-in замена для SentenceTransformer в memory-сервисе.

    Совместим с интерфейсом: embedder.encode(text, normalize_embeddings=True) → np.ndarray
    """

    def __init__(self, model_name: str = "hash-embedder"):
        self.model_name = model_name
        self._dim = EMBED_DIM

    def get_sentence_embedding_dimension(self) -> int:
        return self._dim

    def encode(self, text, normalize_embeddings: bool = True):
        if isinstance(text, str):
            v = hash_embed(text, self._dim)
            return v if normalize_embeddings else v * 1.0
        # list of strings
        return np.vstack([hash_embed(t, self._dim) for t in text])


def make_embedder(model_name: str = "all-MiniLM-L6-v2"):
    """Фабрика: пытается загрузить sentence-transformers, падает на HashEmbedder.

    Используется в:
      * python/memory/faiss_memory.py (через patched import)
      * python/agent_core/agent.py (_build_embed_fn для LoopDetector'а)
    """
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(model_name)
    except ImportError:
        return HashEmbedder(model_name)

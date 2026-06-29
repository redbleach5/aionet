"""Ленивая работа с protobuf-схемой.

Если messages_pb2 сгенерирован заранее (через scripts/gen_proto.sh) —
импортируем его. Иначе — генерируем на лету через protoc из grpcio-tools,
чтобы разработчик мог сразу запустить сервисы без дополнительного шага.
"""
from __future__ import annotations

import importlib
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

PROTO_DIR = Path(__file__).resolve().parents[2] / "proto"
PROTO_FILE = PROTO_DIR / "messages.proto"
GEN_DIR = PROTO_DIR / "_gen"

# Подкладываем _gen в sys.path, чтобы импорт работал без установки пакета.
if str(GEN_DIR) not in sys.path:
    sys.path.insert(0, str(GEN_DIR))

_pb2: Any = None


def _ensure_pb2() -> Any:
    global _pb2
    if _pb2 is not None:
        return _pb2
    try:
        _pb2 = importlib.import_module("messages_pb2")
        return _pb2
    except ImportError:
        pass
    # Падаем назад к runtime-генерации.
    _generate_runtime()
    _pb2 = importlib.import_module("messages_pb2")
    return _pb2


def _generate_runtime() -> None:
    GEN_DIR.mkdir(parents=True, exist_ok=True)
    from grpc_tools import protoc  # отложенный импорт — тяжёлая зависимость
    rc = protoc.main([
        "grpc_tools.protoc",
        f"-I{PROTO_DIR}",
        f"--python_out={GEN_DIR}",
        str(PROTO_FILE),
    ])
    if rc != 0:
        raise RuntimeError(f"protoc failed (rc={rc}) for {PROTO_FILE}")


def _pb():
    return _ensure_pb2()


# =============================================================================
# Удобные обёртки
# =============================================================================
class PayloadType:
    AGENT_REQUEST = 1
    AGENT_RESPONSE = 2
    LLM_CALL = 3
    LLM_RESULT = 4
    MEMORY_OP = 5
    MEMORY_RESULT = 6
    TOOL_CALL = 7
    TOOL_RESULT = 8
    AVATAR_CMD = 9
    AVATAR_EVENT = 10
    ERROR = 99


_PAYLOAD_MAP: dict[int, str] = {
    1: "AgentRequest",
    2: "AgentResponse",
    3: "LLMCall",
    4: "LLMResult",
    5: "MemoryOp",
    6: "MemoryResult",
    7: "ToolCallMessage",
    8: "ToolResultMessage",
    9: "AvatarCommand",
    10: "AvatarEvent",
    99: "ErrorPayload",
}


def make_envelope(
    *,
    source: str,
    target: str,
    payload_type: int,
    payload: bytes,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> bytes:
    """Собирает и сериализует Envelope. Возвращает байты для отправки в ZMQ."""
    pb = _pb()
    env = pb.Envelope()
    env.trace_id = trace_id or uuid.uuid4().hex[:16]
    env.span_id = span_id or uuid.uuid4().hex[:8]
    env.source = source
    env.target = target
    env.timestamp = int(time.time() * 1000)
    env.payload = payload
    env.type = payload_type
    return env.SerializeToString()


def parse_envelope(data: bytes):
    pb = _pb()
    env = pb.Envelope()
    env.ParseFromString(data)
    return env


def parse_payload(env) -> Any:
    """Десериализует body-envelope в конкретный protobuf-класс по полю type."""
    pb = _pb()
    name = _PAYLOAD_MAP.get(env.type)
    if not name:
        raise ValueError(f"unknown payload type: {env.type}")
    cls = getattr(pb, name)
    msg = cls()
    msg.ParseFromString(env.payload)
    return msg


def build_payload(payload_type: int, **kwargs):
    """Создаёт и сериализует конкретный payload-класс."""
    pb = _pb()
    name = _PAYLOAD_MAP[payload_type]
    cls = getattr(pb, name)
    msg = cls(**kwargs)
    return msg.SerializeToString()


# Лёгкий шорткат для прямого доступа к классам (например, для agent_core)
def __getattr__(name: str):
    return getattr(_pb(), name)

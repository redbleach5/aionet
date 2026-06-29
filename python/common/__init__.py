"""aionet.common — общие утилиты всех микросервисов.

Экспортирует:
  *_interfaces* — абстрактные базовые классы для всех заменяемых компонентов;
  *zmq_transport* — обёртка над ZeroMQ REQ/REP/PUB/SUB с поддержкой Protobuf Envelope;
  *proto* — ленивая загрузка сгенерированного messages_pb2 (или регенерация на лету);
  *config* — TOML-конфиг с типизированным доступом;
  *logging* — единый логгер с trace_id-контекстом.
"""

from .config import Config, load_config
from .logging import get_logger, trace_context
from .proto import (
    # Классы/функции, гарантированно определённые в proto.py:
    PayloadType, make_envelope, parse_envelope, parse_payload, build_payload,
)
from .zmq_transport import ZMQServer, ZMQClient, ZMQPublisher, ZMQSubscriber

__all__ = [
    "Config", "load_config",
    "get_logger", "trace_context",
    "PayloadType", "make_envelope", "parse_envelope",
    "parse_payload", "build_payload",
    "ZMQServer", "ZMQClient", "ZMQPublisher", "ZMQSubscriber",
]

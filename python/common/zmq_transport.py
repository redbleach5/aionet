"""ZeroMQ-обёртки для синхронного (REQ/REP) и асинхронного (PUB/SUB) транспорта.

Все сообщения — это сериализованный protobuf-Envelope (см. common.proto).
"""
from __future__ import annotations

import time
import uuid
from typing import Callable

import zmq

from .logging import get_logger, trace_context
from .proto import (
    PayloadType,
    make_envelope,
    parse_envelope,
    parse_payload,
    build_payload,
)

log = get_logger(__name__)


# =============================================================================
# Сервер: REP-сокет. Обрабатывает входящие Envelope'ы через handler-функцию.
# =============================================================================
class ZMQServer:
    def __init__(
        self,
        *,
        endpoint: str,
        service_name: str,
        handler: Callable[[object, object], bytes],
        linger_ms: int = 1000,
        rcvtimeo_ms: int = 30000,
    ):
        """handler signature: (envelope, payload) -> serialized_payload_bytes."""
        self.endpoint = endpoint
        self.service_name = service_name
        self.handler = handler
        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.REP)
        self._sock.setsockopt(zmq.LINGER, linger_ms)
        self._sock.setsockopt(zmq.RCVTIMEO, rcvtimeo_ms)
        self._running = False

    def serve_forever(self) -> None:
        self._sock.bind(self.endpoint)
        self._running = True
        log.info("ZMQServer[%s] bound to %s", self.service_name, self.endpoint)
        while self._running:
            try:
                msg = self._sock.recv()
            except zmq.Again:
                continue
            except zmq.ZMQError as e:
                log.error("recv error: %s", e)
                break
            try:
                env = parse_envelope(msg)
                payload = parse_payload(env)
                with trace_context(env.trace_id, env.span_id):
                    log.debug("recv %s from %s", type(payload).__name__, env.source)
                    reply_payload = self.handler(env, payload)
                reply = make_envelope(
                    source=self.service_name,
                    target=env.source,
                    payload_type=_reply_type_for(env.type),
                    payload=reply_payload,
                    trace_id=env.trace_id,
                    span_id=env.span_id,
                )
                self._sock.send(reply)
            except Exception as e:
                log.exception("handler failed: %s", e)
                err = build_payload(
                    PayloadType.ERROR,
                    code="INTERNAL",
                    message=str(e),
                )
                reply = make_envelope(
                    source=self.service_name,
                    target="*",
                    payload_type=PayloadType.ERROR,
                    payload=err,
                    trace_id=env.trace_id if 'env' in locals() else "",
                    span_id=env.span_id if 'env' in locals() else "",
                )
                self._sock.send(reply)

    def stop(self) -> None:
        self._running = False
        try:
            self._sock.close(0)
        except Exception:
            pass


def _reply_type_for(req_type: int) -> int:
    return {
        PayloadType.AGENT_REQUEST: PayloadType.AGENT_RESPONSE,
        PayloadType.LLM_CALL: PayloadType.LLM_RESULT,
        PayloadType.MEMORY_OP: PayloadType.MEMORY_RESULT,
        PayloadType.TOOL_CALL: PayloadType.TOOL_RESULT,
    }.get(req_type, PayloadType.ERROR)


# =============================================================================
# Клиент: REQ. Синхронный запрос→ответ.
# =============================================================================
class ZMQClient:
    def __init__(
        self,
        *,
        endpoint: str,
        service_name: str,
        linger_ms: int = 1000,
        rcvtimeo_ms: int = 30000,
    ):
        self.endpoint = endpoint
        self.service_name = service_name
        self._ctx = zmq.Context.instance()
        self._linger = linger_ms
        self._rcvtimeo = rcvtimeo_ms
        self._sock: zmq.Socket | None = None

    def _ensure(self) -> zmq.Socket:
        if self._sock is None or self._sock.closed:
            s = self._ctx.socket(zmq.REQ)
            s.setsockopt(zmq.LINGER, self._linger)
            s.setsockopt(zmq.RCVTIMEO, self._rcvtimeo_ms)
            s.connect(self.endpoint)
            self._sock = s
        return self._sock

    def call(
        self,
        *,
        target: str,
        payload_type: int,
        payload: bytes,
        trace_id: str | None = None,
        span_id: str | None = None,
    ):
        env_bytes = make_envelope(
            source=self.service_name,
            target=target,
            payload_type=payload_type,
            payload=payload,
            trace_id=trace_id,
            span_id=span_id,
        )
        sock = self._ensure()
        sock.send(env_bytes)
        reply = sock.recv()
        # После каждого REQ/REP цикла переоткрываем сокет, чтобы избежать
        # состояния "in req state" при повторных вызовах.
        sock.close(0)
        self._sock = None
        env = parse_envelope(reply)
        if env.type == PayloadType.ERROR:
            err = parse_payload(env)
            raise RuntimeError(f"{err.code}: {err.message}")
        return parse_payload(env)

    def close(self) -> None:
        if self._sock and not self._sock.closed:
            self._sock.close(0)


# =============================================================================
# Publisher / Subscriber для аватара (команды/события).
# =============================================================================
class ZMQPublisher:
    def __init__(self, *, endpoint: str, service_name: str, linger_ms: int = 1000):
        self.endpoint = endpoint
        self.service_name = service_name
        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.PUB)
        self._sock.setsockopt(zmq.LINGER, linger_ms)
        self._sock.bind(self.endpoint)
        # PUB теряет первые сообщения до подключения SUB — даём 200мс на握手.
        time.sleep(0.2)
        log.info("PUB[%s] bound %s", self.service_name, self.endpoint)

    def publish(self, *, target: str, payload_type: int, payload: bytes,
                trace_id: str | None = None, span_id: str | None = None) -> None:
        env = make_envelope(
            source=self.service_name, target=target,
            payload_type=payload_type, payload=payload,
            trace_id=trace_id, span_id=span_id,
        )
        self._sock.send(env)

    def close(self) -> None:
        self._sock.close(0)


class ZMQSubscriber:
    def __init__(self, *, endpoint: str, service_name: str,
                 handler: Callable[[object, object], None]):
        self.endpoint = endpoint
        self.service_name = service_name
        self.handler = handler
        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.SUB)
        self._sock.setsockopt(zmq.SUBSCRIBE, b"")
        self._sock.connect(self.endpoint)
        self._running = False

    def serve_forever(self) -> None:
        self._running = True
        log.info("SUB[%s] connected %s", self.service_name, self.endpoint)
        poller = zmq.Poller()
        poller.register(self._sock, zmq.POLLIN)
        while self._running:
            events = dict(poller.poll(500))
            if self._sock not in events:
                continue
            msg = self._sock.recv()
            try:
                env = parse_envelope(msg)
                payload = parse_payload(env)
                with trace_context(env.trace_id, env.span_id):
                    self.handler(env, payload)
            except Exception:
                log.exception("subscriber handler failed")

    def stop(self) -> None:
        self._running = False
        self._sock.close(0)

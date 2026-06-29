"""WebSocket-мост аватара.

Принимает AvatarCommand из ZeroMQ SUB-сокета и форвардит их в подключённые
WebSocket-клиенты (Tauri/Three.js). В обратную сторону — события от аватара
публикуются в ZeroMQ PUB для агента.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from common.config import Config, load_config
from common.logging import get_logger, trace_context
from common.proto import PayloadType, build_payload, parse_payload
from common.zmq_transport import ZMQPublisher, ZMQSubscriber

log = get_logger(__name__)


class AvatarBridge:
    """Связывает ZeroMQ-мир (protobuf) с WebSocket-миром (JSON)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        acfg = cfg.avatar
        self.ws_host = acfg.get("ws_listen_host", "127.0.0.1")
        self.ws_port = int(acfg.get("ws_listen_port", 8765))
        # ZMQ: подписываемся на команды от агента, публикуем события от аватара.
        self._sub = ZMQSubscriber(
            endpoint=cfg.zmq["avatar_cmd_endpoint"],
            service_name="avatar_bridge",
            handler=self._on_zmq_cmd,
        )
        self._evt_pub = ZMQPublisher(
            endpoint=cfg.zmq["avatar_evt_endpoint"],
            service_name="avatar_bridge",
        )
        self._ws_clients: set = set()
        # Кольцевой буфер на случай, если WS-клиентов нет (256 сообщений).
        from collections import deque
        self._buffer: deque = deque(maxlen=256)
        self._loop: asyncio.AbstractEventLoop | None = None

    def _on_zmq_cmd(self, env, payload) -> None:
        """ZMQ SUB callback (вызывается в потоке ZMQSubscriber)."""
        action_map = {0: "speak", 1: "emote", 2: "gesture", 3: "idle", 4: "look_at"}
        msg = {
            "type": "command",
            "action": action_map.get(payload.action, "idle"),
            "text": payload.text,
            "emotion": payload.emotion,
            "gesture": payload.gesture,
            "x": payload.x, "y": payload.y, "z": payload.z,
            "trace_id": env.trace_id,
            "ts": int(time.time() * 1000),
        }
        self._buffer.append(msg)
        if self._loop is None:
            return
        # Переключаемся в event-loop, чтобы отправить в WS-клиенты.
        asyncio.run_coroutine_threadsafe(self._broadcast(msg), self._loop)

    async def _broadcast(self, msg: dict) -> None:
        if not self._ws_clients:
            return
        data = json.dumps(msg, ensure_ascii=False)
        dead = []
        for ws in list(self._ws_clients):
            try:
                await ws.send(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.discard(ws)

    # ------------------------------------------------------------------
    # WebSocket side
    # ------------------------------------------------------------------
    async def _ws_handler(self, ws, path: str = "/"):
        self._ws_clients.add(ws)
        log.info("WS client connected (%s); total=%d",
                 ws.remote_address if hasattr(ws, "remote_address") else "?",
                 len(self._ws_clients))
        # Отправляем дамп буфера — чтобы новый клиент получил последние команды.
        try:
            for m in list(self._buffer)[-16:]:
                await ws.send(json.dumps(m, ensure_ascii=False))
        except Exception:
            pass
        try:
            async for raw in ws:
                # События от аватара — форвардим в ZMQ PUB.
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._forward_event(evt)
        except Exception:
            log.exception("WS handler error")
        finally:
            self._ws_clients.discard(ws)
            log.info("WS client disconnected; total=%d", len(self._ws_clients))

    def _forward_event(self, evt: dict) -> None:
        try:
            payload = build_payload(
                PayloadType.AVATAR_EVENT,
                event=evt.get("event", "unknown"),
                data={k: str(v) for k, v in evt.get("data", {}).items()},
            )
            self._evt_pub.publish(
                target="agent_core",
                payload_type=PayloadType.AVATAR_EVENT,
                payload=payload,
            )
        except Exception:
            log.exception("forward_event failed")

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def run(self) -> None:
        import websockets
        # Запускаем ZMQ SUB в отдельном потоке.
        import threading
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        sub_thread = threading.Thread(target=self._sub.serve_forever, daemon=True)
        sub_thread.start()

        async def ws_main():
            async with websockets.serve(
                self._ws_handler, self.ws_host, self.ws_port,
                max_size=2**20,
            ):
                log.info("Avatar WS bridge listening ws://%s:%d",
                         self.ws_host, self.ws_port)
                # Ждем вечно.
                await asyncio.Future()

        try:
            self._loop.run_until_complete(ws_main())
        except KeyboardInterrupt:
            log.info("Avatar bridge shutting down")
        finally:
            self._sub.stop()
            try:
                self._evt_pub.close()
            except Exception:
                pass


def main():
    cfg = load_config()
    bridge = AvatarBridge(cfg)
    bridge.run()


if __name__ == "__main__":
    main()

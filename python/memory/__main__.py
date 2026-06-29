"""Точка входа микросервиса памяти."""
from __future__ import annotations

import json

from common.config import load_config
from common.logging import get_logger
from common.proto import PayloadType, build_payload
from common.zmq_transport import ZMQServer
from .faiss_memory import FaissMemoryStore

log = get_logger(__name__)


def main():
    cfg = load_config()
    store = FaissMemoryStore(cfg)

    def handler(env, payload) -> bytes:
        op = payload.op  # 0=STORE, 1=RETRIEVE, 2=FORGET, 3=STATS
        log.info("MemoryOp op=%d session=%s", op, payload.session_id)
        try:
            if op == 0:  # STORE
                rid = store.store(
                    session_id=payload.session_id,
                    text=payload.text,
                    metadata=dict(payload.metadata),
                )
                return build_payload(PayloadType.MEMORY_RESULT, ok=True,
                                     records=[], error="")
            elif op == 1:  # RETRIEVE
                recs = store.retrieve(
                    session_id=payload.session_id or None,
                    text=payload.text,
                    top_k=payload.top_k or 5,
                    channels=list(payload.channels) if payload.channels else None,
                )
                return build_payload(
                    PayloadType.MEMORY_RESULT,
                    ok=True,
                    records=[
                        {
                            "id": r.id, "text": r.text, "score": r.score,
                            "importance": r.importance, "created_at": r.created_at,
                            "last_accessed": r.last_accessed,
                            "access_count": r.access_count,
                            "metadata": r.metadata,
                        }
                        for r in recs
                    ],
                    error="",
                )
            elif op == 2:  # FORGET
                n = store.forget(session_id=payload.session_id or None)
                return build_payload(PayloadType.MEMORY_RESULT, ok=True,
                                     records=[], error="",
                                     stats={"deleted": str(n)})
            elif op == 3:  # STATS
                s = store.stats()
                return build_payload(PayloadType.MEMORY_RESULT, ok=True,
                                     records=[], error="", stats=s)
            else:
                return build_payload(PayloadType.MEMORY_RESULT, ok=False,
                                     error=f"unknown op {op}")
        except Exception as e:
            log.exception("MemoryOp failed")
            return build_payload(PayloadType.MEMORY_RESULT, ok=False,
                                 error=str(e))

    server = ZMQServer(
        endpoint=cfg.zmq["memory_endpoint"],
        service_name="memory",
        handler=handler,
        rcvtimeo_ms=cfg.zmq.get("zmq_rcvtimeo_ms", 30000),
    )
    log.info("Memory service at %s", cfg.zmq["memory_endpoint"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()

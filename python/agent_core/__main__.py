"""Точка входа микросервиса Agent Core."""
from __future__ import annotations

from common.config import load_config
from common.logging import get_logger
from common.zmq_transport import ZMQServer
from .agent import AgentRuntime

log = get_logger(__name__)


def main():
    cfg = load_config()
    runtime = AgentRuntime(cfg)

    server = ZMQServer(
        endpoint=cfg.zmq["agent_core_endpoint"],
        service_name="agent_core",
        handler=runtime.handle_request,
        rcvtimeo_ms=cfg.zmq.get("zmq_rcvtimeo_ms", 30000),
    )
    log.info("Agent Core starting at %s", cfg.zmq["agent_core_endpoint"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Agent Core shutting down")
    finally:
        runtime.shutdown()
        server.stop()


if __name__ == "__main__":
    main()

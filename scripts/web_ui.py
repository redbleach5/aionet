"""Web-UI для Aionet — простой HTTP-сервер + статический HTML/JS чат.

Запуск (после того как сервисы уже подняты через start_bg.sh/.ps1):

    python scripts/web_ui.py
    # откроется http://127.0.0.1:8080

Альтернатива Tauri-десктоп-приложению: не требует Rust/Node.js, работает
в любом браузере. Подходит для быстрого тестирования backend'а.

Архитектура:
    Browser ──HTTP──► web_ui.py ──ZMQ REQ──► agent_core (:5550)
                                   │
                                   └─► WebSocket → avatar_bridge (:8765)
                                       (для аватара, опционально)
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import SO_REUSEADDR, SOL_SOCKET
from urllib.parse import urlparse

# Добавляем python/ и proto/_gen в путь
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "proto" / "_gen"))
os.environ.setdefault("AIONET_CONFIG", str(ROOT / "config.toml"))

from common.config import load_config
from common.proto import build_payload, PayloadType
from common.zmq_transport import ZMQClient

# =============================================================================
# Конфигурация
# =============================================================================
HOST = "127.0.0.1"
PORT = 8080
WEB_UI_DIR = ROOT / "web_ui"

# ZMQ-клиент к agent_core.
# ВАЖНО: ZMQ REQ-сокет нельзя переиспользовать после ошибки — он остаётся
# в состоянии "ожидание reply". Поэтому каждый запрос создаёт новый клиент.
# Это медленнее (tcp handshake), но надёжно. Для прод-нагрузки нужен пул.
# Без глобального lock — каждый поток создаёт свой ZMQClient.


def call_agent(cfg, text: str, session_id: str, timeout_ms: int = 120_000) -> dict:
    """Синхронный вызов agent_core. Возвращает dict-результат или бросает исключение."""
    client = ZMQClient(
        endpoint=cfg.zmq["agent_core_endpoint"],
        service_name="web_ui",
        rcvtimeo_ms=timeout_ms,
    )
    try:
        payload = build_payload(
            PayloadType.AGENT_REQUEST,
            session_id=session_id,
            user_text=text,
        )
        resp = client.call(
            target="agent_core",
            payload_type=PayloadType.AGENT_REQUEST,
            payload=payload,
        )
        tool_calls = []
        for tc in resp.tool_calls:
            tool_calls.append({
                "tool_name": tc.tool_name,
                "arguments": tc.arguments,
                "result": tc.result,
                "duration_ms": tc.duration_ms,
                "ok": tc.ok,
            })
        return {
            "ok": True,
            "session_id": resp.session_id,
            "final_text": resp.final_text,
            "tool_calls": tool_calls,
            "tokens_used": resp.tokens_used,
        }
    finally:
        client.close()


# =============================================================================
# HTTP handler
# =============================================================================
class WebUIHandler(BaseHTTPRequestHandler):
    # Тише логи (каждый статический файл не логируем)
    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            super().log_message(fmt, *args)

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str):
        if not path.exists() or not path.is_file():
            self.send_error(404, f"Not found: {path.name}")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # CORS preflight
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # API endpoints
        if path == "/api/health":
            self._send_json(200, {
                "ok": True,
                "service": "aionet-web-ui",
                "agent_core_endpoint": cfg.zmq["agent_core_endpoint"],
                "ws_endpoint": f"ws://127.0.0.1:{cfg.avatar.get('ws_listen_port', 8765)}",
            })
            return

        # Статические файлы
        if path == "/" or path == "/index.html":
            self._send_file(WEB_UI_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/styles.css":
            self._send_file(WEB_UI_DIR / "styles.css", "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self._send_file(WEB_UI_DIR / "app.js", "application/javascript; charset=utf-8")
            return

        self.send_error(404, f"Not found: {path}")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/chat":
            # Читаем тело
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON"})
                return

            text = (data.get("text") or "").strip()
            session_id = data.get("session_id") or f"web-{uuid.uuid4().hex[:8]}"

            if not text:
                self._send_json(400, {"error": "text is required"})
                return

            # Отправляем запрос к agent_core
            try:
                t0 = time.time()
                result = call_agent(cfg, text, session_id)
                dt_ms = int((time.time() - t0) * 1000)
                result["duration_ms"] = dt_ms
                self._send_json(200, result)
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                print(f"[ERROR] /api/chat failed: {e}", file=sys.stderr)
                print(tb, file=sys.stderr)
                self._send_json(500, {
                    "ok": False,
                    "error": str(e),
                    "traceback": tb,
                })
            return

        self.send_error(404, f"Not found: {path}")


# =============================================================================
# Main
# =============================================================================
cfg = load_config()


def main():
    # Проверяем что WEB_UI_DIR существует
    if not WEB_UI_DIR.exists():
        print(f"[ERROR] web_ui/ directory not found at {WEB_UI_DIR}")
        print("        Run from project root: python scripts/web_ui.py")
        sys.exit(1)

    # Проверяем соединение с agent_core
    print(f"[i] Aionet Web-UI")
    print(f"[i] agent_core: {cfg.zmq['agent_core_endpoint']}")
    print(f"[i] avatar ws:  ws://127.0.0.1:{cfg.avatar.get('ws_listen_port', 8765)}")
    print()
    # ThreadingHTTPServer обрабатывает каждый запрос в отдельном потоке.
    # daemon_threads=False чтобы потоки не убивались при выходе из главной
    # сессии shell (проблема на Linux с nohup).
    server = ThreadingHTTPServer((HOST, PORT), WebUIHandler)
    server.daemon_threads = False
    server.socket.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
    print(f"[✓] Web-UI listening: http://{HOST}:{PORT}")
    print()
    print("    Open in browser:    http://127.0.0.1:8080")
    print()
    print("    Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[i] Shutting down Web-UI...")
        server.shutdown()


if __name__ == "__main__":
    main()

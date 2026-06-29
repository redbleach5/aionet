"""Mock Ollama HTTP-сервер для тестового окружения.

Имитирует API Ollama:
  * GET  /api/tags  → список "установленных" моделей
  * POST /api/show  → детали модели (parameter_size, quantization)
  * POST /api/chat  → "LLM-ответ" с поддержкой tool_calls

Вместо реальной LLM — детерминированная логика по правилам:
  * Если в сообщении есть "list files" / "перечисли файлы" → tool_call fs
  * Если есть "calc" / "посчитай" → tool_call shell
  * Если есть "search" / "найди" → tool_call browser
  * Иначе — короткий текстовый ответ, упоминающий ключевые слова запроса

Это позволяет протестировать весь pipeline: agent → llm_engine → tools → memory → avatar
без реальной LLM. Семантического качества нет, но структурно всё работает.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = [
    {"name": "mock:test-7b", "modified_at": "2025-01-01T00:00:00Z", "size": 4_000_000_000},
    {"name": "mistral:7b-instruct", "modified_at": "2025-01-01T00:00:00Z", "size": 4_000_000_000},
]


def _make_tool_call(name: str, arguments: dict) -> dict:
    """Формат Ollama для tool_calls в /api/chat."""
    return {
        "function": {"name": name, "arguments": arguments},
    }


def _generate_response(user_text: str, tools: list) -> dict:
    """Генерирует "LLM-ответ" по правилам.

    Возвращает dict в формате Ollama /api/chat response:
      {"message": {"role":"assistant", "content":"...", "tool_calls":[...]},
       "prompt_eval_count": N, "eval_count": M, "model": "..."}
    """
    text_lower = user_text.lower()
    tool_names = {t["function"]["name"] for t in tools} if tools else set()

    # 1. Если просят перечислить файлы → tool_call fs/run
    if any(kw in text_lower for kw in ["list files", "перечисли файлы", "список файлов",
                                        "ls", "покажи файлы", "что в директории"]):
        if any(n.startswith("filesystem") for n in tool_names):
            return {
                "message": {
                    "role": "assistant",
                    "content": "Сейчас посмотрю файлы в текущей директории.",
                    "tool_calls": [
                        _make_tool_call("filesystem/run", {
                            "command": "fs_list",
                            "args": ["."]
                        })
                    ],
                },
                "prompt_eval_count": 25,
                "eval_count": 12,
                "model": "mock:test-7b",
            }

    # 2. Если просят посчитать → tool_call shell
    if any(kw in text_lower for kw in ["calc ", "посчитай", "вычисли", "2+2", "math"]):
        if any(n.startswith("shell") for n in tool_names):
            return {
                "message": {
                    "role": "assistant",
                    "content": "Посчитаю через shell.",
                    "tool_calls": [
                        _make_tool_call("shell/run", {
                            "command": "echo",
                            "args": ["42"]
                        })
                    ],
                },
                "prompt_eval_count": 30,
                "eval_count": 10,
                "model": "mock:test-7b",
            }

    # 3. Если есть "tool"-результат в истории (role=tool) — синтезируем финал
    if "tool_call_id" in str(user_text) or "результат" in text_lower:
        return {
            "message": {
                "role": "assistant",
                "content": "Готово — задача выполнена с помощью инструмента.",
            },
            "prompt_eval_count": 20,
            "eval_count": 15,
            "model": "mock:test-7b",
        }

    # 4. Дефолтный текстовый ответ — упоминает ключевые слова
    keywords = re.findall(r"\b[\wа-яё]{4,}\b", user_text.lower())
    kw_str = ", ".join(keywords[:3]) if keywords else "запрос"
    return {
        "message": {
            "role": "assistant",
            "content": f"Понял ваш запрос ({kw_str}). Это mock-ответ без реальной LLM.",
        },
        "prompt_eval_count": 15,
        "eval_count": 15,
        "model": "mock:test-7b",
    }


class MockOllamaHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Тише логи
        pass

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send_json(200, {"models": MODELS})
            return
        if self.path == "/api/ps":
            self._send_json(200, {"models": []})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid json"})
            return

        if self.path == "/api/show":
            # Детали модели — возвращаем как 7B q4_K_M
            self._send_json(200, {
                "details": {
                    "parameter_size": "7B",
                    "quantization_level": "q4_K_M",
                    "family": "llama",
                }
            })
            return

        if self.path == "/api/chat":
            messages = data.get("messages", [])
            tools = data.get("tools", [])
            # Находим последнее user-сообщение
            user_text = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    user_text = m.get("content", "")
                    break
                if m.get("role") == "tool":
                    # Если последнее — tool, агент ждёт synthesis
                    user_text = f"tool result: {m.get('content','')[:100]}"
                    break
            response = _generate_response(user_text, tools)
            # Имитируем "thinking" задержку
            time.sleep(0.05)
            self._send_json(200, response)
            return

        self._send_json(404, {"error": "not found"})


def main(host: str = "127.0.0.1", port: int = 11434):
    server = ThreadingHTTPServer((host, port), MockOllamaHandler)
    print(f"[mock-ollama] listening http://{host}:{port}")
    print(f"[mock-ollama] models: {[m['name'] for m in MODELS]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock-ollama] shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()

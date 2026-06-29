"""LLM Engine — микросервис, оборачивающий Ollama (или совместимый API).

Поддерживаемые модели (приоритетный список, см. config.llm.candidate_models):
  * aion-plan-1.0    — целевая (Microsoft Aion 1.0 Plan), если опубликована в Ollama
  * mistral:7b-instruct
  * llama3.1:8b-instruct

Если ни одна не установлена — движок вернёт диагностику через ERROR-конверт.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from common.config import Config, load_config
from common.interfaces import (
    ChatMessage, LLMClient, LLMResult, ToolCall, ToolSchema,
)
from common.logging import get_logger, trace_context, new_trace

log = get_logger(__name__)


class OllamaClient(LLMClient):
    """Обёртка над /api/chat Ollama с поддержкой tool-calls (формат Ollama)."""

    def __init__(self, host: str, candidate_models: list[str],
                 fallback_model: str | None = None):
        self.host = host.rstrip("/")
        self.candidate_models = list(candidate_models)
        self.fallback_model = fallback_model
        self._available: list[str] | None = None

    def list_available_models(self) -> list[str]:
        if self._available is not None:
            return self._available
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=5)
            r.raise_for_status()
            tags = r.json().get("models", [])
            self._available = [m["name"] for m in tags]
        except Exception as e:
            log.error("ollama /api/tags failed: %s", e)
            self._available = []
        return self._available

    def _resolve_model(self, requested: str | None) -> str:
        available = self.list_available_models()
        # 1) Явно запрошенная модель, если установлена
        if requested and requested in available:
            return requested
        # 2) Первая доступная из кандидатного списка
        for m in self.candidate_models:
            if m in available:
                return m
        # 3) Явный fallback, если установлен
        if self.fallback_model and self.fallback_model in available:
            return self.fallback_model
        # 4) Любая доступная
        if available:
            log.warning("no candidate model installed; using %s", available[0])
            return available[0]
        raise RuntimeError(
            "no Ollama models installed. Pull one: `ollama pull mistral:7b-instruct`"
        )

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=2, max=15),
           reraise=True)
    def call(self, *, model, system_prompt, messages, tools=None,
             temperature=0.3, max_tokens=2048, timeout_s=120,
             static_prefix: str | None = None,
             dynamic_suffix: str | None = None) -> LLMResult:
        """Вызов LLM с поддержкой static_prefix + dynamic_suffix.

        Если `static_prefix` задан (непустой) — отправляем в LLM два отдельных
        system-сообщения: [static_prefix, dynamic_suffix]. Это позволяет Ollama
        кэшировать KV-prefix для static_prefix и переиспользовать его между
        вызовами (ускорение 3-5× для повторных запросов).

        Если `static_prefix` пустой/None — fallback на единый system_prompt
        (legacy-режим, обратная совместимость).
        """
        resolved = self._resolve_model(model)
        # ── Сборка messages[] с учётом static/dynamic split ──
        sys_messages: list[dict[str, Any]] = []
        if static_prefix:
            # Новый режим: два system-сообщения для KV-cache friendly промпта.
            sys_messages.append({"role": "system", "content": static_prefix})
            if dynamic_suffix:
                sys_messages.append({"role": "system", "content": dynamic_suffix})
        elif system_prompt:
            # Legacy-режим: один system-prompt.
            sys_messages.append({"role": "system", "content": system_prompt})
        payload: dict[str, Any] = {
            "model": resolved,
            "messages": list(sys_messages),
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        for m in messages:
            item = {"role": m.role, "content": m.content or ""}
            if m.tool_call_id:
                item["tool_call_id"] = m.tool_call_id
            if m.name:
                item["name"] = m.name
            payload["messages"].append(item)
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": json.loads(t.parameters_json),
                    },
                }
                for t in tools
            ]

        log.debug("ollama call model=%s msgs=%d tools=%d static=%s",
                  resolved, len(payload["messages"]), len(tools or []))
        t0 = time.time()
        r = requests.post(f"{self.host}/api/chat", json=payload, timeout=timeout_s)
        r.raise_for_status()
        data = r.json()
        dt = int((time.time() - t0) * 1000)

        msg = data.get("message", {})
        content = msg.get("content", "")
        tool_calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls", [])):
            fn = tc.get("function", {})
            tool_calls.append(ToolCall(
                id=tc.get("id") or f"call_{i}",
                name=fn.get("name", ""),
                arguments_json=json.dumps(fn.get("arguments", {}), ensure_ascii=False),
            ))
        return LLMResult(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=int(data.get("prompt_eval_count", 0)),
            completion_tokens=int(data.get("eval_count", 0)),
            model_used=resolved,
        )


class LLMEngineService:
    """Микросервис: REP-сокет, принимает LLMCall → возвращает LLMResult."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        llm_cfg = cfg.llm
        self.client = OllamaClient(
            host=llm_cfg.get("ollama_host", "http://127.0.0.1:11434"),
            candidate_models=llm_cfg.get("candidate_models", []),
            fallback_model=llm_cfg.get("fallback_model"),
        )
        self.default_temperature = float(llm_cfg.get("temperature", 0.3))
        self.default_max_tokens = int(llm_cfg.get("max_tokens", 2048))
        self.default_timeout = int(llm_cfg.get("timeout_s", 120))

    def handle(self, env, payload) -> bytes:
        from common.proto import build_payload, PayloadType
        # role — enum в proto (USER=0, ASSISTANT=1, SYSTEM=2, TOOL=3)
        # m.role может быть int (значение enum) или самим enum'ом.
        ROLE_NAMES = {0: "user", 1: "assistant", 2: "system", 3: "tool"}
        messages = []
        for m in payload.messages:
            role_val = m.role
            # protobuf enum: если это int-like, берём числовое значение
            try:
                role_int = int(role_val)
                role_name = ROLE_NAMES.get(role_int, "user")
            except (TypeError, ValueError):
                # уже строка
                role_name = str(role_val) if isinstance(role_val, str) else "user"
            messages.append(ChatMessage(
                role=role_name,
                content=m.content,
                tool_call_id=m.tool_call_id or None,
                name=m.name or None,
            ))
        tools = [
            ToolSchema(name=t.name, description=t.description,
                       parameters_json=t.parameters_json)
            for t in payload.tools
        ]
        try:
            result = self.client.call(
                model=payload.model or "",
                system_prompt=payload.system_prompt,
                messages=messages,
                tools=tools or None,
                temperature=payload.temperature or self.default_temperature,
                max_tokens=payload.max_tokens or self.default_max_tokens,
                timeout_s=self.default_timeout,
                static_prefix=payload.static_prefix or None,
                dynamic_suffix=payload.dynamic_suffix or None,
            )
            return build_payload(
                PayloadType.LLM_RESULT,
                content=result.content,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                model_used=result.model_used,
                tool_calls=[
                    {"id": tc.id, "name": tc.name,
                     "arguments_json": tc.arguments_json}
                    for tc in result.tool_calls
                ],
            )
        except Exception as e:
            log.exception("LLM call failed")
            return build_payload(
                PayloadType.ERROR,
                code="LLM_FAILED",
                message=str(e),
            )


def main():
    from common.zmq_transport import ZMQServer
    cfg = load_config()
    svc = LLMEngineService(cfg)

    # Регистрируем handler для LLM_CALL
    def handler(env, payload):
        return svc.handle(env, payload)

    server = ZMQServer(
        endpoint=cfg.zmq["llm_engine_endpoint"],
        service_name="llm_engine",
        handler=handler,
        rcvtimeo_ms=cfg.zmq.get("zmq_rcvtimeo_ms", 30000),
    )
    log.info("LLM Engine starting; available models: %s",
             svc.client.list_available_models())
    server.serve_forever()


if __name__ == "__main__":
    main()

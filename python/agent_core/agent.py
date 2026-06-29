"""aionet.agent_core — оркестратор микросервисов.

Реализует цикл агента:
    1) Получить AgentRequest от Tauri/UI.
    2) Опросить память (MemoryOp RETRIEVE) → собрать контекст.
    3) Вызвать LLM (LLMCall) → получить план/ответ или tool_calls.
    4) Если есть tool_calls — вызвать Tools-брокер (ToolCallMessage),
       результаты передать обратно в LLM как tool-сообщение.
    5) Повторять 3-4, пока LLM не вернёт финальный ответ (без tool_calls).
    6) Сохранить запрос+ответ в память (MemoryOp STORE).
    7) Отправить AvatarCommand SPEAK с финальным ответом.
    8) Вернуть AgentResponse.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from common.config import Config
from common.interfaces import ChatMessage, ToolCall, ToolSchema
from common.logging import get_logger, trace_context, new_trace
from common.proto import build_payload, PayloadType
from common.zmq_transport import ZMQClient

log = get_logger(__name__)

# Лимит итераций plan-act, чтобы предотвратить зацикливание.
MAX_ITER = 6


class AgentRuntime:
    """Оркестрирует LLM ↔ Tools ↔ Memory ↔ Avatar."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # Клиенты к другим микросервисам (REQ).
        self.llm = ZMQClient(
            endpoint=cfg.zmq["llm_engine_endpoint"],
            service_name="agent_core",
            rcvtimeo_ms=cfg.zmq.get("zmq_rcvtimeo_ms", 30000),
        )
        self.memory = ZMQClient(
            endpoint=cfg.zmq["memory_endpoint"],
            service_name="agent_core",
            rcvtimeo_ms=cfg.zmq.get("zmq_rcvtimeo_ms", 30000),
        )
        self.tools = ZMQClient(
            endpoint=cfg.zmq["tools_endpoint"],
            service_name="agent_core",
            rcvtimeo_ms=cfg.zmq.get("zmq_rcvtimeo_ms", 30000),
        )
        # Avatar — асинхронный PUB (не ждём ответа).
        from common.zmq_transport import ZMQPublisher
        self.avatar_pub = ZMQPublisher(
            endpoint=cfg.zmq["avatar_cmd_endpoint"],
            service_name="agent_core",
        )
        self.system_prompt_plan = cfg.llm.get("system_prompt_plan", "")
        self.system_prompt_instruct = cfg.llm.get("system_prompt_instruct", "")
        self.model_hint = cfg.llm.get("candidate_models", [""])[0]

    # ------------------------------------------------------------------
    # Точка входа: обработать AgentRequest.
    # ------------------------------------------------------------------
    def handle_request(self, env, payload) -> bytes:
        trace_id = env.trace_id or uuid.uuid4().hex[:16]
        with trace_context(trace_id, env.span_id or uuid.uuid4().hex[:8]):
            return self._run(payload)

    def _run(self, payload) -> bytes:
        session_id = payload.session_id or uuid.uuid4().hex
        user_text = payload.user_text
        log.info("AgentRequest session=%s text=%r", session_id, user_text[:200])

        # 1. Retrieve из памяти
        memory_ctx = self._retrieve_memory(session_id, user_text)
        log.debug("memory retrieved %d fragments", len(memory_ctx))

        # 2. Загружаем tool-схемы
        tool_schemas = self._list_tools()
        log.debug("tools available: %d", len(tool_schemas))

        # 3. Цикл plan-act
        chat: list[ChatMessage] = []
        # Контекст памяти — как system-сообщение
        if memory_ctx:
            ctx_text = "Контекст из памяти:\n" + "\n---\n".join(memory_ctx)
            chat.append(ChatMessage(role="system", content=ctx_text))
        chat.append(ChatMessage(role="user", content=user_text))

        traces: list[dict[str, Any]] = []
        final_text = ""
        total_prompt_tokens = 0
        total_completion_tokens = 0

        for iteration in range(MAX_ITER):
            log.debug("iter=%d, calling LLM", iteration)
            llm_result = self._call_llm(
                session_id=session_id,
                messages=chat,
                tools=tool_schemas,
            )
            total_prompt_tokens += llm_result.prompt_tokens
            total_completion_tokens += llm_result.completion_tokens

            if not llm_result.tool_calls:
                final_text = llm_result.content
                break

            # Добавляем assistant-сообщение с tool_calls в чат
            chat.append(ChatMessage(
                role="assistant",
                content=llm_result.content or "",
            ))
            # Вызываем каждый инструмент
            for tc in llm_result.tool_calls:
                t0 = time.time()
                log.info("tool_call name=%s args=%s", tc.name, tc.arguments_json)
                result = self._call_tool(tc)
                dt = int((time.time() - t0) * 1000)
                traces.append({
                    "tool_name": tc.name,
                    "arguments": tc.arguments_json,
                    "result": result,
                    "duration_ms": dt,
                    "ok": not result.startswith("ERROR:"),
                })
                # tool-результат возвращается в LLM как отдельное сообщение
                chat.append(ChatMessage(
                    role="tool",
                    content=result,
                    tool_call_id=tc.id,
                    name=tc.name,
                ))
        else:
            final_text = (
                "Достигнут лимит итераций plan-act. "
                "Текущий промежуточный ответ: " + (final_text or "(пусто)")
            )

        # 4. Store в память
        self._store_memory(session_id, user_text, final_text)

        # 5. Avatar: озвучить финальный ответ
        self._avatar_speak(final_text)

        log.info("AgentResponse session=%s tokens=p%d+c%d tools=%d",
                 session_id, total_prompt_tokens, total_completion_tokens, len(traces))

        return build_payload(
            PayloadType.AGENT_RESPONSE,
            session_id=session_id,
            final_text=final_text,
            tokens_used=total_prompt_tokens + total_completion_tokens,
            tool_calls=traces,  # protobuf принимает dict-структуру по имени поля
        )

    # ------------------------------------------------------------------
    # Обёртки над ZeroMQ-вызовами к другим сервисам.
    # ------------------------------------------------------------------
    def _retrieve_memory(self, session_id: str, query: str) -> list[str]:
        try:
            payload = build_payload(
                PayloadType.MEMORY_OP,
                op=1,  # RETRIEVE
                session_id=session_id,
                text=query,
                top_k=self.cfg.memory.get("top_k", 5),
            )
            res = self.memory.call(
                target="memory", payload_type=PayloadType.MEMORY_OP, payload=payload,
            )
            return [r.text for r in res.records]
        except Exception as e:
            log.warning("memory retrieve failed: %s", e)
            return []

    def _store_memory(self, session_id: str, user_text: str, final_text: str):
        try:
            blob = f"User: {user_text}\nAssistant: {final_text}"
            payload = build_payload(
                PayloadType.MEMORY_OP,
                op=0,  # STORE
                session_id=session_id,
                text=blob,
            )
            self.memory.call(
                target="memory", payload_type=PayloadType.MEMORY_OP, payload=payload,
            )
        except Exception as e:
            log.warning("memory store failed: %s", e)

    def _list_tools(self) -> list[ToolSchema]:
        # В простейшем варианте агент не запрашивает listing каждый раз,
        # а работает с предзагруженным конфигом. Но для динамической поддержки
        # можно вызвать tools-брокер со специальным "list" tool.
        # Здесь — статичный список известных инструментов из config.tools.
        schemas: list[ToolSchema] = []
        for srv in self.cfg.tool_servers:
            schemas.append(ToolSchema(
                name=f"{srv['name']}/run",
                description=f"Выполнить команду через MCP-сервер {srv['name']}.",
                parameters_json=json.dumps({
                    "type": "object",
                    "properties": {
                        "command": {"type": "string",
                                    "description": "Команда/подкоманда инструмента"},
                        "args": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["command"],
                }),
            ))
        return schemas

    def _call_llm(self, *, session_id: str, messages: list[ChatMessage],
                  tools: list[ToolSchema]):
        from common.interfaces import LLMResult
        # Конвертируем ChatMessage в protobuf ChatMessage
        from common.proto import _pb
        pb = _pb()
        pb_messages = []
        for m in messages:
            cm = pb.ChatMessage()
            # role — enum в proto, но наш ChatMessage.role — строка.
            role_map = {"user": 0, "assistant": 1, "system": 2, "tool": 3}
            cm.role = role_map.get(m.role, 0)
            cm.content = m.content
            if m.tool_call_id:
                cm.tool_call_id = m.tool_call_id
            if m.name:
                cm.name = m.name
            pb_messages.append(cm)
        pb_tools = []
        for t in tools:
            ts = pb.ToolSchema()
            ts.name = t.name
            ts.description = t.description
            ts.parameters_json = t.parameters_json
            pb_tools.append(ts)

        payload = build_payload(
            PayloadType.LLM_CALL,
            model=self.model_hint,
            system_prompt=self.system_prompt_plan,
            messages=pb_messages,
            tools=pb_tools,
            temperature=self.cfg.llm.get("temperature", 0.3),
            max_tokens=self.cfg.llm.get("max_tokens", 2048),
        )
        res = self.llm.call(target="llm_engine",
                            payload_type=PayloadType.LLM_CALL, payload=payload)
        return LLMResult(
            content=res.content,
            tool_calls=[
                ToolCall(id=tc.id, name=tc.name, arguments_json=tc.arguments_json)
                for tc in res.tool_calls
            ],
            prompt_tokens=res.prompt_tokens,
            completion_tokens=res.completion_tokens,
            model_used=res.model_used,
        )

    def _call_tool(self, tc: ToolCall) -> str:
        try:
            args = json.loads(tc.arguments_json) if tc.arguments_json else {}
            # Раскладываем "server/run" → tool_name="server", command=args["command"]
            tool_name = tc.name
            payload = build_payload(
                PayloadType.TOOL_CALL,
                tool_name=tool_name,
                arguments_json=tc.arguments_json,
                timeout_ms=self.cfg.tools.get("default_timeout_ms", 30000),
            )
            res = self.tools.call(target="tools",
                                  payload_type=PayloadType.TOOL_CALL, payload=payload)
            return res.output_json or res.error or ""
        except Exception as e:
            log.exception("tool call failed: %s", e)
            return f"ERROR: {e}"

    def _avatar_speak(self, text: str) -> None:
        try:
            payload = build_payload(
                PayloadType.AVATAR_CMD,
                action=0,  # SPEAK
                text=text,
            )
            self.avatar_pub.publish(
                target="avatar", payload_type=PayloadType.AVATAR_CMD, payload=payload,
            )
        except Exception as e:
            log.warning("avatar speak failed: %s", e)

    def shutdown(self) -> None:
        for c in (self.llm, self.memory, self.tools):
            try:
                c.close()
            except Exception:
                pass
        try:
            self.avatar_pub.close()
        except Exception:
            pass

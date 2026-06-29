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
from .loop_detector import LoopDetector, AgentStep, make_loop_detector
from .task_complexity import classify_complexity, TaskComplexity, get_defaults
from .prompt_builder import SystemPromptBuilder, DynamicContext

log = get_logger(__name__)

# Жёсткий верхний предел итераций — страховка, даже если CognitivePlanner
# (Sprint 2) разрешит больше. Без этой отсечки зависший LLM может завести
# цикл в бесконечность.
MAX_ITER_HARD_LIMIT = 12


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
        # ── Sprint 1: новые компоненты ──
        self.prompt_builder = SystemPromptBuilder(cfg)
        # Embedding-функция для semantic-loop детектора. Если memory-сервис
        # доступен — используем его; иначе semantic-loop просто пропускается.
        self._embed_fn = self._build_embed_fn()
        self.loop_detector_factory = lambda: make_loop_detector(cfg, self._embed_fn)

    def _build_embed_fn(self):
        """Возвращает callable(text) -> list[float] для semantic-loop детектора.

        Лучший вариант — использовать локальную sentence-transformers модель
        прямо в agent_core (быстро, без IPC). Если она недоступна — None,
        и semantic-loop сигнатура просто не сработает (non-fatal).
        """
        try:
            from sentence_transformers import SentenceTransformer
            model_name = self.cfg.memory.get("embedding_model", "all-MiniLM-L6-v2")
            model = SentenceTransformer(model_name)
            def embed(text: str) -> list[float]:
                v = model.encode(text, normalize_embeddings=True)
                return v.tolist()
            log.info("LoopDetector embed_fn ready (model=%s)", model_name)
            return embed
        except Exception as e:
            log.warning("sentence-transformers unavailable for semantic-loop: %s", e)
            return None

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

        # ── 0. Классификация сложности — определяет max_iter/max_tokens ──
        assessment = classify_complexity(user_text)
        complexity_defaults = get_defaults(assessment.level)
        max_iter = min(complexity_defaults["max_iter"], MAX_ITER_HARD_LIMIT)
        max_tokens = complexity_defaults["max_tokens"]
        log.info("complexity=%s max_iter=%d max_tokens=%d matched=%d",
                 assessment.level.value, max_iter, max_tokens,
                 len(assessment.matched_patterns))

        # 1. Retrieve из памяти
        memory_ctx = self._retrieve_memory(session_id, user_text)
        log.debug("memory retrieved %d fragments", len(memory_ctx))

        # 2. Загружаем tool-схемы (только если complexity разрешает tools)
        tool_schemas = self._list_tools() if complexity_defaults["tools"] else []
        log.debug("tools available: %d (enabled=%s)",
                  len(tool_schemas), complexity_defaults["tools"])

        # 3. Собираем dynamic-context для system-prompt
        dyn_ctx = DynamicContext(
            complexity_level=assessment.level.value,
            complexity_description=assessment.description,
            memory_context=("\n---\n".join(memory_ctx) if memory_ctx else None),
        )
        static_prefix, dynamic_suffix = self.prompt_builder.build(dyn_ctx)

        # 4. Цикл plan-act
        chat: list[ChatMessage] = []
        # Если static/dynamic split не активен (legacy-конфиг) — память
        # добавляем как system-сообщение в chat. Иначе она уже в dynamic_suffix.
        if static_prefix is None and memory_ctx:
            ctx_text = "Контекст из памяти:\n" + "\n---\n".join(memory_ctx)
            chat.append(ChatMessage(role="system", content=ctx_text))
        chat.append(ChatMessage(role="user", content=user_text))

        # 5. LoopDetector — свежий для каждого запроса
        loop_detector = self.loop_detector_factory()

        traces: list[dict[str, Any]] = []
        final_text = ""
        total_prompt_tokens = 0
        total_completion_tokens = 0
        loop_signal = None

        for iteration in range(max_iter):
            log.debug("iter=%d/%d, calling LLM", iteration, max_iter)
            llm_result = self._call_llm(
                session_id=session_id,
                messages=chat,
                tools=tool_schemas,
                static_prefix=static_prefix,
                dynamic_suffix=dynamic_suffix,
                max_tokens=max_tokens,
            )
            total_prompt_tokens += llm_result.prompt_tokens
            total_completion_tokens += llm_result.completion_tokens

            if not llm_result.tool_calls:
                final_text = llm_result.content
                # Фиксируем шаг для детектора (финальный thought)
                loop_detector.add(AgentStep(
                    thought=llm_result.content or "",
                    observation=llm_result.content or "",
                ))
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
                # Регистрируем шаг в LoopDetector'е
                loop_detector.add(AgentStep(
                    thought=llm_result.content or "",
                    tool_name=tc.name,
                    arguments=tc.arguments_json,
                    observation=result,
                ))
                # ── Проверка на зацикливание после каждого tool-вызова ──
                sig = loop_detector.check()
                if sig is not None:
                    loop_signal = sig
                    log.warning("loop detected: %s — %s",
                                sig.kind.value, sig.detail)
                    break
            if loop_signal is not None:
                break
        else:
            final_text = (
                "Достигнут лимит итераций plan-act. "
                "Текущий промежуточный ответ: " + (final_text or "(пусто)")
            )

        # Если сработал LoopDetector — добавляем диагностику в ответ
        if loop_signal is not None:
            final_text = (
                (final_text or "") +
                f"\n\n[⚠️ Обнаружено зацикливание: {loop_signal.kind.value} — "
                f"{loop_signal.detail}. Останавливаюсь.]"
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
                  tools: list[ToolSchema],
                  static_prefix: str | None = None,
                  dynamic_suffix: str | None = None,
                  max_tokens: int | None = None):
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

        # Если static_prefix задан — используем split-режим (KV-cache friendly).
        # Иначе — legacy: единый system_prompt_plan.
        system_prompt_value = self.system_prompt_plan
        if static_prefix is not None:
            system_prompt_value = ""  # пустой → LLM-engine проигнорирует legacy-поле

        payload = build_payload(
            PayloadType.LLM_CALL,
            model=self.model_hint,
            system_prompt=system_prompt_value,
            messages=pb_messages,
            tools=pb_tools,
            temperature=self.cfg.llm.get("temperature", 0.3),
            max_tokens=max_tokens or self.cfg.llm.get("max_tokens", 2048),
            static_prefix=static_prefix or "",
            dynamic_suffix=dynamic_suffix or "",
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

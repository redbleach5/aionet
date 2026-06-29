// IPC: ZMQ REQ-клиент к agent_core.
// Использует protobuf Envelope (proto/messages.proto) — бинарный формат,
// совместимый с Python-сервисами. Сгенерированные типы в src/proto.rs.

use std::time::Duration;
use serde::{Deserialize, Serialize};
use uuid::Uuid;
use zmq::Socket;
use prost::Message;  // для .encode_to_vec() / ::decode()

use crate::proto::{
    Envelope, AgentRequest, AgentResponse,
    PAYLOAD_AGENT_REQUEST, PAYLOAD_AGENT_RESPONSE, PAYLOAD_ERROR,
    ErrorPayload,
};

// =============================================================================
// DTO для Tauri-фронтенда (JSON-сериализация через serde)
// =============================================================================
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentResponseDto {
    pub session_id: String,
    pub final_text: String,
    pub tool_calls: Vec<ToolCallTraceDto>,
    pub tokens_used: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolCallTraceDto {
    pub tool_name: String,
    pub arguments: String,
    pub result: String,
    pub duration_ms: u32,
    pub ok: bool,
}

// =============================================================================
// ZMQ-клиент
// =============================================================================
pub struct AgentClient {
    endpoint: String,
    ctx: zmq::Context,
}

impl AgentClient {
    pub fn new(endpoint: String) -> Self {
        Self {
            endpoint,
            ctx: zmq::Context::new(),
        }
    }

    /// Отправляет AgentRequest (protobuf) и ждёт AgentResponse.
    ///
    /// Формат на проводе — сериализованный protobuf Envelope:
    ///   Envelope {
    ///     trace_id, span_id, source, target, timestamp,
    ///     type: PAYLOAD_AGENT_REQUEST,
    ///     payload: AgentRequest.SerializeToString()
    ///   }
    pub async fn send(&self, text: &str) -> Result<AgentResponseDto, Box<dyn std::error::Error>> {
        let sock = self.ctx.socket(zmq::REQ)?;
        sock.set_linger(1000)?;
        sock.set_rcvtimeo(60_000)?;
        sock.connect(&self.endpoint)?;

        // ── Сборка AgentRequest ──
        let session_id = Uuid::new_v4().to_string();
        let req = AgentRequest {
            session_id,
            user_text: text.to_string(),
            ..Default::default()
        };

        // ── Сборка Envelope ──
        let trace_id = Uuid::new_v4().simple().to_string();
        let span_id = Uuid::new_v4().simple().to_string()[..8].to_string();
        let envelope = Envelope {
            trace_id,
            span_id,
            source: "tauri".to_string(),
            target: "agent_core".to_string(),
            timestamp: std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_millis() as u64)
                .unwrap_or(0),
            r#type: PAYLOAD_AGENT_REQUEST,
            payload: req.encode_to_vec(),
        };

        let env_bytes = envelope.encode_to_vec();
        sock.send(env_bytes, 0)?;

        // ── Приём ответа ──
        let raw = sock.recv_bytes(0)?;
        let reply_env = Envelope::decode(raw.as_slice())
            .map_err(|e| format!("decode envelope: {e}"))?;

        // Если пришёл ERROR
        if reply_env.r#type == PAYLOAD_ERROR {
            let err = ErrorPayload::decode(reply_env.payload.as_slice())
                .map_err(|e| format!("decode error payload: {e}"))?;
            return Err(format!("agent_core error [{}]: {}", err.code, err.message).into());
        }

        // Парсим AgentResponse
        let resp = AgentResponse::decode(reply_env.payload.as_slice())
            .map_err(|e| format!("decode AgentResponse: {e}"))?;

        // Конвертируем в DTO для фронта
        let tool_calls = resp.tool_calls.iter().map(|tc| ToolCallTraceDto {
            tool_name: tc.tool_name.clone(),
            arguments: tc.arguments.clone(),
            result: tc.result.clone(),
            duration_ms: tc.duration_ms,
            ok: tc.ok,
        }).collect();

        Ok(AgentResponseDto {
            session_id: resp.session_id,
            final_text: resp.final_text,
            tool_calls,
            tokens_used: resp.tokens_used,
        })
    }

    /// Минимальная health-проверка: пытаемся открыть сокет.
    pub async fn health(&self) -> serde_json::Value {
        match self.ctx.socket(zmq::REQ) {
            Ok(_) => serde_json::json!({"ok": true, "endpoint": self.endpoint}),
            Err(e) => serde_json::json!({"ok": false, "error": e.to_string()}),
        }
    }
}

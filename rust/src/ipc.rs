// IPC: ZMQ REQ-клиент к agent_core. Простой текстовый протокол поверх protobuf Envelope.
// Используем prost-сгенерированный messages_pb из proto/.
// Сборку .proto для Rust делаем через build.rs (здесь — упрощённо: текстовый JSON).

use std::time::Duration;
use serde::{Deserialize, Serialize};
use uuid::Uuid;
use zmq::Socket;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentResponse {
    pub session_id: String,
    pub final_text: String,
    pub tool_calls: Vec<ToolCallTrace>,
    pub tokens_used: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolCallTrace {
    pub tool_name: String,
    pub arguments: String,
    pub result: String,
    pub duration_ms: u32,
    pub ok: bool,
}

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

    /// Отправляет текстовый запрос агенту и ждёт ответ.
    /// Использует JSON-конверт (а не protobuf) для упрощения сборки фронта —
    /// Agent Core умеет принимать оба формата; при наличии protobuf-биндингов
    /// можно переключиться на бинарный (см. proto/).
    pub async fn send(&self, text: &str) -> Result<AgentResponse, Box<dyn std::error::Error>> {
        let sock = self.ctx.socket(zmq::REQ)?;
        sock.set_linger(1000)?;
        sock.set_rcvtimeo(60_000)?;
        sock.connect(&self.endpoint)?;

        let req = serde_json::json!({
            "trace_id": Uuid::new_v4().to_string(),
            "span_id": Uuid::new_v4().to_string()[..8].to_string(),
            "source": "tauri",
            "target": "agent_core",
            "type": "AGENT_REQUEST",
            "payload": {
                "session_id": Uuid::new_v4().to_string(),
                "user_text": text,
            }
        });
        sock.send(serde_json::to_string(&req)?, 0)?;

        let raw = sock.recv_string(0)
            .map_err(|e| format!("zmq recv: {e}"))??;
        let env: serde_json::Value = serde_json::from_str(&raw)?;
        let payload = env.get("payload").cloned().unwrap_or_default();
        let resp: AgentResponse = serde_json::from_value(payload)?;
        Ok(resp)
    }

    pub async fn health(&self) -> serde_json::Value {
        // Минимальная проверка: пытаемся открыть сокет.
        match self.ctx.socket(zmq::REQ) {
            Ok(_) => serde_json::json!({"ok": true, "endpoint": self.endpoint}),
            Err(e) => serde_json::json!({"ok": false, "error": e.to_string()}),
        }
    }
}

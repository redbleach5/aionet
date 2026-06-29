// Protobuf-биндинги для Rust, сгенерированные prost-build из proto/messages.proto.
//
// Файл генерируется при сборке в $OUT_DIR/aionet.v1.rs и подключается через
// include!. Путь передаётся через PROTO_GEN_PATH env variable (см. build.rs).
//
// Все типы (Envelope, AgentRequest, AgentResponse, и т.д.) доступны как
// pub-структуры в модуле aionet.v1. Здесь реэкспортируем их в корень proto.

pub mod aionet {
    pub mod v1 {
        include!(std::env!("PROTO_GEN_PATH"));
    }
}

// Удобные реэкспорты для использования в ipc.rs
pub use aionet::v1::{
    Envelope, AgentRequest, AgentResponse, LlmCall, LlmResult,
    MemoryOp, MemoryResult, ToolCallMessage, ToolResultMessage,
    AvatarCommand, AvatarEvent, ErrorPayload,
};

// Константы PayloadType — совпадают с protobuf enum
pub const PAYLOAD_UNSPECIFIED: i32 = 0;
pub const PAYLOAD_AGENT_REQUEST: i32 = 1;
pub const PAYLOAD_AGENT_RESPONSE: i32 = 2;
pub const PAYLOAD_LLM_CALL: i32 = 3;
pub const PAYLOAD_LLM_RESULT: i32 = 4;
pub const PAYLOAD_MEMORY_OP: i32 = 5;
pub const PAYLOAD_MEMORY_RESULT: i32 = 6;
pub const PAYLOAD_TOOL_CALL: i32 = 7;
pub const PAYLOAD_TOOL_RESULT: i32 = 8;
pub const PAYLOAD_AVATAR_CMD: i32 = 9;
pub const PAYLOAD_AVATAR_EVENT: i32 = 10;
pub const PAYLOAD_ERROR: i32 = 99;

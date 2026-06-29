// Заглушка модуля proto. При наличии protobuf-биндингов замещается
// сгенерированным messages_pb.rs (через prost-build в build.rs).
// Пока используется JSON-протокол — см. ipc.rs.

pub const PAYLOAD_AGENT_REQUEST: u8 = 1;
pub const PAYLOAD_AGENT_RESPONSE: u8 = 2;
pub const PAYLOAD_LLM_CALL: u8 = 3;
pub const PAYLOAD_LLM_RESULT: u8 = 4;
pub const PAYLOAD_MEMORY_OP: u8 = 5;
pub const PAYLOAD_MEMORY_RESULT: u8 = 6;
pub const PAYLOAD_TOOL_CALL: u8 = 7;
pub const PAYLOAD_TOOL_RESULT: u8 = 8;
pub const PAYLOAD_AVATAR_CMD: u8 = 9;
pub const PAYLOAD_AVATAR_EVENT: u8 = 10;
pub const PAYLOAD_ERROR: u8 = 99;

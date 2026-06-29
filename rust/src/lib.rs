// Aionet — Tauri backend.
// Приложение держит постоянный ZMQ REQ-сокет к agent_core_endpoint и
// пробрасывает сообщения между фронтендом (через Tauri events) и агентом.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod proto;
mod ipc;

use std::sync::Arc;
use tauri::Manager;
use tokio::sync::Mutex;

pub struct AppState {
    pub agent: Arc<Mutex<ipc::AgentClient>>,
}

#[tauri::command]
async fn send_message(
    state: tauri::State<'_, AppState>,
    app: tauri::AppHandle,
    text: String,
) -> Result<serde_json::Value, String> {
    log::info!("UI → agent: {:?}", text);
    let resp = state.agent.lock().await.send(&text).await
        .map_err(|e| e.to_string())?;
    // Эмитим в фронтенд для отрисовки tool-call-traces.
    let _ = app.emit("agent-response", &resp);
    Ok(serde_json::to_value(&resp).map_err(|e| e.to_string())?)
}

#[tauri::command]
async fn health_check(
    state: tauri::State<'_, AppState>,
) -> Result<serde_json::Value, String> {
    Ok(state.agent.lock().await.health().await)
}

pub fn run() {
    env_logger::Builder::from_env(
        env_logger::Env::default().default_filter_or("info")
    ).init();

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            // Читаем config.toml из корня проекта.
            let cfg_path = std::env::var("AIONET_CONFIG")
                .unwrap_or_else(|_| "config.toml".to_string());
            let cfg_text = std::fs::read_to_string(&cfg_path)
                .expect("config.toml not found");
            let cfg: toml::Value = cfg_text.parse()
                .expect("invalid config.toml");
            let endpoint = cfg["zmq"]["agent_core_endpoint"]
                .as_str().expect("agent_core_endpoint missing")
                .to_string();
            log::info!("connecting to agent_core at {}", endpoint);
            let agent = Arc::new(Mutex::new(ipc::AgentClient::new(endpoint)));
            app.manage(AppState { agent });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![send_message, health_check])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

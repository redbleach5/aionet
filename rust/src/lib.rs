// Aionet — Tauri backend.
// Приложение держит постоянный ZMQ REQ-сокет к agent_core_endpoint и
// пробрасывает сообщения между фронтендом (через Tauri commands) и агентом.

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
) -> Result<ipc::AgentResponseDto, String> {
    log::info!("UI → agent: {:?}", text);
    let resp = state.agent.lock().await.send(&text).await
        .map_err(|e| e.to_string())?;
    // Эмитим в фронтенд для отрисовки tool-call-traces (опционально).
    let _ = app.emit("agent-response", &resp);
    Ok(resp)
}

#[tauri::command]
async fn health_check(
    state: tauri::State<'_, AppState>,
) -> Result<serde_json::Value, String> {
    Ok(state.agent.lock().await.health().await)
}

/// Находит config.toml в нескольких местах:
///   1) $AIONET_CONFIG (явная переменная окружения)
///   2) ../config.toml (относительно rust/ — корень проекта при dev-режиме)
///   3) ../../config.toml (на случай если CWD другая)
///   4) config.toml в текущей директории
///   5) ~/.config/aionet/config.toml (prod)
fn find_config_toml() -> Option<std::path::PathBuf> {
    // 1. Явная переменная окружения
    if let Ok(p) = std::env::var("AIONET_CONFIG") {
        let path = std::path::PathBuf::from(p);
        if path.exists() {
            return Some(path);
        }
    }

    // 2-4. Относительные пути (dev-режим: cargo tauri dev запускается из rust/)
    let manifest_dir = std::path::PathBuf::from(
        std::env::var("CARGO_MANIFEST_DIR").unwrap_or_else(|_| ".".to_string())
    );
    let candidates = [
        manifest_dir.join("..").join("config.toml"),  // rust/../config.toml
        std::path::PathBuf::from("config.toml"),       // CWD
        std::path::PathBuf::from("../config.toml"),    // CWD/..
        std::path::PathBuf::from("../../config.toml"), // CWD/../..
    ];
    for c in &candidates {
        if c.exists() {
            return Some(c.to_path_buf());
        }
    }

    // 5. ~/.config/aionet/config.toml (prod)
    if let Some(home) = dirs::config_dir() {
        let p = home.join("aionet").join("config.toml");
        if p.exists() {
            return Some(p);
        }
    }

    None
}

pub fn run() {
    env_logger::Builder::from_env(
        env_logger::Env::default().default_filter_or("info")
    ).init();

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            // Находим config.toml
            let cfg_path = find_config_toml().unwrap_or_else(|| {
                panic!(
                    "config.toml not found. Tried: $AIONET_CONFIG, ../config.toml, \
                     ./config.toml, ~/.config/aionet/config.toml"
                )
            });
            log::info!("using config: {}", cfg_path.display());

            let cfg_text = std::fs::read_to_string(&cfg_path)
                .unwrap_or_else(|e| panic!("cannot read {}: {e}", cfg_path.display()));
            let cfg: toml::Value = cfg_text.parse()
                .unwrap_or_else(|e| panic!("invalid config.toml: {e}"));

            let endpoint = cfg.get("zmq")
                .and_then(|z| z.get("agent_core_endpoint"))
                .and_then(|e| e.as_str())
                .expect("config.toml: [zmq].agent_core_endpoint missing")
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

"""aionet.avatar — мост между ZeroMQ (команды от агента) и WebSocket (HTML5-аватар).

Архитектурно:
    agent_core --[ZMQ PUB avatar_cmd_endpoint]--> WS-Bridge --[WS]--> Tauri/Three.js
    Tauri/Three.js --[WS]--> WS-Bridge --[ZMQ PUB avatar_evt_endpoint]--> agent_core

Если в будущем появится Desktop Homunculus MCP-сервер — его можно подключить
вместо WS-моста: достаточно унаследовать AvatarBackend и заменить реализацию speak().
"""

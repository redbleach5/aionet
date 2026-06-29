import { useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import * as THREE from "three";
import { invoke } from "@tauri-apps/api/core";

// =========================================================================
// Avatar: Three.js-сцена с простейшей головой-«гомункулом».
// Слушает WS-мост на ws://127.0.0.1:8765 и реагирует на команды:
//   speak  → анимация губ (scaleY текстуры рта)
//   emote  → смена цвета материала
//   idle   → лёгкое покачивание
// =========================================================================

const EMOTE_COLORS: Record<string, string> = {
  neutral:  "#7c5cff",
  happy:    "#4ade80",
  sad:      "#60a5fa",
  thinking: "#fbbf24",
  angry:    "#f87171",
};

function Homunculus({ stateRef }: { stateRef: React.MutableRefObject<AvatarState> }) {
  const headRef = useRef<THREE.Group>(null);
  const mouthRef = useRef<THREE.Mesh>(null);
  const matRef = useRef<THREE.MeshStandardMaterial>(null);
  const t0 = useRef(0);

  useFrame((_, dt) => {
    t0.current += dt;
    const st = stateRef.current;
    if (!headRef.current) return;
    // Лёгкое покачивание
    headRef.current.rotation.y = Math.sin(t0.current * 0.8) * 0.15;
    headRef.current.position.y = 0.5 + Math.sin(t0.current * 1.2) * 0.03;

    // Анимация губ при speech
    if (mouthRef.current) {
      const amp = st.speaking ? 0.4 + 0.6 * Math.abs(Math.sin(t0.current * 18)) : 0.1;
      mouthRef.current.scale.y = amp;
    }
    // Цвет по эмоции
    if (matRef.current) {
      const target = new THREE.Color(EMOTE_COLORS[st.emotion] ?? EMOTE_COLORS.neutral);
      matRef.current.color.lerp(target, 0.1);
    }
    // Угасание speaking-флага по таймауту
    if (st.speaking && t0.current - st.speakStart > st.speakDuration) {
      st.speaking = false;
    }
  });

  return (
    <group ref={headRef} position={[0, 0.5, 0]}>
      {/* голова */}
      <mesh>
        <icosahedronGeometry args={[1, 2]} />
        <meshStandardMaterial
          ref={matRef}
          color={EMOTE_COLORS.neutral}
          roughness={0.4}
          metalness={0.1}
          emissive={EMOTE_COLORS.neutral}
          emissiveIntensity={0.15}
        />
      </mesh>
      {/* глаза */}
      <mesh position={[-0.35, 0.2, 0.85]}>
        <sphereGeometry args={[0.12, 16, 16]} />
        <meshStandardMaterial color="white" />
      </mesh>
      <mesh position={[0.35, 0.2, 0.85]}>
        <sphereGeometry args={[0.12, 16, 16]} />
        <meshStandardMaterial color="white" />
      </mesh>
      <mesh position={[-0.35, 0.2, 0.95]}>
        <sphereGeometry args={[0.05, 12, 12]} />
        <meshStandardMaterial color="#0e0f13" />
      </mesh>
      <mesh position={[0.35, 0.2, 0.95]}>
        <sphereGeometry args={[0.05, 12, 12]} />
        <meshStandardMaterial color="#0e0f13" />
      </mesh>
      {/* рот */}
      <mesh ref={mouthRef} position={[0, -0.4, 0.9]}>
        <boxGeometry args={[0.5, 0.08, 0.05]} />
        <meshStandardMaterial color="#1a1a1a" />
      </mesh>
    </group>
  );
}

interface AvatarState {
  emotion: string;
  speaking: boolean;
  speakStart: number;
  speakDuration: number;
}

function useAvatarWS(stateRef: React.MutableRefObject<AvatarState>, onStatus: (s: string) => void) {
  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry: number | undefined;
    let closed = false;

    const connect = () => {
      ws = new WebSocket("ws://127.0.0.1:8765");
      ws.onopen = () => onStatus("online");
      ws.onclose = () => {
        onStatus("offline");
        if (!closed) retry = window.setTimeout(connect, 2000);
      };
      ws.onerror = () => ws?.close();
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type !== "command") return;
          const st = stateRef.current;
          if (msg.action === "speak") {
            st.speaking = true;
            st.speakStart = performance.now() / 1000;
            // ~65ms на символ, минимум 1.2с, максимум 12с
            const len = (msg.text || "").length;
            st.speakDuration = Math.min(12, Math.max(1.2, len * 0.065));
          } else if (msg.action === "emote") {
            st.emotion = msg.emotion || "neutral";
          } else if (msg.action === "idle") {
            st.emotion = "neutral";
            st.speaking = false;
          }
        } catch { /* ignore */ }
      };
    };
    connect();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      ws?.close();
    };
  }, [stateRef, onStatus]);
}

// =========================================================================
// App
// =========================================================================
interface ToolCallTrace {
  tool_name: string;
  arguments: string;
  result: string;
  duration_ms: number;
  ok: boolean;
}
interface AgentResponse {
  session_id: string;
  final_text: string;
  tool_calls: ToolCallTrace[];
  tokens_used: number;
}
interface ChatMsg {
  role: "user" | "assistant";
  text: string;
  tools?: ToolCallTrace[];
  tokens?: number;
  ts: number;
}

export default function App() {
  const avatarState = useRef<AvatarState>({
    emotion: "neutral", speaking: false, speakStart: 0, speakDuration: 0,
  });
  const [status, setStatus] = useState<"online" | "offline">("offline");
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const messagesEnd = useRef<HTMLDivElement>(null);

  useAvatarWS(avatarState, (s) => setStatus(s as "online" | "offline"));

  useEffect(() => {
    messagesEnd.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const send = async () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setBusy(true);
    setMessages((m) => [...m, { role: "user", text, ts: Date.now() }]);
    try {
      const resp = await invoke<AgentResponse>("send_message", { text });
      setMessages((m) => [...m, {
        role: "assistant",
        text: resp.final_text,
        tools: resp.tool_calls,
        tokens: resp.tokens_used,
        ts: Date.now(),
      }]);
    } catch (e) {
      setMessages((m) => [...m, {
        role: "assistant",
        text: `Ошибка: ${e}`,
        ts: Date.now(),
      }]);
    } finally {
      setBusy(false);
    }
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="app">
      <div className="avatar-zone">
        <Canvas camera={{ position: [0, 0.5, 4], fov: 45 }}>
          <ambientLight intensity={0.4} />
          <directionalLight position={[3, 5, 4]} intensity={0.8} />
          <Homunculus stateRef={avatarState} />
        </Canvas>
        <div className="avatar-status">
          <span>
            <span className={`dot ${status}`} />
            Аватар: {status === "online" ? "подключён" : "отключён"}
          </span>
          <span>{avatarState.current.emotion}</span>
        </div>
      </div>

      <div className="chat-zone">
        <div className="chat-header">
          <h1>Aionet</h1>
          <span className="subtitle">Локальный AI-агент · ZeroMQ · MCP</span>
        </div>
        <div className="chat-messages">
          {messages.length === 0 && (
            <div className="msg assistant">
              Привет! Я — Aionet, локальный AI-агент. Спросите что-нибудь —
              я могу запускать shell-команды, читать/писать файлы, искать пакеты
              через WinGet и работать с браузером.
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i}>
              <div className={`msg ${m.role}`}>
                {m.tokens !== undefined && (
                  <div className="meta">
                    {new Date(m.ts).toLocaleTimeString()} · {m.tokens} токенов
                  </div>
                )}
                {m.text}
              </div>
              {m.tools && m.tools.length > 0 && (
                <div className="tool-trace">
                  {m.tools.map((t, j) => (
                    <div key={j}>
                      <span className={t.ok ? "ok" : "err"}>
                        {t.ok ? "✓" : "✗"} {t.tool_name}
                      </span>{" "}
                      ({t.duration_ms}мс)
                      <pre style={{ margin: "4px 0 0 0", whiteSpace: "pre-wrap" }}>
                        args: {t.arguments}
                        {"\n"}res:  {t.result.slice(0, 240)}
                      </pre>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
          <div ref={messagesEnd} />
        </div>
        <div className="chat-input">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Напишите сообщение агенту… (Enter — отправить, Shift+Enter — перенос)"
            rows={1}
            disabled={busy}
          />
          <button onClick={send} disabled={busy || !input.trim()}>
            {busy ? "…" : "Отправить"}
          </button>
        </div>
      </div>
    </div>
  );
}

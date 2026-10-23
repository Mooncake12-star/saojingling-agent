import { useEffect, useRef, useState } from "react";
import { api, streamChat } from "../api";

interface Msg {
  role: "user" | "assistant";
  content: string;
}

interface Kb {
  id: number;
  name: string;
}

/** 聊天页：SSE 流式渲染 + 会话隔离 + 可选知识库。 */
export default function ChatPage() {
  // session_id 由前端生成、每个会话一个，后端按它隔离对话历史
  const [sessionId] = useState(() => crypto.randomUUID());
  const [kbs, setKbs] = useState<Kb[]>([]);
  const [kbId, setKbId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // 加载一次知识库列表供选择；不选也能聊（Agent 默认走主知识库）
  useEffect(() => {
    api<Kb[]>("/knowledge-bases").then(setKbs).catch(() => setKbs([]));
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const send = async () => {
    const query = input.trim();
    if (!query || streaming) return;
    setInput("");
    setError("");
    setMessages((m) => [...m, { role: "user", content: query }, { role: "assistant", content: "" }]);
    setStreaming(true);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamChat({
        query,
        sessionId,
        knowledgeBaseId: kbId,
        signal: controller.signal,
        onDelta: (text) =>
          setMessages((m) => {
            const copy = [...m];
            copy[copy.length - 1] = { role: "assistant", content: copy[copy.length - 1].content + text };
            return copy;
          }),
      });
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "对话失败");
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  };

  const stop = () => abortRef.current?.abort();

  const clearHistory = async () => {
    // session_id 是查询参数（FastAPI 对标量参数默认从 query 取）
    await api(`/history/clear?session_id=${encodeURIComponent(sessionId)}`, { method: "POST" });
    setMessages([]);
  };

  return (
    <div className="chat-page">
      <div className="chat-toolbar">
        <select value={kbId ?? ""} onChange={(e) => setKbId(e.target.value ? Number(e.target.value) : null)}>
          <option value="">默认知识库</option>
          {kbs.map((kb) => (
            <option key={kb.id} value={kb.id}>
              {kb.name}
            </option>
          ))}
        </select>
        <button className="ghost" onClick={clearHistory} disabled={streaming}>
          清空本会话
        </button>
      </div>

      <div className="chat-list">
        {messages.length === 0 && (
          <div className="chat-welcome">
            <div className="logo">🤖</div>
            <h2>你好，我是扫净灵</h2>
            <p>机器出故障、选型纠结、保养疑问，直接问我就行</p>
            <div className="suggestions">
              {["机器人轮子不转了怎么办", "如何保养滤网和边刷", "推荐一款适合 100 平米的机型"].map((q) => (
                <button key={q} onClick={() => setInput(q)}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg-row ${m.role}`}>
            <div className={`avatar ${m.role === "assistant" ? "bot" : "me"}`}>
              {m.role === "assistant" ? "🤖" : "🙂"}
            </div>
            <div className={`bubble ${m.role}`}>
              {m.content ||
                (streaming && i === messages.length - 1 ? (
                  <span className="typing">
                    <i />
                    <i />
                    <i />
                  </span>
                ) : (
                  ""
                ))}
            </div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      {error && <div className="error bar">{error}</div>}

      <div className="chat-input">
        <textarea
          rows={2}
          value={input}
          placeholder={streaming ? "正在生成回答…" : "输入问题，回车发送"}
          disabled={streaming}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
        />
        {streaming ? (
          <button className="danger" onClick={stop}>
            停止
          </button>
        ) : (
          <button onClick={() => void send()} disabled={!input.trim()}>
            发送
          </button>
        )}
      </div>
    </div>
  );
}

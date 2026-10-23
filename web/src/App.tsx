import { useState } from "react";
import { AuthProvider, useAuth } from "./auth";
import LoginPage from "./pages/LoginPage";
import ChatPage from "./pages/ChatPage";
import KnowledgePage from "./pages/KnowledgePage";

type Tab = "chat" | "kb";

function Shell() {
  const { user, loading, logout } = useAuth();
  const [tab, setTab] = useState<Tab>("chat");

  if (loading) return <div className="center">加载中…</div>;
  if (!user) return <LoginPage />;

  return (
    <div className="app">
      <div className="starry" aria-hidden="true" />
      <header>
        <strong>扫净灵</strong>
        <nav>
          <button className={tab === "chat" ? "tab active" : "tab"} onClick={() => setTab("chat")}>
            对话
          </button>
          <button className={tab === "kb" ? "tab active" : "tab"} onClick={() => setTab("kb")}>
            知识库
          </button>
        </nav>
        <span className="meta">{user.email}</span>
        <button className="ghost" onClick={() => void logout()}>
          退出
        </button>
      </header>
      <main>
        {/* 两个页面都保持挂载、用 CSS 切换显隐：切去知识库再回来，
            聊天记录和会话 id 不丢（卸载组件会销毁其内部状态） */}
        <div style={{ display: tab === "chat" ? "block" : "none", height: "100%" }}>
          <ChatPage />
        </div>
        <div style={{ display: tab === "kb" ? "block" : "none", height: "100%" }}>
          <KnowledgePage />
        </div>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Shell />
    </AuthProvider>
  );
}

import { useState } from "react";
import { useAuth } from "../auth";

/** 登录 / 注册二合一页面。 */
export default function LoginPage() {
  const { login, register } = useAuth();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (mode === "login") await login(email, password);
      else await register(email, password);
    } catch (err) {
      setError(err instanceof Error ? err.message : "出错了，请重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-wrap">
      <div className="starry" aria-hidden="true" />
      <form className="login-card" onSubmit={submit}>
        <div className="brand">
          <div className="logo">🤖</div>
          <h1>扫净灵</h1>
          <p className="sub">扫地机器人智能售后助手</p>
          <div className="features">
            <span>⚡ 故障秒级诊断</span>
            <span>📷 拍照识故障</span>
            <span>📚 产品知识库</span>
          </div>
        </div>

        <label>
          邮箱
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
            autoFocus
          />
        </label>
        <label>
          密码
          <input
            type="password"
            required
            minLength={6}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="至少 6 位"
          />
        </label>

        {error && <div className="error">{error}</div>}

        <button type="submit" disabled={busy}>
          {busy ? "请稍候…" : mode === "login" ? "登录" : "注册并登录"}
        </button>

        <button
          type="button"
          className="link"
          onClick={() => {
            setMode(mode === "login" ? "register" : "login");
            setError("");
          }}
        >
          {mode === "login" ? "没有账号？注册一个" : "已有账号？去登录"}
        </button>
      </form>
    </div>
  );
}

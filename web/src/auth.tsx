// 登录态上下文：App 启动时用 token 换一次用户信息，判断是否已登录。
import { createContext, useContext, useEffect, useState } from "react";
import { api, clearTokens, getAccessToken, login as apiLogin, type ApiError } from "./api";

export interface User {
  id: number;
  email: string;
  role: string;
}

interface AuthState {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  // 首次进入：有 token 就问后端「我是谁」，顺便完成 token 有效性校验
  useEffect(() => {
    if (!getAccessToken()) {
      setLoading(false);
      return;
    }
    api<User>("/auth/me")
      .then(setUser)
      .catch(() => clearTokens())
      .finally(() => setLoading(false));
  }, []);

  const login = async (email: string, password: string) => {
    await apiLogin(email, password);
    setUser(await api<User>("/auth/me"));
  };

  const register = async (email: string, password: string) => {
    await api("/auth/register", { method: "POST", body: JSON.stringify({ email, password }) });
    // 注册成功直接登录，省一步
    await login(email, password);
  };

  const logout = async () => {
    try {
      await api("/auth/logout", { method: "POST" });
    } catch (e) {
      // token 已失效也照样本地登出
      if ((e as ApiError).status !== 401) throw e;
    }
    clearTokens();
    setUser(null);
  };

  return (
    <AuthContext.Provider value={{ user, loading, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth 必须在 AuthProvider 内使用");
  return ctx;
}

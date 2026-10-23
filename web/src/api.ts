// 统一的请求层：自动带 token、自动用 refresh 换新、SSE 流式读取。
// 所有路径都带 /api 前缀，由 vite 代理转发到 localhost:8000 并剥掉前缀。

const TOKEN_KEY = "sql_access";
const REFRESH_KEY = "sql_refresh";

export function getAccessToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_KEY);
}

export function saveTokens(access: string, refresh: string): void {
  localStorage.setItem(TOKEN_KEY, access);
  localStorage.setItem(REFRESH_KEY, refresh);
}

export function clearTokens(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(REFRESH_KEY);
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function parseError(res: Response): Promise<never> {
  let detail = `请求失败（${res.status}）`;
  try {
    const body = await res.json();
    if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    /* 忽略解析失败，用默认信息 */
  }
  throw new ApiError(res.status, detail);
}

/** 用 refresh 换一对新 token；失败则清空登录态。 */
async function tryRefresh(): Promise<boolean> {
  const refresh = getRefreshToken();
  if (!refresh) return false;
  const res = await fetch("/api/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refresh }),
  });
  if (!res.ok) {
    clearTokens();
    return false;
  }
  const pair = (await res.json()) as { access_token: string; refresh_token: string };
  saveTokens(pair.access_token, pair.refresh_token);
  return true;
}

/** 普通 JSON 请求：401 时自动刷新一次 token 再重试。 */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getAccessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");

  let res = await fetch(`/api${path}`, { ...init, headers });
  if (res.status === 401 && (await tryRefresh())) {
    headers.set("Authorization", `Bearer ${getAccessToken()}`);
    res = await fetch(`/api${path}`, { ...init, headers });
  }
  if (!res.ok) await parseError(res);
  return (await res.json()) as T;
}

/** 登录是表单格式（OAuth2 规范），不是 JSON。 */
export async function login(email: string, password: string): Promise<void> {
  const form = new URLSearchParams({ username: email, password });
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: form.toString(),
  });
  if (!res.ok) await parseError(res);
  const pair = (await res.json()) as { access_token: string; refresh_token: string };
  saveTokens(pair.access_token, pair.refresh_token);
}

export interface ChatOptions {
  query: string;
  sessionId: string;
  knowledgeBaseId: number | null;
  onDelta: (text: string) => void;
  signal?: AbortSignal;
}

/**
 * 流式对话：用 fetch 读 SSE（EventSource 不支持 POST）。
 * 服务端格式：data: <文本片段>\n\n，最后一条是 data: [DONE]。
 * 401 时先刷新 token，重发一次。
 */
export async function streamChat(opts: ChatOptions): Promise<void> {
  const send = async (): Promise<Response> => {
    const token = getAccessToken();
    return fetch("/api/chat/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({
        query: opts.query,
        session_id: opts.sessionId,
        knowledge_base_id: opts.knowledgeBaseId,
      }),
      signal: opts.signal,
    });
  };

  let res = await send();
  if (res.status === 401 && (await tryRefresh())) res = await send();
  if (!res.ok) await parseError(res);
  if (!res.body) throw new ApiError(500, "服务端没有返回流");

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE 消息以空行分隔；逐条取出处理，残包留在 buffer
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      for (const line of part.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trimStart();
        if (payload === "[DONE]") return;
        if (!payload) continue;
        // 服务端把片段 JSON 编码后发出（防换行破坏 SSE 帧结构），这里还原；
        // 解析失败按纯文本兜底，兼容旧格式
        try {
          opts.onDelta(JSON.parse(payload) as string);
        } catch {
          opts.onDelta(payload);
        }
      }
    }
  }
}

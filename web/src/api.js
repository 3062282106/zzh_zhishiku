import { auth, clearToken } from "./auth";

export class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function explain(data, status) {
  if (data && typeof data.detail === "string") return data.detail;
  if (data?.detail && typeof data.detail.message === "string") return data.detail.message;
  if (status === 401) return "登录已过期，请重新登录。";
  if (status === 404) return "找不到对象，可能已被删除，请刷新后重试。";
  if (status === 409) return "数据已变化，请刷新后重试。";
  if (status === 413) return "文件超过大小上限。";
  if (status === 422) return "提交的内容不符合要求，请检查后重试。";
  if (status === 503) return "后台尚未配置完成，请联系运维。";
  return "请求失败（HTTP " + status + "）。";
}

async function request(path, options = {}) {
  const { method = "GET", body, signal, tokenOverride, raw = false } = options;
  const token = tokenOverride ?? auth.token;
  const headers = token ? { Authorization: "Bearer " + token } : {};
  const form = body instanceof FormData;
  if (body && !form) headers["Content-Type"] = "application/json";

  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      cache: "no-store",
      signal,
      body: form ? body : body ? JSON.stringify(body) : undefined,
    });
  } catch (error) {
    if (error && error.name === "AbortError") throw error;
    throw new ApiError("无法连接后台。请检查网络或 SSH 隧道，再重试。", 0);
  }

  if (raw && response.ok) return response;
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    // Only drop the stored token when the request used it: a token being tried
    // out on the login screen must not touch the session.
    if (tokenOverride === undefined && auth.token === token) clearToken();
    const detail = data && typeof data.detail === "string" ? data.detail : "登录已过期，请重新登录。";
    throw new ApiError(detail, 401);
  }
  if (!response.ok) throw new ApiError(explain(data, response.status), response.status, data?.detail?.code);
  return data;
}

export const api = (path, options) => request("/v1/admin" + path, options);
export const sessionApi = (path, options) => request("/v1/session" + path, options);
export const knowledgeApi = (path, options) => request("/v1/knowledge" + path, options);
export const enc = (value) => encodeURIComponent(String(value));

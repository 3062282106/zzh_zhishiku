import { auth, clearToken } from "./auth";

const BASE = "/v1/admin";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function explain(data, status) {
  if (data && typeof data.detail === "string") return data.detail;
  if (status === 401) return "管理令牌已失效，请重新连接。";
  if (status === 404) return "找不到对象，可能已被删除，请刷新后重试。";
  if (status === 409) return "数据已变化，请刷新后重试。";
  if (status === 413) return "文件超过大小上限。";
  if (status === 422) return "提交的内容不符合要求，请检查后重试。";
  if (status === 503) return "后台尚未配置完成，请联系运维。";
  return "请求失败（HTTP " + status + "）。";
}

export async function api(path, options = {}) {
  const { method = "GET", body, signal, tokenOverride } = options;
  const headers = { Authorization: "Bearer " + (tokenOverride ?? auth.token) };
  const form = body instanceof FormData;
  if (body && !form) headers["Content-Type"] = "application/json";

  let response;
  try {
    response = await fetch(BASE + path, {
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

  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    // Only drop the stored token when the request used it: a token being tried
    // out on the login screen must not touch the session.
    if (!tokenOverride) clearToken();
    const detail = data && typeof data.detail === "string" ? data.detail : "管理令牌已失效，请重新连接。";
    throw new ApiError(detail, 401);
  }
  if (!response.ok) throw new ApiError(explain(data, response.status), response.status);
  return data;
}

export const enc = (value) => encodeURIComponent(String(value));

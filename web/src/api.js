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

export function feishuError(status, code) {
  const messages = {
    mismatch: "飞书邮箱与知识库账号的企业邮箱不一致。",
    email_missing: "飞书没有返回邮箱。请确认当前飞书账号已设置企业邮箱，并检查应用的两项用户身份邮箱权限已发布。",
    token: "知识库在换取飞书授权凭据时失败。",
    identity: "知识库读取飞书账号信息时失败。",
    session: "知识库登录或上传权限已失效，请重新登录后授权。",
    cancelled: "飞书没有返回授权结果，请重新授权。",
  };
  const message = messages[status] || "飞书授权未完成，请重试。";
  return /^\d{1,12}$/.test(String(code)) ? message + "（飞书错误码 " + code + "）" : message;
}

export const api = (path, options) => request("/v1/admin" + path, options);
export const sessionApi = (path, options) => request("/v1/session" + path, options);
export const knowledgeApi = (path, options) => request("/v1/knowledge" + path, options);
export const enc = (value) => encodeURIComponent(String(value));

export function isKnowledgeFile(value) {
  return /^\/v1\/knowledge\/(files|originals)\/[\da-f/-]+(?:#page=\d+)?$/i.test(value);
}

export async function downloadKnowledgeFile(source, title) {
  if (!isKnowledgeFile(source)) return;
  const path = source.split("#")[0].slice("/v1/knowledge".length);
  const response = await knowledgeApi(path, { raw: true });
  const disposition = response.headers.get("content-disposition") || "";
  const encoded = disposition.split("UTF-8''")[1];
  const link = document.createElement("a");
  link.href = URL.createObjectURL(await response.blob());
  link.download = encoded ? decodeURIComponent(encoded) : title + ".txt";
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

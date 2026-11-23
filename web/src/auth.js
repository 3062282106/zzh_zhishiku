import { reactive } from "vue";

// The token never leaves this browser tab: sessionStorage is cleared when the tab
// closes, and the value is only ever sent as an Authorization header.
const KEY = "kb.admin.token";

function read() {
  try {
    return sessionStorage.getItem(KEY) || "";
  } catch {
    return "";
  }
}

export const auth = reactive({ token: read() });

export function setToken(value) {
  auth.token = value || "";
  try {
    if (auth.token) sessionStorage.setItem(KEY, auth.token);
    else sessionStorage.removeItem(KEY);
  } catch {
    /* private mode: keep it in memory only */
  }
}

export function clearToken() {
  setToken("");
}

import { reactive } from "vue";

const state = reactive({ items: [] });
let seq = 0;

function push(message, kind) {
  const id = ++seq;
  const item = { id, message, kind, timer: 0 };
  item.timer = setTimeout(() => dismiss(id), kind === "err" ? 9000 : 4200);
  state.items.push(item);
  if (state.items.length > 3) dismiss(state.items[0].id);
  return id;
}

export function dismiss(id) {
  const at = state.items.findIndex((item) => item.id === id);
  if (at < 0) return;
  clearTimeout(state.items[at].timer);
  state.items.splice(at, 1);
}

export const toasts = state;
export const toast = (message) => push(message, "ok");
export const toastError = (message) => push(message, "err");

/** Turn anything thrown by the API layer into a readable, dismissable message. */
export function fail(error) {
  const message = error && error.message ? error.message : "操作失败，请稍后重试。";
  push(message, "err");
}

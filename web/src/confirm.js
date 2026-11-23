import { reactive } from "vue";

/**
 * A single confirmation dialog shared by the whole console. Callers hand over the
 * destructive action itself so the dialog can keep the button busy while it runs,
 * exactly like the old console did — no "fire and forget" dialogs.
 */
export const confirmState = reactive({
  open: false,
  title: "",
  note: "",
  keys: "",
  label: "确认",
  danger: false,
  busy: false,
  action: null,
});

export function confirmBox({ title, note, label, danger = false, keys = "", action }) {
  Object.assign(confirmState, {
    open: true, title, note, keys, label, danger, busy: false, action,
  });
}

export function closeConfirm() {
  confirmState.open = false;
  confirmState.action = null;
  confirmState.busy = false;
}

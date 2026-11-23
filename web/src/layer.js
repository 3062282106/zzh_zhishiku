/**
 * Overlays register themselves so Escape closes only the newest one — a confirm
 * dialog opened from inside a drawer must not take the drawer down with it.
 */
const stack = [];

export function pushLayer(close) {
  const entry = () => close();
  stack.push(entry);
  return () => {
    const at = stack.indexOf(entry);
    if (at >= 0) stack.splice(at, 1);
  };
}

export function closeTopLayer() {
  const close = stack[stack.length - 1];
  if (close) close();
}

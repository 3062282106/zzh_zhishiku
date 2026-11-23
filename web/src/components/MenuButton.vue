<script setup>
import { ref, onUnmounted } from "vue";

const props = defineProps({
  label: { type: String, default: "更多" },
  size: { type: String, default: "sm" },
});
const open = ref(false);
const pos = ref({});
const button = ref(null);
let detach = null;

function close() {
  open.value = false;
  if (detach) {
    detach();
    detach = null;
  }
}

function show() {
  const rect = button.value.getBoundingClientRect();
  const width = 168;
  const left = Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8));
  pos.value = { top: rect.bottom + 6 + "px", left: left + "px" };
  open.value = true;
  // A click anywhere else dismisses the menu, like a native popover.
  detach = () => document.removeEventListener("click", close);
  document.addEventListener("click", close);
}

function toggle() {
  open.value ? close() : show();
}

onUnmounted(close);
</script>

<template>
  <button ref="button" class="btn" :class="size" @click.stop="toggle">{{ label }}</button>
  <Teleport to="body">
    <div v-if="open" class="menu card" :style="pos">
      <slot :close="close" />
    </div>
  </Teleport>
</template>

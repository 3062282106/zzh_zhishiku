<script setup>
import { ref, watch, onUnmounted } from "vue";
import { confirmState, closeConfirm } from "../confirm";
import { pushLayer } from "../layer";
import { fail } from "../toast";

let release = null;

watch(
  () => confirmState.open,
  (open) => {
    if (open) {
      release = pushLayer(closeConfirm);
    } else if (release) {
      release();
      release = null;
    }
  },
);
onUnmounted(() => release && release());

async function run() {
  if (confirmState.busy || !confirmState.action) return;
  confirmState.busy = true;
  try {
    await confirmState.action();
    closeConfirm();
  } catch (error) {
    fail(error);
    confirmState.busy = false;
  }
}
</script>

<template>
  <Teleport to="body">
    <template v-if="confirmState.open">
      <div class="overlay" @click="closeConfirm"></div>
      <div class="modal" role="dialog" aria-modal="true">
        <h3>{{ confirmState.title }}</h3>
        <p>{{ confirmState.note }}</p>
        <div v-if="confirmState.keys" class="keys">{{ confirmState.keys }}</div>
        <div class="modal-foot">
          <button class="btn" @click="closeConfirm">取消</button>
          <button class="btn" :class="confirmState.danger ? 'danger' : 'primary'" :disabled="confirmState.busy"
                  @click="run">{{ confirmState.label }}</button>
        </div>
      </div>
    </template>
  </Teleport>
</template>

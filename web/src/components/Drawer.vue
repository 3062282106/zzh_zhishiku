<script setup>
import { ref, onMounted, onUnmounted, nextTick } from "vue";
import { pushLayer } from "../layer";

const props = defineProps({
  title: { type: String, default: "" },
  subtitle: { type: String, default: "" },
  // Put the caret in the first empty field: the reason a drawer opens is almost
  // always to type something.
  autofocus: { type: Boolean, default: true },
});
const emit = defineEmits(["close"]);

const body = ref(null);
let release = null;

onMounted(async () => {
  release = pushLayer(() => emit("close"));
  if (!props.autofocus) return;
  await nextTick();
  // Only text-like fields: a date picker or a checkbox is never the reason the
  // drawer was opened, so leaving focus on the body is better than guessing.
  const field = body.value && body.value.querySelector(
    'input[type=text], input[type=url], input[type=email], input[type=password], textarea, select',
  );
  if (field) field.focus();
});
onUnmounted(() => release && release());
</script>

<template>
  <Teleport to="body">
    <div class="overlay" @click="$emit('close')"></div>
    <div class="drawer" role="dialog" aria-modal="true">
      <div class="drawer-head">
        <div>
          <h3>{{ title }}</h3>
          <p v-if="subtitle">{{ subtitle }}</p>
        </div>
        <button class="close" @click="$emit('close')">关闭</button>
      </div>
      <div ref="body" class="drawer-body"><slot /></div>
      <div class="drawer-foot"><slot name="footer" /></div>
    </div>
  </Teleport>
</template>

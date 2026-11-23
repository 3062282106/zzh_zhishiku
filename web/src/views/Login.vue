<script setup>
import { ref, onMounted } from "vue";
import { setToken } from "../auth";
import { loadLibraries } from "../store";

const emit = defineEmits(["connected"]);
const value = ref("");
const error = ref("");
const busy = ref(false);
const input = ref(null);

async function submit() {
  const token = value.value.trim();
  if (!token || busy.value) return;
  busy.value = true;
  error.value = "";
  // Validate before storing: setting the token first would swap this screen for
  // the shell, destroy this component and swallow the error message.
  try {
    await loadLibraries(token);
    setToken(token);
    value.value = "";
    emit("connected");
  } catch (problem) {
    error.value = problem.message;
  } finally {
    busy.value = false;
  }
}

onMounted(() => input.value && input.value.focus());
</script>

<template>
  <div class="login">
    <div class="box">
      <div style="display:flex;align-items:center;gap:10px">
        <div class="mark">知</div><b style="font-size:14px">知序 · 企业知识库</b>
      </div>
      <h1>连接管理后台</h1>
      <p>输入管理令牌后维护文档、来源与成员权限。令牌保存在本标签页的会话里，关闭标签页即失效。</p>
      <form style="display:grid;gap:10px" @submit.prevent="submit">
        <input ref="input" v-model="value" type="password" placeholder="管理令牌" autocomplete="off"
               aria-label="管理令牌" required>
        <button class="btn primary" style="height:38px" :disabled="busy">连接后台</button>
      </form>
      <div class="err">{{ error }}</div>
    </div>
  </div>
</template>

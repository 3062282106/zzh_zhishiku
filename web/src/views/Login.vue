<script setup>
import { ref, onMounted } from "vue";
import { sessionApi } from "../api";
import { setToken } from "../auth";

const emit = defineEmits(["connected"]);
const username = ref("");
const password = ref("");
const error = ref("");
const busy = ref(false);
const userInput = ref(null);

async function submit() {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const session = await sessionApi("/login", {
      method: "POST",
      tokenOverride: "",
      body: { username: username.value, password: password.value },
    });
    setToken(session.token);
    password.value = "";
    emit("connected");
  } catch (problem) {
    error.value = problem.message;
  } finally {
    busy.value = false;
  }
}

onMounted(() => userInput.value && userInput.value.focus());
</script>

<template>
  <div class="login">
    <div class="box">
      <div style="display:flex;align-items:center;gap:10px">
        <div class="mark">知</div><b style="font-size:14px">知序 · 企业知识库</b>
      </div>
      <h1>登录知识库</h1>
      <form style="display:grid;gap:10px" @submit.prevent="submit">
        <input ref="userInput" v-model="username" type="text" placeholder="用户名或成员 ID" autocomplete="username"
               aria-label="用户名或成员 ID" required>
        <input v-model="password" type="password" placeholder="密码或成员令牌" autocomplete="current-password"
               aria-label="密码或成员令牌" required>
        <button class="btn primary" style="height:38px" :disabled="busy">登录</button>
      </form>
      <div class="err">{{ error }}</div>
    </div>
  </div>
</template>

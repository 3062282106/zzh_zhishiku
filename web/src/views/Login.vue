<script setup>
import { ref, onMounted } from "vue";
import { api } from "../api";
import { setToken } from "../auth";
import { loadLibraries } from "../store";

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
  // Exchange the password for a session token first, then validate the token before
  // storing it: setting it earlier would swap this screen for the shell, destroy the
  // component and swallow the error message.
  try {
    const session = await api("/login", {
      method: "POST",
      tokenOverride: "",
      body: { username: username.value, password: password.value },
    });
    await loadLibraries(session.token);
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
      <h1>登录管理后台</h1>
      <p>用管理员账号维护文档、来源与成员权限。登录状态保存在本标签页，关闭标签页即失效。</p>
      <form style="display:grid;gap:10px" @submit.prevent="submit">
        <input ref="userInput" v-model="username" type="text" placeholder="用户名" autocomplete="username"
               aria-label="用户名" required>
        <input v-model="password" type="password" placeholder="密码" autocomplete="current-password"
               aria-label="密码" required>
        <button class="btn primary" style="height:38px" :disabled="busy">登录</button>
      </form>
      <div class="err">{{ error }}</div>
    </div>
  </div>
</template>

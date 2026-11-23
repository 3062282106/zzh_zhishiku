<script setup>
import { ref, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { sessionApi } from "../api";
import { setToken } from "../auth";

const emit = defineEmits(["connected"]);
const username = ref("");
const password = ref("");
const error = ref("");
const busy = ref(false);
const userInput = ref(null);
const settingPassword = ref(false);
const setupCode = ref("");
const contactUrl = ref("");
const route = useRoute();
const router = useRouter();

function cancelSetup() {
  settingPassword.value = false;
  setupCode.value = "";
  password.value = "";
  error.value = "";
}

async function submit() {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const session = await sessionApi("/login", {
      method: "POST",
      tokenOverride: "",
      body: { username: username.value, password: password.value,
        setup_code: settingPassword.value ? setupCode.value.trim() : null },
    });
    setToken(session.token);
    password.value = "";
    emit("connected");
  } catch (problem) {
    if (problem.code === "setup_required") settingPassword.value = true;
    if (problem.code === "setup_used" || problem.code === "setup_expired") cancelSetup();
    error.value = problem.message;
  } finally {
    busy.value = false;
  }
}

onMounted(() => {
  const setup = new URLSearchParams(window.location.hash.slice(1));
  if (setup.get("setup") && setup.get("email")) {
    username.value = setup.get("email");
    setupCode.value = setup.get("setup");
    settingPassword.value = true;
    router.replace({ path: route.path, query: route.query, hash: "" });
  } else {
    userInput.value?.focus();
  }
  sessionApi("/access-contact", { tokenOverride: "" }).then((config) => { contactUrl.value = config.url; }).catch(() => {});
});
</script>

<template>
  <div class="login">
    <div class="box">
      <div style="display:flex;align-items:center;gap:10px">
        <div class="mark">知</div><b style="font-size:14px">知序 · 企业知识库</b>
      </div>
      <h1>{{ settingPassword ? "设置知识库密码" : "登录知识库" }}</h1>
      <form style="display:grid;gap:10px" @submit.prevent="submit">
        <input ref="userInput" v-model="username" type="text" placeholder="成员邮箱或管理员用户名" autocomplete="username"
               aria-label="成员邮箱或管理员用户名" maxlength="200" required>
        <input v-model="password" type="password" :placeholder="settingPassword ? '设置新密码（至少 10 位）' : '密码'"
               :autocomplete="settingPassword ? 'new-password' : 'current-password'"
               :aria-label="settingPassword ? '新密码' : '密码'" :minlength="settingPassword ? 10 : undefined" maxlength="200" required>
        <input v-if="settingPassword" v-model="setupCode" type="password" placeholder="管理员发给你的设置码"
               aria-label="设置码" autocomplete="off" maxlength="128" required>
        <button class="btn primary" style="height:38px" :disabled="busy">{{ settingPassword ? "设置密码并登录" : "登录" }}</button>
      </form>
      <div v-if="error" class="err" role="alert">{{ error }}</div>
      <button v-if="settingPassword" class="btn link" @click="cancelSetup">返回登录</button>
      <a v-else-if="contactUrl" class="btn" :href="contactUrl" target="_blank" rel="noopener noreferrer">找管理员申请开通</a>
      <button v-else class="btn" disabled title="管理员尚未配置飞书私聊链接">找管理员申请开通</button>
    </div>
  </div>
</template>

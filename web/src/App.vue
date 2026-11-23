<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { auth, clearToken, setIdentity } from "./auth";
import { sessionApi } from "./api";
import { loadShell, resetStore } from "./store";
import { fail } from "./toast";
import { closeTopLayer } from "./layer";
import Login from "./views/Login.vue";
import ConfirmDialog from "./components/ConfirmDialog.vue";
import ToastHost from "./components/ToastHost.vue";

const NAV = [
  { title: "知识管理", items: [
    { key: "kb", label: "知识库", to: "/kb" },
    { key: "sources", label: "同步来源", to: "/sources" },
  ] },
];
const TITLES = {
  kb: "知识库", doc: "知识库", sources: "同步来源",
};

const route = useRoute();
const router = useRouter();
const activeKey = computed(() => (route.name === "doc" ? "kb" : route.name));
const title = computed(() => route.meta.operations ? "运维管理" : TITLES[route.name] || "知识库");
const view = ref(null);
const checking = ref(false);
const sessionError = ref("");

// The scroll container is .view, not the window, so router scrollBehavior cannot
// reset it. Without this, opening a long document and going back lands you
// halfway down the next page.
watch(
  () => route.fullPath,
  () => {
    if (view.value) view.value.scrollTop = 0;
  },
);

watch([() => auth.me?.role, () => route.name], ([role, page]) => {
  if (role === "member" && page !== "query") router.replace({ name: "query" });
  if (role === "admin" && page === "query") router.replace({ name: "kb" });
});

async function connected() {
  const token = auth.token;
  if (!token || checking.value) return;
  checking.value = true;
  sessionError.value = "";
  try {
    const identity = await sessionApi("/me");
    if (auth.token !== token) return;
    setIdentity(identity);
    if (identity.role === "admin") {
      const failed = await loadShell();
      if (failed) fail(failed.reason);
    }
  } catch (error) {
    if (auth.token === token) sessionError.value = error.message;
    else if (!auth.token) fail(error);
  } finally {
    checking.value = false;
  }
}

// A reload or a deep link arrives with a token already in the tab: load the shell
// before the page asks for anything else.
onMounted(() => {
  if (auth.token) connected();
});
watch(() => auth.token, (token) => { if (!token) resetStore(); });

async function logout() {
  // Best effort: an expired session already fails server-side, and the local state
  // must be cleared either way.
  try {
    await sessionApi("/logout", { method: "POST" });
  } catch {
    /* ignore */
  }
  clearToken();
  resetStore();
  router.replace("/");
}

function onKeydown(event) {
  if (event.key === "Escape") closeTopLayer();
}
onMounted(() => window.addEventListener("keydown", onKeydown));
onUnmounted(() => window.removeEventListener("keydown", onKeydown));
</script>

<template>
  <Login v-if="!auth.token" @connected="connected" />

  <div v-else-if="!auth.me" class="login">
    <div class="box" role="status">
      <h1>{{ sessionError || "正在验证会话…" }}</h1>
      <div v-if="sessionError" class="rowacts">
        <button class="btn" @click="clearToken">返回登录</button>
        <button class="btn primary" @click="connected">重试</button>
      </div>
    </div>
  </div>

  <router-view v-else-if="auth.me.role === 'member' && route.name === 'query'" @logout="logout" />

  <div v-else-if="auth.me.role === 'admin' && route.name !== 'query'" class="app">
    <aside class="side">
      <div class="brand"><div class="mark">知</div><b>知序 · 企业知识库</b></div>
      <nav class="nav" aria-label="后台导航">
        <div v-for="group in NAV" :key="group.title" class="nav-group">
          <div class="nav-title">{{ group.title }}</div>
          <router-link v-for="item in group.items" :key="item.key" :to="item.to"
                       :class="{ on: activeKey === item.key }"><span>{{ item.label }}</span></router-link>
        </div>
      </nav>
      <div class="side-foot">
        <router-link class="btn link" to="/operations" :class="{ 'operations-active': route.meta.operations }">运维管理</router-link>
        <button class="btn link sign-out" @click="logout">退出登录</button>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <div class="crumb">{{ title }}</div>
        <div class="org">管理后台</div>
      </header>
      <div ref="view" class="view">
        <router-view />
      </div>
    </div>
  </div>

  <ConfirmDialog />
  <ToastHost />
</template>

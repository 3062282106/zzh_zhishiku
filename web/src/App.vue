<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { auth, clearToken, setIdentity } from "./auth";
import { api } from "./api";
import { loadShell, resetStore } from "./store";
import { fail } from "./toast";
import { closeTopLayer } from "./layer";
import Login from "./views/Login.vue";
import ConfirmDialog from "./components/ConfirmDialog.vue";
import ToastHost from "./components/ToastHost.vue";

const NAV = [
  { key: "kb", label: "知识库", to: "/kb" },
  { key: "sources", label: "同步来源", to: "/sources" },
  { key: "tasks", label: "处理任务", to: "/tasks" },
  { key: "members", label: "成员与权限", to: "/members" },
  { key: "administrators", label: "管理员", to: "/administrators" },
  { key: "audit", label: "操作记录", to: "/audit" },
];
const TITLES = {
  kb: "知识库", doc: "知识库", sources: "同步来源", tasks: "处理任务",
  members: "成员与权限", administrators: "管理员", audit: "操作记录", settings: "设置",
};

const route = useRoute();
const router = useRouter();
const activeKey = computed(() => (route.name === "doc" ? "kb" : route.name));
const title = computed(() => TITLES[route.name] || "知识库");
const view = ref(null);

// The scroll container is .view, not the window, so router scrollBehavior cannot
// reset it. Without this, opening a long document and going back lands you
// halfway down the next page.
watch(
  () => route.fullPath,
  () => {
    if (view.value) view.value.scrollTop = 0;
  },
);

async function connected() {
  try {
    // Ask who we are first: the shell shows a name, and a stale session must bounce
    // back to the login screen instead of loading four requests that all 401.
    setIdentity(await api("/me"));
    const failed = await loadShell();
    if (failed) fail(failed.reason);
  } catch (error) {
    fail(error);
  }
}

// A reload or a deep link arrives with a token already in the tab: load the shell
// before the page asks for anything else.
onMounted(() => {
  if (auth.token) connected();
});

async function logout() {
  // Best effort: an expired session already fails server-side, and the local state
  // must be cleared either way.
  try {
    await api("/logout", { method: "POST" });
  } catch {
    /* ignore */
  }
  clearToken();
  resetStore();
  router.replace("/kb");
}

function onKeydown(event) {
  if (event.key === "Escape") closeTopLayer();
}
onMounted(() => window.addEventListener("keydown", onKeydown));
onUnmounted(() => window.removeEventListener("keydown", onKeydown));
</script>

<template>
  <Login v-if="!auth.token" @connected="connected" />

  <div v-else class="app">
    <aside class="side">
      <div class="brand"><div class="mark">知</div><b>知序 · 企业知识库</b></div>
      <div class="workspace">企业知识库 / 工作空间</div>
      <nav class="nav">
        <router-link v-for="item in NAV" :key="item.key" :to="item.to"
                     :class="{ on: activeKey === item.key }"><span>{{ item.label }}</span></router-link>
      </nav>
      <div class="side-foot">
        <router-link class="btn link" to="/settings">设置</router-link>
        <div class="who">{{ auth.me ? auth.me.name : "管理员" }}</div>
        <button class="btn link" style="margin-top:8px" @click="logout">退出登录</button>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <div class="crumb">{{ title }}</div>
        <div class="org">企业版</div>
      </header>
      <div ref="view" class="view">
        <router-view />
      </div>
    </div>
  </div>

  <ConfirmDialog />
  <ToastHost />
</template>

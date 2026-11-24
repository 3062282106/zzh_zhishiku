<script setup>
import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { auth } from "../auth";
import { downloadKnowledgeFile, enc, isKnowledgeFile, knowledgeApi } from "../api";
import { chunkWhere } from "../format";
import Drawer from "../components/Drawer.vue";
import MemberCatalog from "./MemberCatalog.vue";
import PersonalLibrary from "./PersonalLibrary.vue";

defineEmits(["logout"]);

const route = useRoute();
const router = useRouter();
const showPersonal = computed(() => Boolean(route.query.personal || route.query.document || route.query.feishu));
const question = ref("");
const result = ref(null);
const error = ref("");
const busy = ref(false);
const opening = ref(false);
const downloading = ref(false);
const selected = ref(null);

async function search() {
  const query = question.value.trim();
  if (!query || busy.value) return;
  busy.value = true;
  result.value = null;
  error.value = "";
  try {
    result.value = await knowledgeApi("/search", { method: "POST", body: { query } });
  } catch (problem) {
    error.value = problem.message;
  } finally {
    busy.value = false;
  }
}

async function openReference(item) {
  opening.value = true;
  error.value = "";
  try {
    selected.value = await knowledgeApi("/references/" + enc(item.chunk_id));
  } catch (problem) {
    error.value = problem.message;
  } finally {
    opening.value = false;
  }
}

async function downloadSource(item) {
  if (downloading.value) return;
  downloading.value = true;
  try {
    await downloadKnowledgeFile(item.source_url, item.title);
  } catch (problem) {
    error.value = problem.message;
    selected.value = null;
  } finally {
    downloading.value = false;
  }
}
</script>

<template>
  <div class="member-page">
    <header class="topbar">
      <div class="member-brand"><span class="mark">知</span><b>知序 · 企业知识库</b></div>
      <div class="member-account"><span>{{ auth.me.name }}</span><button class="btn link" @click="$emit('logout')">退出登录</button></div>
    </header>
    <main>
      <nav class="member-nav" aria-label="知识库视图">
        <button class="btn" :class="{ primary: !showPersonal }" :aria-pressed="!showPersonal" @click="router.replace({ query: {} })">查询知识库</button>
        <button class="btn" :class="{ primary: showPersonal }" :aria-pressed="showPersonal" @click="router.replace({ query: { personal: '1' } })">个人知识库</button>
      </nav>
      <PersonalLibrary v-if="showPersonal" />
      <template v-else>
        <h1>查询知识库</h1>
        <form class="search-box" @submit.prevent="search">
          <input v-model="question" type="search" placeholder="输入要查询的问题" maxlength="1024" required aria-label="查询问题">
          <button class="btn primary" :disabled="busy || !question.trim()">{{ busy ? "查询中…" : "查询" }}</button>
        </form>
        <p v-if="error" class="member-error" role="alert">{{ error }}</p>
        <div v-if="result?.evidence.length" class="results">
          <h2>相关资料</h2>
          <article v-for="item in result.evidence" :key="item.chunk_id" class="card result-item">
            <h3>{{ item.title }}</h3>
            <p v-if="item.heading_path.length" class="muted">{{ item.heading_path.join(" / ") }}</p>
            <p v-if="item.locator.position_unit === 'csv_line'" class="muted">{{ chunkWhere(item, 0) }}</p>
            <p class="snippet">{{ item.snippet }}</p>
            <button class="btn link" :disabled="opening" @click="openReference(item)">查看引用</button>
          </article>
        </div>
        <p v-else-if="result" class="note" role="status">没有找到相关资料。</p>
        <MemberCatalog />
      </template>
    </main>
  </div>

  <Drawer v-if="selected" :title="selected.title" :subtitle="selected.heading_path.join(' / ')" :autofocus="false" @close="selected = null">
    <p v-if="selected.locator.position_unit === 'csv_line'" class="muted">{{ chunkWhere(selected, 0) }}</p>
    <p class="snippet">{{ selected.snippet }}</p>
    <template #footer>
      <a v-if="selected.source_url.startsWith('https://')" :href="selected.source_url" target="_blank" rel="noopener noreferrer" class="btn">打开来源 ↗</a>
      <button v-else-if="isKnowledgeFile(selected.source_url)" class="btn" :disabled="downloading" @click="downloadSource(selected)">下载原文</button>
    </template>
  </Drawer>
</template>

<style scoped>
.member-page { min-height: 100vh; }
.member-brand, .member-account, .search-box { display: flex; align-items: center; gap: 10px; }
.member-account { color: var(--muted); font-size: 13px; }
main { width: min(1000px, calc(100% - 32px)); margin: 34px auto; }
h1 { margin-bottom: 16px; font-size: 26px; }
.member-nav { display: flex; gap: 8px; margin-bottom: 20px; }
.search-box { margin-bottom: 24px; }
.search-box input { flex: 1; min-width: 0; height: 42px; padding: 0 12px; background: var(--surface); border: 1px solid var(--line-2); border-radius: 6px; }
.search-box button { height: 42px; }
.results { display: grid; gap: 12px; }
h2 { font-size: 16px; }
h3 { font-size: 15px; }
.result-item { padding: 16px; }
.result-item > .muted { font-size: 12px; margin-top: 4px; }
.snippet { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.75; margin: 8px 0; }
.member-error { color: var(--red); margin-bottom: 14px; }
</style>

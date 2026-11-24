<script setup>
import { onMounted, onUnmounted, ref } from "vue";
import { downloadKnowledgeFile, enc, knowledgeApi } from "../api";
import { SOURCE, fmtDay } from "../format";
import Drawer from "../components/Drawer.vue";
import EmptyState from "../components/EmptyState.vue";

const libraries = ref([]);
const library = ref(null);
const items = ref([]);
const next = ref(null);
const document = ref(null);
const loading = ref(true);
const documentLoading = ref(false);
const downloading = ref(false);
const libraryError = ref("");
const listError = ref("");
const previewError = ref("");
let libraryRequest;
let listRequest;
let previewRequest;

async function loadLibraries() {
  libraryRequest?.abort();
  const request = new AbortController();
  libraryRequest = request;
  libraryError.value = "";
  loading.value = true;
  try {
    libraries.value = await knowledgeApi("/libraries", { signal: request.signal });
    if (libraries.value.length) selectLibrary(libraries.value[0]);
    else loading.value = false;
  } catch (error) {
    if (!request.signal.aborted) {
      libraryError.value = error.message;
      loading.value = false;
    }
  }
}

function selectLibrary(item) {
  if (library.value?.id === item.id) return;
  library.value = item;
  closePreview();
  loadDocuments();
}

async function loadDocuments(offset = 0) {
  listRequest?.abort();
  const request = new AbortController();
  listRequest = request;
  loading.value = true;
  listError.value = "";
  if (!offset) {
    items.value = [];
    next.value = null;
  }
  try {
    const result = await knowledgeApi("/libraries/" + enc(library.value.id) + "/documents?offset=" + offset,
                                      { signal: request.signal });
    items.value = [...items.value, ...result.items];
    next.value = result.next_offset;
  } catch (error) {
    if (!request.signal.aborted) listError.value = error.message;
  } finally {
    if (listRequest === request) loading.value = false;
  }
}

async function openDocument(item) {
  previewRequest?.abort();
  const request = new AbortController();
  previewRequest = request;
  previewError.value = "";
  documentLoading.value = true;
  const path = [library.value.id, "documents", item.platform, item.id].map(enc).join("/");
  try {
    document.value = await knowledgeApi("/libraries/" + path, { signal: request.signal });
  } catch (error) {
    if (!request.signal.aborted) previewError.value = error.message;
  } finally {
    if (previewRequest === request) documentLoading.value = false;
  }
}

function closePreview() {
  previewRequest?.abort();
  document.value = null;
  previewError.value = "";
}

async function download() {
  if (downloading.value) return;
  downloading.value = true;
  previewError.value = "";
  try {
    await downloadKnowledgeFile(document.value.download_path, document.value.title);
  } catch (error) {
    previewError.value = error.message;
  } finally {
    downloading.value = false;
  }
}

onMounted(loadLibraries);
onUnmounted(() => {
  libraryRequest?.abort();
  listRequest?.abort();
  previewRequest?.abort();
});
</script>

<template>
  <section class="catalog" aria-labelledby="catalog-title">
    <h2 id="catalog-title">可访问的知识库</h2>
    <div v-if="libraryError" class="card catalog-state" role="alert">
      <p>{{ libraryError }}</p><button class="btn sm" @click="loadLibraries">重试</button>
    </div>
    <div v-else-if="!libraries.length" class="card">
      <p v-if="loading" class="loading" role="status">正在加载…</p>
      <EmptyState v-else title="暂无可访问的企业知识库" note="联系管理员申请知识库权限。" />
    </div>
    <div v-else class="catalog-layout">
      <nav class="card catalog-nav" aria-label="选择知识库">
        <button v-for="item in libraries" :key="item.id" type="button" :aria-pressed="library?.id === item.id"
                @click="selectLibrary(item)">{{ item.name }}</button>
      </nav>
      <div class="card catalog-panel">
        <div class="catalog-heading"><h3>{{ library?.name }}</h3><span class="pill draft">只读</span></div>
        <p v-if="listError || (previewError && !document)" class="catalog-error" role="alert">{{ listError || previewError }}</p>
        <p v-if="loading && !items.length" class="loading" role="status">正在加载文档…</p>
        <EmptyState v-else-if="!items.length && !listError" title="暂无已发布的文档" />
        <table v-if="items.length" class="catalog-table">
          <thead><tr><th scope="col">文档名称</th><th scope="col">来源</th><th scope="col">更新时间</th></tr></thead>
          <tbody>
            <tr v-for="item in items" :key="item.platform + ':' + item.id">
              <td><button class="catalog-link" type="button" :disabled="documentLoading" @click="openDocument(item)">{{ item.title }}</button></td>
              <td>{{ SOURCE[item.source] || '资料' }}</td>
              <td class="nowrap">{{ item.updated_at ? fmtDay(item.updated_at) : '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="next !== null || listError" class="catalog-more">
          <button class="btn sm" :disabled="loading" @click="loadDocuments(next ?? 0)">
            {{ loading ? '加载中…' : listError ? '重试' : '加载更多' }}
          </button>
        </div>
      </div>
    </div>
  </section>

  <Drawer v-if="document" :title="document.title" :subtitle="library?.name" :autofocus="false" @close="closePreview">
    <p v-if="previewError" class="catalog-error" role="alert">{{ previewError }}</p>
    <pre class="catalog-original">{{ document.original }}</pre>
    <p v-if="document.truncated" class="note">当前显示部分内容，可查看原文。</p>
    <template #footer>
      <a v-if="document.source_url?.startsWith('https://')" :href="document.source_url"
         target="_blank" rel="noopener noreferrer" class="btn">打开原文 ↗</a>
      <button v-if="document.download_path" class="btn" :disabled="downloading" @click="download">下载原文</button>
    </template>
  </Drawer>
</template>

<style scoped>
.catalog { margin-top: 28px; }
.catalog h2 { font-size: 17px; margin-bottom: 12px; }
.catalog-layout { display: grid; grid-template-columns: minmax(145px, 205px) minmax(0, 1fr); gap: 12px; align-items: start; }
.catalog-nav { display: grid; gap: 3px; padding: 6px; }
.catalog-nav button { padding: 9px 10px; color: var(--ink-2); background: transparent; text-align: left; border-radius: 6px; overflow-wrap: anywhere; }
.catalog-nav button:hover, .catalog-nav button:focus-visible, .catalog-nav button[aria-pressed=true] { background: var(--brand-weak); color: var(--brand-ink); }
.catalog-heading { padding: 13px 15px; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.catalog-heading h3 { font-size: 14px; overflow-wrap: anywhere; }
.catalog-table th:first-child { width: 59%; }
.catalog-table td:not(:first-child) { color: var(--muted); font-size: 12px; }
.catalog-link { color: var(--ink); background: transparent; text-align: left; white-space: normal; overflow-wrap: anywhere; font-weight: 550; padding: 0; }
.catalog-link:hover, .catalog-link:focus-visible { color: var(--brand); text-decoration: underline; }
.catalog-more, .catalog-state { padding: 12px 15px; }
.catalog-state { display: flex; align-items: center; justify-content: space-between; }
.catalog-error { color: var(--red); margin: 10px 15px; }
.catalog-original { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.7; }
@media (max-width: 600px) {
  .catalog-layout { grid-template-columns: 1fr; }
  .catalog-nav { display: flex; flex-wrap: wrap; }
  .catalog-nav button { flex: 1 1 auto; }
}
</style>

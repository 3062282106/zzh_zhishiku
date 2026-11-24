<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import PageHead from "../components/PageHead.vue";
import EmptyState from "../components/EmptyState.vue";
import Drawer from "../components/Drawer.vue";
import { api, enc } from "../api";
import { store, loadDocs, loadTasks, libraryById, defaultLibrary } from "../store";
import { SOURCE, fmtTime } from "../format";
import { toast, fail } from "../toast";

const route = useRoute();
const router = useRouter();

const q = ref("");
const busyId = ref("");
const newOpen = ref(false);
const busy = ref(false);
const provider = ref("feishu");
const title = ref("");
const url = ref("");
const heading = ref("");
const sourceRows = ref([]);
let timer;

const libraryId = computed(() => route.params.libraryId || "");
const library = computed(() => libraryById(libraryId.value));
const rows = computed(() => {
  const needle = q.value.trim().toLowerCase();
  return sourceRows.value.filter((row) => !needle || row.title.toLowerCase().includes(needle));
});

async function loadSources(id) {
  const sources = await api("/libraries/" + enc(id) + "/sources");
  if (libraryId.value === id) sourceRows.value = sources;
}

watch(
  () => route.params.libraryId,
  async (id) => {
    sourceRows.value = [];
    if (!id) {
      if (store.libraries.length) router.replace({ name: "sources", params: { libraryId: defaultLibrary() } });
      return;
    }
    try {
      await loadSources(id);
    } catch (error) {
      fail(error);
    }
  },
  { immediate: true },
);

watch(
  () => store.libraries.length,
  () => {
    if (!route.params.libraryId && store.libraries.length) {
      router.replace({ name: "sources", params: { libraryId: defaultLibrary() } });
    }
  },
  { immediate: true },
);

async function openSourceDoc(row) {
  if (!row.last_version_id) return toast("这个来源还没有生成文档。");
  router.push({ name: "doc", params: { libraryId: libraryId.value, docId: row.document_id } });
}

async function sync(row) {
  if (busyId.value) return;
  busyId.value = row.id;
  try {
    const result = await api("/sources/" + enc(row.id) + "/sync", { method: "POST" });
    if (result.status === "queued") {
      await loadSources(libraryId.value);
      toast("检查已排队。");
      return;
    }
    await Promise.all([loadSources(libraryId.value), loadDocs(libraryId.value), loadTasks()]);
    if (result.changed) {
      toast("已同步为新草稿，请核对后发布。");
      router.push({ name: "doc", params: { libraryId: libraryId.value, docId: result.document_id } });
    } else {
      toast("原文没有变化，未生成新版本。");
    }
  } catch (error) {
    fail(error);
  } finally {
    busyId.value = "";
  }
}

async function toggleSync(row) {
  if (busyId.value) return;
  busyId.value = row.id;
  try {
    await api("/sources/" + enc(row.id) + "/auto-sync", {
      method: "PUT", body: { enabled: !row.auto_sync },
    });
    await loadSources(libraryId.value);
    toast(row.auto_sync ? "已暂停自动同步。" : "已开启自动同步。");
  } catch (error) {
    fail(error);
  } finally {
    busyId.value = "";
  }
}

async function create() {
  if (busy.value) return;
  if (!libraryId.value) return toast("请先选择知识库。");
  if (!title.value.trim() || !url.value.trim()) return toast("请填写来源名称和原文链接。");
  busy.value = true;
  try {
    const payload = { title: title.value.trim(), url: url.value.trim() };
    if (provider.value === "feishu" && heading.value.trim()) payload.section_heading = heading.value.trim();
    const created = await api("/libraries/" + enc(libraryId.value) + "/sources/" + provider.value,
      { method: "POST", body: payload });
    newOpen.value = false;
    await loadSources(libraryId.value);
    if (provider.value === "feishu") toast("已添加来源，首次同步已排队。");
    else await sync({ id: created.id, provider: provider.value, title: payload.title });
  } catch (error) {
    fail(error);
  } finally {
    busy.value = false;
  }
}

function openNew() {
  provider.value = "feishu";
  title.value = "";
  url.value = "";
  heading.value = "";
  newOpen.value = true;
}

onMounted(() => {
  timer = setInterval(() => {
    if (!document.hidden && libraryId.value && !busyId.value) loadSources(libraryId.value).catch(() => {});
  }, 10000);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <div class="wrap">
    <PageHead title="同步来源">
      <select :value="libraryId" aria-label="目标知识库"
              @change="router.replace({ name: 'sources', params: { libraryId: $event.target.value } })">
        <option v-for="item in store.libraries" :key="item.id" :value="item.id">{{ item.name }}</option>
      </select>
      <button class="btn primary" @click="openNew">新增来源</button>
    </PageHead>

    <div class="card">
      <EmptyState v-if="!sourceRows.length" title="还没有在线来源"
                  note="添加飞书云文档或 Notion 页面链接。">
        <button class="btn primary" @click="openNew">新增来源</button>
      </EmptyState>

      <template v-else>
        <div class="filters">
          <input v-model="q" class="search" type="text" placeholder="搜索来源名称">
        </div>
        <EmptyState v-if="!rows.length" title="没有匹配的来源" note="换个关键词再试。" />
        <table v-else>
          <colgroup>
            <col style="width:30%"><col style="width:18%">
            <col style="width:26%"><col style="width:26%">
          </colgroup>
          <thead>
            <tr><th>来源名称</th><th>最近检查</th><th>状态</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id" :class="{ tall: !!row.last_error }" @click="openSourceDoc(row)">
              <td>
                <div class="t-main">{{ row.title }}</div>
                <div class="t-sub">
                  {{ SOURCE[row.provider] || row.provider }}{{ row.section_heading ? " · " + row.section_heading : "" }}
                  <template v-if="row.auto_sync"> · 每 15 分钟</template>
                </div>
              </td>
              <td class="muted">{{ row.checked_at ? fmtTime(row.checked_at) : "尚未同步" }}</td>
              <td>
                <span v-if="row.sync_requested" class="pill queued">{{ row.indexing ? "构建索引中" : "待检查" }}</span>
                <div v-else-if="row.last_error" class="source-error">
                  <span class="pill failed">同步失败</span>
                  <span>{{ row.last_error }}</span>
                </div>
                <span v-else-if="row.provider === 'feishu' && !row.auto_sync" class="pill draft">已暂停</span>
                <span v-else-if="!row.checked_at" class="pill queued">待同步</span>
                <span v-else class="pill live">正常</span>
              </td>
              <td class="right" @click.stop>
                <div class="source-actions">
                  <button class="btn sm" :disabled="busyId === row.id || row.sync_requested" @click="sync(row)">
                    {{ row.provider === "feishu" ? "立即检查" : "立即同步" }}
                  </button>
                  <button v-if="row.provider === 'feishu'" class="btn ghost sm" :disabled="busyId === row.id" @click="toggleSync(row)">
                    {{ row.auto_sync ? "暂停" : "开启自动同步" }}
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="table-foot"><span>共 {{ sourceRows.length }} 个来源</span><span></span></div>
      </template>
    </div>
  </div>

  <Drawer v-if="newOpen" title="新增同步来源" :subtitle="library ? library.name : ''" @close="newOpen = false">
    <div class="block">
      <div class="k">目标知识库</div>
      <div class="v">{{ library ? library.name : "未选择" }}</div>
    </div>
    <div class="field">
      <label>来源类型</label>
      <div class="choice">
        <button :class="{ on: provider === 'feishu' }" @click="provider = 'feishu'"><span class="dot"></span>飞书</button>
        <button :class="{ on: provider === 'notion' }" @click="provider = 'notion'"><span class="dot"></span>Notion</button>
      </div>
    </div>
    <div class="field">
      <label>来源名称</label>
      <input v-model="title" type="text" placeholder="来源名称" maxlength="160">
    </div>
    <div class="field">
      <label>原文链接</label>
      <input v-model="url" type="url" maxlength="2048"
             :placeholder="provider === 'feishu' ? 'https://…feishu.cn/wiki/…' : 'https://app.notion.com/p/…'">
    </div>
    <div v-if="provider === 'feishu'" class="field">
      <label>同步章节（可选）</label>
      <input v-model="heading" type="text" placeholder="留空同步整篇，或填写完整章节标题" maxlength="160">
    </div>
    <div v-if="provider === 'feishu'" class="note">飞书应用需能读取此文档。检查到改动后自动更新。</div>
    <div v-else class="note">同步生成草稿，发布后可检索。</div>
    <template #footer>
      <button class="btn" @click="newOpen = false">取消</button>
      <button class="btn primary" :disabled="busy" @click="create">添加并同步</button>
    </template>
  </Drawer>
</template>

<style scoped>
.source-actions { display:flex; justify-content:flex-end; flex-wrap:wrap; gap:5px; }
.source-error { display:grid; gap:5px; justify-items:start; }
.source-error > span + span { color:var(--red); font-size:12px; line-height:1.45; white-space:normal; overflow-wrap:anywhere; }
</style>

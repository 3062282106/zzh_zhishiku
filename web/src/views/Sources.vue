<script setup>
import { ref, computed, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import PageHead from "../components/PageHead.vue";
import EmptyState from "../components/EmptyState.vue";
import Drawer from "../components/Drawer.vue";
import { api, enc } from "../api";
import { store, loadSources, loadDocs, loadTasks, libraryById, defaultLibrary } from "../store";
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

const libraryId = computed(() => route.params.libraryId || "");
const library = computed(() => libraryById(libraryId.value));
const rows = computed(() => {
  const needle = q.value.trim().toLowerCase();
  return store.sources.filter((row) => !needle || row.title.toLowerCase().includes(needle));
});

watch(
  () => route.params.libraryId,
  async (id) => {
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
  if (!row.document_id) return toast("这个来源还没有生成文档。");
  router.push({ name: "doc", params: { libraryId: libraryId.value, docId: row.document_id } });
}

async function sync(row) {
  if (busyId.value) return;
  busyId.value = row.id;
  toast("正在读取" + (SOURCE[row.provider] || "来源") + "原文…");
  try {
    const result = await api("/sources/" + enc(row.id) + "/sync", { method: "POST" });
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

async function create() {
  if (busy.value) return;
  if (!libraryId.value) return toast("请先选择知识库。");
  if (!title.value.trim() || !url.value.trim()) return toast("请填写来源名称和原文链接。");
  if (provider.value === "feishu" && !heading.value.trim()) return toast("飞书来源需要填写完整章节标题。");
  busy.value = true;
  try {
    const payload = { title: title.value.trim(), url: url.value.trim() };
    if (provider.value === "feishu") payload.section_heading = heading.value.trim();
    const created = await api("/libraries/" + enc(libraryId.value) + "/sources/" + provider.value,
      { method: "POST", body: payload });
    newOpen.value = false;
    await loadSources(libraryId.value);
    await sync({ id: created.id, provider: provider.value, title: payload.title });
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
</script>

<template>
  <div class="wrap">
    <PageHead title="同步来源" subtitle="统一维护 Notion 与飞书的原文来源">
      <button class="btn primary" @click="openNew">新增来源</button>
    </PageHead>

    <div class="card">
      <EmptyState v-if="!store.sources.length" title="还没有在线来源"
                  note="绑定 Notion 页面或飞书章节后，同步会生成草稿等你发布。">
        <button class="btn primary" @click="openNew">新增来源</button>
      </EmptyState>

      <template v-else>
        <div class="filters">
          <input v-model="q" class="search" type="text" placeholder="搜索来源名称">
        </div>
        <EmptyState v-if="!rows.length" title="没有匹配的来源" note="换个关键词再试。" />
        <table v-else>
          <colgroup>
            <col style="width:28%"><col style="width:15%"><col style="width:17%">
            <col style="width:22%"><col style="width:18%">
          </colgroup>
          <thead>
            <tr><th>来源名称</th><th>目标知识库</th><th>最近同步</th><th>状态</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id" :class="{ tall: !!row.last_error }" @click="openSourceDoc(row)">
              <td>
                <div class="t-main">{{ row.title }}</div>
                <div class="t-sub">{{ SOURCE[row.provider] || row.provider }}{{ row.section_heading ? " · " + row.section_heading : "" }}</div>
              </td>
              <td class="muted">{{ library ? library.name : "—" }}</td>
              <td class="muted">{{ row.checked_at ? fmtTime(row.checked_at) : "尚未同步" }}</td>
              <td>
                <div v-if="row.last_error" style="display:grid;gap:5px;justify-items:start">
                  <span class="pill failed">同步失败</span>
                  <div style="color:var(--red);font-size:12px;line-height:1.45;white-space:normal">{{ row.last_error }}</div>
                </div>
                <span v-else-if="!row.checked_at" class="pill queued">待同步</span>
                <span v-else class="pill live">正常</span>
              </td>
              <td class="right" @click.stop>
                <button class="btn sm" :disabled="busyId === row.id" @click="sync(row)">立即同步</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="table-foot"><span>共 {{ store.sources.length }} 个来源</span><span></span></div>
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
      <label>同步章节（飞书）</label>
      <input v-model="heading" type="text" placeholder="完整章节标题，例如 9. 知识库与 RAG" maxlength="160">
    </div>
    <div class="note">添加后会立即同步一次；发现内容变化时生成草稿，由你核对并发布。</div>
    <template #footer>
      <button class="btn" @click="newOpen = false">取消</button>
      <button class="btn primary" :disabled="busy" @click="create">添加并同步</button>
    </template>
  </Drawer>
</template>

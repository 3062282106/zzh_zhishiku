<script setup>
import { ref, computed, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import PageHead from "../components/PageHead.vue";
import EmptyState from "../components/EmptyState.vue";
import StatusPill from "../components/StatusPill.vue";
import DocMenu from "../components/DocMenu.vue";
import Drawer from "../components/Drawer.vue";
import UploadDrawer from "../components/UploadDrawer.vue";
import { store, loadLibraries, loadDocs, loadSources, loadTasks, libraryById, defaultLibrary } from "../store";
import { api } from "../api";
import { VISIBILITY, SOURCE, fmtTime } from "../format";
import { toast, fail } from "../toast";

const route = useRoute();
const router = useRouter();

const filters = ref({ q: "", source: "", status: "" });
const pageNo = ref(1);
const pageSize = 10;
const uploadOpen = ref(false);
const newLibraryOpen = ref(false);
const newLibraryName = ref("");
const busy = ref(false);

const libraryId = computed(() => route.params.libraryId || "");
const library = computed(() => libraryById(libraryId.value));

const rows = computed(() => {
  const q = filters.value.q.trim().toLowerCase();
  return store.docs.filter((doc) => {
    if (q && !String(doc.title).toLowerCase().includes(q)) return false;
    if (filters.value.source && doc.source !== filters.value.source) return false;
    if (filters.value.status && doc.visibility_status !== filters.value.status) return false;
    return true;
  });
});
const pages = computed(() => Math.max(1, Math.ceil(rows.value.length / pageSize)));
const slice = computed(() => rows.value.slice((pageNo.value - 1) * pageSize, pageNo.value * pageSize));

/** Uploads that exist but are not documents yet: no version means no row in the list. */
const parsing = computed(() =>
  store.tasks.filter((task) => task.kind === "parse" && task.library === libraryId.value
    && (task.status === "queued" || task.status === "running")));
const parsingStalled = computed(() => parsing.value.filter((task) => task.stalled).length);

async function refreshDocs(id = libraryId.value) {
  try {
    await Promise.all([loadDocs(id), loadSources(id), loadTasks()]);
  } catch (error) {
    fail(error);
  }
}

watch(
  () => route.params.libraryId,
  async (id) => {
    pageNo.value = 1;
    filters.value = { q: "", source: "", status: "" };
    if (id) await refreshDocs(id);
    else if (store.libraries.length) router.replace({ name: "kb", params: { libraryId: defaultLibrary() } });
  },
  { immediate: true },
);

// Land on a real library so the URL always describes what you are looking at.
watch(
  () => store.libraries.length,
  () => {
    if (!route.params.libraryId && store.libraries.length) {
      router.replace({ name: "kb", params: { libraryId: defaultLibrary() } });
    }
  },
  { immediate: true },
);

function open(doc) {
  router.push({ name: "doc", params: { libraryId: libraryId.value, docId: doc.id } });
}
function openHistory(doc) {
  router.push({ name: "doc", params: { libraryId: libraryId.value, docId: doc.id }, query: { panel: "history" } });
}
function setFilter(key, value) {
  filters.value[key] = value;
  pageNo.value = 1;
}
function resetFilters() {
  filters.value = { q: "", source: "", status: "" };
  pageNo.value = 1;
}

async function onUploaded(result) {
  uploadOpen.value = false;
  if (result.job) {
    toast("文件已上传，正在解析。");
    try {
      await loadTasks();
    } catch (error) {
      fail(error);
    }
    router.push({ name: "tasks" });
    return;
  }
  toast("草稿已生成，请核对分块后发布。");
  try {
    await Promise.all([loadDocs(libraryId.value), loadTasks()]);
  } catch (error) {
    fail(error);
  }
  router.push({ name: "doc", params: { libraryId: libraryId.value, docId: result.document_id } });
}

async function createLibrary() {
  const name = newLibraryName.value.trim();
  if (!name) return toast("请填写知识库名称。");
  busy.value = true;
  try {
    const created = await api("/libraries", { method: "POST", body: { name } });
    await loadLibraries();
    newLibraryOpen.value = false;
    newLibraryName.value = "";
    toast("知识库已创建。");
    router.push({ name: "kb", params: { libraryId: created.id } });
  } catch (error) {
    fail(error);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="wrap">
    <PageHead title="知识库" subtitle="维护团队知识，让每一次回答都有依据">
      <button class="btn primary" @click="uploadOpen = true">上传文档</button>
    </PageHead>

    <div class="card split">
      <div class="rail">
        <div class="rail-title">全部知识库</div>
        <router-link v-for="lib in store.libraries" :key="lib.id" :to="`/kb/${lib.id}`"
                     :class="{ on: lib.id === libraryId }" :title="lib.name">{{ lib.name }}</router-link>
        <button class="new" @click="newLibraryOpen = true">＋ 新建知识库</button>
      </div>

      <div class="pane">
        <div class="pane-head">
          <h2>
            {{ library ? library.name : "未选择知识库" }}
            <span class="count">{{ store.docs.length }} 篇文档</span>
          </h2>
          <div style="display:flex;gap:8px">
            <button class="btn sm" @click="refreshDocs()">刷新</button>
          </div>
        </div>

        <template v-if="!store.libraries.length">
          <EmptyState title="还没有知识库" note="先创建一个知识库，再上传文档。">
            <button class="btn primary" @click="newLibraryOpen = true">新建知识库</button>
          </EmptyState>
        </template>

        <template v-else>
          <div class="filters">
            <input class="search" type="text" placeholder="搜索文档标题" :value="filters.q"
                   @input="setFilter('q', $event.target.value)">
            <select style="width:150px" :value="filters.source" @change="setFilter('source', $event.target.value)">
              <option value="">全部来源</option>
              <option value="upload">本地上传</option>
              <option value="notion">Notion</option>
              <option value="feishu">飞书</option>
            </select>
            <select style="width:150px" :value="filters.status" @change="setFilter('status', $event.target.value)">
              <option value="">全部状态</option>
              <option v-for="(label, key) in VISIBILITY" :key="key" :value="key">{{ label }}</option>
            </select>
            <button class="btn" @click="resetFilters">重置</button>
          </div>

          <div v-if="parsingStalled" class="banner expired">
            <b>解析服务没有在运行</b>
            <span>{{ parsingStalled }} 个文件排队超过 5 分钟没有进展，所以还不会出现在下面的列表里。启动解析服务后会自动继续。</span>
          </div>
          <div v-else-if="parsing.length" class="banner pending">
            <span>{{ parsing.length }} 个文件正在解析，完成后会出现在下面的列表里。</span>
          </div>

          <EmptyState v-if="!store.docs.length"
                      :title="parsingStalled ? '文件还在排队' : (parsing.length ? '文件正在解析' : '暂无文档')"
                      :note="parsingStalled ? '解析服务启动后会自动继续，不需要重新上传。'
                            : (parsing.length ? '解析完成后就能预览、发布。' : '上传第一份文档，开始沉淀团队知识。')">
            <button class="btn primary" @click="uploadOpen = true">上传文档</button>
          </EmptyState>
          <EmptyState v-else-if="!rows.length" title="没有匹配的文档" note="调整搜索词或筛选条件后再试。">
            <button class="btn" @click="resetFilters">清除筛选</button>
          </EmptyState>

          <template v-else>
            <table>
              <colgroup>
                <col style="width:33%"><col style="width:12%"><col style="width:13%">
                <col style="width:11%"><col style="width:16%"><col style="width:15%">
              </colgroup>
              <thead>
                <tr><th>标题</th><th>来源</th><th>当前状态</th><th>负责人</th><th>更新时间</th><th></th></tr>
              </thead>
              <tbody>
                <tr v-for="doc in slice" :key="doc.id" @click="open(doc)">
                  <td><span class="linkish">{{ doc.title }}</span></td>
                  <td class="muted">{{ SOURCE[doc.source] || doc.source }}</td>
                  <td><StatusPill :status="doc.visibility_status" /></td>
                  <td class="muted">{{ doc.owner || "未指定" }}</td>
                  <td class="muted">{{ fmtTime(doc.updated_at) }}</td>
                  <td class="right" @click.stop>
                    <DocMenu :doc="doc" @changed="refreshDocs()" @deleted="refreshDocs()" @open-doc="open(doc)" @history="openHistory(doc)" />
                  </td>
                </tr>
              </tbody>
            </table>
            <div class="table-foot">
              <span>共 {{ rows.length }} 篇文档</span>
              <div class="pager">
                <button :disabled="pageNo <= 1" @click="pageNo--">‹</button>
                <span class="mono">{{ pageNo }} / {{ pages }}</span>
                <button :disabled="pageNo >= pages" @click="pageNo++">›</button>
                <span>10 条 / 页</span>
              </div>
            </div>
          </template>
        </template>
      </div>
    </div>
  </div>

  <UploadDrawer v-if="uploadOpen" :library-id="libraryId" @close="uploadOpen = false" @uploaded="onUploaded" />

  <Drawer v-if="newLibraryOpen" title="新建知识库" subtitle="为不同业务维护独立的知识范围" @close="newLibraryOpen = false">
    <div class="field">
      <label>知识库名称</label>
      <input v-model="newLibraryName" type="text" placeholder="例如：售后政策" maxlength="160" @keydown.enter="createLibrary">
      <div class="note" style="margin-top:6px">创建后可以立即上传文档，并按库给成员授权。</div>
    </div>
    <template #footer>
      <button class="btn" @click="newLibraryOpen = false">取消</button>
      <button class="btn primary" :disabled="busy" @click="createLibrary">创建</button>
    </template>
  </Drawer>
</template>

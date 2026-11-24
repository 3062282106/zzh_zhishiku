<script setup>
import { ref, computed, watch, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import EmptyState from "../components/EmptyState.vue";
import StatusPill from "../components/StatusPill.vue";
import DocMenu from "../components/DocMenu.vue";
import Drawer from "../components/Drawer.vue";
import UploadDrawer from "../components/UploadDrawer.vue";
import PublishDrawer from "../components/PublishDrawer.vue";
import AvailabilityDrawer from "../components/AvailabilityDrawer.vue";
import { api, enc } from "../api";
import { store, loadDocs, loadTasks, loadVersions, loadPreview, libraryById } from "../store";
import { VISIBILITY, fmtTime, fmtDay, versionLabel, ext, chunkWhere } from "../format";
import { toast, fail } from "../toast";
import { confirmBox } from "../confirm";

const route = useRoute();
const router = useRouter();

const versions = ref([]);
const versionId = ref("");
const version = ref(null);
const loading = ref(true);
const problem = ref("");
const tab = ref("chunks");
const chunkId = ref("");

const probeQuestion = ref("");
const probe = ref(null);
const probeBusy = ref(false);

const historyOpen = ref(false);
const publishOpen = ref(false);
const availabilityOpen = ref(false);
const uploadOpen = ref(false);

const docId = computed(() => route.params.docId);
const libraryId = computed(() => route.params.libraryId || "");
const library = computed(() => libraryById(libraryId.value));

const head = computed(() => versions.value[0] || null);
const row = computed(() => versions.value.find((item) => item.id === versionId.value) || head.value);
const isCurrent = computed(() => !!row.value && row.value.id === row.value.current_version);
const liveRow = computed(() => versions.value.find((item) => item.id === item.current_version) || null);
const label = computed(() => {
  const at = versions.value.indexOf(row.value);
  return at < 0 ? "" : versionLabel(at, versions.value.length);
});
const listDoc = computed(() => store.docs.find((item) => item.id === docId.value) || {});
const menuDoc = computed(() => ({
  id: docId.value,
  title: (row.value && row.value.title) || listDoc.value.title || "",
  owner: listDoc.value.owner || "",
  visibility_status: row.value ? row.value.visibility_status : "draft",
  current_version: row.value ? row.value.current_version : null,
}));

const chunks = computed(() => (version.value && version.value.chunks) || []);
const paper = computed(() => {
  const text = (version.value && version.value.original) || "";
  const active = chunks.value.find((item) => item.chunk_id === chunkId.value);
  if (active && active.text && text.includes(active.text)) {
    const at = text.indexOf(active.text);
    return { before: text.slice(0, at), hit: active.text, after: text.slice(at + active.text.length) };
  }
  return { before: text, hit: "", after: "" };
});
const bannerStatus = computed(() => (isCurrent.value ? row.value.visibility_status : "draft"));
const bannerText = computed(() => {
  if (!row.value) return "";
  if (!isCurrent.value) return "草稿 " + label.value + " · 仅预览可见，尚未向员工开放";
  const base = VISIBILITY[bannerStatus.value] || "未知";
  return row.value.valid_until
    ? base + " · 到期 " + fmtDay(row.value.valid_until)
    : base + " · 员工当前可查询 " + label.value;
});

async function selectVersion(id, syncUrl = true) {
  versionId.value = id;
  chunkId.value = "";
  probe.value = null;
  version.value = await loadPreview(docId.value, id);
  if (!syncUrl) return;
  const query = { ...route.query };
  delete query.panel;
  if (versions.value[0] && versions.value[0].id === id) delete query.v;
  else query.v = id;
  router.replace({ query });
}

async function load() {
  loading.value = true;
  problem.value = "";
  tab.value = route.query.tab === "probe" ? "probe" : "chunks";
  try {
    versions.value = await loadVersions(docId.value);
    if (!versions.value.length) {
      // A stale link should explain itself and offer a way back, not bounce the
      // reader to the list with a toast that disappears in four seconds.
      problem.value = "找不到这份文档：可能已被删除，或还没有上传任何版本。";
      return;
    }
    const wanted = route.query.v && versions.value.find((item) => item.id === route.query.v);
    await selectVersion(wanted ? wanted.id : versions.value[0].id, false);
    if (route.query.panel === "history") {
      historyOpen.value = true;
      const query = { ...route.query };
      delete query.panel;
      router.replace({ query });
    }
  } catch (error) {
    problem.value = error.message;
  } finally {
    loading.value = false;
  }
}

async function reload() {
  try {
    await Promise.all([loadVersions(docId.value).then((rows) => (versions.value = rows)), loadDocs(libraryId.value)]);
    if (versionId.value && !versions.value.some((item) => item.id === versionId.value)) {
      versionId.value = versions.value[0] ? versions.value[0].id : "";
    }
    if (versionId.value) version.value = await loadPreview(docId.value, versionId.value);
  } catch (error) {
    fail(error);
  }
}

onMounted(load);
watch(() => route.params.docId, load);

function switchTab(next) {
  tab.value = next;
  const query = { ...route.query };
  if (next === "probe") query.tab = "probe";
  else delete query.tab;
  router.replace({ query });
}

async function runProbe() {
  if (!probeQuestion.value.trim()) return toast("请输入要检索的问题。");
  if (!store.probeMember) return toast("请选择模拟员工。");
  probeBusy.value = true;
  try {
    probe.value = await api(
      "/documents/" + enc(docId.value) + "/versions/" + enc(row.value.id) + "/probe",
      { method: "POST", body: { question: probeQuestion.value.trim(), member: store.probeMember } },
    );
  } catch (error) {
    fail(error);
  } finally {
    probeBusy.value = false;
  }
}

function rollback(target) {
  const at = versions.value.indexOf(target);
  const targetLabel = versionLabel(at, versions.value.length);
  confirmBox({
    title: "回滚至 " + targetLabel + "？",
    note: "发布完成后员工将查询 " + targetLabel + " 的内容，当前版本会被替换。系统会基于 " + targetLabel +
      " 构建新版本，并保留全部历史记录。",
    label: "确认回滚",
    action: async () => {
      const result = await api(
        "/documents/" + enc(docId.value) + "/versions/" + enc(target.id) + "/rollback",
        { method: "POST", body: { expected: liveRow.value ? liveRow.value.current_version : "" } },
      );
      historyOpen.value = false;
      if (result.publication_status) {
        toast("已加入发布队列，可在处理任务查看进度。");
        await loadTasks();
        router.push({ name: "tasks" });
      } else {
        toast("已回滚到 " + targetLabel + "。");
        await reload();
      }
    },
  });
}

async function onUploaded(result) {
  uploadOpen.value = false;
  if (result.job) {
    toast("文件已上传，正在解析。");
    await loadTasks().catch(fail);
    router.push({ name: "tasks" });
    return;
  }
  toast("草稿已生成，请核对分块后发布。");
  await reload();
  if (result.document_id === docId.value) {
    await selectVersion(versions.value[0].id);
  } else {
    router.push({ name: "doc", params: { libraryId: libraryId.value, docId: result.document_id } });
  }
}

async function publish(target) {
  if (target && target.id !== versionId.value) await selectVersion(target.id);
  publishOpen.value = true;
}

async function onQueued() {
  publishOpen.value = false;
  toast("已加入发布队列，正在构建索引。");
  await loadTasks().catch(fail);
  router.push({ name: "tasks" });
}

async function onPublished(info) {
  publishOpen.value = false;
  toast("已发布。" + (info.scheduled ? "将在设定时间生效。" : "员工现在可以查询。"));
  await reload();
}
</script>

<template>
  <div class="wrap">
    <div v-if="loading" class="loading">正在读取版本…</div>

    <div v-else-if="problem" class="errbox">
      <b>无法打开这份文档</b>
      <p>{{ problem }}</p>
      <div style="margin-top:12px">
        <router-link class="btn" :to="`/kb/${libraryId}`">返回知识库</router-link>
      </div>
    </div>

    <EmptyState v-else-if="!row" title="找不到文档" note="返回列表后重新打开。">
      <router-link class="btn" :to="`/kb/${libraryId}`">返回知识库</router-link>
    </EmptyState>

    <template v-else>
      <div class="crumbline">
        <router-link class="btn link" :to="`/kb/${libraryId}`">知识库</router-link>
        <span>/</span>
        <span>{{ library ? library.name : "知识库" }}</span>
      </div>

      <div class="doc-head">
        <div>
          <h1>{{ row.title || (version && version.title) || "未命名文档" }}</h1>
          <div class="doc-meta">
            {{ label }} · {{ ext(row.filename) }} · {{ row.chunk_count }} 块 · {{ fmtTime(row.created_at) }}
          </div>
        </div>
        <div class="page-actions">
          <button class="btn" @click="historyOpen = true">版本历史</button>
          <DocMenu :doc="menuDoc" size="" @changed="reload"
                   @deleted="router.replace({ name: 'kb', params: { libraryId } })" />
          <button class="btn primary" :disabled="isCurrent" @click="publish(row)">
            {{ isCurrent ? "已生效" : "发布" }}
          </button>
        </div>
      </div>

      <div class="banner" :class="bannerStatus">
        <span>{{ bannerText }}</span>
        <span class="spacer"></span>
        <button v-if="isCurrent" class="btn link" @click="availabilityOpen = true">调整上下架与到期</button>
      </div>

      <div class="tabs">
        <a :class="{ on: tab === 'chunks' }" @click="switchTab('chunks')">原文与分块</a>
        <a :class="{ on: tab === 'probe' }" @click="switchTab('probe')">试检索</a>
        <span class="right">原文证据优先</span>
      </div>

      <div v-if="tab === 'chunks'" class="doc-grid">
        <div class="panel">
          <div class="panel-head">
            <b>原文快照</b>
            <span>{{ row.filename }} · {{ chunks.length }} 块</span>
          </div>
          <div class="paper">
            <template v-if="version && version.original">{{ paper.before }}<mark v-if="paper.hit">{{ paper.hit }}</mark>{{ paper.after }}</template>
            <template v-else>此版本没有可展示的原文。</template>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head"><b>原文分块</b><span>共 {{ chunks.length }} 块</span></div>
          <div class="chunks">
            <div v-if="!chunks.length" class="loading">此版本没有分块。</div>
            <button v-for="(chunk, index) in chunks" :key="chunk.chunk_id" class="chunk"
                    :class="{ on: chunk.chunk_id === chunkId }" @click="chunkId = chunk.chunk_id">
              <div class="where">{{ chunkWhere(chunk, index) }}</div>
              <div class="body">{{ chunk.text }}</div>
            </button>
          </div>
        </div>
      </div>

      <div v-else class="panel">
        <div class="panel-head">
          <b>按员工身份试检索</b>
          <span>只返回该员工有权访问的内容</span>
        </div>
        <div class="probe">
          <div class="probe-row">
            <select v-model="store.probeMember" style="width:240px">
              <option v-for="member in store.members" :key="member.id" :value="member.id">
                {{ member.name }}{{ member.kind === "guest" ? " · 受邀个人" : " · 员工" }}
              </option>
            </select>
            <input v-model="probeQuestion" type="text" placeholder="例如：质量问题退货运费谁承担？"
                   @keydown.enter="runProbe">
            <button class="btn primary" :disabled="probeBusy" @click="runProbe">检索</button>
          </div>
          <div class="probe-note">试检索在当前版本的分块内匹配，不写入线上索引；权限判断与线上一致。</div>

          <template v-if="probe">
            <div v-if="!probe.allowed" class="denied">
              <b>{{ probe.member }} 无权访问「{{ probe.library }}」</b>
              <p>未返回任何文档内容。可切换模拟身份，或到成员与权限里调整授权。</p>
              <div style="margin-top:10px">
                <router-link class="btn sm" :to="{ name: 'members' }">管理成员权限</router-link>
              </div>
            </div>
            <EmptyState v-else-if="!probe.hits.length" title="没有命中分块" note="换个问法，或确认这一版是否包含相关内容。" />
            <template v-else>
              <div class="probe-note">
                模拟身份：{{ probe.member }} · 命中 {{ probe.hits.length }} 块{{ probe.live ? "（线上版本）" : "（草稿版本）" }}
              </div>
              <div v-for="(hit, index) in probe.hits" :key="index" class="hit">
                <div class="where">
                  {{ (hit.heading_path || []).join(" / ") || "正文" }} · {{ chunkWhere({ locator: hit.locator || {} }, 0) }}
                </div>
                <div class="body">{{ hit.text }}</div>
              </div>
            </template>
          </template>
        </div>
      </div>
    </template>
  </div>

  <Drawer v-if="historyOpen" title="版本历史"
          :subtitle="((version && version.title) || '') + ' · 共 ' + versions.length + ' 个版本'"
          @close="historyOpen = false">
    <div style="display:grid;gap:8px">
      <div v-for="(item, index) in versions" :key="item.id" class="block"
           style="display:flex;align-items:center;gap:12px">
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:8px">
            <b>{{ versionLabel(index, versions.length) }}</b>
            <StatusPill v-if="item.id === item.current_version" :status="item.visibility_status" />
            <span v-else class="pill draft">{{ item.ready ? "历史版本" : "草稿" }}</span>
          </div>
          <div class="t-sub">{{ item.filename }} · {{ item.chunk_count }} 块 · {{ fmtTime(item.created_at) }}</div>
        </div>
        <div style="display:flex;gap:6px">
          <button class="btn sm" @click="selectVersion(item.id); historyOpen = false">查看</button>
          <button v-if="item.id !== item.current_version" class="btn sm"
                  @click="item.ready ? rollback(item) : publish(item)">{{ item.ready ? "回滚" : "发布" }}</button>
        </div>
      </div>
    </div>
    <template #footer>
      <button class="btn" @click="historyOpen = false; uploadOpen = true">上传新版</button>
    </template>
  </Drawer>

  <UploadDrawer v-if="uploadOpen" :library-id="libraryId" :document-id="docId"
                @close="uploadOpen = false" @uploaded="onUploaded" />

  <PublishDrawer v-if="publishOpen && row" :doc-id="docId" :version-id="row.id"
                 :expected="liveRow ? liveRow.current_version : ''"
                 :title="row.title" :label="label"
                 :library-name="library ? library.name : '当前知识库'"
                 :live-label="liveRow ? versionLabel(versions.indexOf(liveRow), versions.length) : ''"
                 @close="publishOpen = false" @queued="onQueued" @published="onPublished" />

  <AvailabilityDrawer v-if="availabilityOpen && row" :doc-id="docId" :row="row"
                      @close="availabilityOpen = false" @saved="reload(); availabilityOpen = false" />
</template>

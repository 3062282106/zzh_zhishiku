<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { enc, feishuError, knowledgeApi } from "../api";
import { fmtTime, VISIBILITY } from "../format";
import Drawer from "../components/Drawer.vue";
import UploadDrawer from "../components/UploadDrawer.vue";
import { confirmBox } from "../confirm";

const route = useRoute();
const router = useRouter();
const status = ref(null);
const files = ref([]);
const jobs = ref([]);
const uploadOpen = ref(false);
let timer;
let refreshPromise;
const rows = computed(() => {
  const latestJobs = new Map();
  for (const job of jobs.value) {
    if (!latestJobs.has(job.document_id)) latestJobs.set(job.document_id, job);
  }
  const result = files.value.map((doc) => ({ ...doc, job: latestJobs.get(doc.id) }));
  const known = new Set(result.map((doc) => doc.id));
  for (const [id, job] of latestJobs) {
    if (!known.has(id)) result.push({ id, title: job.title, visibility_status: null, job });
  }
  return result;
});
const isProcessing = (job) => Boolean(job && (
  ["queued", "parsing"].includes(job.status) || ["queued", "indexing"].includes(job.publication_status)
  || (job.status === "ready" && job.auto_publish && !job.publication_status)
));
const selected = ref(null);
const pending = ref(true);
const busy = ref(false);
const error = ref("");
const notice = ref("");
const current = computed(() => selected.value?.versions.find((row) => row.id === selected.value.version));
const selectedFile = computed(() => files.value.find((file) => file.id === selected.value?.document));

function loadFiles() {
  refreshPromise ||= knowledgeApi("/personal").then((personal) => {
    files.value = personal.documents;
    jobs.value = personal.jobs;
  }).finally(() => { refreshPromise = null; });
  return refreshPromise;
}

async function load() {
  await Promise.all([
    loadFiles(),
    knowledgeApi("/feishu/status").then((authorization) => { status.value = authorization; }),
  ]);
}

function rowStatus(file) {
  const job = file.job;
  if (job?.status === "failed" || job?.publication_status === "failed") return "处理失败";
  if (isProcessing(job)) return job.status === "ready" ? "正在建索引" : "正在解析";
  if (file.visibility_status === "live") return "已发布";
  return VISIBILITY[file.visibility_status] || "草稿";
}

async function uploaded(count) {
  uploadOpen.value = false;
  notice.value = "已提交 " + count + " 份文档，处理完成后自动发布。";
  try {
    await loadFiles();
  } catch (problem) {
    error.value = problem.message;
  }
}

function remove(file) {
  confirmBox({
    title: "删除这份文档？", keys: file.title,
    note: "文档、版本和上传原件将从你的个人知识库中删除。",
    label: "确认删除", danger: true,
    action: async () => {
      await knowledgeApi("/personal/" + enc(file.id), { method: "DELETE" });
      if (selected.value?.document === file.id) close();
      notice.value = "文档已删除。";
      await loadFiles();
    },
  });
}

async function switchVisibility(file) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    await knowledgeApi("/personal/" + enc(file.id) + "/availability", {
      method: "PUT", body: { published: !file.published, expected_version: file.current_version },
    });
    notice.value = file.published ? "文档已下架。" : "文档已恢复上线。";
    await loadFiles();
  } catch (problem) {
    error.value = problem.message;
  } finally {
    busy.value = false;
  }
}

onMounted(async () => {
  timer = setInterval(() => {
    if (jobs.value.some(isProcessing) || files.value.some((file) => file.sync_requested)) loadFiles().catch(() => {});
  }, 3000);
  try {
    const { feishu, document, version } = route.query;
    if (feishu) {
      if (feishu === "ok") notice.value = "飞书已授权。";
      else error.value = feishuError(feishu, route.query.feishu_code);
      router.replace({ query: { personal: "1" } });
    }
    await load();
    if (typeof document === "string") await open(document, typeof version === "string" ? version : "");
  } catch (problem) {
    error.value = problem.message;
  } finally {
    pending.value = false;
  }
});
onUnmounted(() => clearInterval(timer));

async function authorize() {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const result = await knowledgeApi("/feishu/authorize", { method: "POST" });
    window.location.assign(result.url);
  } catch (problem) {
    error.value = problem.message;
    busy.value = false;
  }
}

async function syncFile(file, enabled = null) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  const path = "/personal/" + enc(file.id);
  try {
    if (enabled === null) await knowledgeApi(path + "/sync", { method: "POST" });
    else await knowledgeApi(path + "/auto-sync", { method: "PUT", body: { enabled } });
    notice.value = enabled === false ? "已暂停自动同步。" : "检查已排队。";
    await loadFiles();
  } catch (problem) {
    error.value = problem.message;
  } finally {
    busy.value = false;
  }
}

async function open(document, version = "") {
  const entry = await knowledgeApi("/personal/" + enc(document) + (version ? "?version=" + enc(version) : ""));
  selected.value = { ...entry, document };
}

async function select(document, version) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    await open(document, version);
  } catch (problem) {
    error.value = problem.message;
  } finally {
    busy.value = false;
  }
}

function close() {
  selected.value = null;
  if (route.query.document) router.replace({ query: { personal: "1" } });
}

async function publish() {
  if (busy.value || !selected.value || !current.value) return;
  const { document, version } = selected.value;
  busy.value = true;
  error.value = "";
  try {
    await knowledgeApi("/personal/" + enc(document) + "/versions/" + enc(version) + "/publish", {
      method: "POST", body: { expected: current.value.current_version },
    });
    notice.value = "已发布到个人知识库。";
    close();
    await load();
  } catch (problem) {
    error.value = problem.message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section>
    <header class="personal-head">
      <h1>个人知识库</h1>
      <div v-if="status?.upload_enabled" class="personal-actions">
        <button class="btn" :disabled="busy" @click="authorize">
          {{ status.authorized ? "重新授权飞书" : "授权飞书" }}
        </button>
        <button class="btn primary" :disabled="busy" @click="uploadOpen = true">上传文档</button>
      </div>
    </header>
    <p v-if="notice" class="note" role="status">{{ notice }}</p>
    <p v-if="error && !selected" class="personal-error" role="alert">{{ error }}</p>
    <p v-if="pending" class="muted">正在加载…</p>
    <p v-else-if="!rows.length" class="note">
      {{ status?.upload_enabled ? "上传文件，或在 Agent 中发送飞书链接。" : "联系管理员开通个人库上传权限。" }}
    </p>
    <div v-else class="card">
      <table>
        <thead><tr><th>文档</th><th>状态</th><th></th></tr></thead>
        <tbody>
          <tr v-for="file in rows" :key="file.id">
            <td>
              <b>{{ file.title }}</b>
              <div v-if="file.source === 'feishu'" class="t-sub">
                {{ file.sync_requested ? "飞书 · 待检查" : file.source_error ? "飞书 · 同步失败"
                  : file.auto_sync ? "飞书 · 自动同步" : "飞书 · 已暂停" }}
              </div>
              <div v-if="file.job?.last_error || file.job?.publication_error" class="t-sub">
                {{ file.job.last_error || file.job.publication_error }}
              </div>
            </td>
            <td><span class="pill" :class="file.visibility_status === 'live' ? 'live' : 'draft'">{{ rowStatus(file) }}</span></td>
            <td class="right">
              <div class="personal-actions">
                <button v-if="file.visibility_status" class="btn sm" :disabled="busy" @click="select(file.id)">查看</button>
                <button v-if="file.current_version" class="btn sm" :disabled="busy" @click="switchVisibility(file)">
                  {{ file.published ? "下架" : "恢复" }}
                </button>
                <button class="btn sm danger" :disabled="busy" @click="remove(file)">删除</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
  <UploadDrawer v-if="uploadOpen" personal @close="uploadOpen = false" @uploaded="uploaded" />
  <Drawer v-if="selected" :title="selected.title" @close="close">
    <div v-if="selectedFile?.source === 'feishu'" class="block">
      <div class="k">同步飞书原文</div>
      <div class="v">
        {{ selectedFile.source_checked_at ? "最近检查 " + fmtTime(selectedFile.source_checked_at) : "尚未检查" }}
      </div>
      <p v-if="selectedFile.source_error" class="personal-error">{{ selectedFile.source_error }}</p>
      <div v-if="status?.upload_enabled" class="personal-sync">
        <button class="btn sm" :disabled="busy || selectedFile.sync_requested" @click="syncFile(selectedFile)">立即检查</button>
        <button class="btn ghost sm" :disabled="busy" @click="syncFile(selectedFile, !selectedFile.auto_sync)">
          {{ selectedFile.auto_sync ? "暂停同步" : "开启自动同步" }}
        </button>
      </div>
    </div>
    <div class="field">
      <label for="personal-version">文档版本</label>
      <select id="personal-version" :value="selected.version" :disabled="busy" @change="select(selected.document, $event.target.value)">
        <option v-for="item in selected.versions" :key="item.id" :value="item.id">
          {{ fmtTime(item.created_at) }} · {{ item.id === item.current_version ? "已发布" : "草稿" }}
        </option>
      </select>
    </div>
    <p v-if="error" class="personal-error" role="alert">{{ error }}</p>
    <pre class="original">{{ selected.original }}</pre>
    <template #footer>
      <button class="btn" @click="close">关闭</button>
      <button v-if="status?.upload_enabled && current && current.id !== current.current_version"
              class="btn primary" :disabled="busy" @click="publish">{{ busy ? "处理中…" : "发布到个人知识库" }}</button>
    </template>
  </Drawer>
</template>

<style scoped>
.personal-head { display: flex; gap: 12px; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.personal-head h1 { font-size: 26px; }
.personal-actions { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 6px; }
.personal-sync { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.personal-error { color: var(--red); margin: 12px 0; }
.original { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.7; }
</style>

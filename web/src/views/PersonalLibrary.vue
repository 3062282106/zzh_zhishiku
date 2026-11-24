<script setup>
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { enc, feishuError, knowledgeApi } from "../api";
import { fmtTime } from "../format";
import Drawer from "../components/Drawer.vue";

const route = useRoute();
const router = useRouter();
const status = ref(null);
const files = ref([]);
const selected = ref(null);
const pending = ref(true);
const busy = ref(false);
const error = ref("");
const notice = ref("");
const current = computed(() => selected.value?.versions.find((row) => row.id === selected.value.version));

async function load() {
  const [personal, authorization] = await Promise.all([knowledgeApi("/personal"), knowledgeApi("/feishu/status")]);
  files.value = personal.documents;
  status.value = authorization;
}

onMounted(async () => {
  try {
    const { feishu, document, version } = route.query;
    if (feishu) {
      if (feishu === "ok") notice.value = "飞书已授权，可以回到 Agent 导入。";
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
      <button v-if="status?.upload_enabled" class="btn" :disabled="busy" @click="authorize">
        {{ status.authorized ? "重新授权飞书" : "授权飞书" }}
      </button>
    </header>
    <p v-if="notice" class="note" role="status">{{ notice }}</p>
    <p v-if="error && !selected" class="personal-error" role="alert">{{ error }}</p>
    <p v-if="pending" class="muted">正在加载…</p>
    <p v-else-if="!files.length" class="note">
      {{ status?.upload_enabled ? "在已连接知识库的 Agent 里发送飞书链接，即可生成草稿。" : "联系管理员开通个人库上传权限。" }}
    </p>
    <div v-else class="card">
      <table>
        <thead><tr><th>文档</th><th>状态</th><th></th></tr></thead>
        <tbody>
          <tr v-for="file in files" :key="file.id">
            <td><b>{{ file.title }}</b></td>
            <td><span class="pill" :class="file.current_version ? 'live' : 'draft'">{{ file.current_version ? "已发布" : "草稿" }}</span></td>
            <td class="right"><button class="btn sm" :disabled="busy" @click="select(file.id)">查看</button></td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
  <Drawer v-if="selected" :title="selected.title" @close="close">
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
.personal-error { color: var(--red); margin: 12px 0; }
.original { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.7; }
</style>

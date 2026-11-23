<script setup>
import { ref, computed, onMounted, onUnmounted } from "vue";
import { useRouter } from "vue-router";
import PageHead from "../components/PageHead.vue";
import EmptyState from "../components/EmptyState.vue";
import StatusPill from "../components/StatusPill.vue";
import { api, enc } from "../api";
import { store, loadTasks } from "../store";
import { TASK_KIND, TASK_STATE } from "../format";
import { toast, fail } from "../toast";

const router = useRouter();
const filter = ref("all");
const busyId = ref("");
let timer = 0;

const counts = computed(() => {
  const value = { all: store.tasks.length, running: 0, failed: 0, done: 0 };
  for (const task of store.tasks) {
    if (task.status === "running" || task.status === "queued") value.running++;
    else if (task.status === "failed") value.failed++;
    else value.done++;
  }
  return value;
});

const rows = computed(() =>
  store.tasks.filter((task) => {
    if (filter.value === "running") return task.status === "running" || task.status === "queued";
    if (filter.value === "failed") return task.status === "failed";
    if (filter.value === "done") return task.status === "done";
    return true;
  }),
);

const TABS = [["all", "全部"], ["running", "处理中"], ["failed", "失败"], ["done", "已完成"]];

/** Jobs nobody has touched for five minutes: the parser is down, not slow. */
const stalled = computed(() => store.tasks.filter((task) => task.stalled));

function waited(seconds) {
  const value = Number(seconds) || 0;
  if (value < 60) return "刚刚";
  const minutes = Math.round(value / 60);
  return minutes < 60 ? minutes + " 分钟" : Math.round(minutes / 60) + " 小时";
}

function resultText(task) {
  if (task.stalled) return "排队 " + waited(task.waiting_seconds) + " 没有进展，解析服务可能没有运行";
  if (task.status === "queued") {
    return task.kind === "parse" ? "等待解析服务处理 · 已排队 " + waited(task.waiting_seconds) : "等待构建索引";
  }
  if (task.status === "running") {
    return task.kind === "parse" ? "正在解析原文" : "正在构建索引";
  }
  if (task.kind === "parse") return "解析完成 · 草稿已就绪";
  if (task.kind === "publish") return "索引已切换，发布完成";
  return "已检查，未发现变化";
}

function openDoc(task) {
  if (!task.document_id) return toast("这个任务没有关联文档。");
  // A parse job has no version yet, so there is nothing to preview until it is done.
  if (task.kind === "parse" && task.status !== "done") {
    return toast("文件还没解析完成，暂时没有可预览的文档。");
  }
  router.push({ name: "doc", params: { libraryId: task.library, docId: task.document_id } });
}

async function retry(task) {
  if (busyId.value) return;
  busyId.value = task.id || task.job_id || task.version_id;
  try {
    if (task.kind === "parse") {
      await api("/file-jobs/" + enc(task.job_id) + "/retry", { method: "POST" });
      toast("已重新提交解析任务。");
    } else if (task.kind === "sync") {
      await api("/sources/" + enc(task.source_id) + "/sync", { method: "POST" });
      toast("已重新同步来源。");
    } else {
      const versions = await api("/documents/" + enc(task.document_id) + "/versions");
      const head = versions[0];
      await api("/documents/" + enc(task.document_id) + "/versions/" + enc(task.version_id) + "/publish",
        { method: "POST", body: { expected: head ? head.current_version : "" } });
      toast("已重新提交发布任务。");
    }
    await loadTasks();
    schedule();
  } catch (error) {
    fail(error);
  } finally {
    busyId.value = "";
  }
}

/** Keep polling only while something is actually moving. */
function schedule() {
  clearTimeout(timer);
  if (!store.tasks.some((task) => task.status === "running" || task.status === "queued")) return;
  timer = setTimeout(async () => {
    try {
      await loadTasks();
    } catch {
      /* a failed poll stays silent; the next click surfaces the real error */
    }
    schedule();
  }, 4000);
}

onMounted(async () => {
  try {
    await loadTasks();
  } catch (error) {
    fail(error);
  }
  schedule();
});
onUnmounted(() => clearTimeout(timer));
</script>

<template>
  <div class="wrap">
    <PageHead title="处理任务" subtitle="解析、同步与发布任务集中处理">
      <button class="btn" @click="loadTasks().then(schedule).catch(fail)">刷新</button>
    </PageHead>

    <div v-if="stalled.length" class="banner expired">
      <b>解析服务没有在运行</b>
      <span>{{ stalled.length }} 个任务排队超过 5 分钟没有任何进展，上传的文件不会被处理。启动解析服务后任务会自动继续，不需要重新上传。</span>
      <span class="spacer"></span>
      <button class="btn sm" @click="loadTasks().then(schedule).catch(fail)">刷新</button>
    </div>

    <div class="card">
      <div class="filters" style="gap:6px">
        <button v-for="[key, label] in TABS" :key="key" class="chip" :class="{ on: filter === key }"
                @click="filter = key">{{ label }}{{ key === "all" ? "" : " · " + counts[key] }}</button>
      </div>

      <EmptyState v-if="!rows.length"
                  :title="filter === 'failed' ? '没有失败任务' : '暂无任务'"
                  :note="filter === 'failed' ? '所有解析、同步与发布任务都已处理完成。' : '上传文件或同步来源后，任务会出现在这里。'" />

      <template v-else>
        <table>
          <colgroup>
            <col style="width:29%"><col style="width:9%"><col style="width:11%">
            <col style="width:33%"><col style="width:18%">
          </colgroup>
          <thead>
            <tr><th>任务 / 文档</th><th>类型</th><th>状态</th><th>失败原因 / 处理结果</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="task in rows" :key="task.id || task.job_id || task.version_id" @click="openDoc(task)">
              <td>
                <div class="t-main">{{ task.title }}</div>
                <div class="t-sub mono">{{ task.version_id ? "#" + String(task.version_id).slice(0, 8) : (task.filename || "") }}</div>
              </td>
              <td class="muted">{{ TASK_KIND[task.kind] || task.kind }}</td>
              <td><StatusPill :status="task.status" :label="TASK_STATE[task.status] || task.status" /></td>
              <td :class="{ muted: task.status !== 'failed' && !task.stalled }">
                <span :style="task.status === 'failed' ? 'color:var(--red)' : (task.stalled ? 'color:var(--amber)' : '')">{{ task.error || resultText(task) }}</span>
              </td>
              <td class="right" @click.stop>
                <div style="display:flex;gap:8px;justify-content:flex-end">
                  <button v-if="task.status === 'failed'" class="btn link" :disabled="busyId !== ''" @click="retry(task)">重试</button>
                  <button v-if="task.kind !== 'parse' || task.status === 'done'" class="btn link" @click="openDoc(task)">打开文档</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="table-foot"><span>共 {{ rows.length }} 项任务 · 最近 30 天</span><span></span></div>
      </template>
    </div>
  </div>
</template>

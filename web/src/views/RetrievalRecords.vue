<script setup>
import { onMounted, onUnmounted, reactive, ref } from "vue";
import { api } from "../api";
import Drawer from "../components/Drawer.vue";
import EmptyState from "../components/EmptyState.vue";
import PageHead from "../components/PageHead.vue";
import StatusPill from "../components/StatusPill.vue";
import { fmtTime } from "../format";
import { store } from "../store";

const STATUS = {
  evidence_found: { label: "返回证据", style: "live" },
  insufficient_evidence: { label: "证据不足", style: "draft" },
  unavailable: { label: "服务异常", style: "failed" },
};
const STAGES = { authorization: "权限校验", recall: "召回（含向量化）", rerank: "重排" };
const filters = reactive({ days: 7, member: "", library: "", status: "" });
const rows = ref([]);
const cursor = ref(null);
const history = ref([]);
const next = ref(null);
const busy = ref(false);
const error = ref("");
const selected = ref(null);
let request = null;

const names = (values) => values.join("、") || "—";
const time = (ms) => ms >= 1000 ? (ms / 1000).toFixed(2) + " 秒" : ms + " 毫秒";

async function load(before = null, previous = []) {
  request?.abort();
  const controller = new AbortController();
  request = controller;
  busy.value = true;
  error.value = "";
  const params = new URLSearchParams({ days: String(filters.days) });
  for (const field of ["member", "library", "status"]) {
    if (filters[field]) params.set(field, filters[field]);
  }
  if (before !== null) params.set("before", String(before));
  try {
    const result = await api("/retrieval-records?" + params, { signal: controller.signal });
    if (request !== controller) return;
    rows.value = result.items;
    next.value = result.next_before;
    cursor.value = before;
    history.value = previous;
  } catch (problem) {
    if (request === controller && !controller.signal.aborted) error.value = problem.message;
  } finally {
    if (request === controller) busy.value = false;
  }
}

onMounted(() => load());
onUnmounted(() => request?.abort());
</script>

<template>
  <div class="wrap">
    <PageHead title="检索记录">
      <button class="btn" :disabled="busy" @click="load()">刷新</button>
    </PageHead>

    <div class="card">
      <div class="filters">
        <select v-model.number="filters.days" aria-label="时间范围" @change="load()">
          <option :value="1">最近 24 小时</option>
          <option :value="7">最近 7 天</option>
          <option :value="30">最近 30 天</option>
        </select>
        <select v-model="filters.member" aria-label="成员" @change="load()">
          <option value="">全部成员</option>
          <option v-for="member in store.members" :key="member.id" :value="member.id">{{ member.name }}</option>
        </select>
        <select v-model="filters.library" aria-label="知识库" @change="load()">
          <option value="">全部知识库</option>
          <option v-for="library in store.libraries" :key="library.id" :value="library.id">{{ library.name }}</option>
        </select>
        <select v-model="filters.status" aria-label="检索结果" @change="load()">
          <option value="">全部结果</option>
          <option v-for="(status, key) in STATUS" :key="key" :value="key">{{ status.label }}</option>
        </select>
      </div>

      <p v-if="busy" class="loading" role="status">正在加载…</p>
      <div v-else-if="error" class="errbox" role="alert">
        <b>加载失败</b><p>{{ error }}</p>
      </div>
      <EmptyState v-else-if="!rows.length" title="没有符合条件的检索记录" />
      <template v-else>
        <div class="table-scroll">
          <table>
            <thead><tr><th>时间</th><th>成员</th><th>检索范围</th><th>证据来源</th><th>结果</th><th>耗时</th><th></th></tr></thead>
            <tbody>
              <tr v-for="row in rows" :key="row.id">
                <td class="mono nowrap">{{ fmtTime(row.recorded_at) }}</td>
                <td>{{ row.member_name }}</td>
                <td>{{ names(row.searched_libraries) }}</td>
                <td>{{ names(row.evidence_libraries) }}</td>
                <td><StatusPill :status="STATUS[row.status].style" :label="STATUS[row.status].label" /></td>
                <td class="mono nowrap">{{ time(row.duration_ms) }}</td>
                <td><button class="btn link" @click="selected = row">详情</button></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="table-foot" v-if="history.length || next !== null">
          <span>第 {{ history.length + 1 }} 页</span>
          <div class="rowacts">
            <button class="btn sm" :disabled="!history.length"
                    @click="load(history.at(-1), history.slice(0, -1))">上一页</button>
            <button class="btn sm" :disabled="next === null"
                    @click="load(next, [...history, cursor])">下一页</button>
          </div>
        </div>
      </template>
    </div>
  </div>

  <Drawer v-if="selected" title="检索详情" :subtitle="'#' + selected.id" :autofocus="false" @close="selected = null">
    <div class="kv">
      <div class="k">成员</div><div>{{ selected.member_name }}</div>
      <div class="k">时间</div><div>{{ new Date(selected.recorded_at).toLocaleString('zh-CN', { hour12: false }) }}</div>
      <div class="k">接入方式</div><div>{{ selected.channel === 'mcp' ? 'MCP' : 'API' }}</div>
      <div class="k">检索范围</div><div>{{ names(selected.searched_libraries) }}</div>
      <div class="k">证据来源</div><div>{{ names(selected.evidence_libraries) }}</div>
      <div class="k">返回片段</div><div>{{ selected.evidence_count }} 条</div>
      <div class="k">服务端耗时</div><div>{{ time(selected.duration_ms) }}</div>
      <template v-for="(label, stage) in STAGES" :key="stage">
        <template v-if="selected.stages_ms[stage] != null">
          <div class="k">{{ label }}</div><div>{{ time(selected.stages_ms[stage]) }}</div>
        </template>
      </template>
    </div>
    <template #footer><button class="btn" @click="selected = null">关闭</button></template>
  </Drawer>
</template>

<style scoped>
.table-scroll { overflow-x: auto; }
table { min-width: 730px; }
.nowrap { white-space: nowrap; }
.filters select { max-width: 235px; }
.kv > div { overflow-wrap: anywhere; }
</style>

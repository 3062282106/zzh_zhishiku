<script setup>
import { ref, onMounted, onUnmounted } from "vue";
import PageHead from "../components/PageHead.vue";
import EmptyState from "../components/EmptyState.vue";
import Drawer from "../components/Drawer.vue";
import { api } from "../api";
import MemberFilter from "../components/MemberFilter.vue";
import { AUDIT_RESULT, fmtTime, fmtDay } from "../format";
import { fail } from "../toast";

const kind = ref("");
const member = ref("");
const row = ref(null);
const rows = ref([]);
const busy = ref(false);
let request;

const KINDS = [
  ["", "全部操作"], ["publish", "发布"], ["rollback", "回滚"], ["unpublish", "下架"],
  ["restore", "恢复上线"], ["schedule", "调整有效期"], ["grant", "授权"], ["revoke", "撤销访问"],
  ["upload", "上传文档"], ["delete", "删除文档"], ["web_opened", "开通网页登录"], ["web_password_reset", "重置网页密码"],
];

async function load() {
  request?.abort();
  const controller = new AbortController();
  request = controller;
  busy.value = true;
  const params = new URLSearchParams({ limit: "300" });
  if (kind.value) params.set("kind", kind.value);
  if (member.value) params.set("member", member.value);
  try {
    const next = await api("/audit?" + params, { signal: controller.signal });
    if (request === controller) rows.value = next;
  } catch (error) {
    if (!controller.signal.aborted) fail(error);
  } finally {
    if (request === controller) busy.value = false;
  }
}
onMounted(load);
onUnmounted(() => request?.abort());
</script>

<template>
  <div class="wrap">
    <PageHead title="操作记录">
      <button class="btn" :disabled="busy" @click="load">刷新</button>
    </PageHead>

    <div class="card">
      <div class="filters">
        <MemberFilter v-model="member" @update:model-value="load" />
        <select v-model="kind" aria-label="操作类型" @change="load">
          <option v-for="[value, label] in KINDS" :key="value" :value="value">{{ label }}</option>
        </select>
        <span class="note">共 {{ rows.length }} 条记录</span>
      </div>

      <p v-if="busy" class="loading" role="status">正在加载…</p>
      <EmptyState v-else-if="!rows.length" title="没有符合条件的操作记录" />

      <template v-else>
        <table>
          <colgroup>
            <col style="width:17%"><col style="width:15%"><col style="width:14%">
            <col style="width:38%"><col style="width:16%">
          </colgroup>
          <thead>
            <tr><th>时间</th><th>操作者</th><th>操作</th><th>对象</th><th>结果</th></tr>
          </thead>
          <tbody>
            <tr v-for="(item, index) in rows" :key="index" @click="row = item">
              <td class="muted mono">{{ fmtTime(item.at) }}</td>
              <td class="muted">{{ item.member_name || "管理员" }}</td>
              <td>{{ item.action_label }}</td>
              <td>
                <div class="t-main">{{ item.object_title }}</div>
                <div class="t-sub">{{ item.library_name }}</div>
              </td>
              <td><span class="pill live">{{ AUDIT_RESULT[item.action] || "成功" }}</span></td>
            </tr>
          </tbody>
        </table>
        <div class="table-foot"><span>最近 {{ rows.length }} 条</span><span></span></div>
      </template>
    </div>
  </div>

  <Drawer v-if="row" title="变更详情" :subtitle="row.object_title" @close="row = null">
    <div class="kv">
      <div class="k">时间</div><div>{{ fmtDay(row.at) }}</div>
      <div class="k">操作者</div><div>{{ row.member_name || "管理员" }}</div>
      <div class="k">操作</div><div>{{ row.action_label }}</div>
      <div class="k">对象</div><div>{{ row.object_title }}</div>
      <div class="k">知识库</div><div>{{ row.library_name }}</div>
      <div class="k">版本</div><div class="mono">{{ row.version_id ? String(row.version_id).slice(0, 8) : "—" }}</div>
    </div>
    <template #footer>
      <button class="btn" @click="row = null">关闭</button>
    </template>
  </Drawer>
</template>

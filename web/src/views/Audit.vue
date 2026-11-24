<script setup>
import { ref, computed, onMounted } from "vue";
import PageHead from "../components/PageHead.vue";
import EmptyState from "../components/EmptyState.vue";
import Drawer from "../components/Drawer.vue";
import { store, loadAudit } from "../store";
import { AUDIT_RESULT, fmtTime, fmtDay } from "../format";
import { fail } from "../toast";

const kind = ref("");
const row = ref(null);

const KINDS = [
  ["", "全部操作"], ["publish", "发布"], ["rollback", "回滚"], ["unpublish", "下架"],
  ["restore", "恢复上线"], ["schedule", "调整有效期"], ["grant", "授权"], ["revoke", "撤销访问"],
  ["delete", "删除文档"], ["web_opened", "开通网页登录"], ["web_password_reset", "重置网页密码"],
];

const rows = computed(() => store.audit.filter((item) => !kind.value || item.action === kind.value));

onMounted(() => {
  loadAudit().catch(fail);
});
</script>

<template>
  <div class="wrap">
    <PageHead title="操作记录" subtitle="追溯知识与权限的每一次变更">
      <button class="btn" @click="loadAudit().catch(fail)">刷新</button>
    </PageHead>

    <div class="card">
      <div class="filters">
        <select v-model="kind" style="width:170px">
          <option v-for="[value, label] in KINDS" :key="value" :value="value">{{ label }}</option>
        </select>
        <span class="note">共 {{ rows.length }} 条记录</span>
      </div>

      <EmptyState v-if="!rows.length" title="还没有操作记录" note="发布、上下架与授权变更都会记录在这里。" />

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
              <td class="muted">{{ item.member || "管理员" }}</td>
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
      <div class="k">操作者</div><div>{{ row.member || "管理员" }}</div>
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

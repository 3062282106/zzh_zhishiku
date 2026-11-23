<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { api } from "../api";
import PageHead from "../components/PageHead.vue";
import { store, loadDocs, loadSettings, defaultLibrary } from "../store";
import { fail, toast } from "../toast";

const pending = computed(() =>
  store.tasks.filter((task) => task.status === "running" || task.status === "queued").length);
const contact = ref("");
const saving = ref(false);

watch(() => store.settings.access_contact_url, (url) => { contact.value = url || ""; }, { immediate: true });

async function saveContact() {
  if (saving.value) return;
  saving.value = true;
  try {
    const result = await api("/settings/access-contact", { method: "PUT", body: { url: contact.value.trim() } });
    store.settings.access_contact_url = result.url;
    toast("申请入口已保存。");
  } catch (error) {
    fail(error);
  } finally {
    saving.value = false;
  }
}

// Landing here directly (a bookmark) must still show a real document count.
onMounted(() => {
  loadSettings().catch(fail);
  const id = store.activeLibrary || defaultLibrary();
  if (id) loadDocs(id).catch(() => {});
});
</script>

<template>
  <div class="wrap">
    <PageHead title="设置" subtitle="企业设置 / Agent 接入" />

    <div class="card pad" style="margin-bottom:16px">
      <form @submit.prevent="saveContact">
        <div class="field">
          <label for="access-contact">申请开通 · 飞书私聊链接</label>
          <input id="access-contact" v-model="contact" type="url" required maxlength="2048"
                 placeholder="https://applink.feishu.cn/client/chat/open?...">
        </div>
        <button class="btn primary" :disabled="saving">保存链接</button>
      </form>
    </div>

    <div class="card">
      <div class="pane-head"><h2>查询入口</h2></div>
      <table>
        <colgroup>
          <col style="width:24%"><col style="width:16%"><col style="width:22%"><col style="width:38%">
        </colgroup>
        <thead>
          <tr><th>入口</th><th>连接状态</th><th>身份验证</th><th>授权范围</th></tr>
        </thead>
        <tbody>
          <tr>
            <td><div class="t-main">自有 Agent</div><div class="t-sub">MCP / HTTP 检索接口</div></td>
            <td><span class="pill live">已部署</span></td>
            <td class="muted">成员令牌</td>
            <td class="muted">按成员知识库权限</td>
          </tr>
          <tr>
            <td><div class="t-main">豆包工作伙伴</div><div class="t-sub">待接入</div></td>
            <td><span class="pill queued">待连接</span></td>
            <td class="muted">企业账号</td>
            <td class="muted">按成员知识库权限</td>
          </tr>
          <tr>
            <td><div class="t-main">豆包工作</div><div class="t-sub">待接入</div></td>
            <td><span class="pill queued">待连接</span></td>
            <td class="muted">企业账号</td>
            <td class="muted">按成员知识库权限</td>
          </tr>
        </tbody>
      </table>
      <div style="padding:14px 18px">
        <div class="note">接入入口只返回员工有权访问且已生效的文档。回答必须引用原文，权限与时效在检索前过滤。</div>
      </div>
    </div>

    <div class="card pad" style="margin-top:16px;display:grid;gap:10px">
      <b style="font-size:13.5px">运行状态</b>
      <div class="kv">
        <div class="k">知识库</div><div>{{ store.libraries.length }} 个</div>
        <div class="k">文档</div><div>{{ store.docs.length }} 篇（当前库）</div>
        <div class="k">成员</div><div>{{ store.members.length }} 名</div>
        <div class="k">待处理</div><div>{{ pending }} 项</div>
      </div>
      <div class="note">解析队列依赖 Docling 服务；服务不可用时新文件会停在处理中，已发布资料仍可检索。</div>
    </div>
  </div>
</template>

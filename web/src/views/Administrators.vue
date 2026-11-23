<script setup>
import { computed, ref } from "vue";
import PageHead from "../components/PageHead.vue";
import Drawer from "../components/Drawer.vue";
import { api, enc } from "../api";
import { auth } from "../auth";
import { fail, toast } from "../toast";

const list = ref([]);
const busy = ref(false);
const createOpen = ref(false);
const form = ref({ username: "", name: "", password: "" });
const resetting = ref(null);
const newPassword = ref("");

const selfId = computed(() => (auth.me ? auth.me.id : ""));

async function load() {
  list.value = await api("/administrators");
}
load().catch(fail);

function openCreate() {
  form.value = { username: "", name: "", password: "" };
  createOpen.value = true;
}

async function create() {
  if (busy.value) return;
  busy.value = true;
  try {
    await api("/administrators", { method: "POST", body: form.value });
    createOpen.value = false;
    await load();
    toast("管理员已创建，可以让他登录了。");
  } catch (problem) {
    fail(problem);
  } finally {
    busy.value = false;
  }
}

function openReset(row) {
  resetting.value = row;
  newPassword.value = "";
}

async function reset() {
  if (busy.value) return;
  busy.value = true;
  try {
    await api("/administrators/" + enc(resetting.value.id) + "/password", {
      method: "PUT",
      body: { password: newPassword.value },
    });
    resetting.value = null;
    newPassword.value = "";
    toast("密码已更新，这个账号原来的登录状态已全部失效。");
  } catch (problem) {
    fail(problem);
  } finally {
    busy.value = false;
  }
}

async function toggle(row) {
  try {
    await api("/administrators/" + enc(row.id) + "/state", {
      method: "PUT",
      body: { enabled: !row.enabled },
    });
    await load();
    toast(row.enabled ? "已停用，这个账号立刻无法登录。" : "已启用。");
  } catch (problem) {
    fail(problem);
  }
}

function stamp(value) {
  if (!value) return "从未登录";
  const at = new Date(value);
  return at.toLocaleString("zh-CN", { hour12: false });
}
</script>

<template>
  <div class="wrap">
    <PageHead title="管理员" subtitle="能登录这个后台的账号">
      <button class="btn primary" @click="openCreate">新建管理员</button>
    </PageHead>

    <div class="card">
      <div class="pane-head">
        <h2>管理员账号</h2>
        <span class="muted">共 {{ list.length }} 个</span>
      </div>
      <table>
        <colgroup>
          <col style="width:22%"><col style="width:18%"><col style="width:14%">
          <col style="width:24%"><col style="width:22%">
        </colgroup>
        <thead>
          <tr><th>用户名</th><th>显示名</th><th>状态</th><th>最近登录</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="row in list" :key="row.id">
            <td>
              <div class="t-main">{{ row.username }}</div>
              <div v-if="row.id === selfId" class="t-sub">当前登录</div>
            </td>
            <td>{{ row.display_name || row.username }}</td>
            <td>
              <span v-if="row.enabled" class="pill live">启用</span>
              <span v-else class="pill off">已停用</span>
            </td>
            <td class="muted">{{ stamp(row.last_login_at) }}</td>
            <td>
              <div class="rowacts">
                <button class="btn sm" @click="openReset(row)">改密码</button>
                <button class="btn sm" :disabled="row.id === selfId" @click="toggle(row)">
                  {{ row.enabled ? "停用" : "启用" }}
                </button>
              </div>
            </td>
          </tr>
          <tr v-if="!list.length">
            <td colspan="5" class="loading">还没有管理员账号。</td>
          </tr>
        </tbody>
      </table>
      <div style="padding:14px 18px">
        <div class="note">
          改密码会让这个账号已登录的浏览器立刻退出。停用同理，且不影响已经发给同事的成员令牌——
          那套权限是分开的，在「成员与权限」里管。
        </div>
      </div>
    </div>

    <Drawer v-if="createOpen" title="新建管理员" subtitle="给能进后台的人开一个账号"
            @close="createOpen = false">
      <div class="field">
        <label>用户名（登录用，字母数字下划线，3 到 32 位）</label>
        <input v-model="form.username" type="text" placeholder="例如：zhangsan" maxlength="32" autocomplete="off">
      </div>
      <div class="field">
        <label>显示名（可选，列表里显示这个）</label>
        <input v-model="form.name" type="text" placeholder="例如：张三" maxlength="60">
      </div>
      <div class="field">
        <label>初始密码（至少 10 位）</label>
        <input v-model="form.password" type="password" placeholder="让他登录后自己改" maxlength="200"
               autocomplete="new-password">
      </div>
      <div class="note">密码只能用哈希保存，事后无法查看。对方忘了就回来点「改密码」。</div>
      <template #footer>
        <button class="btn" @click="createOpen = false">取消</button>
        <button class="btn primary" :disabled="busy" @click="create">创建</button>
      </template>
    </Drawer>

    <Drawer v-if="resetting" :title="'改密码 · ' + resetting.username"
            subtitle="保存后这个账号已登录的浏览器会立刻退出" @close="resetting = null">
      <div class="field">
        <label>新密码（至少 10 位）</label>
        <input v-model="newPassword" type="password" placeholder="至少 10 位" maxlength="200"
               autocomplete="new-password">
      </div>
      <div class="note">告诉对方新密码时别走群聊，单独发给他。</div>
      <template #footer>
        <button class="btn" @click="resetting = null">取消</button>
        <button class="btn primary" :disabled="busy" @click="reset">保存</button>
      </template>
    </Drawer>
  </div>
</template>

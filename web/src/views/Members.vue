<script setup>
import { ref, computed, onMounted } from "vue";
import PageHead from "../components/PageHead.vue";
import EmptyState from "../components/EmptyState.vue";
import Drawer from "../components/Drawer.vue";
import { api, enc } from "../api";
import { store, loadMembers } from "../store";
import { activeGrants, toLocalInput, iso } from "../format";
import { toast, toastError, fail } from "../toast";
import { confirmBox } from "../confirm";

const q = ref("");
const member = ref(null);
const inviteOpen = ref(false);
const busy = ref(false);
const chosen = ref([]);
const expires = ref("");

const rows = computed(() => {
  const needle = q.value.trim().toLowerCase();
  if (!needle) return store.members;
  return store.members.filter((item) =>
    item.name.toLowerCase().includes(needle) ||
    String(item.email || "").toLowerCase().includes(needle) ||
    item.id.toLowerCase().includes(needle));
});
const granted = computed(() => rows.value.filter((item) => activeGrants(item).length).length);

onMounted(() => {
  loadMembers().catch(fail);
});

function openMember(item) {
  member.value = item;
  chosen.value = activeGrants(item).map((grant) => grant.library);
  const first = activeGrants(item)[0];
  expires.value = toLocalInput(first ? first.expires_at : "");
}

function toggle(id) {
  const at = chosen.value.indexOf(id);
  if (at >= 0) chosen.value.splice(at, 1);
  else chosen.value.push(id);
}

async function save() {
  if (busy.value || !member.value) return;
  busy.value = true;
  try {
    await api("/members/" + enc(member.value.id) + "/grants",
      { method: "POST", body: { libraries: chosen.value, expires_at: iso(expires.value) } });
    toast("已保存 " + member.value.name + " 的访问范围。");
    member.value = null;
    await loadMembers();
  } catch (error) {
    fail(error);
  } finally {
    busy.value = false;
  }
}

function sendInvite() {
  toastError("邀请需要企业身份服务（SSO / 邮件）支持，当前部署未接入。可先在成员列表为已有账号授权。");
}

function revokeAll() {
  const target = member.value;
  const names = activeGrants(target).map((grant) => grant.name);
  confirmBox({
    title: "撤销 " + target.name + " 的访问？",
    note: target.name + " 将立即失去以下 " + names.length + " 个知识库的访问权限。",
    keys: names.join("、"),
    label: "撤销访问",
    danger: true,
    action: async () => {
      await api("/members/" + enc(target.id) + "/grants", { method: "POST", body: { libraries: [], expires_at: null } });
      toast("已撤销 " + target.name + " 的访问。");
      member.value = null;
      await loadMembers();
    },
  });
}
</script>

<template>
  <div class="wrap">
    <PageHead title="成员与权限" subtitle="管理企业员工与受邀个人的知识库访问范围">
      <button class="btn primary" @click="inviteOpen = true">邀请个人</button>
    </PageHead>

    <div class="card">
      <div class="filters">
        <input v-model="q" class="search" type="text" placeholder="搜索姓名或账号">
      </div>

      <EmptyState v-if="!store.members.length" title="还没有成员"
                  note="成员来自企业身份或受邀个人，先在身份系统里创建账号。" />
      <EmptyState v-else-if="!rows.length" title="没有匹配的成员" note="换个姓名或账号再试。" />

      <template v-else>
        <table>
          <colgroup>
            <col style="width:26%"><col style="width:13%"><col style="width:28%">
            <col style="width:15%"><col style="width:18%">
          </colgroup>
          <thead>
            <tr><th>姓名 / 账号</th><th>身份类型</th><th>关联知识库</th><th>授权状态</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="item in rows" :key="item.id" @click="openMember(item)">
              <td>
                <div class="t-main">{{ item.name }}</div>
                <div class="t-sub">{{ item.email || item.id }}</div>
              </td>
              <td class="muted">{{ item.kind === "guest" ? "受邀个人" : "企业员工" }}</td>
              <td class="muted">
                {{ activeGrants(item).length ? activeGrants(item).map((g) => g.name).join("、") : "—" }}
              </td>
              <td>
                <span v-if="activeGrants(item).length" class="pill live">已授权</span>
                <span v-else class="pill draft">未授权</span>
              </td>
              <td class="right" @click.stop>
                <button class="btn sm" @click="openMember(item)">管理权限</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="table-foot">
          <span>共 {{ rows.length }} 名成员 · {{ granted }} 名有授权</span><span></span>
        </div>
      </template>
    </div>
  </div>

  <Drawer v-if="member" title="成员权限" subtitle="调整后可随时修改" @close="member = null">
    <div class="block">
      <div style="display:flex;align-items:center;gap:10px">
        <b>{{ member.name }}</b>
        <span class="pill draft">{{ member.kind === "guest" ? "受邀个人" : "企业员工" }}</span>
      </div>
      <div class="t-sub">{{ member.email || member.id }}</div>
    </div>

    <div class="field">
      <label>可访问的知识库</label>
      <div class="checks">
        <label v-for="lib in store.libraries" :key="lib.id">
          <input type="checkbox" :checked="chosen.includes(lib.id)" @change="toggle(lib.id)">
          <span>{{ lib.name }}</span>
        </label>
        <div v-if="!store.libraries.length" class="loading">还没有知识库。</div>
      </div>
    </div>

    <div class="field">
      <label>访问到期时间（留空表示长期）</label>
      <input v-model="expires" type="datetime-local">
    </div>

    <div class="note">取消勾选并保存会立即撤销对应知识库的访问。检索、引用回读与原件下载都会同步生效。</div>

    <template #footer>
      <button v-if="chosen.length" class="btn danger" @click="revokeAll">撤销全部访问</button>
      <button class="btn" @click="member = null">取消</button>
      <button class="btn primary" :disabled="busy" @click="save">保存</button>
    </template>
  </Drawer>

  <Drawer v-if="inviteOpen" title="邀请个人" subtitle="为外部同事开通指定知识库" @close="inviteOpen = false">
    <div class="field">
      <label>姓名</label>
      <input type="text" placeholder="姓名" maxlength="120">
    </div>
    <div class="field">
      <label>账号邮箱</label>
      <input type="email" placeholder="name@company.com">
    </div>
    <div class="field">
      <label>可访问的知识库</label>
      <div class="checks">
        <label v-for="lib in store.libraries" :key="lib.id">
          <input type="checkbox"><span>{{ lib.name }}</span>
        </label>
      </div>
    </div>
    <div class="note">邀请个人需要企业身份服务生成账号与登录凭证。当前后台可先为已有账号授权，邀请流程待身份服务接入后开放。</div>
    <template #footer>
      <button class="btn" @click="inviteOpen = false">取消</button>
      <button class="btn primary" @click="sendInvite">发送邀请</button>
    </template>
  </Drawer>
</template>

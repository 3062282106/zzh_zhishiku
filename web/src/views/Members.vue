<script setup>
import { ref, computed, onMounted } from "vue";
import PageHead from "../components/PageHead.vue";
import EmptyState from "../components/EmptyState.vue";
import Drawer from "../components/Drawer.vue";
import { api, enc, knowledgeApi } from "../api";
import { useRoute, useRouter } from "vue-router";
import { auth, clearToken } from "../auth";
import { store, loadMembers, loadSettings } from "../store";
import { activeGrants, toLocalInput, iso } from "../format";
import { toast, toastError, fail } from "../toast";
import { confirmBox } from "../confirm";

const route = useRoute();
const router = useRouter();
const q = ref("");
const member = ref(null);
const createOpen = ref(false);
const busy = ref(false);
const chosen = ref([]);
const expires = ref("");
const webEmail = ref("");
const personalUpload = ref(false);

const emptyForm = () => ({ name: "", email: "", libraries: [], expires: "", personalUpload: false, admin: false, username: "", password: "" });
const form = ref(emptyForm());
const administrators = ref([]);
const managedAdmin = ref(null);
const adminPassword = ref("");
const adminImport = ref(null);
const adminImportToken = ref("");
const feishuAuthorized = ref(false);
const ownAdmin = computed(() => managedAdmin.value?.id === auth.me?.id);
const accounts = computed(() => [...store.members, ...administrators.value]);

async function loadAdministrators() {
  const list = await api("/administrators");
  administrators.value = list.map((item) => ({ ...item, name: item.display_name || item.username, kind: "admin" }));
}
// The plaintext token exists only in this object: the server stores a hash and
// cannot return it again, so closing this drawer means it is gone for good.
const issued = ref(null);
const WEB_STATUS = { not_enabled: "未开通", active: "已设置密码", pending: "待设置密码", expired: "设置链接已过期" };

const rows = computed(() => {
  const needle = q.value.trim().toLowerCase();
  if (!needle) return accounts.value;
  return accounts.value.filter((item) =>
    item.name.toLowerCase().includes(needle) ||
    String(item.email || "").toLowerCase().includes(needle) ||
    String(item.username || "").toLowerCase().includes(needle) ||
    item.id.toLowerCase().includes(needle));
});
const granted = computed(() => rows.value.filter((item) => activeGrants(item).length).length);
const active = computed(() => rows.value.filter((item) => item.enabled).length);

onMounted(() => {
  loadMembers().catch(fail);
  loadAdministrators().then(() => {
    const status = route.query.feishu;
    if (!status && route.query.authorize !== "feishu") return;
    const me = administrators.value.find((item) => item.id === auth.me?.id);
    if (me) openMember(me);
    if (status === "ok") toast("飞书已授权。可以回到 Agent 重新导入。");
    else if (status) toastError(status === "mismatch" ? "飞书邮箱与后台企业邮箱不一致。" : "飞书授权未完成，请重试。");
    const { feishu, authorize, ...query } = route.query;
    router.replace({ query });
  }).catch(fail);
  loadSettings().catch(() => {});
});

function openMember(item) {
  if (item.kind === "admin") {
    adminPassword.value = "";
    adminImport.value = null;
    adminImportToken.value = "";
    feishuAuthorized.value = false;
    managedAdmin.value = item;
    loadAdminImport(item).catch(fail);
    return;
  }
  member.value = item;
  personalUpload.value = Boolean(item.personal_upload);
  webEmail.value = item.email || "";
  chosen.value = activeGrants(item).map((grant) => grant.library);
  const first = activeGrants(item)[0];
  expires.value = toLocalInput(first ? first.expires_at : "");
}

function toggle(id) {
  const at = chosen.value.indexOf(id);
  if (at >= 0) chosen.value.splice(at, 1);
  else chosen.value.push(id);
}

function toggleForm(id) {
  const at = form.value.libraries.indexOf(id);
  if (at >= 0) form.value.libraries.splice(at, 1);
  else form.value.libraries.push(id);
}

async function save() {
  if (busy.value || !member.value) return;
  busy.value = true;
  try {
    await api("/members/" + enc(member.value.id) + "/grants",
      { method: "POST", body: { libraries: chosen.value, expires_at: iso(expires.value), personal_upload: personalUpload.value } });
    toast("已保存 " + member.value.name + " 的访问范围。");
    member.value = null;
    await loadMembers();
  } catch (error) {
    fail(error);
  } finally {
    busy.value = false;
  }
}

function openCreate() {
  form.value = emptyForm();
  createOpen.value = true;
}

async function create() {
  if (busy.value) return;
  const name = form.value.name.trim();
  if (!name) return toastError("请填写成员姓名。");
  busy.value = true;
  try {
    if (form.value.admin) {
      await api("/administrators", { method: "POST", body: { name, username: form.value.username.trim(), password: form.value.password } });
      form.value = emptyForm();
      createOpen.value = false;
      await loadAdministrators();
      toast("管理员已创建。");
      return;
    }
    const created = await api("/members", {
      method: "POST",
      body: {
        name,
        email: form.value.email.trim(),
        libraries: form.value.libraries,
        expires_at: iso(form.value.expires),
        personal_upload: form.value.personalUpload,
      },
    });
    createOpen.value = false;
    issued.value = created;
    await loadMembers();
  } catch (error) {
    fail(error);
  } finally {
    busy.value = false;
  }
}

async function loadAdminImport(target) {
  const access = await api("/administrators/" + enc(target.id) + "/import-access");
  if (managedAdmin.value?.id !== target.id) return;
  adminImport.value = access;
  if (ownAdmin.value && access.libraries.length) {
    const status = await knowledgeApi("/feishu/status");
    if (managedAdmin.value?.id === target.id) feishuAuthorized.value = status.authorized;
  }
}

async function adminImportAction(action) {
  if (busy.value || !managedAdmin.value || !adminImport.value) return;
  busy.value = true;
  try {
    if (action === "save") {
      await api("/administrators/" + enc(managedAdmin.value.id) + "/import-access", {
        method: "PUT", body: { email: adminImport.value.email, libraries: adminImport.value.libraries },
      });
      await loadAdminImport(managedAdmin.value);
      await loadAdministrators();
      toast("已保存 MCP 知识库权限。");
    } else if (action === "authorize") {
      const authorization = await knowledgeApi("/feishu/authorize", { method: "POST" });
      window.location.assign(authorization.url);
    } else {
      const result = await api("/import-token", { method: "POST" });
      adminImportToken.value = result.token;
      adminImport.value.has_token = true;
    }
  } catch (error) {
    fail(error);
  } finally {
    busy.value = false;
  }
}

async function updateAdministrator(action) {
  const target = managedAdmin.value;
  if (!target || busy.value || (action === "state" && target.id === auth.me?.id)) return;
  busy.value = true;
  try {
    const body = action === "password" ? { password: adminPassword.value } : { enabled: !target.enabled };
    await api("/administrators/" + enc(target.id) + "/" + action, { method: "PUT", body });
    managedAdmin.value = null;
    adminPassword.value = "";
    if (action === "password" && target.id === auth.me?.id) {
      clearToken();
      toast("密码已更新，请重新登录。");
    } else {
      await loadAdministrators();
      toast(action === "password" ? "密码已更新。" : target.enabled ? "管理员已停用。" : "管理员已启用。");
    }
  } catch (error) {
    fail(error);
  } finally {
    busy.value = false;
  }
}

function rotate() {
  const target = member.value;
  confirmBox({
    title: "重置 " + target.name + " 的令牌？",
    note: "旧令牌立即失效，这个人需要换成新令牌才能继续使用。",
    label: "重置令牌",
    danger: true,
    action: async () => {
      const rotated = await api("/members/" + enc(target.id) + "/token", { method: "POST" });
      member.value = null;
      issued.value = { ...target, token: rotated.token };
    },
  });
}

function configureWeb() {
  const target = member.value;
  const email = webEmail.value.trim();
  if (!email) return toastError("请先填写成员邮箱。");
  const issue = async () => {
    if (busy.value) return;
    busy.value = true;
    try {
      const setup = await api("/members/" + enc(target.id) + "/web-setup", { method: "POST", body: { email } });
      member.value = null;
      issued.value = { ...target, email: setup.email, web_setup: setup };
      await loadMembers();
    } finally {
      busy.value = false;
    }
  };
  if (target.web_status === "active") {
    confirmBox({ title: "重置 " + target.name + " 的网页密码？", note: "旧密码和已登录的网页会话会立即失效。",
      label: "重置密码", danger: true, action: issue });
  } else {
    issue().catch(fail);
  }
}

function setupLink(setup) {
  const url = new URL("/admin/kb", window.location.origin);
  url.hash = new URLSearchParams({ email: setup.email, setup: setup.setup_code }).toString();
  return url.href;
}

async function switchState(target, enabled) {
  try {
    await api("/members/" + enc(target.id) + "/state", { method: "PUT", body: { enabled } });
    toast(enabled ? "已启用 " + target.name + "。" : "已停用 " + target.name + "，立即无法检索。");
    member.value = null;
    await loadMembers();
  } catch (error) {
    fail(error);
  }
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

/** A ready-to-forward message: the admin should never retype a token by hand. */
function guideText(person, token) {
  const url = store.settings.mcp_url || "（管理员还没配置对外地址）";
  return [
    "知识库检索接入说明",
    "",
    "给：" + (person.name || person.id),
    "",
    "网页查询：" + window.location.origin + "/admin/",
    "网页使用你的邮箱和自行设置的密码登录。",
    "",
    "一、在客户端里新建一个自定义连接器（或 MCP 服务器）",
    "二、传输类型选 HTTP",
    "三、服务器 URL 填：" + url,
    "四、自定义 Header 加一条：",
    "    Authorization: Bearer " + token,
    "",
    "令牌是你的个人身份，不要转发给别人；如果泄漏了，找管理员重置。",
    "",
    "连上之后可以直接问，例如：",
    "    用 knowledge 查：<你的问题>，附上原文",
    "",
    "如已开通个人库上传权限，可发送飞书链接并请 Agent 加到个人知识库。",
  ].join("\n");
}

function fallbackCopy(text) {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  document.body.removeChild(area);
  return ok;
}

async function copy(text, label) {
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
    else if (!fallbackCopy(text)) throw new Error("copy failed");
    toast(label + "已复制。");
  } catch {
    toastError("复制失败，请手动选中文本复制。");
  }
}
</script>

<template>
  <div class="wrap">
    <PageHead title="成员与权限" subtitle="给同事开通账号，决定每个人能查哪些知识库">
      <button class="btn primary" @click="openCreate">新建成员</button>
    </PageHead>

    <div class="card">
      <div class="filters">
        <input v-model="q" class="search" type="text" placeholder="搜索姓名、邮箱或账号">
      </div>

      <EmptyState v-if="!accounts.length" title="还没有成员"
                  note="点右上角「新建成员」，填名字、勾上能看的库，把令牌发给他就能用。" />
      <EmptyState v-else-if="!rows.length" title="没有匹配的成员" note="换个姓名或邮箱再试。" />

      <template v-else>
        <table>
          <colgroup>
            <col style="width:26%"><col style="width:12%"><col style="width:28%">
            <col style="width:16%"><col style="width:18%">
          </colgroup>
          <thead>
            <tr><th>姓名 / 邮箱</th><th>身份</th><th>可访问知识库</th><th>状态</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="item in rows" :key="item.id" @click="openMember(item)">
              <td>
                <div class="t-main">{{ item.name }}</div>
                <div class="t-sub">{{ item.email || item.username || item.id }}</div>
              </td>
              <td><span v-if="item.kind === 'admin'" class="pill live">管理员</span><span v-else class="muted">{{ item.kind === "guest" ? "受邀个人" : "企业员工" }}</span></td>
              <td class="muted">
                {{ item.kind === 'admin' ? '管理全部知识库' : activeGrants(item).map((g) => g.name).join("、") || "—" }}
              </td>
              <td>
                <span v-if="!item.enabled" class="pill expired">已停用</span>
                <span v-else-if="item.kind === 'admin'" class="pill live">已启用</span>
                <span v-else-if="activeGrants(item).length" class="pill live">已授权</span>
                <span v-else class="pill draft">未授权</span>
              </td>
              <td class="right" @click.stop>
                <button class="btn sm" @click="openMember(item)">管理</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="table-foot">
          <span>共 {{ rows.length }} 名成员 · {{ active }} 名启用 · {{ granted }} 名有授权</span><span></span>
        </div>
      </template>
    </div>
  </div>

  <Drawer v-if="member" title="成员权限" subtitle="调整后立即生效" @close="member = null">
    <div class="block">
      <div style="display:flex;align-items:center;gap:10px">
        <b>{{ member.name }}</b>
        <span class="pill draft">{{ member.kind === "guest" ? "受邀个人" : "企业员工" }}</span>
        <span v-if="!member.enabled" class="pill expired">已停用</span>
      </div>
      <div v-if="member.email" class="t-sub">{{ member.email }}</div>
      <div class="t-sub">成员 ID {{ member.id }}</div>
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

    <div class="field">
      <div class="checks"><label><input v-model="personalUpload" type="checkbox">允许通过 Agent 导入到个人知识库</label></div>
    </div>

    <div class="field">
      <label>网页登录 · {{ WEB_STATUS[member.web_status] || '未开通' }}</label>
      <input v-if="!member.email" v-model="webEmail" type="email" placeholder="成员邮箱" aria-label="用于网页登录的邮箱" maxlength="200">
      <div v-if="member.enabled" class="rowacts" style="justify-content:flex-start">
        <button class="btn sm" :disabled="busy" @click="configureWeb">
          {{ member.web_status === 'active' ? '重置网页密码' : member.web_status === 'not_enabled' ? '开通网页登录' : '重新生成设置链接' }}
        </button>
      </div>
    </div>

    <div class="field">
      <label>连接器与账号状态</label>
      <div class="rowacts" style="justify-content:flex-start">
        <button class="btn sm" @click="rotate">重置 MCP 令牌</button>
        <button v-if="member.enabled" class="btn sm danger" @click="switchState(member, false)">停用账号</button>
        <button v-else class="btn sm" @click="switchState(member, true)">启用账号</button>
      </div>
    </div>

    <div class="note">
      取消勾选并保存会立即撤销对应知识库的访问，检索、引用回读与原件下载同步生效。
      停用账号会让令牌彻底失效，比逐个撤库更彻底。
    </div>

    <template #footer>
      <button v-if="chosen.length" class="btn danger" @click="revokeAll">撤销全部访问</button>
      <button class="btn" @click="member = null">取消</button>
      <button class="btn primary" :disabled="busy" @click="save">保存</button>
    </template>
  </Drawer>

  <Drawer v-if="createOpen" title="新建成员" @close="createOpen = false">
    <div class="field">
      <label>姓名</label>
      <input v-model="form.name" type="text" placeholder="例如：张三" :maxlength="form.admin ? 60 : 120">
    </div>
    <div class="field"><div class="checks"><label><input v-model="form.admin" type="checkbox">设为管理员</label></div></div>
    <template v-if="form.admin">
      <div class="field"><label for="admin-username">管理员用户名</label><input id="admin-username" v-model="form.username" maxlength="32" autocomplete="off"></div>
      <div class="field"><label for="admin-password">初始密码（至少 10 位）</label><input id="admin-password" v-model="form.password" type="password" maxlength="200" autocomplete="new-password"></div>
    </template>
    <template v-else>
    <div class="field">
      <label>成员邮箱（用于网页登录）</label>
      <input v-model="form.email" type="email" placeholder="name@company.com" maxlength="200">
    </div>
    <div class="field">
      <label>可访问的知识库</label>
      <div class="checks">
        <label v-for="lib in store.libraries" :key="lib.id">
          <input type="checkbox" :checked="form.libraries.includes(lib.id)" @change="toggleForm(lib.id)">
          <span>{{ lib.name }}</span>
        </label>
        <div v-if="!store.libraries.length" class="loading">还没有知识库。</div>
      </div>
    </div>
    <div class="field">
      <label>访问到期时间（留空表示长期）</label>
      <input v-model="form.expires" type="datetime-local">
    </div>
    <div class="field">
      <div class="checks"><label><input v-model="form.personalUpload" type="checkbox">允许通过 Agent 导入到个人知识库</label></div>
    </div>
    </template>
    <template #footer>
      <button class="btn" @click="createOpen = false">取消</button>
      <button class="btn primary" :disabled="busy" @click="create">创建成员</button>
    </template>
  </Drawer>

  <Drawer v-if="managedAdmin" title="管理管理员" :subtitle="managedAdmin.name" @close="managedAdmin = null">
    <template v-if="adminImport">
      <div class="field">
        <label for="admin-import-email">企业邮箱</label>
        <input id="admin-import-email" v-model.trim="adminImport.email" type="email" maxlength="200">
      </div>
      <div class="field">
        <label>MCP 可检索和导入的企业知识库</label>
        <div class="checks">
          <label v-for="lib in store.libraries" :key="lib.id">
            <input v-model="adminImport.libraries" type="checkbox" :value="lib.id">{{ lib.name }}
          </label>
        </div>
      </div>
      <div class="rowacts" style="justify-content:flex-start">
        <button class="btn sm primary" :disabled="busy" @click="adminImportAction('save')">保存 MCP 权限</button>
        <button v-if="ownAdmin && adminImport.libraries.length" class="btn sm" :disabled="busy" @click="adminImportAction('authorize')">
          {{ feishuAuthorized ? '重新授权飞书' : '授权飞书' }}
        </button>
        <button v-if="ownAdmin && adminImport.libraries.length" class="btn sm" :disabled="busy" @click="confirmBox({
          title: adminImport.has_token ? '重置 MCP 令牌？' : '生成 MCP 令牌？',
          note: adminImport.has_token ? '原令牌立即失效，请更新现有连接器。' : '用此令牌连接 MCP 后，可检索和导入勾选的企业知识库。',
          label: '生成', action: () => adminImportAction('token'),
        })">{{ adminImport.has_token ? '重置 MCP 令牌' : '生成 MCP 令牌' }}</button>
      </div>
      <div v-if="adminImportToken" class="field">
        <label>MCP 令牌</label>
        <div class="secret">
          <code>{{ adminImportToken }}</code>
          <button class="btn sm" @click="copy(adminImportToken, 'MCP 令牌')">复制</button>
          <p class="muted">用此令牌更新现有 MCP 连接器，同一个连接可检索和导入。</p>
        </div>
      </div>
    </template>
    <div class="field"><label for="reset-admin-password">新密码（至少 10 位）</label><input id="reset-admin-password" v-model="adminPassword" type="password" maxlength="200" autocomplete="new-password"></div>
    <template #footer>
      <button class="btn" :disabled="busy || managedAdmin.id === auth.me?.id" @click="updateAdministrator('state')">{{ managedAdmin.enabled ? '停用' : '启用' }}</button>
      <button class="btn primary" :disabled="busy || adminPassword.length < 10" @click="updateAdministrator('password')">重置密码</button>
    </template>
  </Drawer>

  <Drawer v-if="issued" :title="issued.web_setup ? '网页登录设置' : 'MCP 令牌已生成'"
          :autofocus="false" @close="issued = null">
    <div class="block">
      <div class="k">成员</div>
      <div class="v"><b>{{ issued.name }}</b><span v-if="issued.email" class="t-sub"> · {{ issued.email }}</span></div>
      <div class="k">成员 ID</div><div class="mono">{{ issued.id }}</div>
    </div>

    <div v-if="issued.web_setup" class="field">
      <label>一次性设置凭证</label>
      <div class="secret">
        <code>{{ issued.web_setup.setup_code }}</code>
        <div class="note">24 小时内有效，请私发给该成员。</div>
        <div class="rowacts" style="justify-content:flex-start">
          <button class="btn sm primary" @click="copy(setupLink(issued.web_setup), '设置链接')">复制设置链接</button>
          <button class="btn sm" @click="copy(issued.web_setup.setup_code, '设置码')">复制设置码</button>
        </div>
      </div>
    </div>

    <div v-if="issued.token" class="field">
      <label>MCP 令牌</label>
      <div class="secret">
        <code>{{ issued.token }}</code>
        <div class="rowacts" style="justify-content:flex-start">
          <button class="btn sm" @click="copy(issued.token, '令牌')">复制令牌</button>
        </div>
      </div>
    </div>

    <div v-if="issued.token && !store.settings.mcp_url" class="errbox" style="margin:0">
      <b>还没配置对外地址</b>
      <p>服务器 api.env 里的 KB_MCP_PUBLIC_URL 没设置，下面的接入说明里地址是空的。配好之后重开这个窗口再复制。</p>
    </div>

    <div v-if="issued.token" class="field">
      <label>MCP 接入说明</label>
      <div class="secret">
        <pre>{{ guideText(issued, issued.token) }}</pre>
        <div class="rowacts" style="justify-content:flex-start">
          <button class="btn sm primary" @click="copy(guideText(issued, issued.token), '接入说明')">复制接入说明</button>
        </div>
      </div>
    </div>

    <template #footer>
      <button class="btn primary" @click="issued = null">我已复制，关闭</button>
    </template>
  </Drawer>
</template>

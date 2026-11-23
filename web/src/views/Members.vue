<script setup>
import { ref, computed, onMounted } from "vue";
import PageHead from "../components/PageHead.vue";
import EmptyState from "../components/EmptyState.vue";
import Drawer from "../components/Drawer.vue";
import { api, enc } from "../api";
import { store, loadMembers, loadSettings } from "../store";
import { activeGrants, toLocalInput, iso } from "../format";
import { toast, toastError, fail } from "../toast";
import { confirmBox } from "../confirm";

const q = ref("");
const member = ref(null);
const createOpen = ref(false);
const busy = ref(false);
const chosen = ref([]);
const expires = ref("");

const form = ref({ name: "", email: "", libraries: [], expires: "" });
// The plaintext token exists only in this object: the server stores a hash and
// cannot return it again, so closing this drawer means it is gone for good.
const issued = ref(null);

const rows = computed(() => {
  const needle = q.value.trim().toLowerCase();
  if (!needle) return store.members;
  return store.members.filter((item) =>
    item.name.toLowerCase().includes(needle) ||
    String(item.email || "").toLowerCase().includes(needle) ||
    item.id.toLowerCase().includes(needle));
});
const granted = computed(() => rows.value.filter((item) => activeGrants(item).length).length);
const active = computed(() => rows.value.filter((item) => item.enabled).length);

onMounted(() => {
  loadMembers().catch(fail);
  loadSettings().catch(() => {});
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

function openCreate() {
  form.value = { name: "", email: "", libraries: [], expires: "" };
  createOpen.value = true;
}

async function create() {
  if (busy.value) return;
  const name = form.value.name.trim();
  if (!name) return toastError("请填写成员姓名。");
  busy.value = true;
  try {
    const created = await api("/members", {
      method: "POST",
      body: {
        name,
        email: form.value.email.trim(),
        libraries: form.value.libraries,
        expires_at: iso(form.value.expires),
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
    "网页登录填写成员 ID「" + person.id + "」和下面的令牌。",
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
    "可用工具：search_knowledge（查内容）、get_knowledge_reference（取原文片段）",
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

      <EmptyState v-if="!store.members.length" title="还没有成员"
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
                <div class="t-sub">{{ item.email || item.id }}</div>
              </td>
              <td class="muted">{{ item.kind === "guest" ? "受邀个人" : "企业员工" }}</td>
              <td class="muted">
                {{ activeGrants(item).length ? activeGrants(item).map((g) => g.name).join("、") : "—" }}
              </td>
              <td>
                <span v-if="!item.enabled" class="pill expired">已停用</span>
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
      <label>账号操作</label>
      <div class="rowacts" style="justify-content:flex-start">
        <button class="btn sm" @click="rotate">重置令牌</button>
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

  <Drawer v-if="createOpen" title="新建成员" subtitle="填名字、勾上能看的库，生成令牌" @close="createOpen = false">
    <div class="field">
      <label>姓名</label>
      <input v-model="form.name" type="text" placeholder="例如：张三" maxlength="120">
    </div>
    <div class="field">
      <label>邮箱（可选，方便你认人）</label>
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
    <div class="note">创建后令牌只显示一次，请当场复制发给他。之后只能重置，无法再查看。</div>
    <template #footer>
      <button class="btn" @click="createOpen = false">取消</button>
      <button class="btn primary" :disabled="busy" @click="create">创建并生成令牌</button>
    </template>
  </Drawer>

  <Drawer v-if="issued" title="令牌已生成" subtitle="只显示这一次，请当场发给对方"
          :autofocus="false" @close="issued = null">
    <div class="block">
      <div class="k">成员</div>
      <div class="v"><b>{{ issued.name }}</b><span v-if="issued.email" class="t-sub"> · {{ issued.email }}</span></div>
      <div class="k">成员 ID</div><div class="mono">{{ issued.id }}</div>
    </div>

    <div class="field">
      <label>个人令牌</label>
      <div class="secret">
        <code>{{ issued.token }}</code>
        <div class="rowacts" style="justify-content:flex-start">
          <button class="btn sm" @click="copy(issued.token, '令牌')">复制令牌</button>
        </div>
      </div>
    </div>

    <div v-if="!store.settings.mcp_url" class="errbox" style="margin:0">
      <b>还没配置对外地址</b>
      <p>服务器 api.env 里的 KB_MCP_PUBLIC_URL 没设置，下面的接入说明里地址是空的。配好之后重开这个窗口再复制。</p>
    </div>

    <div class="field">
      <label>发给他的接入说明（可直接复制）</label>
      <div class="secret">
        <pre>{{ guideText(issued, issued.token) }}</pre>
        <div class="rowacts" style="justify-content:flex-start">
          <button class="btn sm primary" @click="copy(guideText(issued, issued.token), '接入说明')">复制接入说明</button>
        </div>
      </div>
    </div>

    <div class="note">关掉这个窗口后就看不到这串令牌了。对方弄丢了就回来点「重置令牌」。</div>

    <template #footer>
      <button class="btn primary" @click="issued = null">我已复制，关闭</button>
    </template>
  </Drawer>
</template>

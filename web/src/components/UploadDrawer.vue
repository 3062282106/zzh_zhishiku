<script setup>
import { computed, onMounted, ref } from "vue";
import { api, enc, knowledgeApi } from "../api";
import { store } from "../store";

const props = defineProps({
  libraryId: { type: String, default: "" },
  documentId: { type: String, default: "" },
  personal: { type: Boolean, default: false },
});
const emit = defineEmits(["close", "uploaded"]);

const ACCEPT = ".md,.txt,.pdf,.docx,.xlsx,.csv,.pptx,.png,.jpg,.jpeg,.tif,.tiff";
const MAX_FILE = 20 * 1024 * 1024;
const dialog = ref(null);
const picker = ref(null);
const rows = ref([]);
const dragging = ref(false);
const busy = ref(false);
const title = computed(() => props.documentId ? "上传新版" : "上传文档");
const count = computed(() => rows.value.filter((row) => row.done).length);
const pending = computed(() => rows.value.filter((row) => !row.done && !row.invalid && (props.personal || row.library)));

onMounted(() => dialog.value.showModal());

function add(files) {
  if (busy.value) return;
  const selected = Array.from(files);
  if (props.documentId) rows.value = [];
  for (const file of props.documentId ? selected.slice(0, 1) : selected) {
    const extension = file.name.match(/\.[^.]+$/)?.[0].toLowerCase();
    const invalid = file.size > MAX_FILE ? "超过 20 MB" : !file.size ? "文件为空"
      : (!extension || !ACCEPT.split(",").includes(extension) ? "暂不支持此格式" : "");
    const name = (file.name.replace(/\.[^.]+$/, "").trim() || file.name).slice(0, 160);
    rows.value.push({ id: crypto.randomUUID(), file, title: name, name, library: props.libraryId,
      invalid, error: "", done: false, sending: false });
  }
}

function choose(event) {
  add(event.target.files);
  event.target.value = "";
}

function drop(event) {
  dragging.value = false;
  add(event.dataTransfer.files);
}

function close() {
  if (busy.value) return;
  dialog.value.close();
  if (count.value) emit("uploaded", count.value);
  else emit("close");
}

async function send(row) {
  row.error = "";
  row.sending = true;
  const form = new FormData();
  form.set("title", row.title.trim() || row.name);
  form.set("file", row.file);
  form.set(props.documentId ? "document_id" : "upload_id", props.documentId || row.id);
  try {
    if (props.personal) await knowledgeApi("/personal/files", { method: "POST", body: form });
    else await api("/libraries/" + enc(row.library) + "/documents", { method: "POST", body: form });
    row.done = true;
  } catch (error) {
    row.error = error.message;
  } finally {
    row.sending = false;
  }
}

async function upload() {
  if (busy.value || !pending.value.length) return;
  busy.value = true;
  try {
    for (const row of pending.value) await send(row);
  } finally {
    busy.value = false;
  }
  if (count.value === rows.value.length) close();
}
</script>

<template>
  <Teleport to="body">
    <dialog ref="dialog" class="upload-dialog" aria-labelledby="upload-title" @cancel.prevent="close">
      <header class="upload-head">
        <h2 id="upload-title">{{ title }}</h2>
        <button class="close" type="button" :disabled="busy" @click="close">关闭</button>
      </header>

      <div class="upload-body">
        <button type="button" class="drop upload-drop" :class="{ has: dragging }" :disabled="busy" autofocus
                @click="picker.click()" @dragover.prevent="dragging = true" @dragleave="dragging = false" @drop.prevent="drop">
          <b>{{ rows.length ? (documentId ? '更换文件' : '添加文件') : (documentId ? '选择或拖入文件' : '选择或拖入多个文件') }}</b>
          <span>PDF、Word、Excel、CSV、PPT、图片、Markdown、TXT · 单个文件最大 20 MB</span>
        </button>
        <input ref="picker" class="file-picker" type="file" tabindex="-1" :multiple="!documentId" :accept="ACCEPT"
               aria-label="选择上传文件" @change="choose">

        <div v-if="rows.length" class="upload-table">
          <table>
            <colgroup><col style="width:47%"><col style="width:31%"><col></colgroup>
            <thead><tr><th>文档名称</th><th>所属知识库</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="row in rows" :key="row.id">
                <td>
                  <input v-model="row.title" type="text" maxlength="160" :placeholder="row.name"
                         :aria-label="'文档名称 ' + row.file.name" :disabled="busy || row.done">
                  <span class="file-name muted" :title="row.file.name">{{ row.file.name }}</span>
                </td>
                <td>
                  <span v-if="personal">个人知识库</span>
                  <span v-else-if="documentId">{{ store.libraries.find(item => item.id === row.library)?.name }}</span>
                  <select v-else v-model="row.library" :aria-label="'所属知识库 ' + row.file.name" :disabled="busy || row.done">
                    <option value="" disabled>请选择</option>
                    <option v-for="item in store.libraries" :key="item.id" :value="item.id">{{ item.name }}</option>
                  </select>
                </td>
                <td>
                  <div class="upload-state">
                    <span v-if="row.done" class="pill live">已提交</span>
                    <span v-else-if="row.sending" class="muted">提交中…</span>
                    <span v-else class="muted">{{ row.invalid || row.error ? '失败' : '待导入' }}</span>
                    <button v-if="!row.done && !busy" type="button" class="btn ghost sm"
                            :aria-label="'移除 ' + row.file.name" @click="rows = rows.filter(item => item.id !== row.id)">移除</button>
                  </div>
                  <p v-if="row.invalid || row.error" class="upload-error" role="status">{{ row.invalid || row.error }}</p>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <footer class="upload-foot">
        <p class="muted" aria-live="polite">
          {{ count ? '已提交 ' + count + ' / ' + rows.length + ' 份' : '处理完成后自动发布' }}
        </p>
        <button class="btn" type="button" :disabled="busy" @click="close">{{ count ? '完成' : '取消' }}</button>
        <button class="btn primary" type="button" :disabled="busy || !pending.length" @click="upload">
          {{ busy ? '导入中…' : pending.length ? '导入 ' + pending.length + ' 份' : '导入' }}
        </button>
      </footer>
    </dialog>
  </Teleport>
</template>

<style scoped>
.upload-dialog { color: var(--ink); background: var(--surface); width: min(760px, calc(100% - 32px)); max-height: min(760px, calc(100dvh - 40px)); margin: auto; border: 1px solid var(--line); border-radius: 12px; padding: 0; box-shadow: 0 20px 64px #18233d33; overflow: hidden; }
.upload-dialog[open] { display: flex; flex-direction: column; }
.upload-dialog::backdrop { background: #15203466; }
.upload-head, .upload-foot { padding: 16px 20px; display: flex; align-items: center; gap: 8px; }
.upload-head { justify-content: space-between; border-bottom: 1px solid var(--line); }
.upload-head h2 { font-size: 17px; }
.upload-body { min-height: 0; overflow: auto; padding: 16px 20px; display: grid; gap: 14px; }
.upload-drop { width: 100%; padding: 20px 12px; color: var(--ink); }
.file-picker { position: absolute; width: 1px; height: 1px; opacity: 0; overflow: hidden; pointer-events: none; }
.upload-table { overflow: auto; border: 1px solid var(--line); border-radius: 8px; }
.upload-table table { min-width: 460px; }
.upload-table td { vertical-align: top; }
.upload-table :is(input, select) { width: 100%; min-width: 0; }
.upload-state { display: flex; align-items: center; gap: 4px; min-height: 32px; white-space: nowrap; }
.file-name { display: block; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-top: 4px; }
.upload-error { color: var(--red); font-size: 12px; overflow-wrap: anywhere; }
.upload-foot { border-top: 1px solid var(--line); }
.upload-foot p { margin-right: auto; font-size: 12px; }
</style>

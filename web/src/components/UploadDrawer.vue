<script setup>
import { ref } from "vue";
import Drawer from "./Drawer.vue";
import { api, enc } from "../api";
import { toast, fail } from "../toast";
import { libraryById } from "../store";

const props = defineProps({
  libraryId: { type: String, default: "" },
  documentId: { type: String, default: "" },
});
const emit = defineEmits(["close", "uploaded"]);

const ACCEPT = ".md,.txt,.pdf,.docx,.xlsx,.pptx,.png,.jpg,.jpeg,.tif,.tiff";
const library = () => libraryById(props.libraryId);

const title = ref("");
const file = ref(null);
const dragging = ref(false);
const busy = ref(false);
const picker = ref(null);

function choose(event) {
  const picked = event.target.files && event.target.files[0];
  if (!picked) return;
  file.value = picked;
  if (!title.value.trim()) title.value = picked.name.replace(/\.[^.]+$/, "");
}

function dropped(event) {
  event.preventDefault();
  dragging.value = false;
  const picked = event.dataTransfer.files && event.dataTransfer.files[0];
  if (!picked) return;
  file.value = picked;
  if (!title.value.trim()) title.value = picked.name.replace(/\.[^.]+$/, "");
}

async function upload() {
  if (busy.value) return;
  if (!props.libraryId) return toast("请先选择知识库。");
  if (!file.value) return toast("请选择要上传的文件。");
  if (!title.value.trim()) return toast("请填写文档标题。");
  busy.value = true;
  const form = new FormData();
  form.set("title", title.value.trim());
  form.set("file", file.value);
  if (props.documentId) form.set("document_id", props.documentId);
  try {
    const result = await api("/libraries/" + enc(props.libraryId) + "/documents", { method: "POST", body: form });
    emit("uploaded", result);
  } catch (error) {
    fail(error);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <Drawer :title="documentId ? '上传新版' : '上传文档'" :subtitle="library() ? library().name : ''" @close="$emit('close')">
    <div class="block">
      <div class="k">目标知识库</div>
      <div class="v">{{ library() ? library().name : "未选择" }}</div>
    </div>
    <div class="field">
      <label>文档标题</label>
      <input v-model="title" type="text" placeholder="文档标题" maxlength="160">
    </div>
    <div class="field">
      <label>文件</label>
      <div class="drop" :class="{ has: file || dragging }" role="button" tabindex="0"
           @click="picker.click()" @keydown.enter="picker.click()"
           @dragover.prevent="dragging = true" @dragleave="dragging = false" @drop="dropped">
        <b>{{ file ? file.name : "将文件拖到此处，或选择文件" }}</b>
        <span>{{ file ? "已选择 · 上传后自动开始解析" : "PDF、Word、Excel、PPT、扫描图片、Markdown、TXT，最大 20 MiB" }}</span>
      </div>
      <input ref="picker" type="file" hidden :accept="ACCEPT" @change="choose">
    </div>
    <div class="note">
      {{ documentId ? "上传后会生成新草稿，核对分块后再发布。" : "上传后自动解析；解析完成会出现在处理任务里，可直接打开草稿。" }}
    </div>
    <template #footer>
      <button class="btn" @click="$emit('close')">取消</button>
      <button class="btn primary" :disabled="busy" @click="upload">上传</button>
    </template>
  </Drawer>
</template>

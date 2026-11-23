<script setup>
import { ref } from "vue";
import Drawer from "./Drawer.vue";
import { api, enc } from "../api";
import { toast, fail } from "../toast";

const props = defineProps({ doc: { type: Object, required: true } });
const emit = defineEmits(["close", "saved"]);

const owner = ref(props.doc.owner || "");
const busy = ref(false);

async function save() {
  if (busy.value) return;
  busy.value = true;
  try {
    await api("/documents/" + enc(props.doc.id) + "/owner", { method: "PUT", body: { owner: owner.value.trim() } });
    toast("负责人已更新。");
    emit("saved");
    emit("close");
  } catch (error) {
    fail(error);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <Drawer title="指定负责人" :subtitle="doc.title" @close="$emit('close')">
    <div class="field">
      <label>负责人</label>
      <input v-model="owner" type="text" maxlength="120" placeholder="例如：林晓 / 客服内容组">
      <div class="note" style="margin-top:6px">只用于列表展示与交接，不影响检索与授权。留空表示未指定。</div>
    </div>
    <template #footer>
      <button class="btn" @click="$emit('close')">取消</button>
      <button class="btn primary" :disabled="busy" @click="save">保存</button>
    </template>
  </Drawer>
</template>

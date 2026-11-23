<script setup>
import { ref } from "vue";
import Drawer from "./Drawer.vue";
import StatusPill from "./StatusPill.vue";
import { api, enc } from "../api";
import { toLocalInput, iso } from "../format";
import { toast, fail } from "../toast";

const props = defineProps({
  docId: { type: String, required: true },
  row: { type: Object, required: true },
});
const emit = defineEmits(["close", "saved"]);

const from = ref(toLocalInput(props.row.valid_from));
const until = ref(toLocalInput(props.row.valid_until));
const busy = ref(false);
const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;

async function save() {
  if (busy.value) return;
  busy.value = true;
  try {
    await api("/documents/" + enc(props.docId) + "/availability", {
      method: "PUT",
      body: {
        expected: props.row.current_version,
        expected_published: props.row.published,
        expected_valid_until: props.row.valid_until,
        published: props.row.published,
        valid_until: iso(until.value),
        valid_from: iso(from.value),
      },
    });
    toast("已保存可见性设置。");
    emit("saved");
  } catch (error) {
    fail(error);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <Drawer title="调整可见性" :subtitle="row.title" @close="$emit('close')">
    <div class="block">
      <div class="k">当前状态</div>
      <div class="v"><StatusPill :status="row.visibility_status" /></div>
    </div>
    <div class="field">
      <label>生效时间（留空表示立即）</label>
      <input v-model="from" type="datetime-local">
    </div>
    <div class="field">
      <label>到期时间（留空表示不过期）</label>
      <input v-model="until" type="datetime-local">
    </div>
    <div class="note">时间按浏览器时区 {{ zone }} 输入。到期后员工将无法检索此版本。</div>
    <template #footer>
      <button class="btn" @click="$emit('close')">取消</button>
      <button class="btn primary" :disabled="busy" @click="save">保存</button>
    </template>
  </Drawer>
</template>

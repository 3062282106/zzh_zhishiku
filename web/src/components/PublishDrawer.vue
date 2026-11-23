<script setup>
import { ref } from "vue";
import Drawer from "./Drawer.vue";
import { api, enc } from "../api";
import { iso, toLocalInput } from "../format";
import { toast, fail } from "../toast";

const props = defineProps({
  docId: { type: String, required: true },
  versionId: { type: String, required: true },
  expected: { type: String, default: "" },
  title: { type: String, default: "" },
  label: { type: String, default: "" },
  libraryName: { type: String, default: "当前知识库" },
  liveLabel: { type: String, default: "" },
});
const emit = defineEmits(["close", "queued", "published"]);

const mode = ref("now");
const from = ref("");
const until = ref("");
const busy = ref(false);

async function submit() {
  if (busy.value) return;
  if (mode.value === "later" && !from.value) return toast("定时发布需要填写生效时间。");
  const payload = { expected: props.expected, valid_from: null, valid_until: null };
  if (mode.value === "later") payload.valid_from = iso(from.value);
  if (until.value) payload.valid_until = iso(until.value);
  busy.value = true;
  try {
    const result = await api(
      "/documents/" + enc(props.docId) + "/versions/" + enc(props.versionId) + "/publish",
      { method: "POST", body: payload },
    );
    if (result.publication_status) emit("queued");
    else emit("published", { scheduled: !!payload.valid_from });
  } catch (error) {
    fail(error);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <Drawer title="发布文档" :subtitle="title + ' · ' + label" @close="$emit('close')">
    <div class="block">
      <div class="k">发布范围</div>
      <div class="v">{{ libraryName }}</div>
      <div class="k" style="margin-top:8px">员工当前可查询</div>
      <div class="v">{{ liveLabel || "尚无生效版本" }}</div>
    </div>

    <div class="field">
      <label>生效方式</label>
      <div class="choice">
        <button :class="{ on: mode === 'now' }" @click="mode = 'now'"><span class="dot"></span>立即发布</button>
        <button :class="{ on: mode === 'later' }" @click="mode = 'later'"><span class="dot"></span>定时生效</button>
      </div>
    </div>

    <div v-if="mode === 'later'" class="field">
      <label>生效时间（{{ Intl.DateTimeFormat().resolvedOptions().timeZone }}）</label>
      <input v-model="from" type="datetime-local">
    </div>

    <div class="field">
      <label>到期时间（{{ Intl.DateTimeFormat().resolvedOptions().timeZone }}）</label>
      <input v-model="until" type="datetime-local">
    </div>

    <div class="note">到期后员工将无法检索此版本。发布会在后台构建索引，构建完成前员工仍查询当前版本。</div>

    <template #footer>
      <button class="btn" @click="$emit('close')">取消</button>
      <button class="btn primary" :disabled="busy" @click="submit">发布</button>
    </template>
  </Drawer>
</template>

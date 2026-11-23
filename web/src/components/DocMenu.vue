<script setup>
import { ref } from "vue";
import MenuButton from "./MenuButton.vue";
import OwnerDrawer from "./OwnerDrawer.vue";
import { api, enc } from "../api";
import { toast, fail } from "../toast";
import { confirmBox } from "../confirm";

const props = defineProps({
  doc: { type: Object, required: true },
  size: { type: String, default: "sm" },
});
const emit = defineEmits(["changed", "open-doc", "history"]);

const ownerOpen = ref(false);

async function newestVersion() {
  const rows = await api("/documents/" + enc(props.doc.id) + "/versions");
  return rows[0] || null;
}

async function setPublished(published) {
  const head = await newestVersion();
  if (!head) throw new Error("此文档还没有版本。");
  await api("/documents/" + enc(props.doc.id) + "/availability", {
    method: "PUT",
    body: {
      expected: head.current_version,
      expected_published: head.published,
      expected_valid_until: head.valid_until,
      published,
      valid_until: head.valid_until,
      valid_from: head.valid_from,
    },
  });
}

function unpublish() {
  confirmBox({
    title: "下架这份文档？",
    note: "下架后，员工搜索、原文回读与原件下载会立即停止。可以随时恢复上线。",
    label: "下架",
    danger: true,
    action: async () => {
      await setPublished(false);
      toast("已下架，员工侧立即停止返回。");
      emit("changed");
    },
  });
}

async function restore() {
  try {
    await setPublished(true);
    toast("已恢复上线。");
    emit("changed");
  } catch (error) {
    fail(error);
  }
}
</script>

<template>
  <MenuButton :size="size">
    <template #default="{ close }">
      <button @click="close(); $emit('open-doc')">打开文档</button>
      <button @click="close(); $emit('history')">版本历史</button>
      <button @click="close(); ownerOpen = true">指定负责人</button>
      <button v-if="doc.visibility_status === 'live' || doc.visibility_status === 'pending'"
              class="danger" @click="close(); unpublish()">立即下架</button>
      <button v-else-if="doc.current_version" @click="close(); restore()">恢复上线</button>
    </template>
  </MenuButton>

  <OwnerDrawer v-if="ownerOpen" :doc="doc" @close="ownerOpen = false" @saved="$emit('changed')" />
</template>

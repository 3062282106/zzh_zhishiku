<script setup>
import { computed, nextTick, onMounted, ref } from "vue";
import { store, loadOperationAdmins } from "../store";

const props = defineProps({ modelValue: { type: String, default: "" } });
const emit = defineEmits(["update:modelValue"]);
const root = ref(null);
const input = ref(null);
const open = ref(false);
const query = ref("");
const accounts = computed(() => [...store.members, ...store.operationAdmins]);
const selected = computed(() => accounts.value.find((item) => item.id === props.modelValue));
const visible = computed(() => {
  const needle = query.value.trim().toLowerCase();
  return accounts.value.filter((item) =>
    [item.name, item.email, item.username, item.id].some((value) => String(value || "").toLowerCase().includes(needle)));
});

onMounted(() => loadOperationAdmins().catch(() => {}));

async function toggle() {
  open.value = !open.value;
  if (open.value) {
    query.value = "";
    await nextTick();
    input.value?.focus();
  }
}
function close(restoreFocus = false) {
  open.value = false;
  if (restoreFocus) root.value?.querySelector("button")?.focus();
}
function choose(id) {
  emit("update:modelValue", id);
  close(true);
}
</script>

<template>
  <div ref="root" class="member-picker"
       @focusout="event => { if (!root?.contains(event.relatedTarget)) close() }"
       @keydown.esc.stop="close(true)">
    <button type="button" class="btn member-trigger" :aria-expanded="open" @click="toggle">
      <span>{{ selected?.name || (modelValue || '全部成员') }}</span>
      <span class="caret" aria-hidden="true"></span>
    </button>
    <div v-if="open" class="member-popover">
      <input ref="input" v-model="query" type="search" aria-label="搜索成员" placeholder="姓名、邮箱或账号">
      <div class="member-options">
        <button v-if="!query.trim()" type="button" :class="{ chosen: !modelValue }" @click="choose('')">全部成员</button>
        <button v-for="item in visible" :key="item.id" type="button" :class="{ chosen: item.id === modelValue }"
                @click="choose(item.id)">
          <span class="member-name">{{ item.name }}</span>
          <small>{{ item.email || item.username || item.id }}</small>
        </button>
        <p v-if="!visible.length && query.trim()" class="muted">没有匹配的成员</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.member-picker { position: relative; width: min(210px, 100%); }
.member-trigger { width: 100%; display: flex; justify-content: space-between; gap: 10px; }
.member-trigger > span:first-child { overflow: hidden; text-overflow: ellipsis; }
.caret { flex: none; width: 6px; height: 6px; border: solid var(--muted); border-width: 0 1px 1px 0; transform: rotate(45deg); margin-top: -3px; }
.member-popover { position: absolute; z-index: 5; top: calc(100% + 6px); left: 0; width: min(280px, calc(100vw - 48px)); border: 1px solid var(--line-2); border-radius: 8px; background: var(--surface); box-shadow: 0 8px 24px #17254318; padding: 8px; }
.member-popover input { width: 100%; min-height: 34px; padding: 6px 10px; border: 1px solid var(--line-2); border-radius: 6px; color: var(--ink); background: var(--surface); }
.member-popover input:focus { border-color: var(--brand); outline: 0; box-shadow: 0 0 0 2px #eef2ff; }
.member-popover input::placeholder { color: var(--muted); }
.member-options { max-height: 232px; overflow: auto; margin-top: 6px; }
.member-options button { display: block; width: 100%; text-align: left; border: 0; border-radius: 5px; background: transparent; padding: 8px; color: var(--ink); }
.member-options button:hover, .member-options button:focus-visible { background: var(--bg); }
.member-options button.chosen { background: #eef2ff; color: var(--brand-ink); }
.member-name, small { display: block; overflow: hidden; text-overflow: ellipsis; }
small { color: var(--muted); font-size: 11px; margin-top: 2px; }
p { padding: 8px; }
</style>

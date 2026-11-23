<script setup>
const pages = [
  ["members", "成员与权限"],
  ["tasks", "处理任务"], ["retrieval", "检索记录"], ["audit", "操作记录"], ["settings", "设置"],
];
</script>

<template>
  <div class="wrap">
    <nav class="operations-tabs" aria-label="运维功能">
      <router-link v-for="[name, label] in pages" :key="name" :to="{ name }">{{ label }}</router-link>
    </nav>
    <router-view />
  </div>
</template>

<style scoped>
.operations-tabs { display:flex; gap:22px; overflow-x:auto; border-bottom:1px solid var(--line); margin-bottom:20px; }
.operations-tabs a { flex:none; padding:0 2px 10px; color:var(--ink-2); border-bottom:2px solid transparent; }
.operations-tabs a.router-link-exact-active { color:var(--brand-ink); border-color:var(--brand); font-weight:600; }
</style>

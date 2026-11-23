<script setup>
defineProps({
  title: { type: String, required: true },
  note: { type: String, default: "" },
});
</script>

<template>
  <div class="empty">
    <b>{{ title }}</b>
    <p v-if="note">{{ note }}</p>
    <slot />
  </div>
</template>

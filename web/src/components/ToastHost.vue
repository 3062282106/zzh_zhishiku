<script setup>
import { toasts, dismiss } from "../toast";
</script>

<template>
  <Teleport to="body">
    <div v-if="toasts.items.length" class="toasts">
      <div v-for="item in toasts.items" :key="item.id" class="toast" :class="{ err: item.kind === 'err' }">
        <span>{{ item.message }}</span>
        <button class="x" @click="dismiss(item.id)">关闭</button>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { VISIBILITY } from "../format";

const props = defineProps({
  status: { type: String, default: "draft" },
  label: { type: String, default: "" },
});

const KNOWN = new Set(["live", "draft", "pending", "off", "expired", "failed", "done", "running", "queued"]);
const cls = () => (KNOWN.has(props.status) ? props.status : "draft");
const text = () => props.label || VISIBILITY[props.status] || props.status || "未知";
</script>

<template>
  <span class="pill" :class="cls()">{{ text() }}</span>
</template>

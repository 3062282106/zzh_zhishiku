import { createRouter, createWebHistory } from "vue-router";

// Served by FastAPI under /admin/, so every route lives below that prefix.
// Deep links (a document, a specific version, the probe tab) survive a reload.
export const router = createRouter({
  history: createWebHistory("/admin/"),
  routes: [
    { path: "/", redirect: "/kb" },
    { path: "/query", name: "query", component: () => import("./views/MemberSearch.vue") },
    { path: "/kb/:libraryId?", name: "kb", component: () => import("./views/KBList.vue") },
    { path: "/kb/:libraryId/doc/:docId", name: "doc", component: () => import("./views/DocDetail.vue") },
    { path: "/sources/:libraryId?", name: "sources", component: () => import("./views/Sources.vue") },
    { path: "/members", redirect: { name: "members" } },
    { path: "/administrators", redirect: { name: "members" } },
    { path: "/tasks", redirect: { name: "tasks" } },
    { path: "/retrieval", redirect: { name: "retrieval" } },
    { path: "/audit", redirect: { name: "audit" } },
    { path: "/settings", redirect: { name: "settings" } },
    { path: "/operations", component: () => import("./views/Operations.vue"), meta: { operations: true }, children: [
      { path: "", redirect: { name: "members" } },
      { path: "members", name: "members", component: () => import("./views/Members.vue") },
      { path: "tasks", name: "tasks", component: () => import("./views/Tasks.vue") },
      { path: "retrieval", name: "retrieval", component: () => import("./views/RetrievalRecords.vue") },
      { path: "audit", name: "audit", component: () => import("./views/Audit.vue") },
      { path: "settings", name: "settings", component: () => import("./views/Settings.vue") },
    ] },
    { path: "/:pathMatch(.*)*", redirect: "/kb" },
  ],
  scrollBehavior(to, from, saved) {
    return saved || { top: 0 };
  },
});

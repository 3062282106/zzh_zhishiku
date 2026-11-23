import { createRouter, createWebHistory } from "vue-router";

// Served by FastAPI under /admin/, so every route lives below that prefix.
// Deep links (a document, a specific version, the probe tab) survive a reload.
export const router = createRouter({
  history: createWebHistory("/admin/"),
  routes: [
    { path: "/", redirect: "/kb" },
    { path: "/kb/:libraryId?", name: "kb", component: () => import("./views/KBList.vue") },
    { path: "/kb/:libraryId/doc/:docId", name: "doc", component: () => import("./views/DocDetail.vue") },
    { path: "/sources/:libraryId?", name: "sources", component: () => import("./views/Sources.vue") },
    { path: "/tasks", name: "tasks", component: () => import("./views/Tasks.vue") },
    { path: "/members", name: "members", component: () => import("./views/Members.vue") },
    { path: "/audit", name: "audit", component: () => import("./views/Audit.vue") },
    { path: "/settings", name: "settings", component: () => import("./views/Settings.vue") },
    { path: "/:pathMatch(.*)*", redirect: "/kb" },
  ],
  scrollBehavior(to, from, saved) {
    return saved || { top: 0 };
  },
});

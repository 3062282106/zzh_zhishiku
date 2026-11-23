import { reactive } from "vue";
import { api, enc } from "./api";

/** Console-wide data. Small on purpose: every list is refetched, never cached stale. */
export const store = reactive({
  libraries: [],
  docs: [],
  sources: [],
  tasks: [],
  members: [],
  audit: [],
  probeMember: "",
  // Remembers the library you were last in, so the sidebar link does not bounce
  // you back to the first one.
  activeLibrary: "",
});

/** Where "知识库" should land when the URL does not name a library. */
export function defaultLibrary() {
  const known = store.libraries.some((item) => item.id === store.activeLibrary);
  return known ? store.activeLibrary : (store.libraries[0] ? store.libraries[0].id : "");
}

export async function loadLibraries(tokenOverride) {
  store.libraries = await api("/libraries", { tokenOverride });
  return store.libraries;
}

export function libraryById(id) {
  return store.libraries.find((item) => item.id === id) || null;
}

export async function loadDocs(libraryId) {
  if (libraryId) store.activeLibrary = libraryId;
  store.docs = libraryId ? await api("/libraries/" + enc(libraryId) + "/documents") : [];
  return store.docs;
}

export async function loadSources(libraryId) {
  if (libraryId) store.activeLibrary = libraryId;
  store.sources = libraryId ? await api("/libraries/" + enc(libraryId) + "/sources") : [];
  return store.sources;
}

export async function loadTasks() {
  store.tasks = await api("/tasks?days=30");
  return store.tasks;
}

export async function loadMembers() {
  store.members = await api("/members");
  if (!store.probeMember && store.members.length) store.probeMember = store.members[0].id;
  return store.members;
}

export async function loadAudit() {
  store.audit = await api("/audit?limit=300");
  return store.audit;
}

export async function loadVersions(documentId) {
  return api("/documents/" + enc(documentId) + "/versions");
}

export async function loadPreview(documentId, versionId) {
  return api("/documents/" + enc(documentId) + "/versions/" + enc(versionId));
}

/** Everything the shell needs before the first paint of a page. */
export async function loadShell() {
  await loadLibraries();
  const results = await Promise.allSettled([loadTasks(), loadMembers(), loadAudit()]);
  return results.find((item) => item.status === "rejected") || null;
}

export function resetStore() {
  store.libraries = [];
  store.docs = [];
  store.sources = [];
  store.tasks = [];
  store.members = [];
  store.audit = [];
  store.probeMember = "";
}

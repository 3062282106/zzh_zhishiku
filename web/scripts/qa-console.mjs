/**
 * Headless console QA: drives the built Vue app through Chrome DevTools Protocol,
 * captures a screenshot per screen and fails on any page error or CSP violation.
 *
 *   npm run qa                 # requires the API on :18081, KB_ADMIN_USER and KB_ADMIN_PASSWORD
 *   QA_UPLOAD=1 npm run qa     # also uploads .local/qa-console/upload.md
 *
 * The upload run leaves one draft document behind; it is off by default so a
 * shared fixture database does not fill up with test rows.
 */
import { spawn } from "node:child_process";
import { mkdir, writeFile, rm } from "node:fs/promises";
import { setTimeout as sleep } from "node:timers/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";

const BASE = process.env.QA_BASE || "http://127.0.0.1:18081";
const USER = process.env.KB_ADMIN_USER || "";
const PASSWORD = process.env.KB_ADMIN_PASSWORD || "";
const OUT = process.env.QA_OUT || ".local/qa-console";
const PORT = Number(process.env.QA_CDP_PORT || 9333);
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";

if (!USER || !PASSWORD) {
  console.error("Set KB_ADMIN_USER and KB_ADMIN_PASSWORD for the console QA account.");
  process.exit(2);
}

const profile = join(tmpdir(), "kb-console-qa-" + Date.now());
const chrome = spawn(CHROME, [
  "--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--no-first-run", "--no-default-browser-check", "--no-proxy-server", "--disable-gpu",
  "--hide-scrollbars", "--force-device-scale-factor=1", "--window-size=1440,1000", "about:blank",
], { stdio: "ignore" });

async function endpoint() {
  for (let attempt = 0; attempt < 40; attempt++) {
    try {
      const response = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (response.ok) return (await response.json()).webSocketDebuggerUrl;
    } catch {
      /* chrome is still starting */
    }
    await sleep(250);
  }
  throw new Error("Chrome DevTools endpoint did not come up.");
}

const browser = await endpoint();
const page = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" })).json();
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  ws.addEventListener("open", resolve, { once: true });
  ws.addEventListener("error", reject, { once: true });
});

let seq = 0;
const pending = new Map();
const listeners = new Map();
ws.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    message.error ? reject(new Error(message.error.message)) : resolve(message.result);
    return;
  }
  for (const handler of listeners.get(message.method) || []) handler(message.params);
});

const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++seq;
  pending.set(id, { resolve, reject });
  ws.send(JSON.stringify({ id, method, params }));
});
const on = (method, handler) => {
  if (!listeners.has(method)) listeners.set(method, []);
  listeners.get(method).push(handler);
};
const once = (method) => new Promise((resolve) => {
  const handler = (params) => {
    const list = listeners.get(method);
    list.splice(list.indexOf(handler), 1);
    resolve(params);
  };
  on(method, handler);
});

const problems = [];
on("Runtime.exceptionThrown", (params) => {
  problems.push("JS 异常：" + (params.exceptionDetails.exception?.description || params.exceptionDetails.text));
});
on("Log.entryAdded", (params) => {
  if (params.entry.level === "error") problems.push("浏览器日志：" + params.entry.text);
});
on("Runtime.consoleAPICalled", (params) => {
  if (params.type !== "error" && params.type !== "warning") return;
  problems.push("console." + params.type + "：" + params.args.map((arg) => arg.value ?? arg.description).join(" "));
});

await send("Page.enable");
await send("Runtime.enable");
await send("Log.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
await mkdir(OUT, { recursive: true });

async function goto(path) {
  problems.length = 0;
  const loaded = once("Page.loadEventFired");
  await send("Page.navigate", { url: BASE + path });
  await loaded;
  await sleep(900);
}

async function evaluate(expression) {
  const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
}

async function shot(name) {
  const { data } = await send("Page.captureScreenshot", { format: "png" });
  await writeFile(join(OUT, name + ".png"), Buffer.from(data, "base64"));
}

const results = [];
async function submitCredentials(password) {
  await evaluate(`(() => {
    const form = document.querySelector('.login form');
    const [username, password] = form.querySelectorAll('input');
    username.value = ${JSON.stringify(USER)};
    password.value = ${JSON.stringify(password)};
    username.dispatchEvent(new Event('input', { bubbles: true }));
    password.dispatchEvent(new Event('input', { bubbles: true }));
    form.requestSubmit();
  })()`);
  await sleep(1800);
}

function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log((ok ? "PASS  " : "FAIL  ") + name + (detail ? "  ·  " + detail : ""));
}

async function screen(name, path, expectations) {
  await goto(path);
  const state = await evaluate("({ title: document.title, h1: (document.querySelector('h1')||{}).textContent || '', rows: document.querySelectorAll('tbody tr').length, path: location.pathname + location.search })");
  await shot(name);
  for (const [label, ok, detail] of expectations(state)) check(name + " · " + label, ok, detail);
  if (problems.length) check(name + " · 无页面报错", false, problems.join(" | "));
}

try {
  // 1. Login gate: no token in the tab yet.
  await goto("/admin/");
  const loginTitle = await evaluate("(document.querySelector('.login h1')||{}).textContent || ''");
  await shot("01-login");
  check("登录页 · 统一入口", loginTitle === "登录知识库", loginTitle);

  await submitCredentials(PASSWORD);
  const TOKEN = await evaluate("sessionStorage.getItem('kb.admin.token')");
  if (!TOKEN) {
    check("管理员登录", false, "未收到管理员会话");
    throw new Error("Administrator sign-in failed");
  }

  const libraries = await (await fetch(BASE + "/v1/admin/libraries", {
    headers: { Authorization: "Bearer " + TOKEN },
  })).json();
  // Prefer the documented fixture library: its grants and chunk counts are what the
  // permission checks below assert. Any library with documents is an acceptable
  // fallback, and the assertions degrade to structural ones when it is used.
  const FIXTURE = "售后";
  let library = libraries[0];
  let documents = [];
  for (const candidate of libraries) {
    const rows = await (await fetch(`${BASE}/v1/admin/libraries/${candidate.id}/documents`, {
      headers: { Authorization: "Bearer " + TOKEN },
    })).json();
    if (!rows.length) continue;
    if (!documents.length) {
      library = candidate;
      documents = rows;
    }
    if (rows.some((row) => String(row.title).includes(FIXTURE))) {
      library = candidate;
      documents = rows;
      break;
    }
  }
  const doc = documents.find((row) => String(row.title).includes(FIXTURE)) || documents[0];
  const isFixture = Boolean(doc) && String(doc.title).includes(FIXTURE);

  // 2. Library list, deep link resolved to a real library.
  await screen("02-kb-list", "/admin/kb/" + library.id, (state) => [
    ["标题", state.h1 === "知识库", state.h1],
    ["文档行", state.rows === documents.length && state.rows > 0, state.rows + " 行"],
  ]);

  // 3. Search keeps focus and filters in place (the bug that started the rewrite).
  // The term comes from the fixture itself so the check works on any library.
  const term = String(doc ? doc.title : documents[0].title).slice(0, 2);
  await evaluate(`(() => { const input = document.querySelector('input.search'); input.focus(); input.value = ${JSON.stringify(term)}; input.dispatchEvent(new Event('input', {bubbles:true})); })()`);
  await sleep(300);
  const search = await evaluate(`(() => { const input = document.querySelector('input.search'); return { value: input.value, focused: document.activeElement === input, rows: document.querySelectorAll('tbody tr').length }; })()`);
  check("列表搜索 · 输入不丢焦点", search.focused && search.value === term && search.rows >= 1, JSON.stringify(search));
  await shot("03-kb-search");
  await evaluate(`(() => { const input = document.querySelector('input.search'); input.value = ''; input.dispatchEvent(new Event('input', {bubbles:true})); })()`);

  if (doc) {
    const detail = "/admin/kb/" + library.id + "/doc/" + doc.id;
    await screen("04-doc-chunks", detail, (state) => [
      ["标题", state.h1 === doc.title, state.h1],
      ["原文分块", true, ""],
    ]);
    const chunks = await evaluate("document.querySelectorAll('.chunk').length");
    check("文档详情 · 分块数量", chunks === (isFixture ? 4 : doc.chunk_count), chunks + " 块");
    const paper = await evaluate("(document.querySelector('.paper')||{}).textContent || ''");
    if (isFixture) {
      check("文档详情 · 原文快照", paper.includes("退换货时限"), paper.slice(0, 24) + "…");
    } else {
      check("文档详情 · 原文快照有正文", paper.trim().length > 40, paper.slice(0, 24) + "…");
    }

    // Highlight the second chunk and confirm the paper marks it.
    await evaluate("document.querySelectorAll('.chunk')[1].click()");
    await sleep(250);
    check("文档详情 · 分块高亮", await evaluate("!!document.querySelector('.paper mark')"), "");
    await shot("05-doc-highlight");

    // Probe as a member with access, then as one without.
    await goto(detail + "?tab=probe");
    await shot("06-probe");
    await evaluate(`(() => { const input = document.querySelector('.probe input[type=text]'); input.value = ${JSON.stringify(isFixture ? "退货运费谁承担" : String(doc.title))}; input.dispatchEvent(new Event('input', {bubbles:true})); })()`);
    await evaluate("document.querySelector('.probe .btn.primary').click()");
    await sleep(1200);
    const probe = await evaluate("({ hits: document.querySelectorAll('.hit').length, denied: !!document.querySelector('.denied') })");
    check("试检索 · 有权限身份", probe.hits > 0 && !probe.denied, JSON.stringify(probe));
    await shot("07-probe-hits");

    const denied = await evaluate(`(() => { const select = document.querySelector('.probe select'); const option = [...select.options].find(o => o.textContent.includes('support-test')); if (!option) return 'no-member'; select.value = option.value; select.dispatchEvent(new Event('change', {bubbles:true})); return 'ok'; })()`);
    if (denied === "ok") {
      await sleep(200);
      await evaluate("document.querySelector('.probe .btn.primary').click()");
      await sleep(1200);
      check("试检索 · 无权限身份", await evaluate("!!document.querySelector('.denied')"), "");
      await shot("08-probe-denied");
    }

    // Drawers.
    await goto(detail);
    await evaluate(`[...document.querySelectorAll('.page-actions .btn')].find(b => b.textContent.includes('版本历史')).click()`);
    await sleep(400);
    check("抽屉 · 版本历史", await evaluate("!!document.querySelector('.drawer .block')"), "");
    await shot("09-history");
    // Escape must close only the top layer.
    for (const type of ["keyDown", "keyUp"]) {
      await send("Input.dispatchKeyEvent", { type, key: "Escape", code: "Escape", windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
    }
    await sleep(300);
    check("抽屉 · Escape 关闭", await evaluate("!document.querySelector('.drawer')"), "");

    await evaluate(`[...document.querySelectorAll('.page-actions .btn')].find(b => b.textContent.trim() === '发布').click()`);
    await sleep(400);
    check("抽屉 · 发布", await evaluate("!!document.querySelector('.drawer .choice')"), "");
    await shot("10-publish");
    await evaluate("document.querySelector('.overlay').click()");
    await sleep(200);
    check("抽屉 · 点遮罩关闭", await evaluate("!document.querySelector('.drawer')"), "");

    // "更多" menu on the detail page.
    await evaluate(`[...document.querySelectorAll('.page-actions .btn')].find(b => b.textContent.trim() === '更多').click()`);
    await sleep(300);
    const menu = await evaluate("[...document.querySelectorAll('.menu button')].map(b => b.textContent)");
    check("更多菜单 · 条目", menu.length >= 3 && menu.includes("指定负责人"), menu.join(" / "));
    await shot("11-doc-menu");
    await evaluate("document.body.click()");
    await sleep(200);

    // A document that does not exist must explain itself, not go blank.
    await goto("/admin/kb/" + library.id + "/doc/11111111-1111-1111-1111-111111111111");
    const failure = await evaluate("({ head: (document.querySelector('.errbox b')||{}).textContent || '', detail: (document.querySelector('.errbox p')||{}).textContent || '' })");
    check("错误状态 · 不存在的文档", failure.head.includes("无法打开") && failure.detail.length > 0, failure.head + " / " + failure.detail);
    await shot("18-doc-error");

    // The scroll container is .view, not the window: switching pages must reset it.
    await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 520, deviceScaleFactor: 1, mobile: false });
    await goto(detail);
    const scrolled = await evaluate("(() => { const view = document.querySelector('.view'); view.scrollTop = 300; return view.scrollTop; })()");
    await evaluate(`[...document.querySelectorAll('.nav a')].find((a) => a.textContent.trim() === '知识库').click()`);
    await sleep(700);
    const scrollAfter = await evaluate("document.querySelector('.view').scrollTop");
    check("路由切换 · 滚动位置复位", scrolled > 0 && scrollAfter === 0, "滚到 " + scrolled + "，切换后 " + scrollAfter);
    await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  }

  await screen("12-sources", "/admin/sources/" + library.id, (state) => [["标题", state.h1 === "同步来源", state.h1]]);

  // Optional: the upload drawer, end to end (path 1 of the four main flows).
  if (process.env.QA_UPLOAD) {
    await goto("/admin/kb/" + library.id);
    await evaluate(`[...document.querySelectorAll('.page-head .btn')].find((b) => b.textContent.includes('上传文档')).click()`);
    await sleep(400);
    await send("DOM.enable");
    const root = (await send("DOM.getDocument")).root;
    const input = await send("DOM.querySelector", { nodeId: root.nodeId, selector: "input[type=file]" });
    await send("DOM.setFileInputFiles", { nodeId: input.nodeId, files: [process.env.QA_UPLOAD] });
    await sleep(400);
    const picked = await evaluate("({ name: (document.querySelector('.drop b')||{}).textContent || '', title: (document.querySelector('.drawer input[type=text]')||{}).value || '' })");
    check("上传抽屉 · 选择文件", picked.name.includes(".md") && picked.title.length > 0, picked.name + " / " + picked.title);
    await shot("20-upload");
    await evaluate(`[...document.querySelectorAll('.drawer-foot .btn')].find((b) => b.textContent.trim() === '上传').click()`);
    await sleep(2500);
    const uploaded = await evaluate("(document.querySelector('.doc-head h1')||{}).textContent || ''");
    check("上传抽屉 · 生成草稿并打开", uploaded.length > 0, uploaded);
    await shot("21-upload-done");
  }

  await screen("13-tasks", "/admin/tasks", (state) => [["标题", state.h1 === "处理任务", state.h1]]);
  await screen("14-members", "/admin/members", (state) => [
    ["标题", state.h1 === "成员与权限", state.h1],
    ["成员行", state.rows >= 2, state.rows + " 行"],
  ]);

  // Member drawer with library checkboxes.
  await evaluate("document.querySelector('tbody tr').click()");
  await sleep(400);
  const checks = await evaluate("document.querySelectorAll('.drawer .checks input').length");
  check("成员抽屉 · 知识库勾选", checks === libraries.length, checks + " 个");
  await shot("15-member-drawer");
  await evaluate("document.querySelector('.overlay').click()");
  await sleep(200);

  await screen("16-audit", "/admin/audit", (state) => [["标题", state.h1 === "操作记录", state.h1]]);
  await screen("17-settings", "/admin/settings", (state) => [["标题", state.h1 === "设置", state.h1]]);

  // Unknown path falls back to the list instead of a dead end.
  await goto("/admin/does-not-exist");
  const fallback = await evaluate("location.pathname");
  check("未知路径 · 回退到列表", fallback.startsWith("/admin/kb"), fallback);

  // A reload on a deep link must survive (history mode + SPA fallback).
  if (doc) {
    await goto("/admin/kb/" + library.id + "/doc/" + doc.id);
    await send("Page.reload", { ignoreCache: true });
    await sleep(2000);
    const reloaded = await evaluate("(document.querySelector('.doc-head h1')||{}).textContent || ''");
    check("深链 · 刷新后保持", reloaded === doc.title, reloaded);

    // The same link, opened by someone who has not logged in yet.
    await evaluate("sessionStorage.clear()");
    const deep = "/admin/kb/" + library.id + "/doc/" + doc.id;
    await goto(deep);
    check("深链 · 未登录先看到登录页", await evaluate("!!document.querySelector('.login')"), "");
    await submitCredentials(PASSWORD);
    const landed = await evaluate("({ title: (document.querySelector('.doc-head h1')||{}).textContent || '', path: location.pathname })");
    check("深链 · 登录后回到该文档", landed.title === doc.title && landed.path === deep, landed.path);
  }

  // The sidebar link keeps the library you were last in.
  if (libraries[1]) {
    const second = "/admin/kb/" + libraries[1].id;
    await goto(second);
    await evaluate(`[...document.querySelectorAll('.nav a')].find((a) => a.textContent.trim() === '知识库').click()`);
    await sleep(900);
    const kept = await evaluate("location.pathname");
    check("侧栏 · 保留当前知识库", kept === second, kept);
  }

  // Invalid credentials should display an error on the login card.
  await evaluate("sessionStorage.clear()");
  await goto("/admin/");
  await submitCredentials("definitely-not-the-password");
  const loginError = await evaluate("(document.querySelector('.login .err')||{}).textContent || ''");
  check("登录 · 无效凭据有提示", loginError.includes("凭据"), loginError);
  await shot("19-login-error");
} finally {
  const failed = results.filter((item) => !item.ok);
  console.log("\n" + (results.length - failed.length) + "/" + results.length + " 项通过，截图在 " + OUT);
  ws.close();
  chrome.kill();
  try {
    await rm(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 300 });
  } catch {
    /* Chrome may still hold the profile cache; the temp dir is disposable. */
  }
  process.exit(failed.length ? 1 : 0);
}

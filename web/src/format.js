export const VISIBILITY = { live: "生效中", draft: "草稿", pending: "待生效", off: "已下架", expired: "已过期" };
export const SOURCE = { upload: "本地上传", notion: "Notion", feishu: "飞书" };
export const TASK_KIND = { parse: "解析", publish: "发布", sync: "同步" };
export const TASK_STATE = { queued: "等待中", running: "处理中", failed: "失败", done: "已完成" };
export const AUDIT_RESULT = {
  publish: "已生效", rollback: "已生效", unpublish: "已下架", restore: "已上线",
  schedule: "已更新", grant: "已授权", revoke: "已撤销",
};

const pad = (n) => String(n).padStart(2, "0");

export function fmtTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const base = pad(date.getMonth() + 1) + "-" + pad(date.getDate()) + " " + pad(date.getHours()) + ":" + pad(date.getMinutes());
  return date.getFullYear() === new Date().getFullYear() ? base : date.getFullYear() + "-" + base;
}

export function fmtDay(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()) +
    " " + pad(date.getHours()) + ":" + pad(date.getMinutes());
}

/** ISO timestamp -> value for <input type="datetime-local"> in the browser's zone. */
export function toLocalInput(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

/** datetime-local value -> ISO with offset, or null when empty. */
export function iso(value) {
  return value ? new Date(value).toISOString() : null;
}

export const versionLabel = (index, total) => "v" + (total - index);

export function ext(filename) {
  const name = String(filename || "");
  const at = name.lastIndexOf(".");
  return at > 0 ? name.slice(at + 1).toUpperCase() : "文件";
}

/** Where a chunk came from, in the reader's words. */
export function chunkWhere(chunk, index) {
  const locator = (chunk && chunk.locator) || {};
  if (locator.position_unit === "csv_line") {
    const lines = locator.line_start === locator.line_end ? locator.line_start : `${locator.line_start}–${locator.line_end}`;
    return `CSV 第 ${lines} 行`;
  }
  if (locator.worksheets && locator.worksheets.length) return "工作表 " + locator.worksheets.join("、");
  if (locator.pages && locator.pages.length) {
    const unit = { worksheet_cell: "张工作表", slide: "张幻灯片" }[locator.position_unit] || "页";
    return "第 " + locator.pages.join("、") + " " + unit;
  }
  if (locator.line_start) return "快照第 " + locator.line_start + "–" + locator.line_end + " 行";
  return "分块 " + (index + 1);
}

export function activeGrants(member) {
  const now = Date.now();
  return ((member && member.grants) || []).filter(
    (grant) => !grant.revoked_at && (!grant.expires_at || new Date(grant.expires_at).getTime() > now),
  );
}

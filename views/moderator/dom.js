// DOM helpers, formatting, labels, toast and confirm dialog.
// Every dynamic string goes through textContent / createTextNode. href and src are
// refused by el(); use setTiebaHref() / setDataImage() instead.

const FORBIDDEN_ATTRS = new Set(["href", "src", "srcdoc", "innerhtml", "outerhtml"]);

/**
 * Create an element. Props: class, text, dataset, on* handlers, other attributes.
 * Children may be strings, nodes, arrays or null/false (skipped).
 */
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (FORBIDDEN_ATTRS.has(key.toLowerCase())) throw new Error(`el(): attribute ${key} is not allowed`);
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** Replace all children; null/false/undefined entries are skipped (unlike replaceChildren). */
export function setChildren(node, ...children) {
  node.replaceChildren();
  return append(node, children);
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

/** Set href only for https://tieba.baidu.com/ URLs. Returns whether it was set. */
export function setTiebaHref(anchor, url) {
  try {
    const parsed = new URL(String(url));
    if (parsed.protocol !== "https:" || parsed.host !== "tieba.baidu.com") return false;
    anchor.setAttribute("href", parsed.href);
    anchor.setAttribute("target", "_blank");
    anchor.setAttribute("rel", "noopener noreferrer");
    return true;
  } catch {
    return false;
  }
}

/** Set img.src only for data:image/ URLs returned by the image endpoint. */
export function setDataImage(img, value) {
  if (typeof value !== "string" || !value.startsWith("data:image/")) return false;
  img.setAttribute("src", value);
  return true;
}

const pad = (n) => String(n).padStart(2, "0");

/** Unix seconds -> local "YYYY-MM-DD HH:mm". */
export function fmtTime(seconds) {
  if (!seconds) return "-";
  const d = new Date(seconds * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} 秒`;
  return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
}

export function fmtInterval(seconds) {
  if (seconds >= 3600 && seconds % 3600 === 0) return `${seconds / 3600} 小时`;
  if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60} 分钟`;
  return `${seconds} 秒`;
}

export const fmtConf = (value) => (typeof value === "number" ? value.toFixed(2) : "-");

export const STATUS_LABEL = {
  pending: "待处理",
  auto_deleted: "自动删除",
  deleted: "已删除",
  banned: "已封禁",
  ignored: "已忽略",
  restored: "已恢复",
};
export const ALL_STATUSES = Object.keys(STATUS_LABEL);
export const KIND_LABEL = { thread: "主题帖", post: "回复", comment: "楼中楼" };
export const ACTION_LABEL = { none: "不处理", report: "推送给吧务", auto_delete: "自动删除" };
export const BAN_DAYS = [1, 3, 10];

export function kindText(item) {
  const kind = KIND_LABEL[item.kind] || item.kind;
  return item.kind === "thread" || !item.floor ? kind : `${kind} · 第 ${item.floor} 楼`;
}

export function authorText(author) {
  if (!author) return "-";
  return author.nick_name || author.user_name || "(无昵称)";
}

export function statusChip(status) {
  const known = Object.hasOwn(STATUS_LABEL, status);
  return el("span", { class: `chip chip-${known ? status : "unknown"}`, text: known ? STATUS_LABEL[status] : String(status) });
}

/** Text button factory. */
export function button(label, { variant = "", onclick, type = "button", ...rest } = {}) {
  return el("button", { type, class: `btn ${variant ? `btn-${variant}` : ""}`.trim(), onclick, text: label, ...rest });
}

/** Inline waiting indicator for a long-running request (the trial judgement). */
export function spinner(label = "加载中…") {
  return el("div", { class: "state", role: "status" }, el("span", { class: "spin", "aria-hidden": "true" }), label);
}

/** Loading state: the final layout drawn as flat grey bars (no shimmer), `count` blocks of `lines` bars. */
export function skeleton(count = 3, lines = 4, { cards = true } = {}) {
  const block = () => el(
    "div",
    { class: cards ? "sk-block sk-card" : "sk-block" },
    Array.from({ length: lines }, (_, i) => el("span", { class: "sk-bar", dataset: { w: String(i % 3) } })),
  );
  return el("div", { class: "sk-wrap", role: "status", "aria-label": "加载中" }, Array.from({ length: count }, block));
}

export function emptyState(message) {
  return el("div", { class: "state state-empty", text: message });
}

/** Failure inside the block that failed: one sentence, when data was last good, and a retry action. */
export function errorState(message, onRetry, lastGood = 0) {
  return el(
    "div",
    { class: "state state-error", role: "alert" },
    el("span", { class: "state-dot", "aria-hidden": "true" }),
    el("span", { text: lastGood ? `${message}（上次成功：${fmtTime(lastGood / 1000)}）` : message }),
    onRetry ? button("重试", { variant: "link", onclick: onRetry }) : null,
  );
}

export function toast(message, kind = "ok") {
  const host = document.getElementById("toasts");
  if (!host) return;
  const node = el("div", { class: `toast toast-${kind}`, text: message });
  host.append(node);
  setTimeout(() => node.remove(), kind === "error" ? 7000 : 3000);
}

let dialogSeq = 0;

/** Modal confirmation. Resolves true only when the user clicks the confirm button. */
export function confirmDialog(message, { okText = "确认", title = "请确认", danger = false } = {}) {
  return new Promise((resolve) => {
    const id = `dlg-${++dialogSeq}`;
    const cancel = button("取消", { onclick: () => dlg.close("cancel") });
    const ok = button(okText, { variant: danger ? "danger" : "primary", onclick: () => dlg.close("ok") });
    const dlg = el(
      "dialog",
      { class: "dlg", "aria-labelledby": `${id}-t`, "aria-describedby": `${id}-m` },
      el("h2", { id: `${id}-t`, class: "dlg-title", text: title }),
      el("p", { id: `${id}-m`, class: "dlg-msg", text: message }),
      el("div", { class: "dlg-actions" }, cancel, ok),
    );
    dlg.addEventListener("close", () => {
      const confirmed = dlg.returnValue === "ok";
      dlg.remove();
      resolve(confirmed);
    });
    document.body.append(dlg);
    dlg.showModal();
    cancel.focus();
  });
}

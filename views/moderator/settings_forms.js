// Form building blocks for the settings tab: option specs, controls, client-side checks.
// The server is authoritative; these checks only save a round trip and mirror docs/settings.md.

import { el } from "./dom.js";

const CONTROL_RE = /[\u0000-\u001f\u007f]/;
const SPACE_RE = /\s/;
let seq = 0;
export const uid = (prefix) => `${prefix}-${++seq}`;

export const MAX_RULES = 8000;
export const MAX_FORUMS = 50;
export const MAX_ACCOUNTS = 20;

export const LINE_RULES = {
  sessions: { label: "推送会话", max: 20, itemMax: 512, noSpace: false },
  operators: { label: "吧务", max: 200, itemMax: 64, noSpace: true },
  whitelist: { label: "白名单", max: 1000, itemMax: 128, noSpace: false },
};

/** Options that exist globally (as defaults) and per forum (as overrides). */
export const OPTION_SPECS = [
  { key: "rules", label: "吧规", kind: "rules", help: "判定依据，留空等于沿用" },
  { key: "provider_id", label: "审核模型", kind: "provider", help: "留空使用 AstrBot 默认对话模型" },
  { key: "auto_delete_enabled", label: "自动删除", kind: "bool", help: "开启后高置信度内容不经确认直接删除" },
  { key: "auto_delete_threshold", label: "自动删除阈值", kind: "float", min: 0, max: 1, step: 0.01 },
  { key: "report_threshold", label: "上报阈值", kind: "float", min: 0, max: 1, step: 0.01, help: "不能高于自动删除阈值" },
  { key: "thread_pages", label: "每吧巡查页数", kind: "int", min: 1, max: 5 },
  { key: "lookback_hours", label: "回溯小时数", kind: "int", min: 1, max: 168 },
  { key: "review_posts", label: "审查回复", kind: "bool" },
  { key: "review_comments", label: "审查楼中楼", kind: "bool" },
  { key: "skip_top_and_good", label: "跳过置顶和精品帖", kind: "bool" },
  { key: "skip_bawu", label: "跳过吧务发言", kind: "bool" },
];

export const GLOBAL_SPECS = [
  { key: "patrol_enabled", label: "自动巡查", kind: "bool", help: "关闭后只能手动巡查" },
  { key: "interval_seconds", label: "巡查间隔（秒）", kind: "int", min: 60, max: 86400 },
  { key: "max_items_per_round", label: "每轮审查上限", kind: "int", min: 1, max: 500, help: "所有吧合计" },
  { key: "judge_concurrency", label: "判定并发数", kind: "int", min: 1, max: 8 },
  { key: "judge_timeout_seconds", label: "判定超时（秒）", kind: "int", min: 10, max: 600 },
  { key: "review_images", label: "审查图片", kind: "bool", help: "关闭后只审查文字" },
  { key: "max_images", label: "每条最多审查图片数", kind: "int", min: 0, max: 9 },
];

/** Human text for "沿用全局（当前：X）". */
export function formatOption(spec, value, providers = []) {
  if (spec.kind === "bool") return value ? "开" : "关";
  if (spec.kind === "float") return Number(value).toFixed(2);
  if (spec.kind === "provider") {
    if (!value) return "AstrBot 默认对话模型";
    return providers.find((p) => p.id === value)?.label || value;
  }
  if (spec.kind === "rules") {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    return text.length > 24 ? `${text.slice(0, 24)}…（共 ${String(value).length} 字）` : text || "（空）";
  }
  return String(value);
}

/** Textarea with one entry per line and a live count. get() returns the trimmed non-empty lines. */
export function linesControl(rule, initial = [], { placeholder = "" } = {}) {
  const id = uid("lines");
  const area = el("textarea", { id, rows: 4, spellcheck: "false", placeholder });
  area.value = initial.join("\n");
  const counter = el("span", { class: "muted", "aria-live": "off" });
  const get = () => area.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const paint = () => {
    const n = get().length;
    counter.textContent = `${n} / ${rule.max} 条，一行一条`;
    counter.classList.toggle("warn-text", n > rule.max);
  };
  area.addEventListener("input", paint);
  paint();
  return { id, el: el("div", { class: "lines" }, area, counter), input: area, get };
}

export function checkLines(rule, lines) {
  if (lines.length > rule.max) return `${rule.label}最多 ${rule.max} 条（当前 ${lines.length} 条）`;
  for (const [i, line] of lines.entries()) {
    if (line.length > rule.itemMax) return `${rule.label}第 ${i + 1} 行超过 ${rule.itemMax} 个字符`;
    if (CONTROL_RE.test(line)) return `${rule.label}第 ${i + 1} 行含有控制字符`;
    if (rule.noSpace && SPACE_RE.test(line)) return `${rule.label}第 ${i + 1} 行不能包含空白`;
  }
  return "";
}

/**
 * Control for one option value.
 *
 * Args:
 *   spec: Entry of OPTION_SPECS / GLOBAL_SPECS.
 *   value: Initial value.
 *   providers: Provider list for kind "provider".
 *
 * Returns:
 *   {id, el, input, get()}; get() gives the raw value (NaN for an empty number box).
 */
export function valueControl(spec, value, providers = []) {
  const id = uid("opt");
  if (spec.kind === "bool") {
    const input = el("input", { type: "checkbox", id });
    input.checked = Boolean(value);
    return { id, input, el: el("label", { class: "check", for: id }, input, el("span", { text: "开启" })), get: () => input.checked };
  }
  if (spec.kind === "int" || spec.kind === "float") {
    const input = el("input", { type: "number", id, min: spec.min, max: spec.max, step: spec.step || 1, class: "num-input" });
    input.value = String(value ?? "");
    return { id, input, el: input, get: () => input.valueAsNumber };
  }
  if (spec.kind === "rules") {
    const input = el("textarea", { id, rows: 7, spellcheck: "false" });
    input.value = value || "";
    const counter = el("span", { class: "muted" });
    const paint = () => {
      counter.textContent = `${input.value.length} / ${MAX_RULES} 字`;
      counter.classList.toggle("warn-text", input.value.length > MAX_RULES);
    };
    input.addEventListener("input", paint);
    paint();
    return { id, input, el: el("div", { class: "lines" }, input, counter), get: () => input.value };
  }
  // provider
  const input = el("select", { id });
  const options = [{ id: "", label: "AstrBot 默认对话模型", supports_image: null }, ...providers];
  if (value && !providers.some((p) => p.id === value)) options.push({ id: value, label: `${value}（当前不存在）`, supports_image: null });
  for (const p of options) {
    input.append(el("option", { value: p.id, text: p.supports_image === false ? `${p.label}（不支持图片）` : p.label }));
  }
  input.value = value || "";
  return { id, input, el: input, get: () => input.value };
}

/** Returns an error sentence for an invalid option value, or "". */
export function checkOption(spec, value) {
  if (spec.kind === "int") {
    return Number.isInteger(value) && value >= spec.min && value <= spec.max ? "" : `${spec.label}必须是 ${spec.min} 到 ${spec.max} 的整数`;
  }
  if (spec.kind === "float") {
    return Number.isFinite(value) && value >= spec.min && value <= spec.max ? "" : `${spec.label}必须是 ${spec.min} 到 ${spec.max} 之间的数`;
  }
  if (spec.kind === "rules") return String(value).length > MAX_RULES ? `吧规最多 ${MAX_RULES} 字符` : "";
  if (spec.kind === "provider") return String(value).length > 128 ? "审核模型 id 过长" : "";
  return "";
}

/** One settings row: label + help on the left, control(s) on the right. */
export function formRow(label, help, forId, ...content) {
  return el(
    "div",
    { class: "form-row" },
    el(
      "div",
      { class: "form-label" },
      forId ? el("label", { for: forId, class: "form-title", text: label }) : el("span", { class: "form-title", text: label }),
      help ? el("span", { class: "muted form-help", text: help }) : null,
    ),
    el("div", { class: "form-control" }, ...content),
  );
}

/** Inline alert at the top of a form; show() keeps the user's input untouched. */
export function formMessage() {
  const box = el("div", { class: "card-error form-error", role: "alert", hidden: true });
  return {
    el: box,
    show(message) {
      box.textContent = message;
      box.hidden = !message;
      if (message) box.scrollIntoView({ block: "nearest" });
    },
  };
}

export const hasImageWarning = (provider, reviewImages) => Boolean(provider) && provider.supports_image === false && Boolean(reviewImages);

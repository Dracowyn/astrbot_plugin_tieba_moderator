// Tab 3: statistics (summary numbers, stacked bars drawn with CSS, hint, patrol reports).

import { apiGet, errorText } from "./api.js";
import { getSettings, loadSettings } from "./settings_store.js";
import { ALL_STATUSES, STATUS_LABEL, button, el, emptyState, errorState, fmtDuration, setChildren, fmtTime, skeleton } from "./dom.js";

const RANGES = [7, 30, 90];
const MIN_SAMPLE = 30;
const REPORT_LIMIT = 20;
const EPS = 1e-9;

const SEGMENTS = [
  { key: "confirmed", label: "已确认违规", cls: "seg-confirmed" },
  { key: "false_positive", label: "误报（忽略 / 恢复）", cls: "seg-fp" },
  { key: "pending", label: "待处理", cls: "seg-pending" },
];

const SHORT = { confirmed: "确认", false_positive: "误报", pending: "待处理" };
const sum = (list, key) => list.reduce((acc, item) => acc + (item[key] || 0), 0);
const pct = (value) => `${Math.round(value * 1000) / 10}%`;

/** False-positive rate of buckets whose lower bound is at or above `threshold` (rounded up to 0.1). */
function hintData(threshold, buckets) {
  const lo = Math.ceil(threshold * 10 - EPS) / 10;
  const selected = buckets.filter((b) => b.low >= lo - EPS);
  const confirmed = sum(selected, "confirmed");
  const falsePositive = sum(selected, "false_positive");
  const n = confirmed + falsePositive;
  const rounded = Math.abs(lo - threshold) > EPS ? `（阈值 ${threshold} 按 0.1 分档向上取整）` : "";
  const text = n === 0
    ? `置信度 ≥ ${lo.toFixed(1)} 的案件暂无已处理样本${rounded}。`
    : `置信度 ≥ ${lo.toFixed(1)} 的案件误报率 ${pct(falsePositive / n)}（${n} 件），样本少于 ${MIN_SAMPLE} 件时仅供参考${rounded}。`;
  return { n, rate: n ? falsePositive / n : null, text };
}

function legend() {
  return el(
    "ul",
    { class: "legend", "aria-label": "图例" },
    SEGMENTS.map((s) => el("li", {}, el("span", { class: `swatch ${s.cls}`, "aria-hidden": "true" }), s.label)),
  );
}

/** One horizontal stacked bar. Width is relative to `max` so rows compare with each other. */
function barRow(label, values, max, extra) {
  const total = SEGMENTS.reduce((acc, s) => acc + (values[s.key] || 0), 0);
  const summary = SEGMENTS.filter((s) => values[s.key] !== undefined).map((s) => `${s.label} ${values[s.key]}`).join("，");
  const stack = el("div", { class: "bar-stack", role: "img", "aria-label": `${label}：共 ${total} 件，${summary}${extra ? `，${extra}` : ""}` });
  stack.style.width = total ? `${Math.max(2, (total / max) * 100)}%` : "0";
  for (const s of SEGMENTS) {
    if (!values[s.key]) continue;
    const seg = el("span", { class: `seg ${s.cls}` });
    seg.style.flexGrow = String(values[s.key]);
    stack.append(seg);
  }
  return el(
    "li",
    { class: "bar-row" },
    el("span", { class: "bar-label", text: label, title: label }),
    el("span", { class: "bar-track" }, stack),
    el(
      "span",
      { class: "bar-nums" },
      el("strong", { text: `${total} 件` }),
      el("span", { class: "muted", text: ` ${SEGMENTS.filter((s) => values[s.key] !== undefined).map((s) => `${SHORT[s.key]} ${values[s.key]}`).join(" / ")}${extra ? ` · ${extra}` : ""}` }),
    ),
  );
}

function categoryChart(categories) {
  if (!categories.length) return emptyState("所选时间范围内没有案件。");
  const rows = categories
    .map((c) => ({
      label: c.category || "未分类",
      confirmed: (c.deleted || 0) + (c.banned || 0) + (c.auto_deleted || 0),
      false_positive: (c.ignored || 0) + (c.restored || 0),
      pending: c.pending || 0,
    }))
    .sort((a, b) => b.confirmed + b.false_positive + b.pending - (a.confirmed + a.false_positive + a.pending));
  const max = Math.max(...rows.map((r) => r.confirmed + r.false_positive + r.pending), 1);
  return el("ul", { class: "bars" }, rows.map((r) => barRow(r.label, r, max)));
}

function bucketChart(buckets) {
  let rows = buckets.map((b) => ({
    label: `${b.low.toFixed(1)}–${b.high.toFixed(1)}`,
    values: { confirmed: b.confirmed, false_positive: b.false_positive },
    total: b.confirmed + b.false_positive,
  }));
  // Leading empty buckets collapse into one row so empty ground does not dominate the card.
  const firstUsed = rows.findIndex((r) => r.total > 0);
  const lead = firstUsed === -1 ? rows.length : firstUsed;
  if (lead >= 2) {
    const merged = { label: `${buckets[0].low.toFixed(1)}–${buckets[lead - 1].high.toFixed(1)}`, values: { confirmed: 0, false_positive: 0 }, total: 0 };
    rows = [merged, ...rows.slice(lead)];
  }
  const max = Math.max(...rows.map((r) => r.total), 1);
  return el(
    "ul",
    { class: "bars" },
    rows.map((r) => barRow(r.label, r.values, max, r.total ? `误报率 ${pct(r.values.false_positive / r.total)}` : "无样本")),
  );
}

function reportItem(report) {
  const counts = [
    ["审查", report.reviewed], ["推送", report.reported], ["自动删除", report.auto_deleted], ["跳过", report.skipped], ["延后", report.deferred],
  ];
  const errors = report.errors || [];
  return el(
    "li",
    { class: "report-item" },
    el(
      "div",
      { class: "report-head" },
      el("strong", { text: fmtTime(report.finished_at) }),
      el("span", { class: "muted", text: `耗时 ${fmtDuration(report.finished_at - report.started_at)}` }),
      el("span", { class: "muted", text: (report.forums || []).map((f) => `${f}吧`).join("、") || "-" }),
    ),
    el("div", { class: "report-counts" }, counts.map(([name, n]) => el("span", { class: "chip chip-count", text: `${name} ${n}` }))),
    errors.length
      ? el("details", { class: "report-errors" }, el("summary", { text: `错误 ${errors.length} 条` }), el("ul", {}, errors.map((e) => el("li", { text: e }))))
      : el("span", { class: "muted", text: "无错误" }),
  );
}

/**
 * Effective thresholds for one forum (or the global defaults when `fname` is empty):
 * from GET settings when loaded, else from the status payload.
 */
function thresholdsFor(fname, status) {
  const settings = getSettings();
  if (settings) {
    const forum = fname ? settings.forums.find((f) => f.fname === fname) : null;
    const o = forum ? forum.effective : settings.global.defaults;
    return { enabled: o.auto_delete_enabled, threshold: o.auto_delete_threshold, report: o.report_threshold };
  }
  if (!status) return null;
  const forum = fname ? (status.forums || []).find((f) => f.fname === fname) : null;
  const auto = (forum ? forum.auto_delete : status.auto_delete) || {};
  return { enabled: auto.enabled, threshold: auto.threshold, report: forum ? forum.report_threshold : status.report_threshold };
}

export function createStatsTab(ctx) {
  let fname = "";
  let days = 30;
  let loading = false;
  let seenVersion = -1;
  let lastLoad = 0;

  const rangeButtons = RANGES.map((d) =>
    button(`近 ${d} 天`, {
      variant: "seg",
      "aria-pressed": String(d === days),
      onclick: () => {
        days = d;
        rangeButtons.forEach((b, i) => b.setAttribute("aria-pressed", String(RANGES[i] === d)));
        load();
      },
    }),
  );
  const forumSelect = el("select", { id: "stats-forum", "aria-label": "按吧筛选" });
  forumSelect.addEventListener("change", () => { fname = forumSelect.value; load(); });
  const refreshBtn = button("刷新", { onclick: () => load() });
  const content = el("div", { class: "stats-body" });
  const root = el(
    "section",
    { class: "tab-body" },
    el("div", { class: "toolbar" }, el("h2", { class: "section-title", text: "统计" }), el("div", { class: "segmented", role: "group", "aria-label": "时间范围" }, rangeButtons), forumSelect, el("span", { class: "grow" }), refreshBtn),
    content,
  );

  /** Rebuild the forum filter from settings (or status) and keep the selection if it still exists. */
  function fillForums() {
    const names = getSettings() ? getSettings().forums.map((f) => f.fname) : (ctx.getStatus()?.forums || []).map((f) => f.fname);
    if (fname && !names.includes(fname)) fname = "";
    forumSelect.replaceChildren(el("option", { value: "", text: "全部吧" }), ...names.map((n) => el("option", { value: n, text: `${n}吧` })));
    forumSelect.value = fname;
  }

  function render(stats, reports) {
    const th = thresholdsFor(fname, ctx.getStatus());
    let lead = null;
    const notes = [];
    if (th) {
      const main = hintData(th.threshold, stats.confidence_buckets);
      const scope = fname ? `${fname}吧：` : "全局默认：";
      lead = el(
        "section",
        { class: "lead", "aria-label": "自动删除参考" },
        el("div", { class: "lead-fig" }, el("span", { class: "lead-value", text: main.rate === null ? "-" : pct(main.rate) }), el("span", { class: "muted", text: "误报率" })),
        el(
          "div",
          { class: "lead-text" },
          el("strong", { text: `${scope}自动删除阈值 ${th.threshold}${th.enabled ? "" : "（当前未开启）"}` }),
          el("p", { text: main.text }),
          ...(Math.abs(th.report - th.threshold) > EPS ? [el("p", { class: "muted", text: `上报阈值 ${th.report}：${hintData(th.report, stats.confidence_buckets).text}` })] : []),
        ),
      );
    }
    const cells = [["案件总数", stats.total], ...ALL_STATUSES.map((s) => [s === "pending" ? "待处理（期内）" : STATUS_LABEL[s], stats.by_status[s] || 0]), ["审查失败", stats.review_failed]];
    setChildren(
      content,
      lead,
      el("div", { class: "kpis", role: "list" }, cells.map(([label, value]) => el("div", { class: "kpi", role: "listitem" }, el("span", { class: "kpi-label", text: label }), el("span", { class: `kpi-value${value === 0 ? " zero" : ""}`, text: String(value) })))),
      ...notes,
      el("div", { class: "grid-2" },
        el("section", { class: "panel-card" }, el("h3", { text: "各类别处置结果" }), legend(), categoryChart(stats.by_category)),
        el("section", { class: "panel-card" }, el("h3", { text: "置信度分档（不含审查失败）" }), legend(), bucketChart(stats.confidence_buckets)),
      ),
      el("section", { class: "panel-card" }, el("h3", { text: "最近巡查报告（所有吧）" }), reports.length ? el("ul", { class: "reports" }, reports.map(reportItem)) : emptyState("还没有巡查报告。")),
    );
  }

  async function load() {
    if (loading) return;
    loading = true;
    refreshBtn.disabled = true;
    if (!content.firstChild) content.replaceChildren(skeleton(3, 5));
    try {
      // Settings feed the forum filter and the thresholds; a failure there must not block the numbers.
      const [stats, reports] = await Promise.all([apiGet("stats", { days, fname }), apiGet("reports", { limit: REPORT_LIMIT }), ctx.refreshStatus(), loadSettings().catch(() => null)]);
      fillForums();
      render(stats, reports);
      lastLoad = Date.now();
      seenVersion = ctx.dataVersion();
    } catch (err) {
      content.replaceChildren(errorState(errorText(err), load, lastLoad));
    } finally {
      loading = false;
      refreshBtn.disabled = false;
    }
  }

  return {
    el: root,
    show: () => (Date.now() - lastLoad > 30000 || seenVersion !== ctx.dataVersion() ? load() : undefined),
    refresh: () => load(),
  };
}

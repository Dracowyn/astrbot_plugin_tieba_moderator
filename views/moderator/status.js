// Top status bar: patrol state, thresholds, pending count and the manual patrol button.

import { apiGet, apiPost, errorText, isConflict } from "./api.js";
import { button, el, fmtConf, fmtInterval, fmtTime, setChildren, toast } from "./dom.js";

const AUTO_DELETE_LABEL = "自动删除";

const FIRST_POLL_MS = 1500;
const POLL_MS = 5000;
const IDLE_POLLS_BEFORE_DONE = 2;

/**
 * Create the status bar.
 *
 * Args:
 *   onOpenPending: Called when the pending-count button is clicked.
 *   onPatrolFinished: Called once a watched manual patrol has ended.
 *
 * Returns:
 *   {el, refresh({changed}), get(), dataVersion()}. dataVersion() increases whenever case data
 *   changed (an action succeeded or a patrol finished) so inactive tabs know to reload.
 */
export function createStatusBar({ onOpenPending, onPatrolFinished, onOpenSettings }) {
  const root = el("section", { class: "kpis statusbar", "aria-label": "巡查状态" });
  let status = null;
  let loadError = "";
  let version = 0;
  let watching = false;
  let sawRunning = false;
  let idlePolls = 0;
  let timer = null;
  let starting = false;

  const cell = (label, ...value) => el("div", { class: "kpi" }, el("span", { class: "kpi-label", text: label }), el("span", { class: "kpi-value sm" }, ...value));

  function findingText(report) {
    if (!report) return "还没有巡查记录。";
    const parts = [`审查 ${report.reviewed} 条`, `推送 ${report.reported} 件`];
    if (report.auto_deleted) parts.push(`自动删除 ${report.auto_deleted} 件`);
    parts.push(report.errors && report.errors.length ? `${report.errors.length} 条错误` : "无错误");
    return `上一轮（${fmtTime(report.finished_at)}）${parts.join("，")}。`;
  }

  /** One chip per forum: name, problem marker (已停用 / 未选择账号) and auto-delete marker. */
  function forumChips(s) {
    const forums = (s.forums || []).map((f) => (typeof f === "string" ? { fname: f, patrolled: true, problem: "" } : f));
    if (!forums.length) {
      return el("span", { class: "forum-empty" }, el("span", { class: "muted", text: "还没有配置吧" }), button("去设置", { variant: "link", onclick: onOpenSettings }));
    }
    return el(
      "ul",
      { class: "forum-chips", "aria-label": "各吧状态" },
      forums.map((f) => {
        const auto = f.auto_delete && f.auto_delete.enabled;
        const parts = [`${f.fname}吧`];
        if (f.problem) parts.push(f.problem);
        const title = [f.account_name ? `账号：${f.account_name}` : "", auto ? `自动删除 ≥ ${fmtConf(f.auto_delete.threshold)}` : "", `上报 ≥ ${fmtConf(f.report_threshold)}`].filter(Boolean).join("；");
        return el(
          "li",
          { class: `forum-chip${f.problem ? " is-problem" : ""}`, title },
          el("span", { class: "forum-name", text: parts.join(" · ") }),
          auto ? el("span", { class: "forum-auto", text: `${AUTO_DELETE_LABEL} ≥ ${fmtConf(f.auto_delete.threshold)}` }) : null,
        );
      }),
    );
  }

  function paint() {
    const finding = document.getElementById("finding");
    const actions = document.getElementById("head-actions");
    if (!status) {
      if (finding) finding.textContent = loadError || "正在读取状态…";
      root.replaceChildren();
      return;
    }
    const s = status;
    const auto = s.auto_delete || {};
    const running = s.manual_running || watching || starting;
    const patrolText = !s.patrol_enabled ? "自动巡查已关闭" : s.loop_running ? "运行中" : "未运行";
    if (finding) finding.textContent = findingText(s.last_report);
    if (actions) {
      setChildren(
        actions,
        s.has_account ? null : el("span", { class: "warn-text", text: "还没有添加吧务账号" }),
        loadError ? el("span", { class: "warn-text", text: loadError }) : null,
        button(running ? "巡查中…" : "立即巡查一轮", {
          variant: "primary",
          disabled: running || !s.has_account,
          "aria-busy": running ? "true" : null,
          title: s.has_account ? null : "还没有添加吧务账号",
          onclick: runPatrol,
        }),
      );
    }
    setChildren(
      root,
      el(
        "div",
        { class: "kpi kpi-lead" },
        el("span", { class: "kpi-label", text: "待处理" }),
        el("button", { type: "button", class: "kpi-value lead-btn", onclick: onOpenPending, "aria-label": `待处理 ${s.pending} 件，打开待处理列表` }, el("span", { class: s.pending === 0 ? "zero" : "", text: String(s.pending) }), el("span", { class: "unit", text: " 件" })),
      ),
      cell("自动巡查", el("span", { class: `dot ${s.loop_running ? "dot-on" : "dot-off"}`, "aria-hidden": "true" }), el("span", { class: "nowrap", text: patrolText }), el("span", { class: "nowrap muted", text: `每 ${fmtInterval(s.interval_seconds)}` })),
      el("div", { class: "kpi kpi-forums" }, el("span", { class: "kpi-label", text: "监控的吧" }), forumChips(s)),
    );
  }

  async function refresh({ changed = false } = {}) {
    if (changed) version += 1;
    try {
      status = await apiGet("status");
      loadError = "";
      if (status.manual_running && !watching) watch(true);
    } catch (err) {
      loadError = `状态读取失败：${errorText(err)}`;
    }
    paint();
    return status;
  }

  function stopWatching() {
    clearTimeout(timer);
    timer = null;
    watching = false;
  }

  async function poll() {
    timer = null;
    try {
      status = await apiGet("status");
      loadError = "";
      if (status.manual_running) {
        sawRunning = true;
      } else if (sawRunning || (idlePolls += 1) >= IDLE_POLLS_BEFORE_DONE) {
        stopWatching();
        version += 1;
        paint();
        toast("手动巡查已结束");
        onPatrolFinished();
        return;
      }
    } catch (err) {
      loadError = `状态读取失败：${errorText(err)}`;
    }
    paint();
    timer = setTimeout(poll, POLL_MS);
  }

  /** Start watching a manual patrol; `alreadyRunning` when the flag was seen set. */
  function watch(alreadyRunning, firstDelay = POLL_MS) {
    if (watching) return;
    watching = true;
    sawRunning = alreadyRunning;
    idlePolls = 0;
    timer = setTimeout(poll, firstDelay);
  }

  async function runPatrol() {
    starting = true;
    paint();
    try {
      await apiPost("patrol/run");
      toast("已开始手动巡查");
      watch(false, FIRST_POLL_MS);
    } catch (err) {
      if (isConflict(err)) {
        toast(errorText(err));
        watch(true, FIRST_POLL_MS);
      } else {
        toast(errorText(err), "error");
      }
    } finally {
      starting = false;
      paint();
    }
  }

  paint();
  return { el: root, refresh, get: () => status, dataVersion: () => version };
}

// Entry point: bridge handshake, theme, status bar and tab switching.

import { errorText, getContext, initBridge, onContext } from "./api.js";
import { confirmDialog, el } from "./dom.js";
import { createStatusBar } from "./status.js";
import { createCheckTab } from "./tab_check.js";
import { createHistoryTab } from "./tab_history.js";
import { createPendingTab } from "./tab_pending.js";
import { createSettingsTab } from "./tab_settings.js";
import { createStatsTab } from "./tab_stats.js";

function applyTheme(context) {
  if (context && typeof context.isDark === "boolean") {
    document.documentElement.setAttribute("data-theme", context.isDark ? "dark" : "light");
  }
}

function mountTabs(statusBar) {
  const tablist = document.getElementById("tablist");
  const panels = document.getElementById("panels");
  const ctx = {
    getStatus: statusBar.get,
    refreshStatus: statusBar.refresh,
    dataVersion: statusBar.dataVersion,
    openTab: (id) => select(defs.find((d) => d.id === id), true),
  };
  const defs = [
    { id: "pending", label: "待处理", tab: createPendingTab(ctx) },
    { id: "history", label: "历史", tab: createHistoryTab(ctx) },
    { id: "stats", label: "统计", tab: createStatsTab(ctx) },
    { id: "check", label: "试判", tab: createCheckTab(ctx) },
    { id: "settings", label: "设置", tab: createSettingsTab(ctx) },
  ];
  let active = defs[0];

  const buttons = defs.map((def) => {
    const btn = el("button", { type: "button", role: "tab", id: `tab-${def.id}`, "aria-controls": `panel-${def.id}`, class: "tab", text: def.label });
    btn.addEventListener("click", () => select(def, false));
    btn.addEventListener("keydown", (event) => {
      const i = defs.indexOf(def);
      const move = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: defs.length - 1 }[event.key];
      if (move === undefined) return;
      event.preventDefault();
      select(defs[(move + defs.length) % defs.length], true);
    });
    tablist.append(btn);
    return btn;
  });
  const panelEls = defs.map((def) => {
    const panel = el("div", { role: "tabpanel", id: `panel-${def.id}`, "aria-labelledby": `tab-${def.id}`, class: "tab-panel", hidden: true }, def.tab.el);
    panels.append(panel);
    return panel;
  });

  async function select(def, focus) {
    // Leaving a tab with an unsaved form asks first.
    if (def !== active && active.tab.isDirty?.()) {
      const leave = await confirmDialog("当前表单有未保存的修改，离开后这些修改会丢失。", { title: "放弃未保存的修改？", okText: "放弃修改", danger: true });
      if (!leave) return;
    }
    active = def;
    defs.forEach((d, i) => {
      const on = d === def;
      buttons[i].setAttribute("aria-selected", String(on));
      buttons[i].tabIndex = on ? 0 : -1;
      panelEls[i].hidden = !on;
    });
    if (focus) buttons[defs.indexOf(def)].focus();
    def.tab.show();
  }

  select(defs[0], false);
  return {
    openPending: () => select(defs[0], true),
    openTab: ctx.openTab,
    refreshActive: () => active.tab.refresh(),
  };
}

async function main() {
  const panels = document.getElementById("panels");
  let context;
  try {
    context = await initBridge();
  } catch (err) {
    panels.replaceChildren(el("div", { class: "state state-error", role: "alert", text: errorText(err) }));
    return;
  }
  applyTheme(context);
  onContext(applyTheme);
  applyTheme(getContext());

  let tabs = null;
  const statusBar = createStatusBar({
    onOpenPending: () => tabs?.openPending(),
    onOpenSettings: () => tabs?.openTab("settings"),
    onPatrolFinished: () => tabs?.refreshActive(),
  });
  document.getElementById("status-slot").replaceWith(statusBar.el);
  await statusBar.refresh();
  tabs = mountTabs(statusBar);
}

main();

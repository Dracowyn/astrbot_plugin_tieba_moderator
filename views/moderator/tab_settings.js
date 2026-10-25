// Tab 5: settings (forums / accounts / global). Owns the unsaved-changes guard for its forms.

import { errorText } from "./api.js";
import { button, confirmDialog, el, errorState, skeleton } from "./dom.js";
import { createAccountSection } from "./settings_account.js";
import { createForumSection } from "./settings_forum.js";
import { createGlobalSection } from "./settings_global.js";
import { getSettings, loadProviders, loadSettings, providerError, setSettings } from "./settings_store.js";

const SECTIONS = [
  { id: "forums", label: "吧" },
  { id: "accounts", label: "账号" },
  { id: "global", label: "全局" },
];

export function createSettingsTab(ctx) {
  let providers = [];
  let activeId = "forums";
  let lastLoad = 0;
  let rendered = false;
  let loading = false;

  const stateBox = el("div");
  const providerNote = el("p", { class: "warn-text", hidden: true });
  const body = el("div", { class: "settings-body" });
  const refreshBtn = button("刷新", { onclick: () => guarded(() => load(true)) });

  const env = {
    getDto: getSettings,
    providers: () => providers,
    guard: confirmLeave,
    saved(dto) {
      // Re-render every section from the returned SettingsDTO; the status bar and stats follow.
      setSettings(dto);
      sections.forEach((s) => s.api.reset());
      ctx.refreshStatus({ changed: true });
    },
  };
  const sections = [
    { id: "forums", api: createForumSection(env) },
    { id: "accounts", api: createAccountSection(env) },
    { id: "global", api: createGlobalSection(env) },
  ];
  const current = () => sections.find((s) => s.id === activeId);

  async function confirmLeave() {
    if (!current().api.isDirty()) return true;
    return confirmDialog("当前表单有未保存的修改，离开后这些修改会丢失。", { title: "放弃未保存的修改？", okText: "放弃修改", danger: true });
  }
  async function guarded(fn) {
    if (await confirmLeave()) fn();
  }

  const nav = el(
    "div",
    { class: "segmented", role: "group", "aria-label": "设置分区" },
    SECTIONS.map((s) =>
      button(s.label, {
        variant: "seg",
        "aria-pressed": String(s.id === activeId),
        onclick: () => guarded(() => select(s.id)),
      })),
  );

  function select(id) {
    activeId = id;
    nav.querySelectorAll("button").forEach((b, i) => b.setAttribute("aria-pressed", String(SECTIONS[i].id === id)));
    body.replaceChildren(current().api.el);
    current().api.reset();
  }

  async function load(force) {
    if (loading) return;
    loading = true;
    refreshBtn.disabled = true;
    if (!getSettings()) stateBox.replaceChildren(skeleton(1, 8));
    try {
      const [, list] = await Promise.all([loadSettings({ force }), loadProviders({ force })]);
      providers = list;
      providerNote.hidden = !providerError();
      providerNote.textContent = providerError() ? `模型列表读取失败：${providerError()}。审核模型下拉里只能选择默认模型。` : "";
      lastLoad = Date.now();
      stateBox.replaceChildren();
      rendered = true;
      select(activeId);
    } catch (err) {
      stateBox.replaceChildren(errorState(errorText(err), () => load(true), lastLoad));
    } finally {
      loading = false;
      refreshBtn.disabled = false;
    }
  }

  const root = el(
    "section",
    { class: "tab-body" },
    el("div", { class: "toolbar" }, el("h2", { class: "section-title", text: "设置" }), nav, el("span", { class: "grow" }), refreshBtn),
    providerNote,
    stateBox,
    body,
  );

  return {
    el: root,
    show: () => (rendered ? undefined : load(false)),
    // A patrol finishing must not wipe a half-edited form.
    refresh: () => (current().api.isDirty() ? undefined : load(true)),
    isDirty: () => current().api.isDirty(),
  };
}

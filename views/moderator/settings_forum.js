// Settings tab, section "吧": forum list and the forum editor (inherit-or-override rows).

import { apiPost, errorText } from "./api.js";
import { button, confirmDialog, el, emptyState, toast } from "./dom.js";
import {
  LINE_RULES, MAX_FORUMS, OPTION_SPECS, checkLines, checkOption, formMessage, formRow, formatOption, hasImageWarning,
  linesControl, uid, valueControl,
} from "./settings_forms.js";

const FNAME_MAX = 64;
const CONTROL_RE = /[\u0000-\u001f\u007f]/;
const overrideCount = (forum) => Object.values(forum.overrides || {}).filter((v) => v !== null && v !== undefined).length;

function forumStatus(forum) {
  if (forum.patrolled) return el("span", { class: "chip chip-restored", text: "巡查中" });
  return el("span", { class: `chip ${forum.problem === "已停用" ? "chip-ignored" : "chip-pending"}`, text: forum.problem || "未巡查" });
}

/**
 * Forum editor.
 *
 * Args:
 *   forum: ForumDTO being edited, or null for a new forum.
 *   env: {dto, providers, onSaved(dto), onCancel()}.
 *
 * Returns:
 *   {el, isDirty()}.
 */
function buildEditor(forum, env) {
  const { dto, providers } = env;
  const defaults = dto.global.defaults;
  const base = forum || { fname: "", enabled: true, account_id: null, sessions: [], operators: [], whitelist: [], overrides: {}, effective: defaults };
  const message = formMessage();

  const nameInput = el("input", { type: "text", id: uid("fname"), maxlength: 80, autocomplete: "off", value: base.fname });
  const suffixHint = el("span", { class: "warn-text", hidden: true, text: "吧名请不要带结尾的「吧」字，否则可能匹配不到。" });
  const syncSuffix = () => { suffixHint.hidden = !nameInput.value.trim().endsWith("吧"); };
  nameInput.addEventListener("input", syncSuffix);
  syncSuffix();

  const enabled = el("input", { type: "checkbox", id: uid("enabled") });
  enabled.checked = base.enabled;
  const account = el("select", { id: uid("account") }, el("option", { value: "", text: "未选择" }), dto.accounts.map((a) => el("option", { value: a.id, text: a.name })));
  account.value = base.account_id === null ? "" : String(base.account_id);

  const sessions = linesControl(LINE_RULES.sessions, base.sessions, { placeholder: "留空使用全局推送会话" });
  const operators = linesControl(LINE_RULES.operators, base.operators);
  const whitelist = linesControl(LINE_RULES.whitelist, base.whitelist, { placeholder: "用户名或 portrait，会与全局白名单合并" });

  // One row per overridable option: "沿用全局" checkbox, and a control that appears when unchecked.
  const rows = OPTION_SPECS.map((spec) => {
    const own = base.overrides?.[spec.key];
    const inherit = own === null || own === undefined;
    const control = valueControl(spec, inherit ? base.effective[spec.key] : own, providers);
    const inheritBox = el("input", { type: "checkbox", id: uid("inh") });
    inheritBox.checked = inherit;
    const wrap = el("div", { class: "override-control", hidden: inherit }, control.el);
    inheritBox.addEventListener("change", () => { wrap.hidden = inheritBox.checked; });
    const globalText = formatOption(spec, defaults[spec.key], providers);
    const warn = spec.key === "provider_id" ? el("p", { class: "warn-text", hidden: true }) : null;
    const rowEl = formRow(
      spec.label,
      spec.help,
      inheritBox.id,
      el("label", { class: "check", for: inheritBox.id }, inheritBox, el("span", { text: `沿用全局（当前：${globalText}）` })),
      wrap,
      warn,
    );
    return { spec, control, inheritBox, warn, el: rowEl };
  });
  const rowOf = (key) => rows.find((r) => r.spec.key === key);

  function refreshWarning() {
    const row = rowOf("provider_id");
    const pid = row.inheritBox.checked ? defaults.provider_id : row.control.get();
    const provider = providers.find((p) => p.id === pid);
    const show = hasImageWarning(provider, dto.global.review_images);
    row.warn.hidden = !show;
    if (show) row.warn.textContent = `${row.inheritBox.checked ? "沿用的全局模型" : "所选模型"}「${provider.label}」不支持图片，而图片审查已开启：含图内容将无法审查。`;
  }

  const collect = () => ({
    fname: nameInput.value,
    enabled: enabled.checked,
    account: account.value,
    sessions: sessions.get(),
    operators: operators.get(),
    whitelist: whitelist.get(),
    overrides: Object.fromEntries(rows.map((r) => [r.spec.key, r.inheritBox.checked ? null : r.control.get()])),
  });
  const initial = JSON.stringify(collect());

  /** Validate and build the ForumDraftDTO; returns {error} or {payload}. */
  function build() {
    const raw = collect();
    const fname = raw.fname.trim();
    if (!fname || fname.length > FNAME_MAX) return { error: `吧名必须是 1 到 ${FNAME_MAX} 个字符` };
    if (CONTROL_RE.test(fname)) return { error: "吧名含有控制字符" };
    for (const key of ["sessions", "operators", "whitelist"]) {
      const error = checkLines(LINE_RULES[key], raw[key]);
      if (error) return { error };
    }
    const overrides = {};
    for (const { spec } of rows) {
      let value = raw.overrides[spec.key];
      if (spec.key === "rules" && typeof value === "string" && !value.trim()) value = null;
      const error = value === null ? "" : checkOption(spec, value);
      if (error) return { error };
      overrides[spec.key] = value;
    }
    const { auto_delete_threshold: auto, report_threshold: report } = overrides;
    if (auto !== null && report !== null && report > auto) return { error: "上报阈值不能高于自动删除阈值" };
    return {
      payload: { fname, enabled: raw.enabled, account_id: raw.account === "" ? null : Number(raw.account), sessions: raw.sessions, operators: raw.operators, whitelist: raw.whitelist, overrides },
    };
  }

  const saveBtn = button("保存", { variant: "primary", type: "submit" });
  const cancelBtn = button("取消", { onclick: () => env.onCancel() });
  const form = el(
    "form",
    { class: "panel-card settings-form", "aria-label": forum ? `编辑吧 ${forum.fname}` : "新增吧" },
    el("h3", { text: forum ? `编辑：${forum.fname}` : "新增吧" }),
    message.el,
    formRow("吧名", "不带「吧」字", nameInput.id, nameInput, suffixHint),
    formRow("启用", "停用后不再巡查，已有案件仍可处理", enabled.id, el("label", { class: "check", for: enabled.id }, enabled, el("span", { text: "启用巡查" }))),
    formRow("吧务账号", dto.accounts.length ? "用该账号读取与处置" : "还没有账号，请先到「账号」添加", account.id, account),
    formRow("推送会话", "本吧新案件推送到这些会话", sessions.id, sessions.el),
    formRow("本吧吧务", "只能处理本吧案件的用户 id", operators.id, operators.el),
    formRow("本吧白名单", "与全局白名单合并", whitelist.id, whitelist.el),
    el("h3", { class: "form-group", text: "审查选项" }),
    rows.map((r) => r.el),
    el("div", { class: "form-actions" }, saveBtn, cancelBtn),
  );
  form.addEventListener("change", refreshWarning);
  refreshWarning();

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const { error, payload } = build();
    message.show(error || "");
    if (error) return;
    saveBtn.disabled = true;
    try {
      env.onSaved(await apiPost(forum ? `forums/${forum.id}` : "forums", payload));
    } catch (err) {
      message.show(errorText(err));
      saveBtn.disabled = false;
    }
  });
  return { el: form, isDirty: () => JSON.stringify(collect()) !== initial };
}

/** Section "吧": returns {el, render(dto), isDirty(), reset()}. */
export function createForumSection(env) {
  const root = el("div", { class: "settings-section" });
  const errorBox = formMessage();
  let editor = null;

  async function open(forum) {
    if (!(await env.guard())) return;
    errorBox.show("");
    editor = buildEditor(forum, {
      dto: env.getDto(),
      providers: env.providers(),
      onSaved: (dto) => { toast("吧设置已保存"); editor = null; env.saved(dto); },
      onCancel: async () => { if (await env.guard()) reset(); },
    });
    root.replaceChildren(editor.el);
    editor.el.querySelector("input")?.focus();
  }

  async function remove(forum) {
    if (!(await confirmDialog(`确认删除吧「${forum.fname}」的设置？已有案件不会被删除，但该吧将不再巡查。`, { okText: "删除", danger: true }))) return;
    try {
      env.saved(await apiPost(`forums/${forum.id}/delete`));
      toast(`已删除吧「${forum.fname}」`);
    } catch (err) {
      errorBox.show(errorText(err));
    }
  }

  function renderList() {
    const dto = env.getDto();
    const accountName = (id) => dto.accounts.find((a) => a.id === id)?.name;
    const addBtn = button("新增吧", { variant: "primary", disabled: dto.forums.length >= MAX_FORUMS, onclick: () => open(null) });
    const head = el("div", { class: "toolbar" }, el("span", { class: "muted", text: `共 ${dto.forums.length} / ${MAX_FORUMS} 个吧` }), el("span", { class: "grow" }), addBtn);
    if (!dto.forums.length) {
      root.replaceChildren(head, errorBox.el, emptyState("还没有配置吧。先在「账号」里添加吧务账号，再在这里添加要巡查的吧。"));
      return;
    }
    const table = el(
      "table",
      { class: "data-table settings-table" },
      el("caption", { class: "sr-only", text: "吧列表" }),
      el("thead", {}, el("tr", {}, ["吧名", "账号", "状态", "独立设置", "操作"].map((h) => el("th", { scope: "col", text: h })))),
      el(
        "tbody",
        {},
        dto.forums.map((forum) => {
          const acc = accountName(forum.account_id);
          const count = overrideCount(forum);
          return el(
            "tr",
            {},
            el("td", { class: "cell-name", text: forum.fname }),
            el("td", { class: acc ? "" : "muted", text: acc || "未选择" }),
            el("td", {}, forumStatus(forum)),
            el("td", { class: count ? "" : "muted", text: count ? `${count} 项` : "全部沿用" }),
            el("td", { class: "row-actions" }, button("编辑", { variant: "link", onclick: () => open(forum), "aria-label": `编辑吧 ${forum.fname}` }), button("删除", { variant: "link", onclick: () => remove(forum), "aria-label": `删除吧 ${forum.fname}` })),
          );
        }),
      ),
    );
    root.replaceChildren(head, errorBox.el, el("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "吧列表，可横向滚动" }, table));
  }

  function reset() {
    editor = null;
    if (env.getDto()) renderList();
  }

  return { el: root, render: reset, reset, isDirty: () => Boolean(editor && editor.isDirty()) };
}

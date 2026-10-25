// Settings tab, section "全局": patrol switches, limits, push sessions, operators and per-forum defaults.

import { apiPost, errorText } from "./api.js";
import { button, el, toast } from "./dom.js";
import {
  GLOBAL_SPECS, LINE_RULES, MAX_RULES, OPTION_SPECS, checkLines, checkOption, formMessage, formRow, hasImageWarning, linesControl,
  valueControl,
} from "./settings_forms.js";

/** Section "全局": returns {el, render(dto), isDirty(), reset()}. */
export function createGlobalSection(env) {
  const root = el("div", { class: "settings-section" });
  let initial = "";
  let collect = () => ({});

  function build(dto) {
    const g = dto.global;
    const providers = env.providers();
    const message = formMessage();
    const globalControls = GLOBAL_SPECS.map((spec) => ({ spec, control: valueControl(spec, g[spec.key]) }));
    const optionControls = OPTION_SPECS.map((spec) => ({ spec, control: valueControl(spec, g.defaults[spec.key], providers) }));
    const sessions = linesControl(LINE_RULES.sessions, g.sessions, { placeholder: "每行一个 unified_msg_origin，例如 aiocqhttp:GroupMessage:123456" });
    const operators = linesControl(LINE_RULES.operators, g.operators);
    const whitelist = linesControl(LINE_RULES.whitelist, g.defaults.whitelist, { placeholder: "用户名或 portrait" });
    const byKey = (list, key) => list.find((c) => c.spec.key === key).control;

    const warn = el("p", { class: "warn-text", hidden: true });
    const refreshWarning = () => {
      const provider = providers.find((p) => p.id === byKey(optionControls, "provider_id").get());
      const show = hasImageWarning(provider, byKey(globalControls, "review_images").get());
      warn.hidden = !show;
      if (show) warn.textContent = `默认模型「${provider.label}」不支持图片，而图片审查已开启：含图内容将无法审查。`;
    };

    const rowFor = ({ spec, control }) => formRow(spec.label, spec.help, control.id, control.el, spec.key === "provider_id" ? warn : null);
    collect = () => ({
      ...Object.fromEntries(globalControls.map(({ spec, control }) => [spec.key, control.get()])),
      sessions: sessions.get(),
      operators: operators.get(),
      defaults: { ...Object.fromEntries(optionControls.map(({ spec, control }) => [spec.key, control.get()])), whitelist: whitelist.get() },
    });
    initial = JSON.stringify(collect());

    const saveBtn = button("保存全局设置", { variant: "primary", type: "submit" });
    const form = el(
      "form",
      { class: "panel-card settings-form", "aria-label": "全局设置" },
      el("h3", { text: "巡查与限额" }),
      message.el,
      globalControls.map(rowFor),
      formRow("默认推送会话", "没有单独设置推送会话的吧使用这些会话", sessions.id, sessions.el),
      formRow("全局操作员", "可处理所有吧案件的用户 id", operators.id, operators.el),
      el("h3", { class: "form-group", text: "各吧默认选项" }),
      optionControls.map(rowFor),
      formRow("全局白名单", "对所有吧生效，吧的白名单会与它合并", whitelist.id, whitelist.el),
      el("div", { class: "form-actions" }, saveBtn),
    );
    form.addEventListener("change", refreshWarning);
    refreshWarning();

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const value = collect();
      let error = "";
      for (const { spec } of globalControls) error = error || checkOption(spec, value[spec.key]);
      for (const { spec } of optionControls) error = error || checkOption(spec, value.defaults[spec.key]);
      error = error || checkLines(LINE_RULES.sessions, value.sessions) || checkLines(LINE_RULES.operators, value.operators) || checkLines(LINE_RULES.whitelist, value.defaults.whitelist);
      if (!error && !value.defaults.rules.trim()) error = "默认吧规不能为空";
      if (!error && value.defaults.rules.length > MAX_RULES) error = `吧规最多 ${MAX_RULES} 字符`;
      if (!error && value.defaults.report_threshold > value.defaults.auto_delete_threshold) error = "上报阈值不能高于自动删除阈值";
      message.show(error);
      if (error) return;
      saveBtn.disabled = true;
      try {
        env.saved(await apiPost("settings/global", value));
        toast("全局设置已保存");
      } catch (err) {
        message.show(errorText(err));
        saveBtn.disabled = false;
      }
    });
    return form;
  }

  function reset() {
    const dto = env.getDto();
    if (dto) root.replaceChildren(build(dto));
  }

  return { el: root, render: reset, reset, isDirty: () => initial !== "" && JSON.stringify(collect()) !== initial };
}

// Tab 4: trial judgement of one thread (review only, nothing is handled).

import { apiPost, errorText, isConflict } from "./api.js";
import { itemBody, verdictMeta } from "./card.js";
import { ACTION_LABEL, authorText, button, el, errorState, kindText, spinner } from "./dom.js";

const MAX_RULES = 8000;
const MAX_TID_DIGITS = 18;

function resultPanel(data) {
  const { item, verdict } = data;
  const failed = verdict.category === "审查失败";
  const partial = data.images_reviewed < data.images_total;
  return el(
    "section",
    { class: "panel-card result", "aria-label": "试判结果" },
    el(
      "div",
      { class: "result-head" },
      el("span", { class: `chip ${verdict.violation ? "chip-deleted" : "chip-restored"}`, text: verdict.violation ? "判定违规" : "未发现违规" }),
      el("span", { class: `chip chip-action-${data.action}`, text: `按当前阈值：${ACTION_LABEL[data.action] || data.action}` }),
      el("span", { class: partial ? "warn-text" : "muted", text: `图片已审查 ${data.images_reviewed} / ${data.images_total} 张` }),
    ),
    verdictMeta(verdict, failed),
    verdict.reason ? el("p", { class: "reason", text: verdict.reason }) : null,
    el("hr", { class: "divider" }),
    el(
      "div",
      { class: "tags" },
      el("span", { class: "chip chip-forum", text: `${item.fname}吧` }),
      el("span", { class: "chip chip-kind", text: kindText(item) }),
      el("span", { class: "muted", text: `作者 ${authorText(item.author)}` }),
    ),
    itemBody(item, null),
  );
}

export function createCheckTab() {
  const tidInput = el("input", { type: "text", id: "ck-tid", inputmode: "numeric", autocomplete: "off", placeholder: "例如 8123456789", required: true, "aria-describedby": "ck-tid-help" });
  const rules = el("textarea", { id: "ck-rules", rows: 6, maxlength: MAX_RULES, placeholder: "留空使用配置中的吧规" });
  const counter = el("span", { class: "muted", "aria-live": "off", text: `0 / ${MAX_RULES}` });
  const submit = button("开始试判", { type: "submit", variant: "primary" });
  const output = el("div", { "aria-live": "polite" });
  const form = el(
    "form",
    { class: "panel-card check-form" },
    el(
      "div",
      { class: "field" },
      el("label", { for: "ck-tid", text: "主题帖 tid" }),
      tidInput,
      el("span", { id: "ck-tid-help", class: "muted", text: "只审查不处置，会按配置下载图片并调用 LLM。" }),
    ),
    el(
      "details",
      { class: "rules-box" },
      el("summary", { text: "临时吧规（留空使用配置中的吧规）" }),
      el("div", { class: "field" }, el("label", { for: "ck-rules", class: "sr-only", text: "临时吧规" }), rules, counter),
    ),
    el("div", {}, submit),
  );
  const root = el("section", { class: "tab-body" }, el("h2", { class: "section-title", text: "试判" }), form, output);

  tidInput.addEventListener("input", () => {
    tidInput.value = tidInput.value.replace(/\D/g, "").slice(0, MAX_TID_DIGITS);
  });
  rules.addEventListener("input", () => {
    counter.textContent = `${rules.value.length} / ${MAX_RULES}`;
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const tid = Number(tidInput.value);
    if (!/^\d+$/.test(tidInput.value) || !Number.isSafeInteger(tid) || tid <= 0) {
      output.replaceChildren(errorState("请输入有效的 tid（正整数）。"));
      tidInput.focus();
      return;
    }
    const body = { tid };
    const customRules = rules.value.trim();
    if (customRules) body.rules = customRules.slice(0, MAX_RULES);
    submit.disabled = true;
    submit.textContent = "审查中…";
    output.replaceChildren(spinner("正在审查，可能需要几十秒…"));
    try {
      output.replaceChildren(resultPanel(await apiPost("check", body)));
    } catch (err) {
      output.replaceChildren(errorState(isConflict(err) ? "已有一次试判在进行" : errorText(err)));
    } finally {
      submit.disabled = false;
      submit.textContent = "开始试判";
    }
  });

  return { el: root, show() {}, refresh() {} };
}

// Settings tab, section "账号": account list, write-only credential editor and the account check.
// Credentials are never rendered, logged or stored: the inputs are read once on submit and always
// start empty; they are cleared after a successful save and the view is re-rendered from the DTO.

import { apiPost, errorText } from "./api.js";
import { button, confirmDialog, el, emptyState, fmtTime, spinner, toast } from "./dom.js";
import { MAX_ACCOUNTS, formMessage, formRow, uid } from "./settings_forms.js";

const NAME_MAX = 32;
const BDUSS_MAX = 512;
const STOKEN_MAX = 256;
const PRINTABLE_RE = /^[\x21-\x7e]+$/;
const CONTROL_RE = /[\u0000-\u001f\u007f]/;

function bawuChip(value) {
  if (value === true) return el("span", { class: "chip chip-restored", text: "是吧务" });
  if (value === false) return el("span", { class: "chip chip-deleted", text: "不是吧务" });
  return el("span", { class: "chip chip-ignored", text: "未知" });
}

/** Check one credential string without echoing it. Returns an error sentence or "". */
function checkSecret(label, value, max, required) {
  if (!value) return required ? `${label}不能为空` : "";
  if (value.length > max || !PRINTABLE_RE.test(value)) return `${label}格式不正确（可打印 ASCII，不含空白，最多 ${max} 字符）`;
  return "";
}

function buildEditor(account, env) {
  const message = formMessage();
  const nameInput = el("input", { type: "text", id: uid("acc-name"), maxlength: 48, autocomplete: "off", value: account ? account.name : "" });
  const secretAttrs = { type: "password", autocomplete: "off", spellcheck: "false", placeholder: account ? "已设置，留空不修改" : "必填" };
  const bduss = el("input", { id: uid("bduss"), ...secretAttrs });
  const stoken = el("input", { id: uid("stoken"), ...secretAttrs });
  const initialName = nameInput.value;
  const saveBtn = button("保存", { variant: "primary", type: "submit" });
  const form = el(
    "form",
    { class: "panel-card settings-form", autocomplete: "off", "aria-label": account ? "编辑账号" : "新增账号" },
    el("h3", { text: account ? `编辑账号：${account.name}` : "新增账号" }),
    message.el,
    formRow("账号名称", `唯一，最多 ${NAME_MAX} 个字符，只用于在设置里区分`, nameInput.id, nameInput),
    formRow("BDUSS", "只写不读：保存后无法再查看", bduss.id, bduss),
    formRow("STOKEN", "同上", stoken.id, stoken),
    el("p", { class: "muted", text: "凭据以明文存放在插件自己的数据库里，请只使用专门的吧务账号。" }),
    el("div", { class: "form-actions" }, saveBtn, button("取消", { onclick: () => env.onCancel() })),
  );

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = nameInput.value.trim();
    const fail = (text) => { message.show(text); };
    if (!name || name.length > NAME_MAX || CONTROL_RE.test(name)) return fail(`账号名称必须是 1 到 ${NAME_MAX} 个字符，不含控制字符`);
    const error = checkSecret("BDUSS", bduss.value, BDUSS_MAX, !account) || checkSecret("STOKEN", stoken.value, STOKEN_MAX, !account);
    if (error) return fail(error);
    message.show("");
    const body = { name };
    if (bduss.value) body.bduss = bduss.value;
    if (stoken.value) body.stoken = stoken.value;
    saveBtn.disabled = true;
    try {
      const dto = await apiPost(account ? `accounts/${account.id}` : "accounts", body);
      bduss.value = "";
      stoken.value = "";
      env.onSaved(dto);
    } catch (err) {
      message.show(errorText(err));
      saveBtn.disabled = false;
    }
  });
  return { el: form, isDirty: () => nameInput.value !== initialName || bduss.value !== "" || stoken.value !== "" };
}

/** Section "账号": returns {el, render(dto), isDirty(), reset()}. */
export function createAccountSection(env) {
  const root = el("div", { class: "settings-section" });
  const errorBox = formMessage();
  const resultBox = el("div", { "aria-live": "polite" });
  let editor = null;

  async function open(account) {
    if (!(await env.guard())) return;
    errorBox.show("");
    editor = buildEditor(account, {
      onSaved: (dto) => { toast("账号已保存"); editor = null; env.saved(dto); },
      onCancel: async () => { if (await env.guard()) reset(); },
    });
    root.replaceChildren(editor.el);
    editor.el.querySelector("input")?.focus();
  }

  async function remove(account) {
    const used = account.forums.length ? `目前被 ${account.forums.length} 个吧使用，需要先改掉这些吧的账号。` : "";
    if (!(await confirmDialog(`确认删除账号「${account.name}」？${used}`, { okText: "删除", danger: true }))) return;
    try {
      env.saved(await apiPost(`accounts/${account.id}/delete`));
      toast(`已删除账号「${account.name}」`);
    } catch (err) {
      errorBox.show(errorText(err));
    }
  }

  async function check(account, btn) {
    btn.disabled = true;
    errorBox.show("");
    resultBox.replaceChildren(spinner(`正在检测账号「${account.name}」，需要逐个吧确认吧务身份，可能要几十秒…`));
    try {
      const r = await apiPost(`accounts/${account.id}/check`);
      resultBox.replaceChildren(
        el(
          "section",
          { class: "panel-card check-result", "aria-label": "账号检测结果" },
          el("div", { class: "result-head" }, el("h3", { text: `账号「${account.name}」` }), el("span", { class: `chip ${r.ok ? "chip-restored" : "chip-deleted"}`, text: r.ok ? "检测通过" : "检测失败" })),
          r.ok ? el("p", { text: `贴吧用户：${r.nick_name || r.user_name || "(无昵称)"}${r.user_name && r.nick_name ? `（${r.user_name}）` : ""}` }) : null,
          r.message ? el("p", { class: r.ok ? "muted" : "", text: r.message }) : null,
          r.forums && r.forums.length
            ? el("ul", { class: "bawu-list" }, r.forums.map((f) => el("li", {}, el("span", { text: `${f.fname}吧` }), bawuChip(f.is_bawu))))
            : el("p", { class: "muted", text: "没有吧使用该账号，未检查吧务身份。" }),
        ),
      );
    } catch (err) {
      resultBox.replaceChildren(el("div", { class: "state state-error", role: "alert" }, el("span", { class: "state-dot", "aria-hidden": "true" }), el("span", { text: errorText(err) })));
    } finally {
      btn.disabled = false;
    }
  }

  function renderList() {
    const dto = env.getDto();
    const addBtn = button("新增账号", { variant: "primary", disabled: dto.accounts.length >= MAX_ACCOUNTS, onclick: () => open(null) });
    const head = el("div", { class: "toolbar" }, el("span", { class: "muted", text: `共 ${dto.accounts.length} / ${MAX_ACCOUNTS} 个账号` }), el("span", { class: "grow" }), addBtn);
    if (!dto.accounts.length) {
      root.replaceChildren(head, errorBox.el, emptyState("还没有账号。添加一个有吧务权限的贴吧账号后，才能巡查和处置。"));
      return;
    }
    const table = el(
      "table",
      { class: "data-table settings-table" },
      el("caption", { class: "sr-only", text: "账号列表" }),
      el("thead", {}, el("tr", {}, ["名称", "使用的吧", "凭据", "更新时间", "操作"].map((h) => el("th", { scope: "col", text: h })))),
      el(
        "tbody",
        {},
        dto.accounts.map((acc) => {
          const checkBtn = button("检测", { variant: "link", "aria-label": `检测账号 ${acc.name}` });
          checkBtn.addEventListener("click", () => check(acc, checkBtn));
          return el(
            "tr",
            {},
            el("td", { class: "cell-name", text: acc.name }),
            el("td", { class: acc.forums.length ? "" : "muted", text: acc.forums.length ? acc.forums.map((f) => `${f}吧`).join("、") : "未被使用" }),
            el("td", {}, el("span", { class: `chip ${acc.has_credentials ? "chip-restored" : "chip-deleted"}`, text: acc.has_credentials ? "已设置" : "缺失" })),
            el("td", { class: "nowrap muted", text: fmtTime(acc.updated_at) }),
            el("td", { class: "row-actions" }, checkBtn, button("编辑", { variant: "link", onclick: () => open(acc), "aria-label": `编辑账号 ${acc.name}` }), button("删除", { variant: "link", onclick: () => remove(acc), "aria-label": `删除账号 ${acc.name}` })),
          );
        }),
      ),
    );
    root.replaceChildren(head, errorBox.el, el("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "账号列表，可横向滚动" }, table), resultBox);
  }

  function reset() {
    editor = null;
    resultBox.replaceChildren();
    if (env.getDto()) renderList();
  }

  return { el: root, render: reset, reset, isDirty: () => Boolean(editor && editor.isDirty()) };
}

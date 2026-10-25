// Tab 2: case history with filters, table, expandable detail and pagination.

import { apiGet, errorText } from "./api.js";
import { createCaseCard } from "./card.js";
import {
  ALL_STATUSES, KIND_LABEL, STATUS_LABEL, authorText, button, el, emptyState, errorState, fmtConf, fmtTime, kindText, skeleton,
  statusChip,
} from "./dom.js";

const PAGE_SIZE = 20;
const MAX_KEYWORD = 50;
const COLUMNS = ["ID", "时间", "吧", "类型", "作者", "类别", "置信度", "状态", "处理人"];

export function createHistoryTab(ctx) {
  // ---- filter form ----
  const statusBoxes = ALL_STATUSES.map((status) => {
    const input = el("input", { type: "checkbox", value: status });
    return { input, label: el("label", { class: "check" }, input, el("span", { text: STATUS_LABEL[status] })) };
  });
  const forumInput = el("input", { type: "text", id: "hf-forum", list: "hf-forums", autocomplete: "off", maxlength: 50 });
  const forumList = el("datalist", { id: "hf-forums" });
  const categoryInput = el("input", { type: "text", id: "hf-category", maxlength: 50 });
  const kindSelect = el(
    "select",
    { id: "hf-kind" },
    el("option", { value: "", text: "全部类型" }),
    Object.entries(KIND_LABEL).map(([value, label]) => el("option", { value, text: label })),
  );
  const keywordInput = el("input", { type: "text", id: "hf-q", maxlength: MAX_KEYWORD, placeholder: "标题、正文、作者" });
  const field = (label, id, control) => el("div", { class: "field" }, el("label", { for: id, text: label }), control);

  const form = el(
    "form",
    { class: "filters", "aria-label": "历史筛选" },
    el("fieldset", { class: "field status-field" }, el("legend", { text: "状态（可多选，不选为全部）" }), el("div", { class: "checks" }, statusBoxes.map((b) => b.label))),
    field("吧", "hf-forum", el("span", {}, forumInput, forumList)),
    field("类别", "hf-category", categoryInput),
    field("类型", "hf-kind", kindSelect),
    field("关键词", "hf-q", keywordInput),
    el(
      "div",
      { class: "field field-actions" },
      button("查询", { type: "submit", variant: "primary" }),
      button("重置", {
        onclick: () => {
          form.reset();
          applyFilters();
        },
      }),
    ),
  );

  // ---- table ----
  const table = el("table", { class: "data-table" }, el("caption", { class: "sr-only", text: "案件历史" }), el("thead", {}, el("tr", {}, COLUMNS.map((c) => el("th", { scope: "col", text: c })))));
  const tableWrap = el("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "案件历史表格，可横向滚动" }, table);
  const stateBox = el("div");
  const summary = el("span", { class: "muted", "aria-live": "polite" });
  const prevBtn = button("上一页", { onclick: () => goto(page - 1) });
  const nextBtn = button("下一页", { onclick: () => goto(page + 1) });
  const refreshBtn = button("刷新", { onclick: () => load() });
  const pager = el("div", { class: "pager" }, summary, el("span", { class: "grow" }), prevBtn, nextBtn);
  const root = el(
    "section",
    { class: "tab-body" },
    el("div", { class: "toolbar" }, el("h2", { class: "section-title", text: "案件历史" }), el("span", { class: "grow" }), refreshBtn),
    form,
    stateBox,
    tableWrap,
    pager,
  );

  let applied = {};
  let page = 0;
  let total = 0;
  let loading = false;
  let loadedOnce = false;
  let seenVersion = -1;
  let lastLoad = 0;

  function currentFilters() {
    const statuses = statusBoxes.filter((b) => b.input.checked).map((b) => b.input.value);
    return {
      // Several statuses are sent comma-separated: the bridge cannot send repeated keys (see report).
      status: statuses.join(","),
      fname: forumInput.value.trim(),
      category: categoryInput.value.trim(),
      kind: kindSelect.value,
      q: keywordInput.value.trim().slice(0, MAX_KEYWORD),
    };
  }

  function applyFilters() {
    applied = currentFilters();
    page = 0;
    load();
  }

  function goto(next) {
    page = Math.max(0, next);
    load();
  }

  // ---- rows ----
  function rowCells(dto) {
    const { item, verdict } = dto;
    return [
      el("td", {}, el("button", { type: "button", class: "row-toggle", "aria-expanded": "false", text: `#${dto.id}` })),
      el("td", { class: "nowrap", text: fmtTime(dto.created_at) }),
      el("td", { text: item.fname }),
      el("td", { class: "nowrap", text: kindText(item) }),
      el("td", { class: "cell-author", text: authorText(item.author) }),
      el("td", {}, dto.review_failed ? el("span", { class: "badge-failed sm", text: "审查失败" }) : el("span", { text: verdict.category || "-" })),
      el("td", { class: dto.review_failed ? "num muted" : "num", text: dto.review_failed ? "-" : fmtConf(verdict.confidence) }),
      el("td", {}, statusChip(dto.status)),
      el("td", { class: "cell-handler", text: dto.handled_by || "-" }),
    ];
  }

  function rowGroup(dto) {
    const row = el("tr", { class: "case-row" }, rowCells(dto));
    const group = el("tbody", { class: "case-group" }, row);
    let detail = null;
    let card = null;
    const refreshRow = (updated) => {
      dto = updated;
      row.replaceChildren(...rowCells(updated));
      row.querySelector(".row-toggle").setAttribute("aria-expanded", String(Boolean(detail)));
      ctx.refreshStatus({ changed: true });
    };
    row.addEventListener("click", () => {
      const toggle = row.querySelector(".row-toggle");
      if (detail) {
        detail.remove();
        detail = null;
        toggle.setAttribute("aria-expanded", "false");
        return;
      }
      card = card || createCaseCard(dto, { onUpdated: refreshRow });
      detail = el("tr", { class: "detail-row" }, el("td", { colspan: COLUMNS.length }, card.el));
      group.append(detail);
      toggle.setAttribute("aria-expanded", "true");
    });
    return group;
  }

  function paintPager() {
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    summary.textContent = `共 ${total} 条，第 ${page + 1} / ${pages} 页`;
    prevBtn.disabled = page <= 0 || loading;
    nextBtn.disabled = page + 1 >= pages || loading;
  }

  async function load() {
    if (loading) return;
    loading = true;
    refreshBtn.disabled = true;
    prevBtn.disabled = nextBtn.disabled = true;
    if (!loadedOnce) stateBox.replaceChildren(skeleton(1, 8, { cards: true }));
    try {
      const data = await apiGet("cases", { ...applied, offset: page * PAGE_SIZE, limit: PAGE_SIZE });
      total = data.total;
      loadedOnce = true;
      seenVersion = ctx.dataVersion();
      lastLoad = Date.now();
      table.querySelectorAll("tbody").forEach((t) => t.remove());
      data.items.forEach((dto) => table.append(rowGroup(dto)));
      tableWrap.hidden = data.items.length === 0;
      stateBox.replaceChildren(data.items.length === 0 ? emptyState("没有符合条件的案件。") : "");
    } catch (err) {
      tableWrap.hidden = true;
      stateBox.replaceChildren(errorState(errorText(err), load, lastLoad));
    } finally {
      loading = false;
      refreshBtn.disabled = false;
      paintPager();
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    applyFilters();
  });

  return {
    el: root,
    show() {
      const forums = ctx.getStatus()?.forums || [];
      forumList.replaceChildren(...forums.map((f) => el("option", { value: f })));
      if (!loadedOnce || seenVersion !== ctx.dataVersion()) load();
    },
    refresh: () => load(),
  };
}

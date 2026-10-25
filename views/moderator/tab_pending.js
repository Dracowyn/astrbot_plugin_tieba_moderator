// Tab 1: pending cases as cards, paged with "load more".

import { apiGet, errorText } from "./api.js";
import { createCaseCard } from "./card.js";
import { button, el, emptyState, errorState, skeleton, toast } from "./dom.js";

const PAGE_SIZE = 20;
const STALE_MS = 30000;

export function createPendingTab(ctx) {
  const count = el("span", { class: "muted" });
  const list = el("div", { class: "card-list" });
  const stateBox = el("div");
  const moreBtn = button("加载更多", { onclick: () => load(false) });
  moreBtn.hidden = true;
  const refreshBtn = button("刷新", { onclick: () => load(true) });
  const root = el(
    "section",
    { class: "tab-body" },
    el("div", { class: "toolbar" }, el("h2", { class: "section-title", text: "待处理案件" }), count, el("span", { class: "grow" }), refreshBtn),
    stateBox,
    list,
    el("div", { class: "more-row" }, moreBtn),
  );

  const cards = new Map(); // id -> card
  let total = 0;
  let loading = false;
  let lastLoad = 0;
  let seenVersion = -1;

  /** First run: nothing is patrolled yet, so point to the settings tab. */
  function guideState() {
    return el(
      "div",
      { class: "state state-empty state-guide" },
      el("div", { class: "guide-text" }, el("strong", { text: "先在设置里添加账号和吧" }), el("span", { class: "muted", text: "巡查需要至少一个吧务账号，和一个已启用并选好账号的吧。" })),
      button("打开设置", { variant: "primary", onclick: () => ctx.openTab("settings") }),
    );
  }

  function paintCount() {
    count.textContent = `共 ${total} 件`;
    moreBtn.hidden = cards.size >= total;
    if (cards.size === 0 && !loading) stateBox.replaceChildren(ctx.getStatus()?.configured === false ? guideState() : emptyState("没有待处理的案件。"));
    else if (stateBox.firstChild?.classList?.contains("state-empty")) stateBox.replaceChildren();
  }

  function addCard(dto) {
    if (cards.has(dto.id)) return;
    const card = createCaseCard(dto, {
      onUpdated: (updated) => {
        if (updated.status === "pending") return;
        cards.delete(updated.id);
        total = Math.max(0, total - 1);
        card.el.remove();
        paintCount();
        ctx.refreshStatus({ changed: true });
      },
    });
    cards.set(dto.id, card);
    list.append(card.el);
  }

  async function load(reset) {
    if (loading) return;
    loading = true;
    refreshBtn.disabled = true;
    moreBtn.disabled = true;
    if (cards.size === 0) stateBox.replaceChildren(skeleton(4, 6));
    try {
      const data = await apiGet("cases", { status: "pending", offset: reset ? 0 : cards.size, limit: PAGE_SIZE });
      if (reset) {
        cards.clear();
        list.replaceChildren();
      }
      total = data.total;
      lastLoad = Date.now();
      seenVersion = ctx.dataVersion();
      stateBox.replaceChildren();
      data.items.forEach(addCard);
    } catch (err) {
      if (cards.size === 0) stateBox.replaceChildren(errorState(errorText(err), () => load(true), lastLoad));
      else toast(errorText(err), "error");
    } finally {
      loading = false;
      refreshBtn.disabled = false;
      moreBtn.disabled = false;
      if (!stateBox.querySelector(".state-error")) paintCount();
    }
  }

  return {
    el: root,
    // Reload on first show, when data changed elsewhere, or when older than 30 s.
    show: () => (Date.now() - lastLoad > STALE_MS || seenVersion !== ctx.dataVersion() ? load(true) : undefined),
    refresh: () => load(true),
  };
}

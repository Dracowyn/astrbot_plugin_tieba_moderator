// Case detail card: content, lazy blurred images and moderator actions.
// Used by the pending tab (as a list item) and the history tab (as row detail).

import { apiGet, apiPost, errorText } from "./api.js";
import {
  BAN_DAYS, authorText, button, confirmDialog, el, fmtConf, fmtTime, kindText, setDataImage, setTiebaHref,
  setChildren, statusChip, toast,
} from "./dom.js";

const MAX_IMAGES = 8;
const LONG_TEXT_CHARS = 240;
const LONG_TEXT_LINES = 6;
const IMAGE_CONCURRENCY = 2;

// ---- image loading: viewport-triggered, at most IMAGE_CONCURRENCY in flight ----

const waiting = [];
let active = 0;

function pump() {
  while (active < IMAGE_CONCURRENCY && waiting.length) {
    const { task, resolve, reject } = waiting.shift();
    active += 1;
    task()
      .then(resolve, reject)
      .finally(() => {
        active -= 1;
        pump();
      });
  }
}

function enqueue(task) {
  return new Promise((resolve, reject) => {
    waiting.push({ task, resolve, reject });
    pump();
  });
}

const loaders = new WeakMap();
const observer = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      observer.unobserve(entry.target);
      loaders.get(entry.target)?.();
    }
  },
  { rootMargin: "240px" },
);

/** One image placeholder. States: idle -> loading -> loaded | error (click to retry). */
function imageSlot(caseId, index) {
  const label = `图片 ${index + 1}`;
  const slot = el("button", { type: "button", class: "img-slot", "data-state": "idle", "aria-label": `${label}，等待加载` });
  const note = el("span", { class: "img-note", text: `${label}\n等待加载` });
  slot.append(note);
  let state = "idle";

  const setState = (next, text) => {
    state = next;
    slot.dataset.state = next;
    note.textContent = text;
  };

  async function load() {
    setState("loading", `${label}\n加载中…`);
    slot.setAttribute("aria-label", `${label}，加载中`);
    try {
      const result = await enqueue(() => apiGet(`cases/${caseId}/images/${index}`));
      const img = el("img", { alt: label, class: "img-pic", decoding: "async" });
      if (!setDataImage(img, result && result.data_url)) throw new Error("返回的图片格式无效");
      note.remove();
      slot.append(img);
      state = "loaded";
      slot.dataset.state = "loaded";
      slot.setAttribute("aria-pressed", "false");
      slot.setAttribute("aria-label", `${label}，已模糊，点击显示`);
    } catch (err) {
      setState("error", `${label} 加载失败\n${errorText(err)}\n点击重试`);
      slot.setAttribute("aria-label", `${label} 加载失败：${errorText(err)}，点击重试`);
    }
  }

  slot.addEventListener("click", () => {
    if (state === "error") {
      load();
    } else if (state === "idle") {
      observer.unobserve(slot);
      load();
    } else if (state === "loaded") {
      const shown = slot.classList.toggle("revealed");
      slot.setAttribute("aria-pressed", String(shown));
      slot.setAttribute("aria-label", `${label}，${shown ? "已显示，点击模糊" : "已模糊，点击显示"}`);
    }
  });

  loaders.set(slot, load);
  observer.observe(slot);
  return slot;
}

function imageSection(caseId, count, hasVideo) {
  const shown = Math.min(count, MAX_IMAGES);
  const grid = el("div", { class: "img-grid" });
  const toggle = button("显示全部图片", {
    "aria-pressed": "false",
    onclick: () => {
      const all = grid.classList.toggle("reveal-all");
      toggle.textContent = all ? "模糊全部图片" : "显示全部图片";
      toggle.setAttribute("aria-pressed", String(all));
      if (!all) grid.querySelectorAll(".img-slot.revealed").forEach((s) => s.click());
    },
  });
  for (let i = 0; i < shown; i += 1) grid.append(imageSlot(caseId, i));
  const caption = `${count} 张图片${count > shown ? `（仅显示前 ${shown} 张）` : ""}${hasVideo ? "，含视频（仅审查封面）" : ""}`;
  return el(
    "section",
    { class: "images", "aria-label": "案件图片" },
    el("div", { class: "images-bar" }, el("span", { class: "muted", text: caption }), toggle),
    grid,
  );
}

// ---- content ----

function collapsibleText(text) {
  const isLong = text.length > LONG_TEXT_CHARS || text.split("\n").length > LONG_TEXT_LINES;
  const body = el("div", { class: `item-text${isLong ? " clamp" : ""}`, text: text || "(无正文)" });
  if (!text) body.classList.add("muted");
  if (!isLong) return body;
  const toggle = button("展开全文", {
    variant: "link",
    "aria-expanded": "false",
    onclick: () => {
      const open = body.classList.toggle("clamp") === false;
      toggle.textContent = open ? "收起" : "展开全文";
      toggle.setAttribute("aria-expanded", String(open));
    },
  });
  return el("div", {}, body, toggle);
}

/**
 * Title, text, images (when a case id is given) and the Tieba link of an item.
 *
 * Args:
 *   item: ItemDTO.
 *   caseId: Case id used to fetch images, or null when the item has no case (trial run).
 */
export function itemBody(item, caseId = null) {
  const link = el("a", { class: "item-link", text: "在贴吧中打开" });
  const hasLink = setTiebaHref(link, item.link);
  return el(
    "div",
    { class: "item-body" },
    item.title ? el("h3", { class: "item-title", text: item.title }) : null,
    collapsibleText(item.text || ""),
    item.image_count > 0
      ? caseId === null
        ? el("p", { class: "muted", text: `含 ${item.image_count} 张图片${item.has_video ? "，含视频" : ""}` })
        : imageSection(caseId, item.image_count, item.has_video)
      : item.has_video ? el("p", { class: "muted", text: "含视频（仅审查封面）" }) : null,
    hasLink ? link : el("span", { class: "muted", text: "链接不可用" }),
  );
}

// ---- verdict summary ----

export function verdictMeta(verdict, reviewFailed) {
  if (reviewFailed) return el("div", { class: "meta-row" }, el("span", { class: "badge-failed", text: "自动审查失败，需人工判断" }));
  const pct = Math.round(Math.max(0, Math.min(1, verdict.confidence || 0)) * 100);
  const fill = el("span", { class: "conf-fill" });
  fill.style.width = `${pct}%`;
  return el(
    "div",
    { class: "meta-row" },
    el("span", { class: "chip chip-category", text: verdict.category || "未分类" }),
    el(
      "span",
      { class: "conf", title: "置信度" },
      el("span", { class: "muted", text: "置信度" }),
      el("span", { class: "conf-bar", role: "img", "aria-label": `置信度 ${fmtConf(verdict.confidence)}` }, fill),
      el("strong", { text: fmtConf(verdict.confidence) }),
    ),
    el("span", { class: "muted", text: verdict.ban_days ? `建议封禁 ${verdict.ban_days} 天` : "无封禁建议" }),
  );
}

// ---- card ----

/**
 * Build a case card.
 *
 * Args:
 *   dto: CaseDTO.
 *   onUpdated: Called with (updatedDto, action) after a successful action.
 *
 * Returns:
 *   {el, update(dto)}; update re-renders status line and actions only, so loaded images stay.
 */
export function createCaseCard(dto, { onUpdated } = {}) {
  let current = dto;
  const statusLine = el("div", { class: "card-status" });
  const actions = el("div", { class: "actions" });
  const errorBox = el("div", { class: "card-error", role: "alert", hidden: true });

  const setBusy = (busy) => actions.querySelectorAll("button,select").forEach((node) => { node.disabled = busy; });
  const showError = (message) => {
    errorBox.textContent = message;
    errorBox.hidden = !message;
  };

  async function run(action, body, successText) {
    setBusy(true);
    showError("");
    try {
      const updated = await apiPost(`cases/${current.id}/${action}`, body);
      toast(`案件 #${current.id}：${successText}`);
      current = updated;
      render();
      onUpdated?.(updated, action);
    } catch (err) {
      showError(errorText(err));
      toast(`案件 #${current.id}：${errorText(err)}`, "error");
      setBusy(false);
    }
  }

  const author = current.item.author;
  const confirmDelete = async () => {
    if (await confirmDialog(`确认删除案件 #${current.id} 对应的内容？该操作会在贴吧执行删除。`, { okText: "删除", danger: true })) {
      run("delete", undefined, "已删除");
    }
  };
  const confirmRestore = async () => {
    if (await confirmDialog(`确认恢复案件 #${current.id} 对应的内容？`, { okText: "恢复" })) run("restore", undefined, "已恢复");
  };

  function banControl() {
    const suggested = BAN_DAYS.includes(current.verdict.ban_days) ? current.verdict.ban_days : BAN_DAYS[0];
    const id = `ban-${current.id}-${Math.random().toString(36).slice(2, 7)}`;
    const select = el("select", { id, class: "select-sm" }, BAN_DAYS.map((d) => el("option", { value: d, selected: d === suggested, text: `${d} 天` })));
    const go = button("封禁", {
      variant: "danger",
      onclick: async () => {
        const days = Number(select.value);
        const deleted = current.status === "pending" ? "删除内容并" : "";
        if (await confirmDialog(`确认${deleted}封禁作者「${authorText(author)}」${days} 天？`, { okText: "封禁", danger: true })) {
          run("ban", { days }, `已封禁 ${days} 天`);
        }
      },
    });
    return el("span", { class: "ban-box" }, el("label", { class: "sr-only", for: id, text: "封禁天数" }), select, go);
  }

  function render() {
    setChildren(
      statusLine,
      statusChip(current.status),
      current.handled_by ? el("span", { class: "muted", text: `${current.handled_by} · ${fmtTime(current.handled_at)}` }) : null,
      current.note ? el("span", { class: "muted note", text: `备注：${current.note}` }) : null,
    );
    const list = [];
    if (current.status === "pending") {
      list.push(button("删除", { variant: "danger", onclick: confirmDelete }), banControl(), button("忽略", { onclick: () => run("ignore", undefined, "已忽略") }));
    } else if (current.status === "auto_deleted") {
      list.push(banControl(), button("恢复", { onclick: confirmRestore }));
    } else if (current.status === "deleted" || current.status === "banned") {
      list.push(button("恢复", { onclick: confirmRestore }));
    }
    actions.replaceChildren(...(list.length ? list : [el("span", { class: "muted", text: "该状态无可用操作" })]));
  }

  const { item, verdict } = current;
  const root = el(
    "article",
    { class: `case-card${current.review_failed ? " is-failed" : ""}`, "aria-label": `案件 ${current.id}` },
    el(
      "header",
      { class: "card-head" },
      el("div", { class: "tags" }, el("span", { class: "chip chip-forum", text: `${item.fname}吧` }), el("span", { class: "chip chip-kind", text: kindText(item) })),
      el("span", { class: "muted", text: `#${current.id} · ${fmtTime(current.created_at)}` }),
    ),
    el(
      "div",
      { class: "author-line" },
      el("span", { text: "作者 " }),
      el("strong", { text: authorText(author) }),
      author.user_name && author.user_name !== author.nick_name ? el("span", { class: "muted", text: ` (${author.user_name})` }) : null,
      el("span", { class: "muted", text: ` · Lv.${author.level}${author.is_bawu ? " · 吧务" : ""}` }),
    ),
    verdictMeta(verdict, current.review_failed),
    verdict.reason ? el("p", { class: "reason", text: verdict.reason }) : null,
    itemBody(item, current.id),
    statusLine,
    errorBox,
    actions,
  );
  render();
  return {
    el: root,
    update(next) {
      current = next;
      showError("");
      render();
    },
  };
}

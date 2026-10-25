// Dev-only stand-in for window.AstrBotPluginView with canned data shaped like docs/webui-api.md.
// Classic script (not a module) so the bridge exists before app.js runs.
// URL options: ?theme=dark  ?latency=ms  ?fail=stats,reports  (endpoints that should reject)
(() => {
  const params = new URLSearchParams(location.search);
  const latency = Number(params.get("latency") || 120);
  const failing = new Set((params.get("fail") || "").split(",").filter(Boolean));
  const NOW = Math.floor(Date.now() / 1000);
  const DAY = 86400;

  function http(message, status) {
    return Object.assign(new Error(message), { status });
  }

  // ---- deterministic filler data ----
  let seed = 7;
  const rand = () => {
    seed = (seed * 1664525 + 1013904223) % 4294967296;
    return seed / 4294967296;
  };
  const pick = (list) => list[Math.floor(rand() * list.length)];

  const makeCase = (id, over) => {
    const base = {
      id, status: "pending", created_at: NOW - id * 5400, handled_by: "", handled_at: 0, note: "", review_failed: false,
      item: {
        kind: "thread", fname: "龙", tid: 8100000000 + id, pid: 150000000000 + id, floor: 1, title: `示例帖子 ${id}`,
        text: `这是第 ${id} 号案件的正文示例。`, link: `https://tieba.baidu.com/p/${8100000000 + id}`, image_count: 0, has_video: false,
        create_time: NOW - id * 5400 - 300,
        author: { nick_name: `用户${id}`, user_name: `user_${id}`, portrait: `tb.1.${id}`, level: 1 + (id % 12), is_bawu: false },
      },
      verdict: { violation: true, category: "广告引流", confidence: 0.8, reason: "示例理由。", ban_days: 0 },
    };
    return {
      ...base, ...over,
      item: { ...base.item, ...(over.item || {}) },
      verdict: { ...base.verdict, ...(over.verdict || {}) },
    };
  };

  const longText = Array.from({ length: 14 }, (_, i) => `第 ${i + 1} 段：这是一段很长的正文，用来检查折叠、换行以及超长内容不会撑破卡片宽度。`).join("\n")
    + "\nhttps://ads.invalid/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

  const cases = [
    makeCase(1, {
      item: { title: "代练刷粉丝，加微信 abc123 低价快速", text: "专业代练，价格美丽。\n详情加 V：abc123（https://ads.invalid/ad）", image_count: 3, create_time: NOW - 1800 },
      verdict: { confidence: 0.93, reason: "标题与正文均含联系方式与外链，符合广告引流规则。", ban_days: 3 },
      created_at: NOW - 1500,
    }),
    makeCase(2, {
      item: {
        title: "正常标题\n<img src=x onerror=alert(1)>",
        text: "<script>alert(2)</script> &lt;b&gt;不应加粗&lt;/b&gt; <b>bold?</b>",
        image_count: 1,
        author: { nick_name: "<b>粗体昵称</b>", user_name: "\"><svg onload=alert(3)>", portrait: "tb.1.x", level: 9, is_bawu: false },
      },
      verdict: { category: "<i>斜体类别</i>", confidence: 0.88, reason: "<img src=x onerror=alert(4)> 理由里也带标签", ban_days: 1 },
      created_at: NOW - 3000,
    }),
    makeCase(3, {
      item: { kind: "post", floor: 12, title: "一个很长的主题帖标题用于检查换行是否正常显示不会溢出容器边界", text: longText },
      verdict: { category: "辱骂引战", confidence: 0.71, reason: "多段内容含人身攻击。".repeat(8), ban_days: 0 },
      created_at: NOW - 4200,
    }),
    makeCase(4, {
      item: { kind: "comment", floor: 5, title: "楼中楼示例", text: "楼中楼回复内容。", image_count: 2, has_video: true },
      verdict: { category: "色情低俗", confidence: 0.62, reason: "疑似低俗图片，需人工确认。", ban_days: 10 },
      created_at: NOW - 5000,
    }),
    makeCase(5, {
      review_failed: true,
      item: { title: "审查失败的帖子", text: "内容略。", image_count: 1 },
      verdict: { violation: true, category: "审查失败", confidence: 0, reason: "LLM 调用超时（60s），未得到判定。", ban_days: 0 },
      created_at: NOW - 6200,
    }),
  ];
  for (let id = 6; id <= 30; id += 1) {
    cases.push(makeCase(id, {
      item: { kind: pick(["thread", "post", "comment"]), floor: 1 + (id % 9), text: `待处理填充案件 ${id}，用于检查分页与加载更多。`, image_count: id % 4 },
      verdict: { category: pick(["广告引流", "辱骂引战", "灌水"]), confidence: Math.round((0.6 + rand() * 0.39) * 100) / 100, ban_days: pick([0, 1, 3]) },
    }));
  }
  const historyStatuses = ["deleted", "banned", "ignored", "restored", "auto_deleted", "deleted", "ignored", "banned"];
  for (let id = 31; id <= 150; id += 1) {
    const status = pick(historyStatuses);
    const category = pick(["广告引流", "广告引流", "辱骂引战", "色情低俗", "灌水", "引战拉踩"]);
    const confidence = Math.round((0.6 + rand() * 0.39) * 100) / 100;
    const failed = id % 37 === 0;
    cases.push(makeCase(id, {
      status,
      created_at: NOW - Math.floor(rand() * 85 * DAY),
      handled_by: status === "auto_deleted" ? "patrol" : `webui:${pick(["admin", "吧务甲"])}`,
      handled_at: NOW - Math.floor(rand() * 80 * DAY),
      review_failed: failed,
      item: { kind: pick(["thread", "post", "comment"]), fname: pick(["龙", "龙", "示例"]), text: `历史案件 ${id}`, image_count: id % 3 },
      verdict: failed
        ? { category: "审查失败", confidence: 0, reason: "解析失败" }
        : { category, confidence, ban_days: pick([0, 1, 3]) },
    }));
  }
  cases.forEach((c) => {
    if (c.status === "pending" && c.id > 30) c.status = "ignored";
  });
  cases.sort((a, b) => b.id - a.id);

  // ---- images: small generated data URLs ----
  function imageUrl(caseId, index) {
    const canvas = document.createElement("canvas");
    canvas.width = 160;
    canvas.height = 160;
    const g = canvas.getContext("2d");
    const hue = (caseId * 47 + index * 90) % 360;
    const grad = g.createLinearGradient(0, 0, 160, 160);
    grad.addColorStop(0, `hsl(${hue} 70% 60%)`);
    grad.addColorStop(1, `hsl(${(hue + 60) % 360} 70% 40%)`);
    g.fillStyle = grad;
    g.fillRect(0, 0, 160, 160);
    g.fillStyle = "rgba(255,255,255,.85)";
    g.font = "bold 28px sans-serif";
    g.textAlign = "center";
    g.fillText(`#${caseId}-${index + 1}`, 80, 88);
    return canvas.toDataURL("image/png");
  }

  // ---- state ----
  const state = { manualRunning: false, reports: [] };
  state.reports = [
    { started_at: NOW - 1800, finished_at: NOW - 1742, forums: ["龙"], reviewed: 12, reported: 2, auto_deleted: 0, skipped: 5, deferred: 0, errors: [] },
    { started_at: NOW - 3600, finished_at: NOW - 3512, forums: ["龙", "示例"], reviewed: 20, reported: 3, auto_deleted: 1, skipped: 9, deferred: 4, errors: ["龙吧：获取回复失败（HTTP 502）", "示例吧：图片下载超时 https://imgsa.baidu.com/forum/pic/item/xxxx"] },
    { started_at: NOW - 5400, finished_at: NOW - 5380, forums: ["龙"], reviewed: 0, reported: 0, auto_deleted: 0, skipped: 3, deferred: 0, errors: [] },
  ];

  const pendingCount = () => cases.filter((c) => c.status === "pending").length;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const clone = (value) => JSON.parse(JSON.stringify(value));

  function statusDto() {
    return {
      has_account: true, patrol_enabled: true, loop_running: true, manual_running: state.manualRunning, interval_seconds: 1800,
      forums: ["龙", "示例"], auto_delete: { enabled: false, threshold: 0.9 }, report_threshold: 0.6, pending: pendingCount(),
      last_report: state.reports[0] || null,
    };
  }

  function listCases(p) {
    const raw = p.status;
    const statuses = (Array.isArray(raw) ? raw : raw ? String(raw).split(",") : []).filter(Boolean);
    const q = String(p.q || "").toLowerCase();
    let rows = cases.filter((c) => (!statuses.length || statuses.includes(c.status))
      && (!p.fname || c.item.fname === p.fname)
      && (!p.category || c.verdict.category === p.category)
      && (!p.kind || c.item.kind === p.kind)
      && (!q || [c.item.title, c.item.text, c.item.author.nick_name, c.item.author.user_name].some((s) => s.toLowerCase().includes(q))));
    const total = rows.length;
    const offset = Number(p.offset || 0);
    rows = rows.slice(offset, offset + Number(p.limit || 20));
    return { total, items: rows };
  }

  function stats(days) {
    const since = NOW - days * DAY;
    const rows = cases.filter((c) => c.created_at >= since);
    const by_status = { pending: 0, auto_deleted: 0, deleted: 0, banned: 0, ignored: 0, restored: 0 };
    const byCat = new Map();
    const buckets = Array.from({ length: 10 }, (_, i) => ({ low: i / 10, high: (i + 1) / 10, total: 0, confirmed: 0, false_positive: 0 }));
    let review_failed = 0;
    for (const c of rows) {
      by_status[c.status] += 1;
      if (c.review_failed) {
        review_failed += 1;
        continue;
      }
      const cat = byCat.get(c.verdict.category) || { category: c.verdict.category, total: 0, pending: 0, auto_deleted: 0, deleted: 0, banned: 0, ignored: 0, restored: 0 };
      cat.total += 1;
      cat[c.status] += 1;
      byCat.set(c.verdict.category, cat);
      const bucket = buckets[Math.min(9, Math.floor(c.verdict.confidence * 10))];
      if (["deleted", "banned", "auto_deleted"].includes(c.status)) {
        bucket.confirmed += 1;
        bucket.total += 1;
      } else if (["ignored", "restored"].includes(c.status)) {
        bucket.false_positive += 1;
        bucket.total += 1;
      }
    }
    return { since, total: rows.length, by_status, by_category: [...byCat.values()], confidence_buckets: buckets, review_failed };
  }

  function act(id, action, body) {
    const c = cases.find((x) => x.id === id);
    if (!c) throw http("案件不存在", 404);
    const allowed = {
      delete: ["pending"], ignore: ["pending"], ban: ["pending", "auto_deleted"], restore: ["deleted", "banned", "auto_deleted"],
    }[action];
    if (!allowed || !allowed.includes(c.status)) throw http(`当前状态（${c.status}）不允许该操作`, 400);
    if (id === 3 && action === "ban") throw http("贴吧拒绝了封禁请求：权限不足（stub 模拟的失败）", 502);
    if (action === "ban" && ![1, 3, 10].includes(body && body.days)) throw http("days 必须是 1、3 或 10", 400);
    c.status = { delete: "deleted", ignore: "ignored", ban: "banned", restore: "restored" }[action];
    c.handled_by = "webui:stub";
    c.handled_at = Math.floor(Date.now() / 1000);
    return c;
  }

  async function route(method, path, payload) {
    if (failing.has(path.split("/")[0])) throw http("模拟的接口错误：服务器内部错误，详见 AstrBot 日志", 500);
    const parts = path.split("/");
    if (method === "GET") {
      if (path === "status") return statusDto();
      if (path === "cases") return listCases(payload);
      if (path === "stats") return stats(Number(payload.days || 30));
      if (path === "reports") return state.reports.slice(0, Number(payload.limit || 20));
      if (parts[0] === "cases" && parts[2] === "images") {
        const c = cases.find((x) => x.id === Number(parts[1]));
        const index = Number(parts[3]);
        if (!c || index >= c.item.image_count) throw http("图片序号不存在", 404);
        if (c.id === 1 && index === 1) throw http("图片下载失败：图床不在白名单（stub 模拟）", 502);
        await sleep(200 + index * 250);
        return { data_url: imageUrl(c.id, index) };
      }
      if (parts[0] === "cases" && parts.length === 2) return cases.find((x) => x.id === Number(parts[1])) || Promise.reject(http("案件不存在", 404));
    } else {
      if (path === "patrol/run") {
        if (state.manualRunning) throw http("已有一轮手动巡查在进行", 409);
        state.manualRunning = true;
        const started = Math.floor(Date.now() / 1000);
        setTimeout(() => {
          state.manualRunning = false;
          state.reports.unshift({ started_at: started, finished_at: started + 7, forums: ["龙", "示例"], reviewed: 6, reported: 1, auto_deleted: 0, skipped: 2, deferred: 0, errors: [] });
        }, 7000);
        return { started: true };
      }
      if (path === "check") {
        const tid = payload && payload.tid;
        if (!Number.isInteger(tid) || tid <= 0) throw http("tid 必须是正整数", 400);
        if (String(tid).endsWith("409")) throw http("已有一次试判在进行", 409);
        if (String(tid).endsWith("0")) throw http("审查失败：LLM 调用超时（stub 模拟）", 502);
        await sleep(1200);
        const item = clone(makeCase(0, { item: { tid, title: `试判帖子 ${tid}`, text: "试判取回的正文。\n第二行。", image_count: 3, link: `https://tieba.baidu.com/p/${tid}` } }).item);
        return {
          item,
          verdict: { violation: true, category: "广告引流", confidence: 0.82, reason: payload.rules ? "按临时吧规判定：含联系方式。" : "按配置吧规判定：含联系方式。", ban_days: 1 },
          action: "report", images_reviewed: 2, images_total: 3,
        };
      }
      if (parts[0] === "cases" && parts.length === 3) return act(Number(parts[1]), parts[2], payload);
    }
    throw http(`未知接口：${method} ${path}`, 404);
  }

  const handlers = new Set();
  const context = { pluginName: "astrbot_plugin_tieba_moderator", displayName: "贴吧吧务", pageName: "moderator", pageTitle: "吧务巡查", locale: "zh-CN", i18n: {}, isDark: params.get("theme") === "dark" };

  function applyTheme() {
    document.documentElement.setAttribute("data-theme", context.isDark ? "dark" : "light");
  }
  applyTheme();

  window.__stubSetTheme = (isDark) => {
    context.isDark = Boolean(isDark);
    applyTheme();
    handlers.forEach((h) => h({ ...context }));
  };

  window.AstrBotPluginView = {
    async ready() { await sleep(30); return { ...context }; },
    getContext: () => ({ ...context }),
    getLocale: () => "zh-CN",
    getI18n: () => ({}),
    t: (key, fallback) => fallback,
    onContext(handler) { handlers.add(handler); return () => handlers.delete(handler); },
    async apiGet(path, query) { await sleep(latency); return clone(await route("GET", path, query || {})); },
    async apiPost(path, body) { await sleep(latency); return clone(await route("POST", path, body || {})); },
  };
})();

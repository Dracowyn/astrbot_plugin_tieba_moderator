// Dev-only: canned v0.3 settings API for the stub bridge (docs/settings.md section 4).
// Classic script; defines window.__stubHttp and window.__stubSettings. Secrets stay write-only:
// responses carry has_credentials only, and assertNoSecrets() throws if a response ever contains
// a credential string that was seeded here or sent in a POST body.
(() => {
  const NOW = Math.floor(Date.now() / 1000);
  const DAY = 86400;
  const http = (message, status) => Object.assign(new Error(message), { status });
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  const secrets = new Set(["SEED-BDUSS-do-not-leak-0001", "SEED-STOKEN-do-not-leak-0001", "SEED-BDUSS-do-not-leak-0002", "SEED-STOKEN-do-not-leak-0002"]);
  const accounts = [
    { id: 1, name: "主号", bduss: "SEED-BDUSS-do-not-leak-0001", stoken: "SEED-STOKEN-do-not-leak-0001", created_at: NOW - 40 * DAY, updated_at: NOW - 3 * DAY },
    { id: 2, name: "备用号<img src=x onerror=alert(5)>", bduss: "SEED-BDUSS-do-not-leak-0002", stoken: "SEED-STOKEN-do-not-leak-0002", created_at: NOW - 12 * DAY, updated_at: NOW - 12 * DAY },
  ];
  const noOverrides = { rules: null, provider_id: null, auto_delete_enabled: null, auto_delete_threshold: null, report_threshold: null, thread_pages: null, lookback_hours: null, review_posts: null, review_comments: null, skip_top_and_good: null, skip_bawu: null };
  const forums = [
    {
      id: 1, fname: "龙", enabled: true, account_id: 1, sessions: [], operators: ["dragon_mod"], whitelist: ["old_friend"], created_at: NOW - 30 * DAY, updated_at: NOW - 2 * DAY,
      overrides: { ...noOverrides, rules: "本吧专用吧规：\n1. 禁止引流和代练广告。\n2. 禁止人身攻击。\n3. 讨论游戏外的政治内容一律删除。", provider_id: "deepseek-chat", auto_delete_enabled: true, auto_delete_threshold: 0.85, report_threshold: 0.5, thread_pages: 2, review_comments: false },
    },
    { id: 2, fname: "示例", enabled: true, account_id: null, sessions: [], operators: [], whitelist: [], overrides: { ...noOverrides }, created_at: NOW - 5 * DAY, updated_at: NOW - 5 * DAY },
    { id: 3, fname: "测试<i>吧</i>", enabled: false, account_id: 2, sessions: ["aiocqhttp:GroupMessage:555666"], operators: [], whitelist: [], overrides: { ...noOverrides, provider_id: "retired-model" }, created_at: NOW - 9 * DAY, updated_at: NOW - 9 * DAY },
  ];
  const state = {
    revision: 7, nextAccount: 3, nextForum: 4, checking: false,
    global: {
      patrol_enabled: true, interval_seconds: 1800, max_items_per_round: 60, judge_concurrency: 2, judge_timeout_seconds: 60, review_images: true, max_images: 4,
      sessions: ["aiocqhttp:GroupMessage:100200300"], operators: ["mod_a", "mod_b"],
      defaults: {
        rules: "1. 禁止发布广告、引流、代练、刷粉信息。\n2. 禁止人身攻击和地域黑。\n3. 禁止发布色情低俗内容。", provider_id: "gpt-4o-mini", auto_delete_enabled: false, auto_delete_threshold: 0.9,
        report_threshold: 0.6, thread_pages: 1, lookback_hours: 6, review_posts: true, review_comments: true, skip_top_and_good: true, skip_bawu: true, whitelist: ["trusted_user"],
      },
    },
  };
  const providers = [
    { id: "gpt-4o-mini", label: "GPT-4o mini（OpenAI）", supports_image: true },
    { id: "deepseek-chat", label: "DeepSeek Chat", supports_image: false },
    { id: "local-qwen", label: "本地 Qwen（未知）", supports_image: null },
  ];

  // ---- DTOs ----
  const resolve = (forum) => {
    const eff = { ...state.global.defaults };
    for (const [key, value] of Object.entries(forum.overrides)) if (value !== null) eff[key] = value;
    eff.whitelist = [...new Set([...state.global.defaults.whitelist, ...forum.whitelist])];
    eff.report_threshold = Math.min(eff.report_threshold, eff.auto_delete_threshold);
    return eff;
  };
  const problemOf = (f) => (!f.enabled ? "已停用" : f.account_id === null ? "未选择账号" : "");
  const forumDto = (f) => ({ ...f, effective: resolve(f), patrolled: problemOf(f) === "", problem: problemOf(f) });
  const accountDto = (a) => ({ id: a.id, name: a.name, has_credentials: true, forums: forums.filter((f) => f.account_id === a.id).map((f) => f.fname), created_at: a.created_at, updated_at: a.updated_at });
  const settingsDto = () => ({ revision: state.revision, global: state.global, forums: forums.map(forumDto), accounts: accounts.map(accountDto) });

  function statusFields() {
    const d = state.global.defaults;
    return {
      has_account: accounts.length > 0, configured: forums.some((f) => problemOf(f) === ""), patrol_enabled: state.global.patrol_enabled, interval_seconds: state.global.interval_seconds,
      forums: forums.map((f) => {
        const e = resolve(f);
        return { fname: f.fname, patrolled: problemOf(f) === "", problem: problemOf(f), account_name: accounts.find((a) => a.id === f.account_id)?.name || "", auto_delete: { enabled: e.auto_delete_enabled, threshold: e.auto_delete_threshold }, report_threshold: e.report_threshold };
      }),
      auto_delete: { enabled: d.auto_delete_enabled, threshold: d.auto_delete_threshold }, report_threshold: d.report_threshold,
    };
  }

  // ---- strict validation (mirrors docs/settings.md section 1; messages name the field) ----
  const isInt = (v) => typeof v === "number" && Number.isInteger(v);
  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  function exactKeys(obj, keys, what) {
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) throw http(`${what} 必须是对象`, 400);
    for (const k of Object.keys(obj)) if (!keys.includes(k)) throw http(`${what} 含有未知字段：${k}`, 400);
    for (const k of keys) if (!(k in obj)) throw http(`${what} 缺少字段：${k}`, 400);
  }
  const intIn = (o, k, lo, hi) => { if (!isInt(o[k]) || o[k] < lo || o[k] > hi) throw http(`${k} 必须是 ${lo} 到 ${hi} 的整数`, 400); };
  const numIn = (o, k) => { if (!isNum(o[k]) || o[k] < 0 || o[k] > 1) throw http(`${k} 必须是 0 到 1 之间的数`, 400); };
  const boolOf = (o, k) => { if (typeof o[k] !== "boolean") throw http(`${k} 必须是布尔值`, 400); };
  function lines(o, k, max, itemMax, noSpace) {
    const list = o[k];
    if (!Array.isArray(list) || list.some((s) => typeof s !== "string")) throw http(`${k} 必须是字符串数组`, 400);
    if (list.length > max) throw http(`${k} 最多 ${max} 条`, 400);
    for (const s of list) if (!s.length || s.length > itemMax || (noSpace && /\s/.test(s))) throw http(`${k} 的每一条必须是 1 到 ${itemMax} 个字符${noSpace ? "且不含空白" : ""}`, 400);
  }
  function options(o, nullable) {
    const spec = { rules: "s", provider_id: "s", auto_delete_enabled: "b", auto_delete_threshold: "f", report_threshold: "f", thread_pages: [1, 5], lookback_hours: [1, 168], review_posts: "b", review_comments: "b", skip_top_and_good: "b", skip_bawu: "b" };
    for (const [k, kind] of Object.entries(spec)) {
      if (nullable && o[k] === null) continue;
      if (kind === "s") { if (typeof o[k] !== "string" || o[k].length > (k === "rules" ? 8000 : 128)) throw http(`${k} 长度超出限制`, 400); } else if (kind === "b") boolOf(o, k);
      else if (kind === "f") numIn(o, k);
      else intIn(o, k, kind[0], kind[1]);
    }
    if (o.provider_id && !providers.some((p) => p.id === o.provider_id)) throw http("provider_id 指向的模型当前不存在，请重新选择", 400);
    if (o.auto_delete_threshold !== null && o.report_threshold !== null && o.report_threshold > o.auto_delete_threshold) throw http("report_threshold 不能高于 auto_delete_threshold", 400);
    if (!nullable && !o.rules.trim()) throw http("rules 不能为空", 400);
  }
  const OPT_KEYS = Object.keys(noOverrides);

  function forumDraft(body, selfId) {
    exactKeys(body, ["fname", "enabled", "account_id", "sessions", "operators", "whitelist", "overrides"], "吧设置");
    const fname = typeof body.fname === "string" ? body.fname.trim() : "";
    if (!fname || fname.length > 64) throw http("fname 必须是 1 到 64 个字符", 400);
    boolOf(body, "enabled");
    if (body.account_id !== null && !isInt(body.account_id)) throw http("account_id 必须是整数或 null", 400);
    if (body.account_id !== null && !accounts.some((a) => a.id === body.account_id)) throw http("account_id 指向的账号不存在", 400);
    lines(body, "sessions", 20, 512, false); lines(body, "operators", 200, 64, true); lines(body, "whitelist", 1000, 128, false);
    exactKeys(body.overrides, OPT_KEYS, "overrides");
    options(body.overrides, true);
    if (forums.some((f) => f.id !== selfId && f.fname.toLowerCase() === fname.toLowerCase())) throw http(`吧「${fname}」已经存在`, 409);
    return { ...body, fname };
  }

  function accountDraft(body, creating) {
    for (const k of ["bduss", "stoken"]) if (typeof body[k] === "string" && body[k]) secrets.add(body[k]);
    if (creating) exactKeys(body, ["name", "bduss", "stoken"], "账号");
    else {
      for (const k of Object.keys(body)) if (!["name", "bduss", "stoken"].includes(k)) throw http(`账号 含有未知字段：${k}`, 400);
      if (!("name" in body)) throw http("账号 缺少字段：name", 400);
    }
    const name = typeof body.name === "string" ? body.name.trim() : "";
    if (!name || name.length > 32) throw http("name 必须是 1 到 32 个字符", 400);
    for (const [k, max] of [["bduss", 512], ["stoken", 256]]) {
      if (body[k] === undefined || body[k] === "") { if (creating) throw http(`${k} 不能为空`, 400); continue; }
      if (typeof body[k] !== "string" || body[k].length > max || !/^[!-~]+$/.test(body[k])) throw http(`${k} 格式不正确`, 400);
    }
    return { name, bduss: body.bduss || null, stoken: body.stoken || null };
  }

  const byId = (list, id, what) => list.find((x) => x.id === Number(id)) || (() => { throw http(`${what}不存在`, 404); })();
  const touch = () => { state.revision += 1; return settingsDto(); };

  async function route(method, path, payload) {
    const parts = path.split("/");
    if (method === "GET") {
      if (path === "settings") return settingsDto();
      if (path === "providers") return providers;
      return undefined;
    }
    if (path === "settings/global") {
      exactKeys(payload, ["patrol_enabled", "interval_seconds", "max_items_per_round", "judge_concurrency", "judge_timeout_seconds", "review_images", "max_images", "sessions", "operators", "defaults"], "全局设置");
      boolOf(payload, "patrol_enabled"); intIn(payload, "interval_seconds", 60, 86400); intIn(payload, "max_items_per_round", 1, 500); intIn(payload, "judge_concurrency", 1, 8);
      intIn(payload, "judge_timeout_seconds", 10, 600); boolOf(payload, "review_images"); intIn(payload, "max_images", 0, 9);
      lines(payload, "sessions", 20, 512, false); lines(payload, "operators", 200, 64, true);
      exactKeys(payload.defaults, [...OPT_KEYS, "whitelist"], "defaults");
      options(payload.defaults, false); lines(payload.defaults, "whitelist", 1000, 128, false);
      Object.assign(state.global, payload);
      return touch();
    }
    if (path === "forums") {
      if (forums.length >= 50) throw http("最多 50 个吧", 409);
      forums.push({ ...forumDraft(payload, null), id: state.nextForum++, created_at: Math.floor(Date.now() / 1000), updated_at: Math.floor(Date.now() / 1000) });
      return touch();
    }
    if (parts[0] === "forums" && parts.length === 2) {
      const forum = byId(forums, parts[1], "吧");
      Object.assign(forum, forumDraft(payload, forum.id), { updated_at: Math.floor(Date.now() / 1000) });
      return touch();
    }
    if (parts[0] === "forums" && parts[2] === "delete") {
      forums.splice(forums.indexOf(byId(forums, parts[1], "吧")), 1);
      return touch();
    }
    if (path === "accounts") {
      if (accounts.length >= 20) throw http("最多 20 个账号", 409);
      const d = accountDraft(payload, true);
      if (accounts.some((a) => a.name.toLowerCase() === d.name.toLowerCase())) throw http(`账号名称「${d.name}」已经存在`, 409);
      accounts.push({ id: state.nextAccount++, name: d.name, bduss: d.bduss, stoken: d.stoken, created_at: Math.floor(Date.now() / 1000), updated_at: Math.floor(Date.now() / 1000) });
      return touch();
    }
    if (parts[0] === "accounts" && parts.length === 2) {
      const account = byId(accounts, parts[1], "账号");
      const d = accountDraft(payload, false);
      if (accounts.some((a) => a.id !== account.id && a.name.toLowerCase() === d.name.toLowerCase())) throw http(`账号名称「${d.name}」已经存在`, 409);
      Object.assign(account, { name: d.name, bduss: d.bduss || account.bduss, stoken: d.stoken || account.stoken, updated_at: Math.floor(Date.now() / 1000) });
      return touch();
    }
    if (parts[0] === "accounts" && parts[2] === "delete") {
      const account = byId(accounts, parts[1], "账号");
      const used = forums.filter((f) => f.account_id === account.id).length;
      if (used) throw http(`仍有 ${used} 个吧在使用该账号`, 409);
      accounts.splice(accounts.indexOf(account), 1);
      return touch();
    }
    if (parts[0] === "accounts" && parts[2] === "check") {
      const account = byId(accounts, parts[1], "账号");
      if (state.checking) throw http("已有一次账号检测在进行，请稍后。", 409);
      state.checking = true;
      try {
        await sleep(2200);
        if (account.id === 2) return { ok: false, user_name: "", nick_name: "", message: "凭据无效或已过期，请重新获取 BDUSS 和 STOKEN。", forums: [] };
        const mine = forums.filter((f) => f.account_id === account.id).map((f) => ({ fname: f.fname, is_bawu: f.fname === "龙" ? true : f.fname === "示例" ? null : false }));
        return { ok: true, user_name: "tb_moderator", nick_name: "巡查小助手", message: "", forums: mine };
      } finally {
        state.checking = false;
      }
    }
    return undefined;
  }

  /** Throws if a response (or error message) contains any credential string seen so far. */
  function assertNoSecrets(value) {
    const text = value instanceof Error ? value.message : JSON.stringify(value);
    for (const s of secrets) if (text && text.includes(s)) throw new Error("STUB ASSERT FAILED: a credential string appeared in an API response");
  }

  // ?firstrun=1 starts with no accounts and no forums (status.configured is false).
  if (new URLSearchParams(location.search).has("firstrun")) { forums.length = 0; accounts.length = 0; }

  window.__stubHttp = http;
  window.__stubSettings = { route, statusFields, resolve: (name) => { const f = forums.find((x) => x.fname === name); return resolve(f || { overrides: noOverrides, whitelist: [] }); }, assertNoSecrets, noteSecret: (s) => secrets.add(s), forumNames: () => forums.map((f) => f.fname) };
})();

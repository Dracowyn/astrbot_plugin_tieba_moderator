// Thin wrapper around the AstrBot plugin-view bridge.
// Endpoints have no leading slash and no query string; params go in the 2nd argument.

const bridge = window.AstrBotPluginView;

/** Waits for the bridge and returns the initial context. */
export async function initBridge() {
  if (!bridge) throw new Error("未检测到 AstrBot 插件视图 bridge，请在 AstrBot WebUI 中打开本页面。");
  return bridge.ready();
}

export function onContext(handler) {
  return bridge.onContext(handler);
}

export function getContext() {
  return bridge.getContext();
}

/** GET with query params; empty values are dropped. */
export function apiGet(path, params = {}) {
  const clean = {};
  for (const [key, value] of Object.entries(params)) {
    if (value !== "" && value !== null && value !== undefined) clean[key] = value;
  }
  return bridge.apiGet(path, clean);
}

export function apiPost(path, body) {
  return bridge.apiPost(path, body);
}

/** Best-effort user-facing message from a rejected bridge call. */
export function errorText(err) {
  if (err && typeof err.message === "string" && err.message) return err.message;
  return "请求失败";
}

/** True when the rejection looks like HTTP 409 (bridge may not expose the status code). */
export function isConflict(err) {
  return Boolean(err) && (err.status === 409 || /\b409\b/.test(String(err.message || "")));
}

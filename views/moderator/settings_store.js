// Shared cache for GET settings and GET providers, used by the settings tab, the stats tab and the forms.

import { apiGet, errorText } from "./api.js";

let current = null;
let providers = null;
let providersError = "";
let inflight = null;
const listeners = new Set();

export const getSettings = () => current;
export const providerError = () => providersError;

/** Replace the cached SettingsDTO (after a save) and notify subscribers. */
export function setSettings(next) {
  current = next;
  listeners.forEach((callback) => callback(next));
}

export function onSettings(callback) {
  listeners.add(callback);
  return () => listeners.delete(callback);
}

/** Load GET settings once; `force` refetches. Concurrent calls share one request. */
export async function loadSettings({ force = false } = {}) {
  if (current && !force) return current;
  if (!inflight) inflight = apiGet("settings").finally(() => { inflight = null; });
  const dto = await inflight;
  setSettings(dto);
  return dto;
}

/** Chat providers for the model selects. A failure returns [] and is not cached, so the next call retries. */
export async function loadProviders({ force = false } = {}) {
  if (providers && !force) return providers;
  try {
    providers = await apiGet("providers");
    providersError = "";
    return providers;
  } catch (err) {
    providersError = errorText(err);
    return [];
  }
}

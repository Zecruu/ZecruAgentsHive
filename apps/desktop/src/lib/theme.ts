// Light/dark theme. Source of truth = the durable userData authStore (survives
// app updates); a localStorage mirror gives a synchronous, flash-free read at
// startup. Default is dark (the original look).

import { ah } from './agentshive';

export type Theme = 'dark' | 'light';

const LS_KEY = 'ah:theme';
const STORE_KEY = 'theme';

export function applyTheme(t: Theme): void {
  const el = document.documentElement;
  if (t === 'dark') el.classList.add('dark');
  else el.classList.remove('dark');
}

/** Synchronous best-effort read for first paint (no flash). Defaults to dark. */
export function getCachedTheme(): Theme {
  try {
    return localStorage.getItem(LS_KEY) === 'light' ? 'light' : 'dark';
  } catch {
    return 'dark';
  }
}

/** Persist + apply a theme choice (durable store + localStorage mirror). */
export async function setTheme(t: Theme): Promise<void> {
  applyTheme(t);
  try { localStorage.setItem(LS_KEY, t); } catch { /* ignore */ }
  try { await ah().authStore.set(STORE_KEY, t); } catch { /* ignore */ }
}

/** Reconcile from the durable store after first paint — handles the case where
 *  localStorage was wiped (e.g. by an update) but the choice lives in userData. */
export async function reconcileTheme(): Promise<Theme> {
  let durable: string | null = null;
  try { durable = await ah().authStore.get(STORE_KEY); } catch { /* ignore */ }
  const t: Theme = durable === 'light' || durable === 'dark' ? durable : getCachedTheme();
  applyTheme(t);
  try { localStorage.setItem(LS_KEY, t); } catch { /* ignore */ }
  return t;
}

// Cloud Sync (opt-in, privacy-first) — desktop client helpers.
//
// Entitlement comes from the server (/web/me, resolved row-based). The opt-in
// CHOICE is stored per-tenant in the durable userData authStore (the same store
// that survives updates), so a user's decision sticks across restarts/updates.
//
// Sync only runs when ALL of: signed in (Supabase token) + entitled (cloud_sync)
// + opted in. With any of those false, transcripts never leave the machine.

import { ah, type Entitlements, type SyncPushPayload, type SyncPullResult } from './agentshive';
import { getAccessToken } from './supabase';

const OPT_IN_KEY = (sub: string) => `cloudSyncOptIn:${sub}`;
const CURSOR_KEY = (sub: string, project: string) => `cloudSyncCursor:${sub}:${project}`;

/** The signed-in tenant's identity + resolved entitlement, or null if not signed in. */
export async function getEntitlements(): Promise<Entitlements | null> {
  const token = getAccessToken();
  if (!token) return null;
  try {
    return await ah().web.me(token);
  } catch {
    return null;
  }
}

export async function getOptIn(sub: string): Promise<boolean> {
  if (!sub) return false;
  try {
    return (await ah().authStore.get(OPT_IN_KEY(sub))) === '1';
  } catch {
    return false;
  }
}

export async function setOptIn(sub: string, on: boolean): Promise<void> {
  if (!sub) return;
  try {
    await ah().authStore.set(OPT_IN_KEY(sub), on ? '1' : '0');
  } catch {
    /* best-effort */
  }
}

/** Resolve whether Cloud Sync should actually run right now (signed in + entitled
 *  + opted in), returning the entitlements alongside for the caller's convenience. */
export async function resolveCloudSync(): Promise<{ active: boolean; ent: Entitlements | null }> {
  const ent = await getEntitlements();
  if (!ent || !ent.cloud_sync) return { active: false, ent };
  const optedIn = await getOptIn(ent.sub);
  return { active: optedIn, ent };
}

export async function getCursor(sub: string, project: string): Promise<string | null> {
  if (!sub || !project) return null;
  try {
    return await ah().authStore.get(CURSOR_KEY(sub, project));
  } catch {
    return null;
  }
}

export async function setCursor(sub: string, project: string, cursor: string | null): Promise<void> {
  if (!sub || !project || !cursor) return;
  try {
    await ah().authStore.set(CURSOR_KEY(sub, project), cursor);
  } catch {
    /* best-effort */
  }
}

/** Push one agent's transcript (fire-and-forget; gated server-side too). */
export async function pushTranscript(payload: SyncPushPayload): Promise<void> {
  const token = getAccessToken();
  if (!token) return;
  try {
    await ah().web.syncPush(token, payload);
  } catch {
    /* network blip — retried on the next settle */
  }
}

/** Pull the tenant's transcripts for a project since a cursor, or null. */
export async function pullTranscripts(project: string, since: string | null): Promise<SyncPullResult | null> {
  const token = getAccessToken();
  if (!token) return null;
  try {
    return await ah().web.syncPull(token, project, since);
  } catch {
    return null;
  }
}

import { createClient, type Session } from '@supabase/supabase-js';

const env = (import.meta as any).env || {};
const SUPABASE_URL: string = env.VITE_SUPABASE_URL || '';
const SUPABASE_ANON: string = env.VITE_SUPABASE_ANON_KEY || '';
// `??` (not `||`) so an explicit empty VITE_AGENTSHIVE_URL='' means SAME-ORIGIN
// (relative /web/* fetches — used when the webapp is served from the agentshive
// server itself). An UNSET var still defaults to localhost:8000 for local dev.
const BASE: string = (env.VITE_AGENTSHIVE_URL ?? 'http://localhost:8000').replace(/\/$/, '');

export const supabaseReady = Boolean(SUPABASE_URL && SUPABASE_ANON);
export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON, {
  auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: false },
});

async function token(): Promise<string | null> {
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

async function webFetch(path: string, init: RequestInit = {}): Promise<any> {
  const t = await token();
  if (!t) throw new Error('not signed in');
  const res = await fetch(BASE + path, {
    ...init,
    headers: { ...(init.headers || {}), Authorization: 'Bearer ' + t, 'Content-Type': 'application/json' },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export interface WebAgent {
  agent_key: string;
  project_slug: string | null;
  label: string | null;
  role: string | null;
  cli: string | null;
  status: string | null;
  online: boolean;
  last_seen: string;
}

export interface WebMessage {
  message_id: string;
  direction: 'web_to_agent' | 'agent_to_web' | string;
  body: string;
  agent_key: string | null;
  parent_id: string | null;
  created_at: string;
}

// v2.x Cloud Sync (opt-in) shapes — the durable synced transcript history.
export interface Entitlements {
  sub: string;
  email: string | null;
  plan: string;
  cloud_sync: boolean;
}
export interface SyncToolCall {
  id?: string;
  name: string;
  input?: unknown;
  result?: unknown;
  isError?: boolean;
  completed?: boolean;
}
export interface SyncTokens {
  input: number;
  output: number;
}
export interface SyncMessage {
  uuid: string;
  idx: number;
  role: 'user' | 'assistant' | 'system';
  text: string;
  tool_calls?: SyncToolCall[] | null;
  tokens?: SyncTokens | null;
  created_at?: string;
  updated_at?: string;
}
export interface SyncConversation {
  agent_id: string;
  project_slug: string;
  label: string | null;
  role: string | null;
  cli: string | null;
  updated_at: string;
  messages: SyncMessage[];
}

export const api = {
  agents: (): Promise<{ agents: WebAgent[] }> => webFetch('/web/agents'),
  conversation: (project: string, agentKey: string): Promise<{ messages: WebMessage[] }> =>
    webFetch(`/web/conversation?project=${encodeURIComponent(project)}&agent_key=${encodeURIComponent(agentKey)}`),
  send: (project: string, agentKey: string, body: string): Promise<WebMessage> =>
    webFetch('/web/message', { method: 'POST', body: JSON.stringify({ project, agent_key: agentKey, body }) }),
  // Purge a consumed agent_to_web relay message (ephemeral relay — durable
  // history lives in the synced transcript store, below).
  relayAck: (messageId: string): Promise<{ ok?: boolean; error?: string }> =>
    webFetch('/web/relay-ack', { method: 'POST', body: JSON.stringify({ message_id: messageId }) }),
  // Cloud Sync: entitlements + the tenant's full synced transcript history.
  me: (): Promise<Entitlements> => webFetch('/web/me'),
  syncHistory: (): Promise<{ conversations?: SyncConversation[]; cursor?: string | null; gated?: boolean }> =>
    webFetch('/web/sync/pull?project='),
};

export async function signIn(email: string, password: string): Promise<{ error: string | null }> {
  const { error } = await supabase.auth.signInWithPassword({ email, password });
  return { error: error ? error.message : null };
}

export async function signUp(email: string, password: string): Promise<{ error: string | null; needsConfirmation: boolean }> {
  const { data, error } = await supabase.auth.signUp({
    email,
    password,
    options: { emailRedirectTo: window.location.origin },
  });
  return {
    error: error ? error.message : null,
    needsConfirmation: Boolean(!error && data.user && !data.session),
  };
}

export async function signOut(): Promise<void> {
  await supabase.auth.signOut();
}

export type { Session };

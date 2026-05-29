// Supabase auth for the desktop renderer (v2.x).
//
// Supabase becomes the PRIMARY identity: a signed-in user's access token is the
// tenant bearer the spawned agents present to the AgentsHive MCP server. The
// legacy shared API key still works as a transitional fallback until the
// supervised cutover, so the app stays usable for the operator tonight.
//
// We cache the current access token in a module variable (kept fresh via
// onAuthStateChange + supabase-js auto-refresh) so the synchronous chat-spawn
// path can read it without awaiting. Each agent turn re-spawns, so it always
// picks up the latest (refreshed) token.

import { createClient, type Session, type SupabaseClient } from '@supabase/supabase-js';

const env = (import.meta as any).env || {};
const URL: string | undefined = env.VITE_SUPABASE_URL;
const ANON: string | undefined = env.VITE_SUPABASE_ANON_KEY;

export const supabaseConfigured = Boolean(URL && ANON);

export const supabase: SupabaseClient | null = supabaseConfigured
  ? createClient(URL as string, ANON as string, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: false },
    })
  : null;

let _accessToken: string | null = null;
let _hasSession = false;

if (supabase) {
  supabase.auth.getSession().then(({ data }) => {
    _accessToken = data.session?.access_token ?? null;
    _hasSession = Boolean(data.session);
  });
  supabase.auth.onAuthStateChange((_event, session) => {
    _accessToken = session?.access_token ?? null;
    _hasSession = Boolean(session);
  });
}

/** The current Supabase access token (tenant bearer), or null if not signed in. */
export function getAccessToken(): string | null {
  return _accessToken;
}

export function hasSupabaseSession(): boolean {
  return _hasSession;
}

export async function signInWithPassword(email: string, password: string): Promise<{ error: string | null }> {
  if (!supabase) return { error: 'Supabase is not configured (missing VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY).' };
  const { error } = await supabase.auth.signInWithPassword({ email, password });
  return { error: error ? error.message : null };
}

export async function signUpWithPassword(email: string, password: string): Promise<{ error: string | null }> {
  if (!supabase) return { error: 'Supabase is not configured.' };
  const { error } = await supabase.auth.signUp({
    email,
    password,
    options: { emailRedirectTo: window.location.origin },
  });
  return { error: error ? error.message : null };
}

export async function signOut(): Promise<void> {
  if (supabase) await supabase.auth.signOut();
  _accessToken = null;
  _hasSession = false;
}

export function onAuthChange(cb: (session: Session | null) => void): () => void {
  if (!supabase) {
    cb(null);
    return () => {};
  }
  supabase.auth.getSession().then(({ data }) => cb(data.session));
  const { data } = supabase.auth.onAuthStateChange((_e, session) => cb(session));
  return () => data.subscription.unsubscribe();
}

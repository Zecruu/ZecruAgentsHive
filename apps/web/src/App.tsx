import { useCallback, useEffect, useRef, useState } from 'react';
import {
  api,
  signIn,
  signOut,
  signUp,
  supabase,
  supabaseReady,
  type Entitlements,
  type Session,
  type SyncConversation,
  type SyncMessage,
  type WebAgent,
  type WebMessage,
} from './lib/api';

// Merge relay messages by id (keep client-side). Relay rows are EPHEMERAL on the
// server now (web_to_agent purged on desktop ack, agent_to_web on web consume), so
// the live view must retain what it has seen rather than replace from each poll.
function mergeById(prev: WebMessage[], next: WebMessage[]): WebMessage[] {
  const map = new Map(prev.map((m) => [m.message_id, m]));
  for (const m of next) map.set(m.message_id, m);
  return [...map.values()].sort((a, b) => a.created_at.localeCompare(b.created_at));
}

export function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => { setSession(data.session); setReady(true); });
    const { data } = supabase.auth.onAuthStateChange((_e, s) => setSession(s));
    return () => data.subscription.unsubscribe();
  }, []);

  if (!ready) return <div className="center muted">Loading…</div>;
  if (!session) return <SignIn />;
  return <Home email={session.user?.email ?? ''} />;
}

function SignIn() {
  const [mode, setMode] = useState<'signin' | 'signup'>('signin');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!email.trim() || !password) { setErr('Email and password required.'); return; }
    setBusy(true); setErr(null); setInfo(null);
    const result = mode === 'signin'
      ? await signIn(email.trim(), password)
      : await signUp(email.trim(), password);
    setBusy(false);
    if (result.error) {
      setErr(result.error);
      return;
    }
    if (mode === 'signup') {
      setInfo('Account created. Check your email if confirmation is required, then sign in.');
      setMode('signin');
    }
  };

  return (
    <div className="center">
      <div className="card signin">
        <h1>AgentsHive</h1>
        <p className="muted">Chat with your desktop agents from anywhere.</p>
        {!supabaseReady && <p className="err">Supabase not configured (set VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY).</p>}
        <input type="email" placeholder="you@example.com" value={email} onChange={(e) => setEmail(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && submit()} />
        <input type="password" placeholder="password" value={password} onChange={(e) => setPassword(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && submit()} />
        {err && <p className="err">{err}</p>}
        {info && <p className="ok">{info}</p>}
        <button onClick={submit} disabled={busy}>{busy ? 'Working...' : mode === 'signin' ? 'Sign in' : 'Sign up'}</button>
        <button
          className="link"
          onClick={() => { setMode(mode === 'signin' ? 'signup' : 'signin'); setErr(null); setInfo(null); }}
        >
          {mode === 'signin' ? 'Need an account? Sign up' : 'Have an account? Sign in'}
        </button>
      </div>
    </div>
  );
}

function Home({ email }: { email: string }) {
  const [mode, setMode] = useState<'live' | 'history'>('live');
  const [agents, setAgents] = useState<WebAgent[]>([]);
  const [selected, setSelected] = useState<WebAgent | null>(null);

  const loadAgents = useCallback(async () => {
    try { const r = await api.agents(); setAgents(r.agents || []); } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    loadAgents();
    const t = setInterval(loadAgents, 8000);
    return () => clearInterval(t);
  }, [loadAgents]);

  // Keep the selected agent's online/label fresh as the roster updates.
  useEffect(() => {
    if (selected) {
      const fresh = agents.find((a) => a.agent_key === selected.agent_key);
      if (fresh && (fresh.online !== selected.online || fresh.label !== selected.label)) setSelected(fresh);
    }
  }, [agents, selected]);

  if (mode === 'history') {
    return <HistoryView email={email} onLive={() => setMode('live')} />;
  }

  return (
    <div className={`layout ${selected ? 'has-chat' : ''}`}>
      <aside className="sidebar">
        <header className="bar">
          <strong>Agents</strong>
          <span className="grow" />
          <button className="link" onClick={() => setMode('history')}>History</button>
          <button className="link" onClick={() => signOut()} title={email}>Sign out</button>
        </header>
        <div className="agent-list">
          {agents.length === 0 && <p className="muted pad">No agents online. Open the desktop app + sign in.</p>}
          {agents.map((a) => (
            <button key={a.agent_key} className={`agent ${selected?.agent_key === a.agent_key ? 'active' : ''}`} onClick={() => setSelected(a)}>
              <span className={`dot ${a.online ? 'on' : 'off'}`} />
              <span className="agent-main">
                <span className="agent-label">{a.label || a.agent_key}</span>
                <span className="agent-sub">{a.role} · {a.cli} · {a.project_slug}</span>
              </span>
            </button>
          ))}
        </div>
      </aside>
      <main className="chat-wrap">
        {selected ? (
          <Chat agent={selected} onBack={() => setSelected(null)} />
        ) : (
          <div className="center muted">Pick an agent to start chatting.</div>
        )}
      </main>
    </div>
  );
}

// Full synced-transcript history (durable). Entitlement-gated: /web/me first, so
// a non-entitled tenant sees the paid add-on state and we never hit the 402 path.
function HistoryView({ email, onLive }: { email: string; onLive: () => void }) {
  const [ent, setEnt] = useState<Entitlements | null | undefined>(undefined);
  const [convos, setConvos] = useState<SyncConversation[]>([]);
  const [selected, setSelected] = useState<SyncConversation | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    (async () => {
      let e: Entitlements | null = null;
      try { e = await api.me(); } catch { e = null; }
      if (!alive) return;
      setEnt(e);
      if (e && e.cloud_sync) {
        try {
          const r = await api.syncHistory();
          if (alive) setConvos((r.conversations || []).slice().sort((a, b) => b.updated_at.localeCompare(a.updated_at)));
        } catch { /* ignore */ }
      }
      if (alive) setLoading(false);
    })();
    return () => { alive = false; };
  }, []);

  return (
    <div className={`layout ${selected ? 'has-chat' : ''}`}>
      <aside className="sidebar">
        <header className="bar">
          <strong>History</strong>
          <span className="grow" />
          <button className="link" onClick={onLive}>Live</button>
          <button className="link" onClick={() => signOut()} title={email}>Sign out</button>
        </header>
        <div className="agent-list">
          {loading && <p className="muted pad">Loading…</p>}
          {!loading && ent && !ent.cloud_sync && (
            <p className="muted pad">Cloud Sync is a paid add-on. Enable it in the desktop app to see your full conversation history here.</p>
          )}
          {!loading && ent && ent.cloud_sync && convos.length === 0 && (
            <p className="muted pad">No synced conversations yet. Turn on Cloud Sync in the desktop app, then run an agent.</p>
          )}
          {convos.map((c) => (
            <button key={`${c.project_slug}:${c.agent_id}`} className={`agent ${selected?.agent_id === c.agent_id && selected?.project_slug === c.project_slug ? 'active' : ''}`} onClick={() => setSelected(c)}>
              <span className="agent-main">
                <span className="agent-label">{c.label || c.agent_id}</span>
                <span className="agent-sub">{c.role} · {c.cli} · {c.project_slug} · {c.messages.length} msgs</span>
              </span>
            </button>
          ))}
        </div>
      </aside>
      <main className="chat-wrap">
        {selected ? (
          <HistoryTranscript convo={selected} onBack={() => setSelected(null)} />
        ) : (
          <div className="center muted">{ent && ent.cloud_sync ? 'Pick a conversation to view its transcript.' : 'Synced conversations appear here.'}</div>
        )}
      </main>
    </div>
  );
}

function HistoryTranscript({ convo, onBack }: { convo: SyncConversation; onBack: () => void }) {
  const ordered = convo.messages.slice().sort((a, b) => a.idx - b.idx);
  return (
    <div className="chat">
      <header className="bar chat-head">
        <button className="link back" onClick={onBack}>‹</button>
        <div className="chat-title">
          <strong>{convo.label || convo.agent_id}</strong>
          <span className="muted small">{convo.role} · {convo.cli} · {convo.project_slug}</span>
        </div>
      </header>
      <div className="messages">
        {ordered.length === 0 && <p className="muted pad">Empty conversation.</p>}
        {ordered.map((m) => (
          <HistoryMessage key={m.uuid} m={m} />
        ))}
      </div>
    </div>
  );
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(n >= 10_000 ? 0 : 1) + 'k';
  return String(n);
}

function HistoryMessage({ m }: { m: SyncMessage }) {
  const tc = m.tool_calls && m.tool_calls.length ? m.tool_calls : null;
  const tok = m.tokens ? (m.tokens.input || 0) + (m.tokens.output || 0) : 0;
  return (
    <div className={`msg ${m.role === 'user' ? 'me' : 'them'}`}>
      <div className="bubble">
        <div className="muted small" style={{ textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: 4 }}>
          {m.role}
          {tok > 0 && <span> · {fmtTokens(tok)} tok</span>}
        </div>
        {m.text && <div style={{ whiteSpace: 'pre-wrap' }}>{m.text}</div>}
        {tc && (
          <div className="muted small" style={{ marginTop: 6 }}>
            {tc.map((t, i) => (
              <div key={t.id || i}>⚙ {(t.name || 'tool').split('__').pop()}{t.isError ? ' (error)' : ''}</div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Chat({ agent, onBack }: { agent: WebAgent; onBack: () => void }) {
  const [messages, setMessages] = useState<WebMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const ackedRef = useRef<Set<string>>(new Set());
  const project = agent.project_slug || '';

  const load = useCallback(async () => {
    try {
      const r = await api.conversation(project, agent.agent_key);
      const fetched = r.messages || [];
      // Merge (don't replace) — relay rows are ephemeral server-side now, so we
      // retain what we've already shown.
      setMessages((prev) => mergeById(prev, fetched));
      // Confirmed-consume ack for agent_to_web → server purges it (once per id).
      for (const m of fetched) {
        if (m.direction === 'agent_to_web' && !ackedRef.current.has(m.message_id)) {
          ackedRef.current.add(m.message_id);
          api.relayAck(m.message_id).catch(() => {});
        }
      }
    } catch { /* ignore */ }
  }, [project, agent.agent_key]);

  useEffect(() => {
    setMessages([]);
    load();
    const t = setInterval(load, 2000); // poll (SSE-with-query-token is a later upgrade)
    return () => clearInterval(t);
  }, [load]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, messages[messages.length - 1]?.body]);

  const send = async () => {
    const t = draft.trim();
    if (!t || sending) return;
    setSending(true);
    try {
      const sent = await api.send(project, agent.agent_key, t);
      setDraft('');
      // Optimistically retain our own send (the desktop purges web_to_agent on
      // ack, so a re-fetch won't include it).
      setMessages((prev) => mergeById(prev, [sent]));
    } catch { /* ignore */ } finally { setSending(false); }
  };

  return (
    <div className="chat">
      <header className="bar chat-head">
        <button className="link back" onClick={onBack}>‹</button>
        <span className={`dot ${agent.online ? 'on' : 'off'}`} />
        <div className="chat-title">
          <strong>{agent.label || agent.agent_key}</strong>
          <span className="muted small">{agent.online ? 'online' : 'desktop offline — messages queue'}</span>
        </div>
      </header>
      <div className="messages" ref={scrollRef}>
        {messages.length === 0 && <p className="muted pad">No messages yet. Say hello.</p>}
        {messages.map((m) => (
          <div key={m.message_id} className={`msg ${m.direction === 'web_to_agent' ? 'me' : 'them'}`}>
            <div className="bubble">{m.body}</div>
          </div>
        ))}
      </div>
      <div className="composer">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
          placeholder={`Message ${agent.label || 'agent'} — Enter to send`}
          rows={2}
        />
        <button onClick={send} disabled={sending || !draft.trim()}>{sending ? '…' : 'Send'}</button>
      </div>
    </div>
  );
}

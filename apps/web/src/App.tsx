import { useCallback, useEffect, useRef, useState } from 'react';
import { api, signIn, signOut, signUp, supabase, supabaseReady, type Session, type WebAgent, type WebMessage } from './lib/api';

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

  return (
    <div className={`layout ${selected ? 'has-chat' : ''}`}>
      <aside className="sidebar">
        <header className="bar">
          <strong>Agents</strong>
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

function Chat({ agent, onBack }: { agent: WebAgent; onBack: () => void }) {
  const [messages, setMessages] = useState<WebMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const project = agent.project_slug || '';

  const load = useCallback(async () => {
    try { const r = await api.conversation(project, agent.agent_key); setMessages(r.messages || []); } catch { /* ignore */ }
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
      await api.send(project, agent.agent_key, t);
      setDraft('');
      await load();
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

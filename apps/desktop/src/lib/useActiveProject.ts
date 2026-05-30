// useActiveProject — the live runtime for the ONE active project.
//
// This is the heart of the desktop app: agent runtime state, IPC streaming
// subscriptions, the cross-machine dashboard poll loop, and the reactive
// auto-wake that keeps the Hivemind/Coder protocol loop moving.
//
// Invariant (mission decision #1): exactly ONE active project runs at a time.
// The hook is keyed to `project.slug`; passing a different project tears down
// the previous project's subscriptions + cancels its in-flight chats before
// spinning up the new one. Passing `null` runs ZERO subscriptions and ZERO
// poll loops — the workspace can be open with no project active.

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ah,
  type AgentData,
  type AgentStatus,
  type AttachmentData,
  type ChatEvent,
  type Cli,
  type OsHint,
  type AgentPresenceLite,
  type Project,
  type Role,
  type ToolCallData,
  type TokenUsage,
} from '@/lib/agentshive';
import { getAccessToken, hasSupabaseSession } from '@/lib/supabase';
import { resolveCloudSync, pushTranscript, pullTranscripts, getCursor, setCursor } from '@/lib/cloudSync';
import type { LauncherValues } from '@/components/LauncherForm';

// Live agent runtime state (DOM + IPC subscriptions + the persisted shape).
export interface AgentRuntime {
  id: string;
  label: string;
  role: Role;
  cli: Cli;
  model: string | null;
  effort: string;
  skipPerms: boolean;
  coderId: string;
  osHint: OsHint;
  sessionId: string | null;
  status: AgentStatus;
  inFlight: boolean;
  // Live-activity timing (transient, not persisted). turnStartedAt is set when a
  // turn goes in-flight and cleared on terminal settle; it drives the elapsed
  // timer. lastEventAt bumps on every stream event so the UI can flag a turn
  // that's still running but has gone quiet (>30s) as "still working".
  turnStartedAt: number | null;
  lastEventAt: number | null;
  createdAt: string;
  // v2.x Cloud Sync: a conversation materialized from another device's pulled
  // transcript is view-only (no local session) — the composer is disabled.
  readOnly?: boolean;
  // Mission A: server-side declared/promoted state of this agent (planner or
  // coder). Populated by the dashboard-state ticks; the sidebar renders a badge
  // below the avatar. null/undefined = no presence row yet (server hasn't seen
  // this agent in any state-bearing tool call).
  presence?: AgentPresenceLite | null;
  messages: MessageRuntime[];
  // v2.x: follow-up messages queued while a turn is in-flight; auto-sent in order
  // when the current turn completes. In-memory (not persisted). Each carries its
  // own attachments so an image attached while busy survives the queue.
  queue: QueuedMessage[];
  // v2.x companion webapp: when a turn was injected by a web message, the
  // originating web_to_agent message id — so onDone relays the response back
  // (agent_to_web, correlated). Transient.
  webParent?: string;
  // Fix B (rate-limit resilience): retries used in the CURRENT logical turn,
  // the pending backoff timer (cleared on cancel/archive/unmount), and a flag so
  // an explicit cancel during a retry-wait wins over the scheduled retry.
  retryCount: number;
  retryTimer?: ReturnType<typeof setTimeout>;
  cancelRequested?: boolean;
  // Internal — not persisted
  toolMap: Map<string, ToolCallData>;
  streamingIdx: number | null; // index in messages of the streaming assistant msg
  dispose?: () => void;
}

// Rate-limit retry policy (Fix B): exponential backoff, bounded attempts.
const RL_MAX_RETRIES = 5;
const RL_BASE_DELAY_MS = 2000;
const RL_MAX_DELAY_MS = 60000;

// A turn errored due to rate limiting if its error/stderr/stream text matches
// any of these. Kept deliberately broad across claude + codex phrasings.
function isRateLimitText(s: string | null | undefined): boolean {
  const t = (s || '').toLowerCase();
  return (
    t.includes('rate limit') ||
    t.includes('rate-limit') ||
    t.includes('rate_limit') ||
    t.includes('temporarily limiting requests') ||
    t.includes('too many requests') ||
    /\b429\b/.test(t)
  );
}

// Normalize a coder identifier the SAME way the server slugifies coder_id
// (lowercase, non-alphanumerics → hyphens, trim hyphens). The Hivemind may pass
// a target like "SEXI LEXI" while the local agent stores "sexi-lexi"; comparing
// raw strings misses the match and wakes the wrong (or no) coder. Fix A.
function normCoderId(s: string | null | undefined): string {
  return (s || '').toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}

// Mission B P1: derive what the desktop ACTUALLY observes for a single agent,
// per the spec's bucket rules. Returns null when the agent shouldn't be
// published (readOnly = materialized from another device → not ours; missing
// agent_key = no slug-able identity).
function deriveObserved(a: AgentRuntime, now: number): {
  agent_key: string;
  state: 'idle' | 'working' | 'dead';
  detail: string | null;
} | null {
  if (a.readOnly) return null;
  const agent_key = a.role === 'hivemind' ? 'planner' : normCoderId(a.coderId || a.label);
  if (!agent_key) return null;
  const last = a.lastEventAt ?? a.turnStartedAt ?? null;
  const ageSec = last != null ? Math.floor((now - last) / 1000) : null;
  if (a.inFlight) {
    if (ageSec == null || ageSec < 5) return { agent_key, state: 'working', detail: 'streaming response' };
    if (ageSec < 30) return { agent_key, state: 'working', detail: 'awaiting tool result' };
    return { agent_key, state: 'working', detail: 'tool call in progress (>30s)' };
  }
  if (a.status === 'err') return { agent_key, state: 'dead', detail: 'process exited' };
  if (ageSec == null) return { agent_key, state: 'idle', detail: null };
  if (ageSec < 10) return { agent_key, state: 'idle', detail: 'just finished' };
  if (ageSec < 5 * 60) return { agent_key, state: 'idle', detail: null };
  if (ageSec < 30 * 60) return { agent_key, state: 'idle', detail: 'quiet' };
  return { agent_key, state: 'idle', detail: null };
}

// Mission A: mutate `agents` in place so each gets its matching AgentPresenceLite
// (or null if no row). agent_key matching: hivemind → "planner"; coder →
// normCoderId(coder.coderId || coder.label) — same normalization the server enforces.
// Called from every dashboard-state tick (the existing dashboard poll + both
// wake-reliability fallbacks already fetch state.agent_presence) so the sidebar
// stays fresh without a new endpoint or poll.
function attachPresence(agents: AgentRuntime[], presenceList: AgentPresenceLite[] | undefined): void {
  if (!Array.isArray(presenceList)) return;
  const byKey = new Map<string, AgentPresenceLite>();
  for (const p of presenceList) {
    if (!p || !p.agent_key) continue;
    const k = p.agent_key === 'planner' ? 'planner' : normCoderId(p.agent_key);
    if (k) byKey.set(k, p);
  }
  for (const a of agents) {
    const key = a.role === 'hivemind' ? 'planner' : normCoderId(a.coderId || a.label);
    a.presence = byKey.get(key) ?? null;
  }
}

// A follow-up queued while a turn is in-flight — text + any attachments, so an
// image attached while busy isn't dropped (sent when the queue drains).
export interface QueuedMessage {
  text: string;
  attachments?: AttachmentData[];
}

export interface MessageRuntime {
  role: 'user' | 'assistant' | 'system';
  text: string;
  at?: string;
  toolCalls?: ToolCallData[];
  tokens?: TokenUsage;
  attachments?: AttachmentData[];
  // v2.x Cloud Sync: stable client id (assigned lazily at first persist, then
  // reused) — the dedupe key for transcript push/pull.
  uuid?: string;
  // True for an assistant THINKING/reasoning entry (rendered collapsed).
  thinking?: boolean;
}

// Stable per-message id for Cloud Sync. crypto.randomUUID in the Electron
// renderer; falls back to a time+random id if unavailable.
function genMsgId(): string {
  const c = (globalThis as any).crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  return 'm-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
}

// Live activity snapshot for the "is this agent alive or frozen?" indicator.
// Derived (not stored): call agentActivity(agent, Date.now()) each render while
// a ticker forces re-renders, so elapsedSec visibly increments.
export interface AgentActivity {
  state: 'running-tool' | 'thinking' | 'rate-limited' | 'ready' | 'idle' | 'err';
  label: string; // e.g. "running Bash", "thinking", "rate-limited", "ready"
  elapsedSec: number | null; // seconds since the turn started; null when not in-flight
  stalled: boolean; // in-flight but no stream event for >30s — likely a long op, not dead
}

// STALL threshold: a turn that's in-flight but hasn't emitted a stream event in
// this long reads as "still working" rather than fabricating progress.
const STALL_MS = 30000;

function prettyToolName(name: string): string {
  // mcp__server__tool → tool; otherwise the name as-is.
  return name.split('__').pop() || name;
}

export function agentActivity(a: AgentRuntime, now: number): AgentActivity {
  const elapsedSec = a.inFlight && a.turnStartedAt ? Math.max(0, Math.floor((now - a.turnStartedAt) / 1000)) : null;
  const stalled = Boolean(a.inFlight && a.lastEventAt && now - a.lastEventAt > STALL_MS);
  if (a.inFlight) {
    if (a.status === 'rate-limited') return { state: 'rate-limited', label: 'rate-limited', elapsedSec, stalled };
    // The current action = the latest still-open tool call in the streaming msg.
    const msg = a.streamingIdx != null ? a.messages[a.streamingIdx] : undefined;
    const calls = msg?.toolCalls;
    if (calls && calls.length) {
      for (let i = calls.length - 1; i >= 0; i--) {
        if (!calls[i].completed) {
          return { state: 'running-tool', label: `running ${prettyToolName(calls[i].name)}`, elapsedSec, stalled };
        }
      }
    }
    return { state: 'thinking', label: 'thinking', elapsedSec, stalled };
  }
  if (a.status === 'err') return { state: 'err', label: 'error', elapsedSec: null, stalled: false };
  if (a.status === 'idle') return { state: 'idle', label: 'idle', elapsedSec: null, stalled: false };
  return { state: 'ready', label: 'ready', elapsedSec: null, stalled: false };
}

// One-line display string for an activity: "running Bash · 14s", "thinking · 22s",
// "still working · 45s" when stalled, or just "ready"/"idle"/"error" when not in-flight.
export function formatActivity(act: AgentActivity): string {
  if (act.elapsedSec == null) return act.label;
  const head = act.stalled ? 'still working' : act.label;
  return `${head} · ${act.elapsedSec}s`;
}

// What to select once the active project's agents finish loading. Set
// imperatively via requestSelect BEFORE switching activeSlug, so activating an
// inactive project can open a specific agent (or the launcher) without racing
// the async disk load that otherwise defaults to the last agent.
export type SelectRequest = { kind: 'agent'; id: string } | { kind: 'launcher' };

export interface ActiveProject {
  agents: AgentRuntime[];
  current: AgentRuntime | null;
  currentId: string | null;
  setCurrentId: (id: string | null) => void;
  requestSelect: (req: SelectRequest) => void;
  showLauncher: boolean;
  setShowLauncher: (b: boolean) => void;
  folder: string | null;
  hostname: string;
  pickFolder: () => Promise<void>;
  clearFolder: () => Promise<void>;
  createAgent: (v: LauncherValues) => void;
  sendTurn: (prompt: string, attachments?: AttachmentData[]) => void;
  setAgentModelEffort: (model: string | null, effort: string) => void;
  queueMessage: (text: string, attachments?: AttachmentData[]) => void;
  removeQueued: (idx: number) => void;
  wakeAgent: (a: AgentRuntime, reason: string) => void;
  archive: (a: AgentRuntime) => void;
  cancelTurn: () => void;
}

// `isActive` — true for the ONE project currently displayed. The runtime (agents,
// chat subscriptions, in-flight subprocesses, settle/persist) runs for EVERY open
// project so an in-flight turn survives a project switch; but the two side-effects
// that must stay single-active (the cross-machine dashboard poll and the companion
// web-relay presence/inbound loop) are gated behind isActive so backgrounded
// projects never double-run them. Local chat-event-driven auto-wake is NOT gated —
// it stays subscribed in every project so a backgrounded Planner's create_mission
// still wakes coders.
export function useActiveProject(project: Project | null, isActive: boolean): ActiveProject {
  const slug = project?.slug ?? null;
  const [agents, setAgents] = useState<AgentRuntime[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [showLauncher, setShowLauncher] = useState(true);
  const [folder, setFolder] = useState<string | null>(null);
  const [hostname, setHostname] = useState('host');
  // Tick to force re-renders when agent internals change (messages array
  // mutated in place during streaming).
  const [, setTick] = useState(0);
  const rerender = useCallback(() => setTick((n) => n + 1), []);
  const agentsRef = useRef<AgentRuntime[]>([]);
  agentsRef.current = agents;
  // Selection requested by the caller for the NEXT active-project load.
  const pendingSelectRef = useRef<SelectRequest | null>(null);
  const requestSelect = useCallback((req: SelectRequest) => {
    pendingSelectRef.current = req;
  }, []);

  const current = agents.find((a) => a.id === currentId) || null;

  // --- load project state on mount / active-slug change ---
  useEffect(() => {
    let alive = true;
    if (!slug) {
      // No active project: clear state, run nothing.
      setAgents([]);
      setCurrentId(null);
      setShowLauncher(true);
      setFolder(null);
      return;
    }
    (async () => {
      const [f, hn, saved] = await Promise.all([
        ah().paths.get(slug).catch(() => null),
        ah().app.hostname().catch(() => 'host'),
        ah().agents.list(slug).catch(() => []),
      ]);
      if (!alive) return;
      setFolder(f);
      setHostname(hn);
      const restored: AgentRuntime[] = (saved || []).map((d) => rehydrate(d));
      setAgents(restored);
      // Honor a caller-requested selection (set right before activation), else
      // default to the most recent agent, else show the launcher.
      const req = pendingSelectRef.current;
      pendingSelectRef.current = null;
      if (req?.kind === 'launcher') {
        setShowLauncher(true);
        setCurrentId(null);
      } else if (req?.kind === 'agent' && restored.some((a) => a.id === req.id)) {
        setShowLauncher(false);
        setCurrentId(req.id);
      } else if (restored.length > 0) {
        setShowLauncher(false);
        setCurrentId(restored[restored.length - 1].id);
      } else {
        setShowLauncher(true);
        setCurrentId(null);
      }

      // Cloud Sync pull-on-open (opt-in). After the local roster renders, pull the
      // tenant's newer transcripts and materialize any conversation this device
      // doesn't have locally as a read-only agent (the cross-device case — agent
      // ids are per-device, so a pulled id we don't know == another device's). We
      // never overwrite local agents here (same-device pull is a cursor-advancing
      // no-op; the deep two-devices-edited-the-same-agent merge is a later slice).
      const { active, ent } = await resolveCloudSync();
      if (!alive || !active || !ent) return;
      const since = await getCursor(ent.sub, slug);
      const res = await pullTranscripts(slug, since);
      if (!alive || !res || !res.conversations || res.conversations.length === 0) {
        if (res && res.cursor) await setCursor(ent.sub, slug, res.cursor);
        return;
      }
      const knownIds = new Set(restored.map((a) => a.id));
      const created: AgentRuntime[] = [];
      for (const conv of res.conversations) {
        if (knownIds.has(conv.agent_id)) continue; // same device — already local + authoritative
        const sorted = [...conv.messages].sort((x, y) => (x.idx ?? 0) - (y.idx ?? 0));
        const d: AgentData = {
          id: conv.agent_id,
          label: conv.label || conv.agent_id,
          role: (conv.role as Role) || 'coder',
          cli: (conv.cli as Cli) || 'claude',
          model: null,
          effort: '',
          skipPerms: false,
          coderId: '',
          osHint: null,
          sessionId: null,
          status: 'idle',
          readOnly: true, // materialized from another device — view-only
          createdAt: new Date().toISOString(),
          messages: sorted.map((m) => ({
            role: m.role,
            text: m.text,
            toolCalls: m.tool_calls || undefined,
            tokens: m.tokens || undefined,
            uuid: m.uuid,
          })),
        };
        const a = rehydrate(d);
        created.push(a);
        persist(a);
      }
      if (alive && created.length > 0) setAgents((prev) => [...prev, ...created]);
      if (res.cursor) await setCursor(ent.sub, slug, res.cursor);
    })();
    return () => {
      alive = false;
      // Tear down ALL subscriptions + cancel in-flight chats for the project
      // we're leaving. agentsRef still points at the OLD project's agents here
      // (the new project's setAgents hasn't run yet), so this disposes exactly
      // the right runtime before the next project spins up.
      for (const a of agentsRef.current) {
        if (a.retryTimer) { clearTimeout(a.retryTimer); a.retryTimer = undefined; }
        if (a.dispose) try { a.dispose(); } catch {}
        if (a.inFlight) ah().chat.cancel(a.id).catch(() => {});
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  const persist = useCallback(
    (a: AgentRuntime) => {
      if (!slug) return;
      ah().agents.save(slug, serialize(a)).catch(() => {});
    },
    [slug],
  );

  // Cross-machine wake. Poll /api/dashboard/state every 20s. On first poll
  // we snapshot all known IDs so we don't wake for stuff that pre-dates the
  // app opening. On subsequent polls, any NEW message/question/summary that
  // targets a local sibling fires a wake on that sibling. Only ONE loop runs:
  // it is keyed to the active slug and never starts when slug is null.
  const seenIdsRef = useRef<Set<string>>(new Set());
  const seenInitializedRef = useRef(false);
  // Holds the latest "check this project's hivemind for pending work and wake it"
  // fn, so the hivemind's settle() can trigger an immediate re-check (defined in
  // the Planner-wake reliability effect below). Default no-op until assigned.
  const checkPendingRef = useRef<(reason: string) => void>(() => {});
  // Same pattern, OTHER direction: a coder's settle() triggers a per-coder
  // pending-from-planner re-check (defined in the Coder-wake fallback effect).
  const checkCoderPendingRef = useRef<(reason: string) => void>(() => {});
  useEffect(() => {
    if (!slug || !isActive) return; // single-active: only the displayed project polls
    seenIdsRef.current = new Set();
    seenInitializedRef.current = false;

    const ingest = (state: any) => {
      const events: Array<{ id: string; kind: 'p2c' | 'c2p' | 'question' | 'summary' | 'mission'; target?: string | null; from?: string | null }> = [];
      const push = (arr: any[] | undefined, kind: any, getTarget: (x: any) => string | null | undefined, getFrom: (x: any) => string | null | undefined) => {
        if (!Array.isArray(arr)) return;
        for (const x of arr) {
          const id = `${kind}:${x.id ?? x.created_at ?? Math.random()}`;
          events.push({ id, kind, target: getTarget(x), from: getFrom(x) });
        }
      };
      // Match the actual server JSON keys (dashboard.py:331-333). Reading the old
      // pending_q/pending_s/p2c/c2p names returned undefined → Array.isArray failed
      // → every q/s/p2c/c2p wake branch was a silent no-op (the reason the operator
      // kept having to manually poke the Planner — this matches the live-verified
      // bug found before 2.0.20).
      push(state.messages?.planner_to_coder, 'p2c', (m) => m.target_coder_id, () => null);
      push(state.messages?.coder_to_planner, 'c2p', () => null, (m) => m.coder_id);
      push(state.pending_questions, 'question', () => null, (q) => q.coder_id);
      push(state.pending_summaries, 'summary', () => null, (s) => s.coder_id);
      push(state.inbox, 'p2c', (m) => m.target_coder_id, () => null);
      // A newly-active mission (e.g. created from a remote Planner like the
      // Claude app) should wake local Coders. We key the event on the mission
      // id; the first-tick snapshot absorbs the mission already active at app
      // open, so only a CHANGE to a new mission id fires a wake.
      const am = state.active_mission;
      const missionId = am && (am.mission_id ?? am.id);
      if (missionId) events.push({ id: `mission:${missionId}`, kind: 'mission' });
      return events;
    };

    const tick = async () => {
      try {
        const state = await ah().dashboard.state(slug);
        // Mission A: merge server-side AgentPresence into local agents so the
        // sidebar badges stay fresh on the existing 20s tick (no new poll).
        attachPresence(agentsRef.current, state.agent_presence);
        rerender();
        const events = ingest(state);
        // v2.x: keep <projectFolder>/agentsmissions/ in sync with server state.
        // Best-effort + idempotent (write-only-if-changed); no-op if no folder set.
        ah().missions.syncDocs(slug).catch(() => {});
        if (!seenInitializedRef.current) {
          for (const e of events) seenIdsRef.current.add(e.id);
          seenInitializedRef.current = true;
          return;
        }
        for (const e of events) {
          if (seenIdsRef.current.has(e.id)) continue;
          seenIdsRef.current.add(e.id);
          // A new active mission wakes every idle Coder once (the seenIds guard
          // above prevents re-waking on later polls; inFlight prevents stomping
          // a Coder mid-turn).
          if (e.kind === 'mission') {
            for (const a of agentsRef.current) {
              if (a.role === 'coder' && !a.inFlight) wakeAgent(a, 'remote:new-mission');
            }
            continue;
          }
          // Resolve which local sibling (if any) should wake.
          let target: AgentRuntime | undefined;
          if (e.kind === 'p2c' && e.target) {
            const want = normCoderId(e.target);
            target = agentsRef.current.find((a) => a.role === 'coder' && normCoderId(a.coderId || a.label) === want);
          } else if (e.kind === 'c2p' || e.kind === 'question' || e.kind === 'summary') {
            // Anything from a coder targets the Hivemind.
            target = agentsRef.current.find((a) => a.role === 'hivemind');
          }
          if (target && !target.inFlight) {
            wakeAgent(target, `remote:${e.kind}`);
          }
        }
      } catch (err) {
        // Network blip or auth issue — just retry next interval.
        console.warn('dashboard poll failed', err);
      }
    };

    // First tick after 5s (lets initial render settle), then every 20s.
    const tInitial = setTimeout(tick, 5000);
    const tInterval = setInterval(tick, 20000);
    return () => {
      clearTimeout(tInitial);
      clearInterval(tInterval);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, isActive]);

  // Planner-wake reliability fallback (belt-and-suspenders over the push wake in
  // maybeWakeOnToolUse). The push wake misses when the hivemind is in-flight at
  // report time or a stream tool_use drops; the dashboard poll above is active-only
  // and snapshots pre-existing pending items as "seen". So independently — for THIS
  // project's hivemind, on EVERY open project (NOT isActive-gated, so the
  // coordination loop progresses even when another project is displayed) — read the
  // server's UNANSWERED pending questions + summaries (the work that BLOCKS coders)
  // and wake the IDLE hivemind. State-based, not event-id dedup: a "pending
  // signature" = the set of pending question+summary ids; wake when it's non-empty
  // and either CHANGED since the last wake (new/cleared work) OR it's been idle past
  // the re-arm window (a dropped/ignored wake can't cause permanent silence). Empty
  // pending resets the signature. The hivemind's settle() also calls this for instant
  // recovery from the in-flight-skip miss.
  useEffect(() => {
    if (!slug) return;
    const REARM_MS = 75000;
    let lastWake: { sig: string; at: number } | null = null;
    let stopped = false;

    const doCheck = async (reason: string) => {
      if (stopped) return;
      const hive = agentsRef.current.find((a) => a.role === 'hivemind');
      if (!hive || hive.inFlight) return;
      let state: any;
      try { state = await ah().dashboard.state(slug); } catch { return; }
      if (stopped) return;
      // Mission A: refresh per-agent presence on the same fetch — no new poll.
      attachPresence(agentsRef.current, state.agent_presence);
      rerender();
      const ids: string[] = [];
      // Match the server JSON keys (dashboard.py:331-332). The earlier
      // pending_q/pending_s names were undefined → the signature was always empty
      // → ffe902c's wake never fired. Reading the right names is the fix.
      if (Array.isArray(state.pending_questions)) for (const q of state.pending_questions) ids.push('q:' + (q.id ?? q.created_at ?? ''));
      if (Array.isArray(state.pending_summaries)) for (const s of state.pending_summaries) ids.push('s:' + (s.id ?? s.created_at ?? ''));
      const sig = ids.sort().join('|');
      if (!sig) { lastWake = null; return; } // no pending work — re-arm immediately on the next item
      const changed = !lastWake || lastWake.sig !== sig;
      const stale = !!lastWake && Date.now() - lastWake.at > REARM_MS;
      if (!changed && !stale) return;
      // Re-resolve the hivemind AFTER the await — the push wake may have started it.
      const h = agentsRef.current.find((a) => a.role === 'hivemind');
      if (!h || h.inFlight) return;
      lastWake = { sig, at: Date.now() };
      wakeAgent(h, `pending:${reason}`);
    };
    checkPendingRef.current = (reason: string) => { void doCheck(reason); };

    const tInit = setTimeout(() => doCheck('init'), 8000);
    const tPoll = setInterval(() => doCheck('poll'), 15000);
    return () => {
      stopped = true;
      clearTimeout(tInit);
      clearInterval(tPoll);
      checkPendingRef.current = () => {};
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  // Coder-wake reliability fallback — symmetric to the Planner-wake one above
  // (ffe902c + the 2.0.20 field-name fix). The push wake (maybeWakeOnToolUse) can
  // miss planner→coder messages when the coder is in-flight at delivery time or
  // a stream tool_use drops (transport socket-closed errors we've hit). So
  // independently — for THIS project's coders, on EVERY open project (NOT
  // isActive-gated, so the loop progresses even when another project is displayed)
  // — read the server's UNDELIVERED planner_to_coder messages (state.pending_from_planner,
  // added in the matching server commit) and wake each IDLE non-hivemind agent
  // that has work waiting. PER-CODER signature dedup (one entry per coder agent,
  // not project-global): wake when the coder's signature is non-empty AND (changed
  // since the last wake OR past the 75s re-arm window). Empty pending for a coder
  // resets that coder's signature. Matches wait_for_planner_message's v1.11
  // delivery matrix: target_coder_id NULL is broadcast (every coder); a named
  // target reaches only the matching coder (normalized via normCoderId).
  useEffect(() => {
    if (!slug) return;
    const REARM_MS = 75000;
    const lastWake = new Map<string, { sig: string; at: number }>(); // agentId → state
    let stopped = false;

    const doCheck = async (reason: string) => {
      if (stopped) return;
      const coders = agentsRef.current.filter((a) => a.role !== 'hivemind' && !a.inFlight);
      if (coders.length === 0) return;
      let state: any;
      try { state = await ah().dashboard.state(slug); } catch { return; }
      if (stopped) return;
      // Mission A: refresh per-agent presence on the same fetch — no new poll.
      attachPresence(agentsRef.current, state.agent_presence);
      rerender();
      const msgs = Array.isArray(state.pending_from_planner) ? state.pending_from_planner : [];
      if (msgs.length === 0) {
        // No pending planner→coder anywhere — clear all per-coder signatures so
        // the next planner message rearms immediately for whichever coder it's for.
        lastWake.clear();
        return;
      }
      // Group: broadcast ids (target=NULL) reach every coder; targeted ids only
      // the matching coder (normalized the same way as wait_for_planner_message).
      const broadcastIds: string[] = [];
      const targetedByCoder = new Map<string, string[]>();
      for (const m of msgs) {
        const id = String(m.id ?? m.created_at ?? '');
        if (!id) continue;
        const target = m.target_coder_id;
        if (target == null || target === '') {
          broadcastIds.push(id);
        } else {
          const want = normCoderId(String(target));
          const arr = targetedByCoder.get(want) ?? [];
          arr.push(id);
          targetedByCoder.set(want, arr);
        }
      }
      const sortedBroadcast = [...broadcastIds].sort();
      for (const c of coders) {
        const my = normCoderId(c.coderId || c.label);
        const mine = (targetedByCoder.get(my) ?? []).sort();
        if (sortedBroadcast.length === 0 && mine.length === 0) {
          lastWake.delete(c.id);
          continue;
        }
        const sig = [...sortedBroadcast, ...mine].join('|');
        const prev = lastWake.get(c.id);
        const changed = !prev || prev.sig !== sig;
        const stale = !!prev && Date.now() - prev.at > REARM_MS;
        if (!changed && !stale) continue;
        // Re-resolve AFTER the await — the push wake may have just started it.
        const fresh = agentsRef.current.find((x) => x.id === c.id);
        if (!fresh || fresh.inFlight) continue;
        lastWake.set(c.id, { sig, at: Date.now() });
        wakeAgent(fresh, `pending:${reason}`);
      }
      // Drop dedup entries for agents that no longer exist (archived) so the map
      // doesn't leak across long sessions.
      const liveIds = new Set(agentsRef.current.map((a) => a.id));
      for (const id of Array.from(lastWake.keys())) {
        if (!liveIds.has(id)) lastWake.delete(id);
      }
    };
    checkCoderPendingRef.current = (reason: string) => { void doCheck(reason); };

    const tInit = setTimeout(() => doCheck('init'), 8000);
    const tPoll = setInterval(() => doCheck('poll'), 15000);
    return () => {
      stopped = true;
      clearTimeout(tInit);
      clearInterval(tPoll);
      checkCoderPendingRef.current = () => {};
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  // Mission B P1: 3s observation publisher. For each agent we spawned locally,
  // derive what we ACTUALLY see (PTY alive / in_flight / last stdout age) and
  // POST to /api/dashboard/presence so cloud-side agents see ground truth
  // (source='observed' on the server row). Per-agent change-only filter — we
  // only POST entries where state OR detail differs from the last successful
  // publish, so a fully-idle steady state is one POST then silence. Runs in EVERY
  // ProjectRuntimeHost (NOT isActive-gated) so backgrounded projects' agents
  // still publish. The server merge already flows back through the existing
  // dashboard.state ticks — no consumer code change needed on the client.
  useEffect(() => {
    if (!slug) return;
    const lastPublished = new Map<string, { state: string; detail: string }>();
    let stopped = false;

    const tick = async () => {
      if (stopped) return;
      const now = Date.now();
      const observations: Array<{ id: string; agent_key: string; state: 'idle' | 'working' | 'dead'; detail: string | null }> = [];
      for (const a of agentsRef.current) {
        const obs = deriveObserved(a, now);
        if (obs) observations.push({ id: a.id, ...obs });
      }
      const changed = observations.filter((o) => {
        const prev = lastPublished.get(o.id);
        return !prev || prev.state !== o.state || prev.detail !== (o.detail ?? '');
      });
      if (changed.length === 0) {
        // Drop cache entries for archived agents even when nothing changed, so
        // the map stays bounded across long sessions.
        const liveIds = new Set(agentsRef.current.map((a) => a.id));
        for (const id of Array.from(lastPublished.keys())) {
          if (!liveIds.has(id)) lastPublished.delete(id);
        }
        return;
      }
      const observedAt = new Date(now).toISOString();
      const payload = changed.map((o) => ({
        agent_key: o.agent_key,
        state: o.state,
        detail: o.detail,
        observed_at: observedAt,
      }));
      try {
        const res = await ah().presence.publish(slug, payload);
        if (res && res.ok) {
          // Cache only on success so a failed POST is retried next tick.
          for (const o of changed) {
            lastPublished.set(o.id, { state: o.state, detail: o.detail ?? '' });
          }
        }
      } catch { /* silent — best-effort 3s ticker */ }
      const liveIds = new Set(agentsRef.current.map((a) => a.id));
      for (const id of Array.from(lastPublished.keys())) {
        if (!liveIds.has(id)) lastPublished.delete(id);
      }
    };

    const tInit = setTimeout(tick, 2000);  // small delay so agents load from disk first
    const tPoll = setInterval(tick, 3000);
    return () => {
      stopped = true;
      clearTimeout(tInit);
      clearInterval(tPoll);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  // W2: companion-webapp relay. Authenticated as the operator's Supabase tenant
  // (so it shares a tenant with the webapp). Publishes the ACTIVE project's agent
  // roster + heartbeat, and polls /web/inbound for web→agent messages addressed to
  // this project's agents — injecting each (when the agent is idle) as a local
  // turn; the response is relayed back in onDone (correlated via webParent). Only
  // the active project is reachable from the web at any moment (single-active
  // runtime invariant). Idle-backoff to avoid hammering when nothing's happening.
  useEffect(() => {
    if (!slug || !isActive) return; // single-active web invariant: only the displayed project owns the relay
    let stopped = false;
    let lastPresence = 0;
    let idleStreak = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      if (stopped) return;
      const token = getAccessToken();
      let didWork = false;
      if (token) {
        const now = Date.now();
        if (now - lastPresence > 10000) {
          lastPresence = now;
          const roster = agentsRef.current.map((a) => ({
            agent_key: a.id, label: a.label, role: a.role, cli: a.cli, status: a.status,
          }));
          ah().web.presence(token, slug, roster).catch(() => {});
        }
        try {
          const r = await ah().web.inbound(token);
          for (const msg of r.messages || []) {
            if (msg.project_slug && msg.project_slug !== slug) continue; // different project — leave unacked
            const target = msg.agent_key
              ? agentsRef.current.find((a) => a.id === msg.agent_key)
              : (agentsRef.current.find((a) => a.role === 'hivemind') || agentsRef.current[0]);
            if (!target) continue;            // no matching local agent — leave for later
            if (target.inFlight) continue;    // busy — leave unacked, retry next poll
            await ah().web.ack(token, msg.message_id).catch(() => {});
            target.webParent = msg.message_id;
            _startUserTurn(target, msg.body);
            didWork = true;
          }
        } catch {
          // network blip — retry next tick
        }
      }
      idleStreak = didWork ? 0 : Math.min(idleStreak + 1, 5);
      // 3s when active; back off toward ~10s when idle.
      const delay = 3000 + idleStreak * 1500;
      if (!stopped) timer = setTimeout(tick, delay);
    };

    timer = setTimeout(tick, 3000);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, isActive]);

  // Live-activity ticker. While ANY agent has a turn in-flight, force a
  // re-render every second so the elapsed timers (ChatPane header + sidebar
  // rows) visibly increment — the key "it's alive, not frozen" signal. Idle
  // projects never re-render (guarded by the inFlight check), so this costs
  // nothing when nothing's running.
  useEffect(() => {
    if (!slug) return;
    const id = setInterval(() => {
      if (agentsRef.current.some((a) => a.inFlight)) rerender();
    }, 1000);
    return () => clearInterval(id);
  }, [slug, rerender]);

  const pickFolder = async () => {
    if (!slug) return;
    const picked = await ah().paths.pick(slug);
    if (picked) setFolder(picked);
  };
  const clearFolder = async () => {
    if (!slug) return;
    await ah().paths.set(slug, null);
    setFolder(null);
  };

  // ------- create + bootstrap an agent ----------------------------------
  const createAgent = (v: LauncherValues) => {
    if (!slug) return;
    const id = 'a-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 5);
    const seq = agentsRef.current.filter((a) => a.role === v.role).length + 1;
    const fallbackLabel = v.coderId || `${v.role}-${seq}`;
    const a: AgentRuntime = {
      id,
      label: fallbackLabel,
      role: v.role,
      cli: v.cli,
      model: v.model || null,
      effort: v.effort || '',
      skipPerms: v.skipPerms,
      coderId: v.coderId,
      osHint: v.osHint,
      sessionId: null,
      status: 'ready',
      inFlight: false,
      turnStartedAt: null,
      lastEventAt: null,
      createdAt: new Date().toISOString(),
      messages: [],
      queue: [],
      retryCount: 0,
      toolMap: new Map(),
      streamingIdx: null,
    };
    setAgents((prev) => [...prev, a]);
    setCurrentId(id);
    setShowLauncher(false);
    persist(a);
    // Kick off bootstrap turn so the agent verifies scope + greets.
    setTimeout(() => bootstrapAgent(id), 0);
  };

  const bootstrapAgent = (id: string) => {
    const a = agentsRef.current.find((x) => x.id === id);
    if (!a) return;
    a.retryCount = 0;
    a.cancelRequested = false;
    a.inFlight = true;
    a.status = 'thinking';
    a.turnStartedAt = Date.now();
    a.lastEventAt = Date.now();
    a.messages.push({ role: 'system', text: 'Initializing — verifying scope + reading AGENTS.md…' });
    // FIX 2: no pre-pushed empty assistant bubble — entries are created lazily on
    // the first streamed block (the "thinking" state shows via the header
    // activity indicator). Avoids an empty text bubble before a tool-first turn.
    a.streamingIdx = null;
    rerender();
    startStreamingChatTurn(a, '', /* bootstrap */ true);
  };

  // ------- send a normal user turn --------------------------------------
  // Core turn-start for a SPECIFIC agent (no inFlight guard — callers ensure it).
  // Used by sendTurn (current agent) and the queue drain (the agent whose turn
  // just finished).
  const _startUserTurn = (a: AgentRuntime, prompt: string, attachments?: AttachmentData[]) => {
    // What the user sees in the bubble: original text + thumbnails.
    a.messages.push({ role: 'user', text: prompt, attachments });

    // What claude actually receives: user text + a paths block. Claude's
    // vision pipeline picks the images up when it Reads the path.
    let claudePrompt = prompt;
    if (attachments && attachments.length > 0) {
      const lines = attachments.map((x) => `  - ${x.path}`).join('\n');
      claudePrompt = `${prompt}\n\n[Attached images — read these paths with the Read tool to view them:]\n${lines}`;
    }

    a.streamingIdx = null; // FIX 2: lazy entry creation (no empty placeholder bubble)
    a.retryCount = 0;
    a.cancelRequested = false;
    a.inFlight = true;
    a.status = 'thinking';
    a.turnStartedAt = Date.now();
    a.lastEventAt = Date.now();
    rerender();
    persist(a);
    startStreamingChatTurn(a, claudePrompt, false);
  };

  // Drain the next queued follow-up as a fresh turn. Re-armed on EVERY turn end
  // (clean completion OR user Stop), so the whole queue sends in order, one per
  // turn, until empty. The shift happens INSIDE the guards so a message is never
  // lost to a race (the old code shifted before an async guard and could drop
  // it). Archived agents (removed from the roster) are skipped — their queue
  // dies with them.
  const drainQueue = (a: AgentRuntime) => {
    if (a.inFlight || a.queue.length === 0) return;
    if (!agentsRef.current.some((x) => x.id === a.id)) return; // archived
    const next = a.queue.shift()!;
    rerender();
    _startUserTurn(a, next.text, next.attachments);
  };

  // Cloud Sync push (opt-in). Fire-and-forget AFTER a turn settles — so it never
  // disrupts an in-flight turn — and only when sync is active (signed in +
  // entitled + opted in). Pushes the agent's FULL transcript; the server upserts
  // by per-message uuid (LWW), so re-pushing the whole thing is cheap + idempotent.
  const maybeSyncPush = (a: AgentRuntime) => {
    if (!slug) return;
    const project = slug;
    resolveCloudSync().then(({ active }) => {
      if (!active) return;
      const messages = a.messages.map((m, i) => {
        if (!m.uuid) m.uuid = genMsgId();
        return {
          uuid: m.uuid,
          idx: i,
          role: m.role,
          text: m.text,
          tool_calls: m.toolCalls ?? null,
          tokens: m.tokens ?? null,
          created_at: m.at,
        };
      });
      void pushTranscript({ project, agent_id: a.id, label: a.label, role: a.role, cli: a.cli, messages });
    }).catch(() => {});
  };

  const sendTurn = (prompt: string, attachments?: AttachmentData[]) => {
    if (!current) return;
    const a = current;
    if (a.inFlight || (!prompt.trim() && (!attachments || attachments.length === 0))) return;
    _startUserTurn(a, prompt, attachments);
  };

  // Change the current agent's model + reasoning effort after launch. The turn
  // spawn reads a.model/a.effort at send time (startStreamingChatTurn), so a
  // mutate-and-persist here applies on the NEXT turn without disrupting any
  // in-flight one (that turn already spawned with the old values).
  const setAgentModelEffort = (model: string | null, effort: string) => {
    if (!current) return;
    const a = current;
    a.model = model;
    a.effort = effort;
    rerender();
    persist(a);
  };

  // P4: queue a follow-up while a turn is in-flight; drained on turn done.
  const queueMessage = (text: string, attachments?: AttachmentData[]) => {
    if (!current) return;
    if (!text.trim() && (!attachments || attachments.length === 0)) return;
    current.queue.push({ text, attachments });
    rerender();
  };

  const removeQueued = (idx: number) => {
    if (!current) return;
    current.queue.splice(idx, 1);
    rerender();
  };

  const siblingPayload = (selfId: string) =>
    agentsRef.current
      .filter((x) => x.id !== selfId)
      .map((x) => ({ label: x.label, role: x.role, cli: x.cli, coderId: x.coderId, status: x.status }));

  const startStreamingChatTurn = (a: AgentRuntime, prompt: string, bootstrap: boolean) => {
    if (!slug) return;
    a.toolMap = new Map();
    let settled = false;
    let rateLimitHit = false;
    const noteRateLimit = (s: string | null | undefined) => { if (isRateLimitText(s)) rateLimitHit = true; };

    const offEvent = ah().chat.onEvent(a.id, (ev) => {
      // Rate-limit signals surface as raw/system stream text on both CLIs.
      if (ev && ev.type === 'raw' && typeof ev.text === 'string') noteRateLimit(ev.text);
      handleEvent(a, ev);
    });
    const offStderr = ah().chat.onStderr(a.id, (t) => { noteRateLimit(t); console.warn(`[${a.label}]`, t); });

    const cleanup = () => {
      try { offEvent(); offStderr(); offDone(); offErr(); } catch {}
      a.dispose = undefined;
    };

    // Single settlement path for this turn — guards against onDone + onError both
    // firing. ok=true is a clean finish; otherwise we either retry (rate limit,
    // bounded exponential backoff — Fix B) or surface a terminal error.
    const settle = (ok: boolean, errText?: string) => {
      if (settled) return;
      settled = true;
      cleanup();

      // An explicit cancel (composer Stop / archive) wins over any retry.
      if (a.cancelRequested) {
        a.cancelRequested = false;
        a.retryCount = 0;
        a.inFlight = false;
        a.turnStartedAt = null;
        a.status = 'idle';
        a.streamingIdx = null;
        rerender();
        persist(a);
        // Stop cancels the CURRENT turn but keeps the queue moving — the user
        // stopped to get to their follow-ups, not to discard them. (Archive
        // removed the agent from the roster, so drainQueue safely no-ops there.)
        setTimeout(() => drainQueue(a), 0);
        return;
      }

      // Rate-limited failure → retry the SAME turn after backoff (prompt +
      // bootstrap are still in scope). Only surface the error once retries are
      // exhausted.
      if (!ok && rateLimitHit && a.retryCount < RL_MAX_RETRIES) {
        a.retryCount += 1;
        const backoff = Math.min(RL_MAX_DELAY_MS, RL_BASE_DELAY_MS * 2 ** (a.retryCount - 1));
        a.status = 'rate-limited';
        a.streamingIdx = null;
        a.messages.push({
          role: 'system',
          text: `rate-limited — retrying in ${Math.ceil(backoff / 1000)}s (attempt ${a.retryCount}/${RL_MAX_RETRIES})…`,
        });
        a.inFlight = true; // stay busy: composer disabled + status visible
        rerender();
        persist(a);
        a.retryTimer = setTimeout(() => {
          a.retryTimer = undefined;
          if (a.cancelRequested) {
            a.cancelRequested = false;
            a.retryCount = 0;
            a.inFlight = false;
            a.turnStartedAt = null;
            a.status = 'idle';
            rerender();
            persist(a);
            return;
          }
          a.streamingIdx = null; // FIX 2: lazy entry creation on retry too
          a.status = 'thinking';
          // New attempt of the same logical turn — reset the elapsed timer.
          a.turnStartedAt = Date.now();
          a.lastEventAt = Date.now();
          rerender();
          startStreamingChatTurn(a, prompt, bootstrap);
        }, backoff);
        return;
      }

      // Terminal: clean success, or a non-retryable / exhausted failure.
      if (!ok && errText) a.messages.push({ role: 'system', text: errText });
      a.retryCount = 0;
      a.inFlight = false;
      a.turnStartedAt = null;
      a.status = ok ? 'ready' : 'err';
      a.streamingIdx = null;
      rerender();
      persist(a);
      // Planner-wake reliability: once a hivemind turn settles (success OR error),
      // immediately re-check for pending coder questions/summaries it didn't reach
      // (the classic miss: a report arrived while it was mid-turn) and re-wake if
      // still work — instant recovery without waiting for the ~15s poll. The
      // signature dedup in checkPendingRef prevents an error-loop (an errored turn
      // doesn't clear pending → same signature → no immediate re-wake).
      // Symmetric for coders: settle → re-check pending_from_planner for this coder
      // (a planner→coder message that arrived while it was mid-turn).
      if (a.role === 'hivemind') {
        setTimeout(() => checkPendingRef.current('settle'), 1500);
      } else {
        setTimeout(() => checkCoderPendingRef.current('settle'), 1500);
      }
      if (!ok) return;

      // Cloud Sync (opt-in): push the finalized transcript after a clean settle.
      maybeSyncPush(a);

      // W2: if this turn was injected by a web message, relay the response back
      // (agent_to_web, correlated to the originating web_to_agent).
      if (a.webParent && slug) {
        const parent = a.webParent;
        a.webParent = undefined;
        // FIX 2: entries are now split — the response text is the last assistant
        // entry WITH text; count tool calls across this turn's entries (back to
        // the last user message).
        const lastAssist = [...a.messages].reverse().find((m) => m.role === 'assistant' && !!m.text && !m.thinking);
        let body = (lastAssist?.text || '').trim();
        let tc = 0;
        for (let i = a.messages.length - 1; i >= 0; i--) {
          if (a.messages[i].role === 'user') break;
          tc += a.messages[i].toolCalls?.length || 0;
        }
        if (tc) body += (body ? '\n\n' : '') + `[${tc} tool call${tc > 1 ? 's' : ''}]`;
        if (!body) body = '(no text response)';
        const token = getAccessToken();
        if (token) ah().web.relay(token, parent, slug, a.id, body).catch(() => {});
      }
      // P4: keep draining the queue (one message per turn) until it's empty.
      setTimeout(() => drainQueue(a), 0);
    };

    const offDone = ah().chat.onDone(a.id, ({ code }) => settle(code === 0));
    const offErr = ah().chat.onError(a.id, ({ message }) => {
      noteRateLimit(message);
      settle(false, 'process error: ' + message);
    });
    a.dispose = cleanup;

    const authToken = getAccessToken();
    ah().chat
      .send({
        chatId: a.id,
        prompt,
        sessionId: a.sessionId,
        projectSlug: slug,
        coderId: a.coderId,
        osHint: a.osHint,
        cli: a.cli,
        model: a.model,
        effort: a.effort,
        skipPerms: a.skipPerms,
        // v2.x: present the signed-in tenant's Supabase token as the MCP bearer
        // (re-read each turn so a refreshed token is always current).
        authToken,
        requireAuthToken: hasSupabaseSession(),
        agentLabel: a.label,
        agentRole: a.role,
        bootstrap,
        siblings: siblingPayload(a.id),
      })
      .catch((err: any) => {
        noteRateLimit(String(err?.message || err));
        settle(false, 'failed to start: ' + (err?.message || err));
      });
  };

  // Reactive auto-wake. Inspect every tool call from a local agent; if it
  // signals "Coder activity expected" or "Hivemind activity expected",
  // wake the appropriate sibling(s) so the protocol loop actually makes
  // progress instead of deadlocking on a long-poll against an idle peer.
  const maybeWakeOnToolUse = (sender: AgentRuntime, toolName: string, toolInput: unknown) => {
    if (!toolName) return;
    const input = (toolInput || {}) as Record<string, any>;
    const shortName = toolName.split('__').pop() || toolName;
    let targets: AgentRuntime[] = [];

    // 1) Hivemind → specific Coder: direct routing tools. A targeted send must
    // wake EXACTLY the matched coder, so normalize both sides (the Hivemind may
    // pass "SEXI LEXI" while the local agent stores "sexi-lexi"). Fix A.
    if (toolName.endsWith('send_to_coder') || toolName.endsWith('answer_question') || toolName.endsWith('respond_to_summary')) {
      const tid = input.target_coder_id;
      if (typeof tid === 'string' && tid) {
        const want = normCoderId(tid);
        const target = agentsRef.current.find(
          (x) => x.id !== sender.id && x.role === 'coder' && normCoderId(x.coderId || x.label) === want,
        );
        if (target) targets = [target];
      }
    }

    // 2) Hivemind → ALL Coders broadcast wake — ONLY create_mission. A new
    // mission is the single legitimate "Coders should start now" signal.
    // wait_for_next_question / wait_for_next_summary are the Planner LONG-POLLING
    // (it is now ready to listen) — NOT a coder-start signal — so they must wake
    // no coder (previously they over-broadcast and woke every idle coder). Fix A.
    if (toolName.endsWith('create_mission')) {
      targets = agentsRef.current.filter((x) => x.id !== sender.id && x.role === 'coder');
    }

    // 3) Coder → Hivemind: any tool that hands off a message/question/summary.
    if (
      toolName.endsWith('send_to_planner') ||
      toolName.endsWith('ask_planner') ||
      toolName.endsWith('submit_progress')
    ) {
      const target = agentsRef.current.find((x) => x.id !== sender.id && x.role === 'hivemind');
      if (target) targets = [target];
    }

    for (const target of targets) {
      if (target.inFlight) continue;
      wakeAgent(target, `${sender.label} → ${shortName}`);
    }
  };

  const wakeAgent = (target: AgentRuntime, reason: string) => {
    const cid = target.coderId || target.label;
    const wakePrompt =
      target.role === 'coder'
        ? `[auto-wake from ${reason}]
The Hivemind is signalling that Coder activity is expected. Do this in order:

1. Call \`mcp__agentshive__get_active_mission(coder_id="${cid}")\` to fetch the current mission brief.
2. If there IS an active mission and you haven't reported progress yet, start working on it. As you reach milestones, call \`mcp__agentshive__submit_progress(summary, status, coder_id="${cid}")\`. When stuck, call \`mcp__agentshive__ask_planner(question, coder_id="${cid}")\` and then \`mcp__agentshive__wait_for_answer(question_id, coder_id="${cid}", timeout=240)\`.
3. If there's NO active mission (or you're mid-mission and want to check for direct messages), call \`mcp__agentshive__wait_for_coder_message(coder_id="${cid}", timeout=5)\` once.
4. If nothing meaningful is waiting after step 3, acknowledge to the operator that you're standing by — don't keep polling.`
        : `[auto-wake from ${reason}]
A new message or question or summary is waiting. Do this in order:

1. Call \`mcp__agentshive__list_pending_questions()\` and \`mcp__agentshive__list_pending_summaries()\` — answer/respond to any pending ones.
2. Call \`mcp__agentshive__wait_for_planner_message(timeout=5)\` once.
3. If nothing meaningful was waiting, acknowledge briefly and stand by.`;
    target.messages.push({ role: 'user', text: wakePrompt });
    target.streamingIdx = null; // FIX 2: lazy entry creation (no empty placeholder bubble)
    target.inFlight = true;
    target.status = 'thinking';
    target.turnStartedAt = Date.now();
    target.lastEventAt = Date.now();
    rerender();
    persist(target);
    startStreamingChatTurn(target, wakePrompt, false);
  };

  const handleEvent = (a: AgentRuntime, ev: ChatEvent) => {
    if (!ev || !ev.type) return;
    // Any stream event proves the turn is alive — bump the activity heartbeat so
    // the UI's "still working" stall detection only trips on genuine quiet.
    a.lastEventAt = Date.now();
    if (ev.type === 'system' && ev.subtype === 'init') {
      if (ev.session_id) a.sessionId = ev.session_id;
      return;
    }
    if (ev.type === 'assistant' && ev.message) {
      const content = ev.message.content || [];
      for (const c of content) {
        if (c.type === 'thinking') {
          // Extended-thinking/reasoning: its OWN collapsed entry. Append to the
          // open thinking entry, else start a new one. (Real reasoning text — only
          // present when the model actually emits thinking; never fabricated.)
          const open = a.streamingIdx != null ? a.messages[a.streamingIdx] : null;
          const openIsThinking = !!open && open.role === 'assistant' && !!open.thinking;
          if (openIsThinking && open) {
            open.text += (c as any).thinking || '';
          } else {
            a.messages.push({ role: 'assistant', text: (c as any).thinking || '', toolCalls: [], thinking: true });
            a.streamingIdx = a.messages.length - 1;
          }
        } else if (c.type === 'text') {
          // FIX 2: text lives in its own bubble. Append to the current OPEN text
          // entry; if the open entry is a tool group / thinking entry (or nothing's
          // open yet), start a NEW text entry — so text AFTER a tool/thinking
          // begins a fresh bubble, interleaved in order.
          const open = a.streamingIdx != null ? a.messages[a.streamingIdx] : null;
          const openIsText = !!open && open.role === 'assistant' && !open.thinking && !(open.toolCalls && open.toolCalls.length > 0);
          if (openIsText && open) {
            open.text += c.text;
          } else {
            a.messages.push({ role: 'assistant', text: c.text, toolCalls: [] });
            a.streamingIdx = a.messages.length - 1;
          }
        } else if (c.type === 'tool_use') {
          const tc: ToolCallData = { id: c.id, name: c.name, input: c.input, completed: false };
          // FIX 2: CONSECUTIVE tool calls group into one entry (own bubble, no
          // text); text resuming closes the group (handled above). The open entry
          // is a tool group iff it has toolCalls and no text.
          const open = a.streamingIdx != null ? a.messages[a.streamingIdx] : null;
          const openIsToolGroup = !!open && open.role === 'assistant' && !!open.toolCalls && open.toolCalls.length > 0 && !open.text;
          if (openIsToolGroup && open) {
            open.toolCalls!.push(tc);
          } else {
            a.messages.push({ role: 'assistant', text: '', toolCalls: [tc] });
            a.streamingIdx = a.messages.length - 1;
          }
          a.toolMap.set(c.id, tc);
          // Auto-wake: when the active agent calls a routing tool aimed at
          // another sibling, fire a wake turn on the target if it's idle.
          maybeWakeOnToolUse(a, c.name, c.input);
        }
      }
      rerender();
      return;
    }
    if (ev.type === 'user' && ev.message) {
      const content = ev.message.content || [];
      for (const c of content) {
        if (c.type === 'tool_result') {
          const tc = a.toolMap.get(c.tool_use_id);
          if (tc) {
            tc.result = c.content;
            tc.isError = Boolean(c.is_error);
            tc.completed = true;
          }
        }
      }
      rerender();
      return;
    }
    if (ev.type === 'result') {
      const cur = a.streamingIdx != null ? a.messages[a.streamingIdx] : null;
      if (cur && ev.usage) {
        const u = ev.usage;
        // NEW tokens this turn (input excludes cache_read — that's the whole
        // cached context RE-READ every turn; summing it compounds to bogus
        // millions). Summed across turns for the cumulative session total.
        // `context` = the turn's TOTAL input incl. cache_read = the point-in-time
        // CONTEXT FILL the CLIs show; used as the LATEST value, never summed.
        cur.tokens = {
          input: (u.input_tokens || 0) + (u.cache_creation_input_tokens || 0),
          output: u.output_tokens || 0,
          context: (u.input_tokens || 0) + (u.cache_creation_input_tokens || 0) + (u.cache_read_input_tokens || 0),
        };
        rerender();
      }
      return;
    }
    if (ev.type === 'raw' && typeof ev.text === 'string') {
      a.messages.push({ role: 'system', text: ev.text });
      rerender();
    }
  };

  // ------- agent actions ------------------------------------------------
  const archive = (a: AgentRuntime) => {
    if (!slug) return;
    // Archiving must TERMINATE the agent's CLI subprocess, not just drop it from
    // the UI. We ALWAYS issue a cancel (which tree-kills the spawned CLI + its
    // children in the main process) so a removed agent can never keep running and
    // editing files. Also clear any pending rate-limit retry so it can't re-spawn.
    a.cancelRequested = true;
    if (a.retryTimer) { clearTimeout(a.retryTimer); a.retryTimer = undefined; }
    ah().chat.cancel(a.id).catch(() => {});
    if (a.dispose) try { a.dispose(); } catch {}
    ah().agents.delete(slug, a.id).catch(() => {});
    setAgents((prev) => {
      const next = prev.filter((x) => x.id !== a.id);
      if (currentId === a.id) {
        if (next.length) setCurrentId(next[next.length - 1].id);
        else { setCurrentId(null); setShowLauncher(true); }
      }
      return next;
    });
  };

  const cancelTurn = () => {
    if (!current) return;
    const a = current;
    a.cancelRequested = true;
    // If we're waiting between rate-limit retries there's no live process to
    // cancel — clear the timer and reflect the cancellation immediately.
    if (a.retryTimer) {
      clearTimeout(a.retryTimer);
      a.retryTimer = undefined;
      a.cancelRequested = false;
      a.retryCount = 0;
      a.inFlight = false;
      a.turnStartedAt = null;
      a.status = 'idle';
      a.streamingIdx = null;
      rerender();
      persist(a);
      // Stopping during a rate-limit wait still advances the queue.
      setTimeout(() => drainQueue(a), 0);
      return;
    }
    // Live process: tree-kill it; settle() sees cancelRequested and finishes clean.
    ah().chat.cancel(a.id).catch(() => {});
  };

  return {
    agents,
    current,
    currentId,
    setCurrentId,
    requestSelect,
    showLauncher,
    setShowLauncher,
    folder,
    hostname,
    pickFolder,
    clearFolder,
    createAgent,
    sendTurn,
    setAgentModelEffort,
    queueMessage,
    removeQueued,
    wakeAgent,
    archive,
    cancelTurn,
  };
}

// --- (de)serialize between persisted AgentData and runtime AgentRuntime ---
function rehydrate(d: AgentData): AgentRuntime {
  return {
    id: d.id,
    label: d.label,
    role: d.role,
    cli: d.cli,
    model: d.model || null,
    effort: d.effort || '',
    skipPerms: Boolean(d.skipPerms),
    coderId: d.coderId || '',
    osHint: d.osHint || null,
    sessionId: d.sessionId || null,
    status: 'idle',
    inFlight: false,
    turnStartedAt: null,
    lastEventAt: null,
    readOnly: Boolean(d.readOnly),
    createdAt: d.createdAt || new Date().toISOString(),
    messages: (d.messages || []).map((m) => ({
      role: m.role,
      text: m.text,
      at: m.at,
      toolCalls: m.toolCalls,
      tokens: m.tokens,
      attachments: m.attachments,
      uuid: m.uuid,
      thinking: m.thinking,
    })),
    queue: [],
    retryCount: 0,
    toolMap: new Map(),
    streamingIdx: null,
  };
}

function serialize(a: AgentRuntime): AgentData {
  return {
    id: a.id,
    label: a.label,
    role: a.role,
    cli: a.cli,
    model: a.model,
    effort: a.effort,
    skipPerms: a.skipPerms,
    coderId: a.coderId,
    osHint: a.osHint,
    sessionId: a.sessionId,
    // Transient busy states never persist as busy.
    status: a.status === 'thinking' || a.status === 'rate-limited' ? 'idle' : a.status,
    readOnly: a.readOnly,
    createdAt: a.createdAt,
    updatedAt: new Date().toISOString(),
    messages: a.messages.map((m) => {
      // Assign a stable uuid at first persist (≈ creation) and reuse it forever —
      // mutating the live object so the streaming assistant message keeps ONE id
      // from first render through settle (required for Cloud Sync LWW dedupe).
      if (!m.uuid) m.uuid = genMsgId();
      return {
        role: m.role,
        text: m.text,
        at: m.at,
        toolCalls: m.toolCalls,
        tokens: m.tokens,
        attachments: m.attachments,
        uuid: m.uuid,
        thinking: m.thinking,
      };
    }),
  };
}

// ProjectSidebar — VS-Code / Cursor-style always-on sidebar.
//
// Lists every "opened" project as a collapsible folder; each folder's children
// are that project's agents. Only the ACTIVE project has a live runtime, so:
//   - the active project's agents come from `activeAgents` (live status, hover
//     Zap=wake / X=archive),
//   - every other (inactive) project's agents are lazy-loaded read-only from
//     disk (agents.list) and rendered dormant — clicking one ACTIVATES that
//     project first, it never fires against a non-running runtime.
//
// The top "+ Open / New project" control opens an inline panel to add an
// existing server project or create a new one (slug validation preserved from
// the old full-page ProjectPicker).

import { useEffect, useState } from 'react';
import {
  Bot,
  ChevronDown,
  ChevronRight,
  Code2,
  Crown,
  ExternalLink,
  FolderOpen,
  Loader2,
  Plus,
  RefreshCw,
  TerminalSquare,
  X,
  Zap,
} from 'lucide-react';
import {
  ah,
  slugify,
  validateSlug,
  type AgentData,
  type AgentPresenceLite,
  type AgentStatus,
  type Project,
  type Role,
  type Cli,
} from '@/lib/agentshive';
import { agentActivity, formatActivity, type AgentActivity, type AgentRuntime } from '@/lib/useActiveProject';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Separator } from '@/components/ui/separator';
import { cn } from '@/lib/utils';

// Common shape both live runtime agents and on-disk agents render through.
interface RowAgent {
  id: string;
  label: string;
  role: Role;
  cli: Cli;
  model: string | null;
  status: AgentStatus;
  // Live activity, present for ANY project with a mounted runtime (not just the
  // active one). Undefined only for projects falling back to on-disk (dormant).
  activity?: AgentActivity;
  // True when this row is backed by a live runtime (status dot reflects real
  // state); false for the on-disk dormant fallback.
  live: boolean;
  // Mission A: server-side declared/promoted state of this agent.
  presence?: AgentPresenceLite | null;
}

interface Props {
  projects: Project[]; // opened projects, in sidebar order
  activeSlug: string | null;
  activeCurrentId: string | null; // which agent row is open in the chat pane
  collapsed: Record<string, boolean>;
  liveAgentsBySlug: Record<string, AgentRuntime[]>; // live agents per OPEN project
  activeFolder: string | null;
  serverProjects: Project[]; // all server projects (for the add panel)
  refreshKey: number; // bump to force inactive-agent reloads
  onToggleCollapse: (slug: string) => void;
  onSelectProject: (slug: string) => void; // activate + ensure expanded
  onSelectAgent: (slug: string, agentId: string) => void;
  onNewAgent: (slug: string) => void;
  onWakeActive: (agentId: string) => void;
  onArchiveActive: (agentId: string) => void;
  onOpenProject: (slug: string) => void; // add existing server project
  onCreateProject: (slug: string, name: string) => Promise<void>;
  onCloseProject: (slug: string) => void; // remove from sidebar
  onOpenDashboard: (slug: string) => void;
  onPickFolder: () => void;
  onClearFolder: () => void;
  onRefreshServerProjects: () => void;
}

export function ProjectSidebar(props: Props) {
  const {
    projects,
    activeSlug,
    collapsed,
    liveAgentsBySlug,
    activeFolder,
    serverProjects,
    refreshKey,
  } = props;

  // Lazy-loaded read-only agent lists for INACTIVE projects.
  const [inactiveAgents, setInactiveAgents] = useState<Record<string, AgentData[]>>({});
  const [showAdd, setShowAdd] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      const entries = await Promise.all(
        projects
          .filter((p) => p.slug !== activeSlug)
          .map(async (p) => {
            const list = await ah().agents.list(p.slug).catch(() => [] as AgentData[]);
            return [p.slug, list] as const;
          }),
      );
      if (!alive) return;
      const next: Record<string, AgentData[]> = {};
      for (const [slug, list] of entries) next[slug] = list;
      setInactiveAgents(next);
    })();
    return () => {
      alive = false;
    };
    // Reload when the set of projects changes, when the active project changes
    // (the just-deactivated one must refresh from disk), or on explicit bump.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projects.map((p) => p.slug).join(','), activeSlug, refreshKey]);

  const rowsFor = (slug: string): RowAgent[] => {
    // Prefer the live runtime roster for ANY open project (every open project has
    // a mounted ProjectRuntimeHost now), so a backgrounded project's running
    // agent shows live "thinking · Ns" just like the active one. Fall back to the
    // on-disk dormant list only during the brief pre-mount window.
    const live = liveAgentsBySlug[slug];
    if (live) {
      const now = Date.now();
      return live.map((a) => ({
        id: a.id,
        label: a.label,
        role: a.role,
        cli: a.cli,
        model: a.model,
        status: a.status,
        activity: agentActivity(a, now),
        live: true,
        presence: a.presence ?? null,
      }));
    }
    return (inactiveAgents[slug] || []).map((d) => ({
      id: d.id,
      label: d.label,
      role: d.role,
      cli: d.cli,
      model: d.model || null,
      status: d.status === 'thinking' ? 'idle' : d.status || 'idle',
      live: false,
    }));
  };

  return (
    <aside className="relative flex w-72 flex-none flex-col overflow-hidden border-r border-border/60 sidebar-glass">
      {/* Add control */}
      <div className="border-b border-border/60 p-2.5">
        <Button
          variant="outline"
          size="sm"
          className="w-full justify-start gap-2 text-[12px]"
          onClick={() => setShowAdd((s) => !s)}
        >
          <Plus className="h-3.5 w-3.5" /> Open / New project
        </Button>
        {showAdd && (
          <AddProjectPanel
            openedSlugs={projects.map((p) => p.slug)}
            serverProjects={serverProjects}
            onOpenProject={(slug) => {
              props.onOpenProject(slug);
              setShowAdd(false);
            }}
            onCreateProject={async (slug, name) => {
              await props.onCreateProject(slug, name);
              setShowAdd(false);
            }}
            onRefresh={props.onRefreshServerProjects}
          />
        )}
      </div>

      {/* Project folders */}
      <div className="flex-1 overflow-y-auto scrollbar-thin py-1.5">
        {projects.length === 0 && (
          <p className="px-4 py-6 text-center text-xs text-muted-foreground">
            No projects open.<br />Click <b>Open / New project</b> to begin.
          </p>
        )}

        {projects.map((p) => {
          const isActive = p.slug === activeSlug;
          const isCollapsed = Boolean(collapsed[p.slug]);
          const rows = rowsFor(p.slug);
          return (
            <div key={p.slug} className="mb-0.5">
              {/* Folder header */}
              <div
                className={cn(
                  'group flex items-center gap-1 px-1.5 py-1.5 transition-colors',
                  isActive ? 'bg-primary/10' : 'hover:bg-secondary/40',
                )}
              >
                <button
                  className="flex h-5 w-5 flex-none items-center justify-center rounded text-muted-foreground hover:text-foreground"
                  onClick={() => props.onToggleCollapse(p.slug)}
                  title={isCollapsed ? 'Expand' : 'Collapse'}
                >
                  {isCollapsed ? <ChevronRight className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
                </button>
                <button
                  className="min-w-0 flex-1 text-left"
                  onClick={() => props.onSelectProject(p.slug)}
                  title={isActive ? 'Active project' : 'Click to activate'}
                >
                  <div className="flex items-center gap-1.5">
                    <span
                      className={cn(
                        'truncate text-[13px] font-semibold tracking-tight',
                        isActive ? 'text-foreground' : 'text-muted-foreground',
                      )}
                    >
                      {p.name || p.slug}
                    </span>
                    {isActive && (
                      <span
                        className="h-1.5 w-1.5 flex-none rounded-full bg-success"
                        style={{ boxShadow: '0 0 6px -1px currentColor' }}
                        title="Live runtime"
                      />
                    )}
                  </div>
                  <code className="block truncate text-[10px] text-muted-foreground">{p.slug}</code>
                </button>
                <button
                  className="flex h-5 w-5 flex-none items-center justify-center rounded text-muted-foreground opacity-0 transition-opacity hover:text-accent group-hover:opacity-100"
                  onClick={(e) => {
                    e.stopPropagation();
                    props.onNewAgent(p.slug);
                  }}
                  title="New agent in this project"
                >
                  <Plus className="h-3.5 w-3.5" />
                </button>
                <button
                  className="flex h-5 w-5 flex-none items-center justify-center rounded text-muted-foreground opacity-0 transition-opacity hover:text-destructive group-hover:opacity-100"
                  onClick={(e) => {
                    e.stopPropagation();
                    props.onCloseProject(p.slug);
                  }}
                  title="Close project (remove from sidebar)"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>

              {/* Agents */}
              {!isCollapsed && (
                <ul className="space-y-0.5 pb-1 pl-3 pr-1.5 pt-0.5">
                  {rows.length === 0 && (
                    <li className="px-2.5 py-1.5 text-[11px] text-muted-foreground">
                      No agents — <button className="underline hover:text-foreground" onClick={() => props.onNewAgent(p.slug)}>add one</button>.
                    </li>
                  )}
                  {rows.map((a) => {
                    const selected = isActive && props.activeCurrentId === a.id;
                    const canWake = isActive && (a.status === 'idle' || a.status === 'ready' || a.status === 'err');
                    const isLead = a.role === 'hivemind';
                    return (
                      <li key={a.id}>
                        <button
                          onClick={() => props.onSelectAgent(p.slug, a.id)}
                          className={cn(
                            'group/agent flex w-full items-center gap-2 rounded-md border border-transparent px-2 py-1.5 text-left transition-all',
                            selected
                              ? 'bg-gradient-to-b from-primary/10 to-primary/0 border-primary/40 ring-glow-primary'
                              : isLead
                                ? 'hover:bg-accent/10'
                                : 'hover:bg-secondary/60',
                            // Dim only truly-dormant (on-disk fallback) rows — a
                            // backgrounded project with a live runtime stays full
                            // opacity so its live status reads clearly.
                            !a.live && 'opacity-60',
                          )}
                        >
                          <StatusDot status={a.status} live={a.live} />
                          <AgentGlyph role={a.role} cli={a.cli} selected={selected} />
                          <div className="min-w-0 flex-1">
                            <div className="flex min-w-0 items-center gap-1.5">
                              <span className={cn('truncate text-[13px] font-medium tracking-tight', isLead && 'font-semibold text-accent')}>
                                {a.label}
                              </span>
                              {isLead && (
                                <span className="rounded-full border border-accent/25 bg-accent/10 px-1.5 py-0.5 text-[8.5px] font-semibold uppercase tracking-wide text-accent">
                                  lead
                                </span>
                              )}
                            </div>
                            {a.activity && a.activity.elapsedSec != null ? (
                              <div className={cn('flex items-center gap-1 truncate text-[10px]', a.activity.stalled ? 'text-warn' : 'text-accent')}>
                                <Loader2 className="h-2.5 w-2.5 flex-none animate-spin" />
                                <span className="truncate">{formatActivity(a.activity)}</span>
                              </div>
                            ) : (
                              <div className="truncate text-[10px] text-muted-foreground">
                                {a.role} · {a.cli}{a.model ? ` · ${a.model}` : ''}
                              </div>
                            )}
                            {/* Mission A: server-side declared/promoted state. Functional
                                badge — Sarah polishes the visuals in a follow-up. */}
                            <PresenceBadge presence={a.presence} />
                          </div>
                          {canWake && (
                            <Zap
                              className="h-3.5 w-3.5 flex-none text-muted-foreground opacity-0 transition-opacity hover:text-accent group-hover/agent:opacity-100"
                              onClick={(e) => {
                                e.stopPropagation();
                                props.onWakeActive(a.id);
                              }}
                            />
                          )}
                          {isActive && (
                            <X
                              className="h-3.5 w-3.5 flex-none text-muted-foreground opacity-0 transition-opacity hover:text-destructive group-hover/agent:opacity-100"
                              onClick={(e) => {
                                e.stopPropagation();
                                props.onArchiveActive(a.id);
                              }}
                            />
                          )}
                        </button>
                      </li>
                    );
                  })}

                  {/* Per-project footer — only the active project (live folder state). */}
                  {isActive && (
                    <li className="mt-1.5 flex items-center gap-1.5 px-1.5">
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-6 px-2 text-[10px]"
                        onClick={() => props.onOpenDashboard(p.slug)}
                      >
                        <ExternalLink className="h-3 w-3" /> Dashboard
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-6 w-6"
                        onClick={props.onPickFolder}
                        title={activeFolder || 'Pick folder'}
                      >
                        <FolderOpen className="h-3.5 w-3.5" />
                      </Button>
                      {activeFolder && (
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-6 w-6"
                          onClick={props.onClearFolder}
                          title="Clear folder"
                        >
                          <X className="h-3.5 w-3.5" />
                        </Button>
                      )}
                    </li>
                  )}
                  {isActive && activeFolder && (
                    <li className="px-1.5">
                      <code
                        className="block truncate rounded border border-border bg-input/50 px-2 py-1 font-mono text-[10px] text-muted-foreground"
                        title={activeFolder}
                      >
                        {activeFolder}
                      </code>
                    </li>
                  )}
                </ul>
              )}
            </div>
          );
        })}
      </div>
    </aside>
  );
}

// Mission A: small inline badge under each agent row carrying the server-side
// declared/promoted state. Functional defaults only — Sarah picks up the visual
// design in a follow-up. Hidden when state=idle or no presence row exists yet.
function PresenceBadge({ presence }: { presence: AgentPresenceLite | null | undefined }) {
  if (!presence) return null;
  const state = presence.state || '';
  if (state === 'idle') return null;  // explicit "nothing to do" — stay quiet
  const stateLabel =
    state === 'working' ? 'Working'
    : state === 'waiting_on_planner' ? 'Waiting on planner'
    : state === 'waiting_on_coder' ? 'Waiting on coder'
    : state === 'waiting_on_user' ? 'Waiting on user'
    : state === 'blocked' ? 'Blocked'
    : state === 'stale' ? 'Stale'
    : state === 'dead' ? 'Dead'
    : state;
  // Tone classes — explicit branches so Tailwind picks them up at build time.
  const toneClass =
    state === 'working' ? 'text-success'
    : state.startsWith('waiting_') ? 'text-warn'
    : state === 'blocked' ? 'text-destructive'
    : state === 'stale' ? 'text-warn opacity-80'
    : state === 'dead' ? 'text-muted-foreground opacity-70'
    : 'text-muted-foreground';
  const detail = (presence.detail || '').trim();
  const wasDeclared = state === 'stale' && presence.declared_state && presence.declared_state !== 'stale'
    ? ` (was: ${presence.declared_state})`
    : '';
  const now = Date.now();
  let timeHint = '';
  if (state === 'stale' || state === 'dead') {
    const since = presence.seconds_since_heartbeat;
    if (typeof since === 'number') timeHint = ` · last heard ${formatPresenceDuration(since)}`;
  } else if (presence.expected_done_at) {
    const due = Date.parse(presence.expected_done_at);
    if (!Number.isNaN(due)) {
      const remaining = Math.round((due - now) / 1000);
      timeHint = remaining >= 0
        ? ` · ~${formatPresenceDuration(remaining)} left`
        : ` · over by ${formatPresenceDuration(-remaining)}`;
    }
  } else if (presence.transitioned_at) {
    const t = Date.parse(presence.transitioned_at);
    if (!Number.isNaN(t)) {
      const elapsed = Math.round((now - t) / 1000);
      if (elapsed >= 5) timeHint = ` · ${formatPresenceDuration(elapsed)}`;
    }
  }
  const text = `${stateLabel}${wasDeclared}${detail ? ': ' + detail : ''}${timeHint}`;
  return (
    <div className={cn('truncate text-[10px]', toneClass)} title={text}>{text}</div>
  );
}

function formatPresenceDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m < 60) return s > 0 ? `${m}m${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm > 0 ? `${h}h${mm}m` : `${h}h`;
}

function StatusDot({ status, live }: { status: AgentStatus; live: boolean }) {
  const cls = !live
    ? 'bg-muted-foreground/50'
    : status === 'ready' ? 'bg-success'
    : status === 'thinking' ? 'bg-accent animate-pulse-ring'
    : status === 'rate-limited' ? 'bg-warn animate-pulse-ring'
    : status === 'err' ? 'bg-destructive'
    : 'bg-muted-foreground';
  return (
    <span
      className={cn('h-2 w-2 flex-none rounded-full', cls)}
      style={live ? { boxShadow: '0 0 6px -1px currentColor' } : undefined}
    />
  );
}

function AgentGlyph({ role, cli, selected }: { role: Role; cli: Cli; selected: boolean }) {
  const Icon = role === 'hivemind' ? Crown : cli === 'codex' ? TerminalSquare : cli === 'claude' ? Bot : Code2;
  const title =
    role === 'hivemind'
      ? 'Hivemind / Planner'
      : cli === 'codex'
        ? 'Coder · Codex'
        : cli === 'claude'
          ? 'Coder · Claude'
          : 'Coder';

  return (
    <span
      className={cn(
        'flex h-6 w-6 flex-none items-center justify-center rounded-md border transition-colors',
        role === 'hivemind'
          ? 'border-accent/35 bg-accent/10 text-accent'
          : cli === 'codex'
            ? 'border-primary/30 bg-primary/10 text-primary'
            : 'border-border bg-input/65 text-muted-foreground',
        selected && 'bg-background/35',
      )}
      title={title}
    >
      <Icon className="h-3.5 w-3.5" />
    </span>
  );
}

// --- inline add/open project panel (replaces the full-page ProjectPicker) ---

interface AddPanelProps {
  openedSlugs: string[];
  serverProjects: Project[];
  onOpenProject: (slug: string) => void;
  onCreateProject: (slug: string, name: string) => Promise<void>;
  onRefresh: () => void;
}

function AddProjectPanel({ openedSlugs, serverProjects, onOpenProject, onCreateProject, onRefresh }: AddPanelProps) {
  const [newName, setNewName] = useState('');
  const [newSlug, setNewSlug] = useState('');
  const [slugTouched, setSlugTouched] = useState(false);
  const [createErr, setCreateErr] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [showCreate, setShowCreate] = useState(false);

  const available = serverProjects.filter((p) => !openedSlugs.includes(p.slug));

  const create = async () => {
    const name = newName.trim();
    const slug = newSlug.trim() || slugify(name);
    if (!name) return setCreateErr('Display name is required.');
    const v = validateSlug(slug);
    if (v) return setCreateErr(v);
    setCreating(true);
    setCreateErr(null);
    try {
      await onCreateProject(slug, name);
      setNewName('');
      setNewSlug('');
      setSlugTouched(false);
      setShowCreate(false);
    } catch (e: any) {
      setCreateErr('Create failed: ' + (e?.message || e));
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="mt-2 space-y-2 rounded-md border border-border bg-card/60 p-2.5">
      <div className="flex items-center justify-between">
        <Label className="text-[10px]">Open an existing project</Label>
        <Button variant="ghost" size="icon" className="h-5 w-5" onClick={onRefresh} title="Refresh">
          <RefreshCw className="h-3 w-3" />
        </Button>
      </div>
      {available.length === 0 ? (
        <p className="text-[11px] text-muted-foreground">All server projects are already open.</p>
      ) : (
        <ul className="max-h-40 space-y-1 overflow-y-auto scrollbar-thin">
          {available.map((p) => (
            <li key={p.slug}>
              <button
                onClick={() => onOpenProject(p.slug)}
                className="group flex w-full items-center justify-between rounded border border-border bg-card/40 px-2 py-1.5 text-left transition-colors hover:border-primary/60 hover:bg-card"
              >
                <div className="min-w-0">
                  <div className="truncate text-[12px] font-medium">{p.name || p.slug}</div>
                  <code className="block truncate text-[10px] text-muted-foreground">{p.slug}</code>
                </div>
                <ChevronRight className="h-3.5 w-3.5 flex-none text-muted-foreground group-hover:text-primary" />
              </button>
            </li>
          ))}
        </ul>
      )}

      <Separator />

      {!showCreate ? (
        <Button variant="ghost" size="sm" className="h-7 w-full justify-start gap-1.5 text-[11px]" onClick={() => setShowCreate(true)}>
          <Plus className="h-3 w-3" /> Create a new project
        </Button>
      ) : (
        <div className="space-y-2">
          <div className="space-y-1">
            <Label className="text-[10px]">Display name</Label>
            <Input
              className="h-7 text-[12px]"
              placeholder="e.g. Zecru Games"
              value={newName}
              onChange={(e) => {
                setNewName(e.target.value);
                if (!slugTouched) setNewSlug(slugify(e.target.value));
                setCreateErr(null);
              }}
            />
          </div>
          <div className="space-y-1">
            <Label className="text-[10px]">Slug</Label>
            <Input
              className="h-7 font-mono text-[12px]"
              placeholder="auto"
              value={newSlug}
              onChange={(e) => {
                setSlugTouched(e.target.value.length > 0);
                setNewSlug(e.target.value);
                setCreateErr(null);
              }}
            />
          </div>
          <p className="text-[10px] text-muted-foreground">
            1–42 chars, lowercase letters/digits with internal hyphens.
          </p>
          {createErr && <p className="text-[11px] text-destructive">{createErr}</p>}
          <div className="flex gap-1.5">
            <Button size="sm" className="h-7 text-[11px]" onClick={create} disabled={creating}>
              {creating ? 'Creating…' : 'Create + open'}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-7 text-[11px]"
              onClick={() => {
                setShowCreate(false);
                setCreateErr(null);
              }}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

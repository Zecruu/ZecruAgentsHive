import { useCallback, useEffect, useState } from 'react';
import { Archive, Crown, ExternalLink, FolderOpen, Loader2, Maximize2, MessageSquare, Minimize2, PanelRight, RadioTower, ScrollText, TerminalSquare, Users } from 'lucide-react';
import { ah, type ExportedMission, type MissionsExport, type Project } from '@/lib/agentshive';
import { agentActivity, formatActivity, type ActiveProject, type AgentRuntime, type MessageRuntime } from '@/lib/useActiveProject';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { ChatPane } from './ChatPane';

interface Props {
  project: Project;
  rt: ActiveProject;
  maximized?: boolean;
  onToggleMaximize?: () => void;
  missionsPanelOpen?: boolean;
  onToggleMissionsPanel?: () => void;
}

const SPEC_PREVIEW = 360;

export function HivemindDashboard({ project, rt, maximized, onToggleMaximize, missionsPanelOpen, onToggleMissionsPanel }: Props) {
  const { agents, current } = rt;
  const [missions, setMissions] = useState<MissionsExport | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await ah().missions.export(project.slug);
      setMissions(data);
      setErr(null);
    } catch (e: any) {
      setErr(e?.message || String(e));
    }
  }, [project.slug]);

  useEffect(() => {
    load();
    const t = setInterval(load, 12000);
    return () => clearInterval(t);
  }, [load]);

  if (!current) return null;

  const now = Date.now();
  const activity = agentActivity(current, now);
  const coders = agents.filter((a) => a.role === 'coder');
  const activeMission = missions?.missions?.find((m) => m.status === 'active') || null;
  const recentMissions = (missions?.missions || []).filter((m) => m.status !== 'active').slice(-3).reverse();
  const timeline = agents.flatMap((a) => recentEntries(a)).sort((a, b) => b.at.localeCompare(a.at)).slice(0, 7);

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <header className="flex flex-none items-center justify-between gap-3 rounded-lg border border-border/70 bg-card/75 px-4 py-3 shadow-[0_1px_0_hsl(0_0%_100%/0.04)_inset]">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="flex h-8 w-8 flex-none items-center justify-center rounded-md border border-accent/35 bg-accent/10 text-accent">
              <Crown className="h-4 w-4" />
            </span>
            <div className="min-w-0">
              <div className="flex min-w-0 items-center gap-2">
                <h2 className="truncate text-[16px] font-semibold tracking-tight">{current.label}</h2>
                <Badge variant={current.inFlight ? 'warn' : activity.state === 'err' ? 'err' : 'ok'} className="gap-1">
                  {current.inFlight && <Loader2 className="h-3 w-3 animate-spin" />}
                  {formatActivity(activity)}
                </Badge>
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[10px] text-muted-foreground">
                <InfoPill>{project.slug}</InfoPill>
                <InfoPill>{rt.hostname}</InfoPill>
                {rt.folder && <InfoPill icon={<FolderOpen className="h-3 w-3" />}>{rt.folder}</InfoPill>}
              </div>
            </div>
          </div>
        </div>
        <div className="flex flex-none items-center gap-1.5">
          {onToggleMissionsPanel && !maximized && (
            <Button
              variant="ghost"
              size="icon"
              className={cn('h-8 w-8', missionsPanelOpen && 'text-accent')}
              onClick={onToggleMissionsPanel}
              title={missionsPanelOpen ? 'Hide missions panel' : 'Show missions panel'}
            >
              <PanelRight className="h-4 w-4" />
            </Button>
          )}
          {onToggleMaximize && (
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={onToggleMaximize}
              title={maximized ? 'Exit full-width dashboard' : 'Maximize dashboard'}
            >
              {maximized ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
            </Button>
          )}
          <Button variant="ghost" size="sm" onClick={() => rt.archive(current)}>
            <Archive className="h-3.5 w-3.5" /> Archive
          </Button>
        </div>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1.25fr)_minmax(330px,0.75fr)]">
        <div className="flex min-h-0 flex-col gap-3">
          <div className="grid flex-none grid-cols-1 gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(260px,0.7fr)]">
            <MissionOverview mission={activeMission} err={err} />
            <TimelinePanel entries={timeline} />
          </div>
          <div className="min-h-0 flex-1">
            <ChatPane
              agent={current}
              siblings={agents.filter((a) => a.id !== current.id)}
              onSend={rt.sendTurn}
              onChangeModelEffort={rt.setAgentModelEffort}
              onCancel={rt.cancelTurn}
              onArchive={() => rt.archive(current)}
              onSwitchAgent={(a) => rt.setCurrentId(a.id)}
              projectSlug={project.slug}
              maximized={false}
              onToggleMaximize={onToggleMaximize}
              missionsPanelOpen={missionsPanelOpen}
              onToggleMissionsPanel={onToggleMissionsPanel}
              onQueue={rt.queueMessage}
              onRemoveQueued={rt.removeQueued}
            />
          </div>
        </div>

        <aside className="flex min-h-0 flex-col gap-3">
          <CoderRoster coders={coders} currentId={rt.currentId} onSwitch={(a) => rt.setCurrentId(a.id)} />
          <RecentMissions missions={recentMissions} />
        </aside>
      </div>
    </div>
  );
}

function InfoPill({ children, icon }: { children: React.ReactNode; icon?: React.ReactNode }) {
  return (
    <span className="inline-flex max-w-[280px] items-center gap-1.5 rounded-full border border-border/70 bg-input/45 px-2 py-0.5">
      {icon}
      <span className="truncate">{children}</span>
    </span>
  );
}

function MissionOverview({ mission, err }: { mission: ExportedMission | null; err: string | null }) {
  return (
    <section className="min-h-[174px] rounded-lg border border-border/70 bg-card/70 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="flex items-center gap-1.5 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
          <RadioTower className="h-3.5 w-3.5 text-accent" /> Active Mission
        </h3>
        {mission && <Badge variant="ok">{mission.status}</Badge>}
      </div>
      {err ? (
        <p className="text-xs text-destructive">Failed to load missions: {err}</p>
      ) : mission ? (
        <>
          <div className="text-[15px] font-semibold tracking-tight">{mission.name}</div>
          <p className="mt-2 max-h-24 overflow-hidden whitespace-pre-wrap break-words text-[12px] leading-relaxed text-muted-foreground">
            {preview(mission.spec || '')}
          </p>
          <div className="mt-2 flex items-center gap-2 text-[10px] text-muted-foreground">
            <span>{(mission.summaries || []).length} report{(mission.summaries || []).length === 1 ? '' : 's'}</span>
            <span className="h-1 w-1 rounded-full bg-muted-foreground/50" />
            <span>{mission.mission_id.slice(0, 8)}</span>
          </div>
        </>
      ) : (
        <p className="text-[12px] text-muted-foreground">No active mission loaded yet.</p>
      )}
    </section>
  );
}

function CoderRoster({ coders, currentId, onSwitch }: { coders: AgentRuntime[]; currentId: string | null; onSwitch: (a: AgentRuntime) => void }) {
  return (
    <section className="min-h-0 flex-1 rounded-lg border border-border/70 bg-card/70 p-3">
      <h3 className="mb-2 flex items-center gap-1.5 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
        <Users className="h-3.5 w-3.5 text-primary" /> Coder Roster
      </h3>
      <div className="max-h-full space-y-2 overflow-y-auto pr-1 scrollbar-thin">
        {coders.length === 0 && <p className="text-xs text-muted-foreground">No coder agents in this project.</p>}
        {coders.map((a) => {
          const act = agentActivity(a, Date.now());
          const snippet = lastSnippet(a);
          return (
            <button
              key={a.id}
              onClick={() => onSwitch(a)}
              className={cn(
                'group flex w-full items-start gap-2 rounded-md border border-border/60 bg-input/25 p-2 text-left transition-colors hover:border-primary/40 hover:bg-input/45',
                currentId === a.id && 'border-primary/45 bg-primary/10',
              )}
            >
              <span className="mt-0.5 flex h-7 w-7 flex-none items-center justify-center rounded-md border border-border bg-background/50 text-muted-foreground">
                <TerminalSquare className="h-3.5 w-3.5" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex min-w-0 items-center gap-1.5">
                  <span className="truncate text-[13px] font-medium">{a.label}</span>
                  {a.queue.length > 0 && <Badge variant="warn">{a.queue.length} queued</Badge>}
                </span>
                <span className="block truncate text-[10px] text-muted-foreground">
                  {a.coderId || 'no coder_id'} / {a.cli}{a.model ? ` / ${a.model}` : ''}
                </span>
                <span className={cn('mt-1 flex items-center gap-1 truncate text-[10px]', a.inFlight ? 'text-accent' : 'text-muted-foreground')}>
                  {a.inFlight && <Loader2 className="h-3 w-3 animate-spin" />}
                  {formatActivity(act)}
                </span>
                {snippet && <span className="mt-1 block truncate text-[11px] text-muted-foreground">{snippet}</span>}
              </span>
              <ExternalLink className="mt-1 h-3.5 w-3.5 flex-none text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
            </button>
          );
        })}
      </div>
    </section>
  );
}

function TimelinePanel({ entries }: { entries: TimelineEntry[] }) {
  return (
    <section className="min-h-[174px] rounded-lg border border-border/70 bg-card/70 p-3">
      <h3 className="mb-2 flex items-center gap-1.5 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
        <MessageSquare className="h-3.5 w-3.5 text-primary" /> Recent Activity
      </h3>
      <div className="space-y-1.5">
        {entries.length === 0 && <p className="text-xs text-muted-foreground">No local transcript activity yet.</p>}
        {entries.map((e) => (
          <div key={e.id} className="rounded-md border border-border/50 bg-input/25 px-2 py-1.5">
            <div className="flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
              <span className="truncate font-medium text-foreground">{e.agent}</span>
              <span className="shrink-0">{e.role}</span>
            </div>
            <p className="mt-0.5 truncate text-[11px] text-muted-foreground">{e.text}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function RecentMissions({ missions }: { missions: ExportedMission[] }) {
  return (
    <section className="flex-none rounded-lg border border-border/70 bg-card/70 p-3">
      <h3 className="mb-2 flex items-center gap-1.5 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
        <ScrollText className="h-3.5 w-3.5 text-accent" /> Recent Missions
      </h3>
      <div className="space-y-2">
        {missions.length === 0 && <p className="text-xs text-muted-foreground">Recent missions will appear here after export sync.</p>}
        {missions.map((m) => (
          <div key={m.mission_id} className="rounded-md border border-border/50 bg-input/25 px-2 py-1.5">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 truncate text-[12px] font-medium">{m.name}</div>
              <Badge variant={m.status === 'done' ? 'muted' : 'warn'}>{m.status}</Badge>
            </div>
            <div className="mt-1 text-[10px] text-muted-foreground">{(m.summaries || []).length} report{(m.summaries || []).length === 1 ? '' : 's'}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

interface TimelineEntry {
  id: string;
  at: string;
  agent: string;
  role: string;
  text: string;
}

function recentEntries(agent: AgentRuntime): TimelineEntry[] {
  return agent.messages.slice(-5).map((m, idx) => ({
    id: `${agent.id}:${agent.messages.length - 5 + idx}`,
    at: m.at || String(idx).padStart(3, '0'),
    agent: agent.label,
    role: m.toolCalls?.length ? `${m.toolCalls.length} tool call${m.toolCalls.length === 1 ? '' : 's'}` : m.role,
    text: entryText(m),
  }));
}

function lastSnippet(agent: AgentRuntime): string {
  const msg = [...agent.messages].reverse().find((m) => m.text || m.toolCalls?.length);
  return msg ? entryText(msg) : '';
}

function entryText(m: MessageRuntime): string {
  if (m.text) return preview(m.text.replace(/\s+/g, ' '), 140);
  if (m.toolCalls?.length) return m.toolCalls.map((tc) => tc.name.split('__').pop() || tc.name).join(', ');
  return '';
}

function preview(s: string, limit = SPEC_PREVIEW): string {
  return s.length > limit ? s.slice(0, limit - 1) + '...' : s;
}

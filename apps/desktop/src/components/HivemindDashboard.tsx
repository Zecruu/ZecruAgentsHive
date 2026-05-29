import { useCallback, useEffect, useState } from 'react';
import { Archive, Crown, FolderOpen, Loader2, Maximize2, Minimize2, PanelRight, RadioTower } from 'lucide-react';
import { ah, type ExportedMission, type MissionsExport, type Project } from '@/lib/agentshive';
import { agentActivity, formatActivity, type ActiveProject } from '@/lib/useActiveProject';
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

const SPEC_PREVIEW = 520;

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

  const activity = agentActivity(current, Date.now());
  const activeMission = missions?.missions?.find((m) => m.status === 'active') || null;

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

      <MissionOverview mission={activeMission} err={err} />

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
    <section className="flex-none rounded-lg border border-border/70 bg-card/70 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="flex items-center gap-1.5 text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
          <RadioTower className="h-3.5 w-3.5 text-accent" /> Active Mission
        </h3>
        {mission && <Badge variant="ok">{mission.status}</Badge>}
      </div>
      {err ? (
        <p className="text-xs text-destructive">Failed to load missions: {err}</p>
      ) : mission ? (
        <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto]">
          <div className="min-w-0">
            <div className="truncate text-[15px] font-semibold tracking-tight">{mission.name}</div>
            <p className="mt-1 max-h-20 overflow-hidden whitespace-pre-wrap break-words text-[12px] leading-relaxed text-muted-foreground">
              {preview(mission.spec || '')}
            </p>
          </div>
          <div className="flex flex-wrap items-start gap-2 text-[10px] text-muted-foreground lg:justify-end">
            <InfoPill>{(mission.summaries || []).length} report{(mission.summaries || []).length === 1 ? '' : 's'}</InfoPill>
            <InfoPill>{mission.mission_id.slice(0, 8)}</InfoPill>
          </div>
        </div>
      ) : (
        <p className="text-[12px] text-muted-foreground">No active mission loaded yet.</p>
      )}
    </section>
  );
}

function preview(s: string): string {
  return s.length > SPEC_PREVIEW ? s.slice(0, SPEC_PREVIEW - 1) + '...' : s;
}

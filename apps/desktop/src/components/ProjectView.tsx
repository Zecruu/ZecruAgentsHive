// ProjectView — the active project's MAIN PANE (launcher or chat).
//
// All runtime (agent state, IPC subscriptions, poll loop, auto-wake) now lives
// in the useActiveProject hook, owned by Workspace. This component is purely
// presentational: given the active project + the hook result, it renders the
// LauncherForm (when no agent is selected) or the ChatPane for the current
// agent. The sidebar is rendered separately by Workspace via ProjectSidebar.

import type { Project } from '@/lib/agentshive';
import type { ActiveProject } from '@/lib/useActiveProject';
import { cn } from '@/lib/utils';
import { LauncherForm } from './LauncherForm';
import { ChatPane } from './ChatPane';

interface Props {
  project: Project;
  rt: ActiveProject;
  maximized?: boolean;
  onToggleMaximize?: () => void;
  missionsPanelOpen?: boolean;
  onToggleMissionsPanel?: () => void;
}

export function ProjectView({ project, rt, maximized, onToggleMaximize, missionsPanelOpen, onToggleMissionsPanel }: Props) {
  const { agents, current, showLauncher } = rt;

  return (
    <section className="relative flex h-full flex-col overflow-hidden">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(800px_600px_at_80%_0%,hsl(var(--primary)/0.04),transparent_60%)]" />
      <div className={cn('relative flex h-full flex-col', maximized ? 'p-0' : 'p-3')}>
        {showLauncher || !current ? (
          <div className="overflow-y-auto scrollbar-thin">
            <LauncherForm
              project={project}
              folder={rt.folder}
              hostname={rt.hostname}
              onLaunch={rt.createAgent}
              onPickFolder={rt.pickFolder}
            />
          </div>
        ) : (
          <ChatPane
            agent={current}
            siblings={agents.filter((a) => a.id !== current.id)}
            onSend={rt.sendTurn}
            onChangeModelEffort={rt.setAgentModelEffort}
            onCancel={rt.cancelTurn}
            onArchive={() => rt.archive(current)}
            onSwitchAgent={(a) => rt.setCurrentId(a.id)}
            projectSlug={project.slug}
            maximized={maximized}
            onToggleMaximize={onToggleMaximize}
            missionsPanelOpen={missionsPanelOpen}
            onToggleMissionsPanel={onToggleMissionsPanel}
            onQueue={rt.queueMessage}
            onRemoveQueued={rt.removeQueued}
          />
        )}
      </div>
    </section>
  );
}

// Workspace — the always-on multi-project shell.
//
// Owns the VS-Code-style workspace state: which projects are opened in the
// sidebar, their collapse state, and which one is active (DISPLAYED).
//
// Each OPEN project gets its own persistent runtime via a <ProjectRuntimeHost>
// (one useActiveProject per open project), so an in-flight turn SURVIVES a
// project switch — switching only changes which runtime is displayed, it does
// NOT tear down the others' agents/subprocesses. A project's runtime is torn
// down only when the project is CLOSED (the host unmounts). Hosts render nothing;
// they publish their live runtime into a registry, and the chrome (sidebar +
// main + missions panel) is rendered here from the ACTIVE project's runtime.
// Exactly one host is isActive at a time (the single-active web invariant).

import { memo, useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { Compass } from 'lucide-react';
import { ah, unwrapProjects, type Project } from '@/lib/agentshive';
import { useActiveProject, type ActiveProject, type AgentRuntime } from '@/lib/useActiveProject';
import { ProjectSidebar } from './ProjectSidebar';
import { ProjectView } from './ProjectView';
import { MissionsPanel } from './MissionsPanel';

// Stable empty roster so the sidebar gets a referentially-stable array when no
// project is active (avoids a new [] each render).
const EMPTY_AGENTS: AgentRuntime[] = [];

export function Workspace() {
  const [loaded, setLoaded] = useState(false);
  const [serverProjects, setServerProjects] = useState<Project[]>([]);
  const [openedSlugs, setOpenedSlugs] = useState<string[]>([]);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [activeSlug, setActiveSlug] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const bumpRefresh = useCallback(() => setRefreshKey((n) => n + 1), []);
  // Full-width chat mode: hide the sidebar so the chat fills the window. Cheap
  // persistence via localStorage.
  const [chatMaximized, setChatMaximized] = useState<boolean>(() => {
    try { return localStorage.getItem('ah:chatMaximized') === '1'; } catch { return false; }
  });
  const toggleMaximize = useCallback(() => {
    setChatMaximized((m) => {
      const next = !m;
      try { localStorage.setItem('ah:chatMaximized', next ? '1' : '0'); } catch {}
      return next;
    });
  }, []);
  // Right-side missions panel (default open). Hidden when chat is maximized.
  const [missionsPanelOpen, setMissionsPanelOpen] = useState<boolean>(() => {
    try { return localStorage.getItem('ah:missionsPanel') !== '0'; } catch { return true; }
  });
  const toggleMissionsPanel = useCallback(() => {
    setMissionsPanelOpen((o) => {
      const next = !o;
      try { localStorage.setItem('ah:missionsPanel', next ? '1' : '0'); } catch {}
      return next;
    });
  }, []);

  // Resolve opened slugs to full Project objects (server metadata when known,
  // otherwise a minimal fallback so an offline/unknown slug still renders).
  const openedProjects = useMemo<Project[]>(
    () =>
      openedSlugs.map(
        (slug) => serverProjects.find((p) => p.slug === slug) || { slug, name: slug },
      ),
    [openedSlugs, serverProjects],
  );

  const activeProject = useMemo<Project | null>(
    () => (activeSlug ? openedProjects.find((p) => p.slug === activeSlug) || { slug: activeSlug, name: activeSlug } : null),
    [activeSlug, openedProjects],
  );

  // Registry of every OPEN project's live runtime, published by its
  // <ProjectRuntimeHost>. The chrome below renders from the ACTIVE project's
  // entry. A host re-renders this Workspace (via onActiveRender) whenever the
  // active runtime changes, so streaming + sidebar live-status stay current
  // without re-rendering the (memoized) background hosts.
  const rtRegistry = useRef<Map<string, ActiveProject>>(new Map());
  const [, forceRender] = useReducer((n: number) => n + 1, 0);
  const registerRt = useCallback((slug: string, rt: ActiveProject) => {
    rtRegistry.current.set(slug, rt);
  }, []);
  const unregisterRt = useCallback((slug: string) => {
    rtRegistry.current.delete(slug);
  }, []);
  const onActiveRender = useCallback(() => forceRender(), []);
  const activeRt = activeSlug ? rtRegistry.current.get(activeSlug) ?? null : null;

  // --- initial load: workspace state + server project list ---
  useEffect(() => {
    (async () => {
      const [ws, projsRaw] = await Promise.all([
        ah().workspace.get().catch(() => ({ openedProjects: [], collapsed: {}, lastActive: null })),
        ah().projects.list().then(unwrapProjects).catch(() => [] as Project[]),
      ]);
      setServerProjects(projsRaw);
      setOpenedSlugs(Array.isArray(ws.openedProjects) ? ws.openedProjects : []);
      setCollapsed(ws.collapsed || {});
      // Restore the last active project ONLY if it's still opened. Otherwise
      // start with no active runtime (guardrail: don't auto-activate / no
      // surprise poll loops on a fresh start).
      const la = ws.lastActive && (ws.openedProjects || []).includes(ws.lastActive) ? ws.lastActive : null;
      setActiveSlug(la);
      setLoaded(true);
    })();
  }, []);

  // --- persist workspace state (after initial load) ---
  useEffect(() => {
    if (loaded) ah().workspace.set({ openedProjects: openedSlugs }).catch(() => {});
  }, [loaded, openedSlugs]);
  useEffect(() => {
    if (loaded) ah().workspace.set({ collapsed }).catch(() => {});
  }, [loaded, collapsed]);
  useEffect(() => {
    if (loaded) ah().workspace.set({ lastActive: activeSlug }).catch(() => {});
  }, [loaded, activeSlug]);

  // --- handlers ---
  const expand = (slug: string) => setCollapsed((c) => ({ ...c, [slug]: false }));

  const handleToggleCollapse = (slug: string) =>
    setCollapsed((c) => ({ ...c, [slug]: !c[slug] }));

  const handleSelectProject = (slug: string) => {
    expand(slug);
    if (slug !== activeSlug) setActiveSlug(slug);
  };

  const handleSelectAgent = (slug: string, agentId: string) => {
    expand(slug);
    // Every open project's host is already mounted with its agents loaded, so we
    // set the target runtime's selection directly (no requestSelect-on-load dance).
    // The sidebar only renders agent rows for mounted projects, so the target is
    // always registered here.
    const target = rtRegistry.current.get(slug);
    if (target) {
      target.setCurrentId(agentId);
      target.setShowLauncher(false);
    }
    if (slug !== activeSlug) setActiveSlug(slug);
  };

  const handleNewAgent = (slug: string) => {
    expand(slug);
    const target = rtRegistry.current.get(slug);
    if (target) {
      target.setShowLauncher(true);
      target.setCurrentId(null);
    }
    if (slug !== activeSlug) setActiveSlug(slug);
  };

  const handleWakeActive = (agentId: string) => {
    if (!activeRt) return;
    const a = activeRt.agents.find((x) => x.id === agentId);
    if (a) activeRt.wakeAgent(a, 'manual');
  };

  const handleArchiveActive = (agentId: string) => {
    if (!activeRt) return;
    const a = activeRt.agents.find((x) => x.id === agentId);
    if (a) activeRt.archive(a);
  };

  const handleOpenProject = (slug: string) => {
    if (!openedSlugs.includes(slug)) setOpenedSlugs((prev) => [...prev, slug]);
    expand(slug);
    setActiveSlug(slug);
    bumpRefresh();
  };

  const handleCreateProject = async (slug: string, name: string) => {
    await ah().projects.create(slug, name);
    const projsRaw = await ah().projects.list().then(unwrapProjects).catch(() => serverProjects);
    setServerProjects(projsRaw);
    if (!openedSlugs.includes(slug)) setOpenedSlugs((prev) => [...prev, slug]);
    expand(slug);
    setActiveSlug(slug);
    bumpRefresh();
  };

  const handleCloseProject = (slug: string) => {
    setOpenedSlugs((prev) => prev.filter((s) => s !== slug));
    // Closing the active project deactivates it — the hook then tears down its
    // subscriptions + cancels in-flight chats. Agents stay on disk; reopening
    // the project restores them.
    if (slug === activeSlug) setActiveSlug(null);
    setCollapsed((c) => {
      const next = { ...c };
      delete next[slug];
      return next;
    });
    bumpRefresh();
  };

  const refreshServerProjects = async () => {
    const projsRaw = await ah().projects.list().then(unwrapProjects).catch(() => serverProjects);
    setServerProjects(projsRaw);
  };

  if (!loaded) {
    return <div className="flex h-full items-center justify-center text-sm text-muted-foreground">Loading workspace…</div>;
  }

  // Hide the sidebar only while actively focused on a chat in full-width mode —
  // so navigation is always available when not in a chat.
  const sidebarHidden = chatMaximized && !!activeProject && !!activeRt?.current && !activeRt?.showLauncher;

  return (
    <div className="relative flex h-full">
      {/* Persistent per-project runtimes. Each stays mounted while its project is
          open (so an in-flight turn survives switching away); they render nothing
          and publish into rtRegistry. Exactly one is isActive. */}
      {openedProjects.map((p) => (
        <ProjectRuntimeHost
          key={p.slug}
          project={p}
          isActive={p.slug === activeSlug}
          registerRt={registerRt}
          unregisterRt={unregisterRt}
          onActiveRender={onActiveRender}
        />
      ))}

      {!sidebarHidden && (
      <ProjectSidebar
        projects={openedProjects}
        activeSlug={activeSlug}
        activeCurrentId={activeRt?.currentId ?? null}
        collapsed={collapsed}
        activeAgents={activeRt?.agents ?? EMPTY_AGENTS}
        activeFolder={activeRt?.folder ?? null}
        serverProjects={serverProjects}
        refreshKey={refreshKey}
        onToggleCollapse={handleToggleCollapse}
        onSelectProject={handleSelectProject}
        onSelectAgent={handleSelectAgent}
        onNewAgent={handleNewAgent}
        onWakeActive={handleWakeActive}
        onArchiveActive={handleArchiveActive}
        onOpenProject={handleOpenProject}
        onCreateProject={handleCreateProject}
        onCloseProject={handleCloseProject}
        onOpenDashboard={(slug) => ah().dashboard.open(slug)}
        onPickFolder={() => activeRt?.pickFolder()}
        onClearFolder={() => activeRt?.clearFolder()}
        onRefreshServerProjects={refreshServerProjects}
      />
      )}

      <main className="relative flex-1 overflow-hidden">
        {activeProject && activeRt ? (
          <ProjectView
            project={activeProject}
            rt={activeRt}
            maximized={chatMaximized}
            onToggleMaximize={toggleMaximize}
            missionsPanelOpen={missionsPanelOpen}
            onToggleMissionsPanel={toggleMissionsPanel}
          />
        ) : (
          <EmptyState hasProjects={openedProjects.length > 0} />
        )}
      </main>

      {/* Right-side missions panel — hidden in full-width chat mode (P3). */}
      {activeProject && activeRt && missionsPanelOpen && !chatMaximized && (
        <MissionsPanel
          projectSlug={activeProject.slug}
          refreshKey={refreshKey}
          onClose={toggleMissionsPanel}
        />
      )}
      {activeProject && activeRt && !missionsPanelOpen && !chatMaximized && (
        <button
          type="button"
          onClick={toggleMissionsPanel}
          className="absolute right-2 top-1/2 z-20 flex -translate-y-1/2 items-center gap-1.5 rounded-full border border-border/70 bg-card/80 px-2.5 py-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground shadow-[0_8px_28px_-18px_hsl(0_0%_0%/0.55)] backdrop-blur transition-colors hover:border-accent/45 hover:bg-card hover:text-accent"
          title="Show missions panel"
        >
          <Compass className="h-3.5 w-3.5" />
          <span className="[writing-mode:vertical-rl]">Missions</span>
        </button>
      )}
    </div>
  );
}

// Persistent runtime for ONE open project. Renders nothing — it just runs the
// project's useActiveProject hook (kept alive while the project is open) and
// publishes the live runtime into the Workspace registry. memo'd so a Workspace
// re-render (e.g. the active host's onActiveRender bump) does NOT re-render the
// other hosts: only this host's OWN streaming state or an isActive/project change
// re-renders it. That memo is what breaks the bump → re-render loop.
const ProjectRuntimeHost = memo(function ProjectRuntimeHost({
  project,
  isActive,
  registerRt,
  unregisterRt,
  onActiveRender,
}: {
  project: Project;
  isActive: boolean;
  registerRt: (slug: string, rt: ActiveProject) => void;
  unregisterRt: (slug: string) => void;
  onActiveRender: () => void;
}) {
  const rt = useActiveProject(project, isActive);
  // Publish on every render so the chrome reads the latest. When this is the
  // active project, nudge Workspace to re-render (the hook re-renders THIS host
  // on each stream event; this propagates that up to the displayed chrome).
  useEffect(() => {
    registerRt(project.slug, rt);
    if (isActive) onActiveRender();
  });
  // Drop from the registry on unmount (project closed). The hook's own
  // [slug]-effect cleanup tree-kills any in-flight subprocess on the same unmount.
  useEffect(() => {
    const slug = project.slug;
    return () => unregisterRt(slug);
  }, [project.slug, unregisterRt]);
  return null;
});

function EmptyState({ hasProjects }: { hasProjects: boolean }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center">
      <div className="text-sm font-medium text-foreground">
        {hasProjects ? 'No project active' : 'No projects open'}
      </div>
      <p className="max-w-sm text-xs text-muted-foreground">
        {hasProjects
          ? 'Pick a project in the sidebar to make it live, or click an agent to open its chat.'
          : 'Use “Open / New project” in the sidebar to open an existing project or create one.'}
      </p>
    </div>
  );
}

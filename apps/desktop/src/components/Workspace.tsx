// Workspace — the always-on multi-project shell.
//
// Owns the VS-Code-style workspace state: which projects are opened in the
// sidebar, their collapse state, and which one is active. Exactly ONE project
// is active at a time; its live runtime comes from useActiveProject(active).
// The sidebar (ProjectSidebar) renders every opened project read-only and only
// the active one streams. Switching projects tears down the previous runtime
// before the next spins up (the hook handles teardown on slug change).

import { useCallback, useEffect, useMemo, useState } from 'react';
import { ah, unwrapProjects, type Project } from '@/lib/agentshive';
import { useActiveProject } from '@/lib/useActiveProject';
import { ProjectSidebar } from './ProjectSidebar';
import { ProjectView } from './ProjectView';
import { MissionsPanel } from './MissionsPanel';

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

  // The single live runtime — keyed to the active project inside the hook.
  const rt = useActiveProject(activeProject);

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
    if (slug === activeSlug) {
      rt.setCurrentId(agentId);
      rt.setShowLauncher(false);
    } else {
      rt.requestSelect({ kind: 'agent', id: agentId });
      setActiveSlug(slug);
    }
  };

  const handleNewAgent = (slug: string) => {
    expand(slug);
    if (slug === activeSlug) {
      rt.setShowLauncher(true);
      rt.setCurrentId(null);
    } else {
      rt.requestSelect({ kind: 'launcher' });
      setActiveSlug(slug);
    }
  };

  const handleWakeActive = (agentId: string) => {
    const a = rt.agents.find((x) => x.id === agentId);
    if (a) rt.wakeAgent(a, 'manual');
  };

  const handleArchiveActive = (agentId: string) => {
    const a = rt.agents.find((x) => x.id === agentId);
    if (a) rt.archive(a);
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
  const sidebarHidden = chatMaximized && !!activeProject && !!rt.current && !rt.showLauncher;

  return (
    <div className="flex h-full">
      {!sidebarHidden && (
      <ProjectSidebar
        projects={openedProjects}
        activeSlug={activeSlug}
        activeCurrentId={rt.currentId}
        collapsed={collapsed}
        activeAgents={rt.agents}
        activeFolder={rt.folder}
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
        onPickFolder={rt.pickFolder}
        onClearFolder={rt.clearFolder}
        onRefreshServerProjects={refreshServerProjects}
      />
      )}

      <main className="relative flex-1 overflow-hidden">
        {activeProject ? (
          <ProjectView
            project={activeProject}
            rt={rt}
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
      {activeProject && missionsPanelOpen && !chatMaximized && (
        <MissionsPanel
          projectSlug={activeProject.slug}
          refreshKey={refreshKey}
          onClose={toggleMissionsPanel}
        />
      )}
    </div>
  );
}

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

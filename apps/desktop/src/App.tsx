import { useEffect, useState, type ReactNode } from 'react';
import { Settings as SettingsIcon, LogOut, ShieldAlert, LogIn, RefreshCw } from 'lucide-react';
import { ah, type ConfigState } from '@/lib/agentshive';
import { onAuthChange, signOut, supabaseConfigured } from '@/lib/supabase';
import type { Session } from '@supabase/supabase-js';
import { Settings } from '@/components/Settings';
import { Workspace } from '@/components/Workspace';
import { SignIn } from '@/components/SignIn';
import { AdminPanel } from '@/components/AdminPanel';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';

type View = 'settings' | 'workspace' | 'admin' | 'signin';

export default function App() {
  const [view, setView] = useState<View>('workspace');
  const [config, setConfig] = useState<ConfigState | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [authReady, setAuthReady] = useState(!supabaseConfigured);
  const [settingsReturn, setSettingsReturn] = useState<View>('workspace');
  // Auto-update state. `updateReady` flips true once a new version finished
  // downloading (electron-updater, packaged builds only — no events fire in dev,
  // so this control never shows there). `downloadPct` drives a subtle progress
  // hint while it downloads.
  const [updateReady, setUpdateReady] = useState(false);
  const [downloadPct, setDownloadPct] = useState<number | null>(null);
  // The running app's version (from the main process), shown in the header
  // badge so it always matches the deployed release — never a hardcoded string.
  const [appVersion, setAppVersion] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      const c = await ah().config.get();
      setConfig(c);
    })();
    ah().app.version().then(setAppVersion).catch(() => {});
    const off = onAuthChange((s) => {
      setSession(s);
      setAuthReady(true);
      // A Supabase session arriving while the sign-in screen is up means the
      // operator just signed in — drop them into the workspace.
      if (s) setView((v) => (v === 'signin' ? 'workspace' : v));
    });
    return off;
  }, []);

  // Subscribe to auto-update events from the main process. In dev these never
  // fire (updater is gated on app.isPackaged), so the control stays hidden.
  useEffect(() => {
    const u = ah().updates;
    if (!u) return;
    const offProgress = u.onProgress((p) => {
      if (typeof p.percent === 'number') setDownloadPct(p.percent);
    });
    const offDownloaded = u.onDownloaded(() => { setUpdateReady(true); setDownloadPct(null); });
    return () => { offProgress(); offDownloaded(); };
  }, []);

  const reloadConfig = async () => {
    const c = await ah().config.get();
    setConfig(c);
    return c;
  };

  // Authenticated via Supabase OR via the legacy shared key (transitional).
  const authed = Boolean(session) || Boolean(config?.legacyKeyEnabled && config?.apiKeyConfigured);
  // Admin = verified role in the session's app_metadata (set server-side via the
  // admin API). UI gating only — the server independently enforces is_admin().
  const isAdmin = ((session?.user?.app_metadata as Record<string, unknown> | undefined)?.role) === 'admin';

  const identity = session?.user?.email
    ? session.user.email
    : config?.legacyKeyEnabled && config?.apiKeyConfigured
      ? `legacy · ${shortHost(config.baseUrl)}`
      : null;

  const status = !config
    ? { text: 'connecting…', tone: 'muted' as const }
    : authed
      ? { text: identity ? `connected · ${identity}` : 'connected', tone: 'ok' as const }
      : { text: 'sign in to start', tone: 'err' as const };

  const handleSignOut = async () => {
    await signOut();
    setSession(null);
  };

  return (
    <div className="flex h-full flex-col">
      <header className="relative z-10 flex flex-none items-center justify-between border-b border-border/60 glass px-5 py-2.5">
        <div className="flex items-center gap-2 text-sm font-semibold tracking-tight">
          <img src="./icon.png" alt="" className="h-5 w-5" />
          <span>AgentsHive</span>
          <Badge variant="outline" className="ml-1 text-[10px] normal-case">
            {appVersion ? `v${appVersion}` : '…'}
          </Badge>
        </div>
        <div className="flex items-center gap-2">
          {updateReady ? (
            <Button
              size="sm"
              onClick={() => ah().updates.quitAndInstall()}
              title="A new version was downloaded — restart to install it"
              className="gap-1.5"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              Restart to update
            </Button>
          ) : downloadPct !== null ? (
            <Badge variant="muted" className="normal-case">
              Downloading update… {Math.round(downloadPct)}%
            </Badge>
          ) : null}
          <Badge variant={status.tone === 'ok' ? 'ok' : status.tone === 'err' ? 'err' : 'muted'} className="normal-case">
            {status.text}
          </Badge>
          {isAdmin && (
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setView(view === 'admin' ? 'workspace' : 'admin')}
              title="Admin"
              className={cn(view === 'admin' && 'text-accent')}
            >
              <ShieldAlert className="h-4 w-4" />
            </Button>
          )}
          {/* Discoverable Supabase sign-in: when legacy-authed (no Supabase
              session) the workspace renders straight through, so there's
              otherwise no way to reach the sign-in screen. Surface it here. */}
          {supabaseConfigured && authed && !session && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => { setSettingsReturn(view === 'signin' ? 'workspace' : view); setView('signin'); }}
              title="Sign in with Supabase"
              className={cn(view === 'signin' && 'border-accent text-accent')}
            >
              <LogIn className="h-4 w-4" /> Sign in
            </Button>
          )}
          {session && (
            <Button variant="ghost" size="icon" onClick={handleSignOut} title="Sign out">
              <LogOut className="h-4 w-4" />
            </Button>
          )}
          <Button
            variant="ghost"
            size="icon"
            onClick={() => {
              setSettingsReturn(view);
              setView('settings');
            }}
            title="Settings"
          >
            <SettingsIcon className="h-4 w-4" />
          </Button>
        </div>
      </header>

      <main className="relative z-0 flex-1 overflow-hidden">
        {!authReady || !config ? (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">connecting…</div>
        ) : !authed ? (
          // Hard auth gate: there's no workspace to preserve yet, so the
          // sign-in / legacy-key-entry screens are full-screen replacements.
          view === 'settings' ? (
            <div className="h-full overflow-y-auto scrollbar-thin">
              <Settings
                config={config}
                firstRun={!(config.legacyKeyEnabled && config.apiKeyConfigured) && !session}
                onSaved={async () => { await reloadConfig(); setView('workspace'); }}
                onCancel={() => setView('workspace')}
              />
            </div>
          ) : (
            <SignIn
              legacyKeyEnabled={config.legacyKeyEnabled}
              onUseLegacyKey={() => { setSettingsReturn('workspace'); setView('settings'); }}
            />
          )
        ) : (
          // Authed: the Workspace stays MOUNTED so its useActiveProject runtime
          // (and any in-flight agent turn) survives. Settings / Sign-in / Admin
          // render as OVERLAYS above it — they never unmount the workspace, so
          // opening them no longer cancels running agents. The overlay sits
          // inside <main> (below the z-10 header), so the header stays usable.
          <>
            <Workspace />
            {view === 'settings' && (
              <Overlay>
                <Settings
                  config={config}
                  firstRun={false}
                  onSaved={async () => {
                    await reloadConfig();
                    setView(settingsReturn === 'settings' ? 'workspace' : settingsReturn);
                  }}
                  onCancel={() => setView(settingsReturn === 'settings' ? 'workspace' : settingsReturn)}
                />
              </Overlay>
            )}
            {view === 'signin' && (
              <Overlay>
                <SignIn
                  legacyKeyEnabled={config.legacyKeyEnabled}
                  onUseLegacyKey={() => { setSettingsReturn('workspace'); setView('settings'); }}
                  onBack={() => setView('workspace')}
                />
              </Overlay>
            )}
            {view === 'admin' && isAdmin && (
              <Overlay>
                <AdminPanel />
              </Overlay>
            )}
          </>
        )}
      </main>
    </div>
  );
}

// A full-area scrim layered above the (still-mounted) Workspace. Positioned
// inside <main>, so it covers the workspace but not the z-10 header — the
// header controls (Settings gear, Admin, Sign-in, Sign-out) stay clickable.
function Overlay({ children }: { children: ReactNode }) {
  return (
    <div className="absolute inset-0 z-20 overflow-y-auto scrollbar-thin bg-background/95 backdrop-blur-sm">
      {children}
    </div>
  );
}

function shortHost(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

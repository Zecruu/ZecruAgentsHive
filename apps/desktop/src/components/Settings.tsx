import { useEffect, useState } from 'react';
import { CheckCircle2, CloudOff, Cloud, Copy, Github, KeyRound, RefreshCw, Train, Triangle, XCircle } from 'lucide-react';
import { ah, type AgentTokenLite, type ConfigState, type Entitlements, type ToolStatus } from '@/lib/agentshive';
import { getEntitlements, getOptIn, setOptIn } from '@/lib/cloudSync';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import { Separator } from '@/components/ui/separator';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';

interface Props {
  config: ConfigState;
  firstRun: boolean;
  onSaved: () => void;
  onCancel: () => void;
}

export function Settings({ config, firstRun, onSaved, onCancel }: Props) {
  const [baseUrl, setBaseUrl] = useState(config.baseUrl);
  const [apiKey, setApiKey] = useState('');
  const [osHint, setOsHint] = useState(config.defaultOsHint || '');
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const save = async () => {
    setSaving(true);
    setErr(null);
    try {
      const patch: { baseUrl?: string; apiKey?: string; defaultOsHint?: string | null } = {
        baseUrl,
        defaultOsHint: osHint || null,
      };
      if (apiKey.trim()) patch.apiKey = apiKey;
      const res = await ah().config.set(patch);
      if (!res.apiKeyConfigured) {
        setErr('API key still missing.');
        setSaving(false);
        return;
      }
      onSaved();
    } catch (e: any) {
      setErr('Save failed: ' + (e?.message || e));
      setSaving(false);
    }
  };

  return (
    <div className="mx-auto w-full max-w-2xl overflow-y-auto p-8 scrollbar-thin">
      <Card>
        <CardHeader>
          <CardTitle>Connect to AgentsHive</CardTitle>
          <CardDescription>
            Point this app at your AgentsHive server. Your API key never leaves this machine — it's stored in your app data folder.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-2">
            <Label htmlFor="baseUrl">Server URL</Label>
            <Input
              id="baseUrl"
              type="url"
              placeholder="https://agentshive-production.up.railway.app"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="apiKey">API key</Label>
            <Input
              id="apiKey"
              type="password"
              placeholder={config.apiKeyConfigured ? `current: ${config.apiKeyMasked} (leave blank to keep)` : 'paste AGENTSHIVE_API_KEY'}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label>Default OS hint</Label>
            <Select value={osHint || '__default'} onValueChange={(v) => setOsHint(v === '__default' ? '' : v)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__default">(detect)</SelectItem>
                <SelectItem value="windows">windows</SelectItem>
                <SelectItem value="macos">macos</SelectItem>
                <SelectItem value="linux">linux</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {err && <p className="text-sm text-destructive">{err}</p>}
          <div className="flex gap-3 pt-2">
            <Button onClick={save} disabled={saving}>
              {saving ? 'Saving…' : 'Save'}
            </Button>
            {!firstRun && (
              <Button variant="ghost" onClick={onCancel}>
                Cancel
              </Button>
            )}
          </div>

          <Separator className="my-2" />

          <CloudSyncSection />

          <Separator className="my-2" />

          <AgentTokensSection />

          <Separator className="my-2" />

          <ToolsSection />
        </CardContent>
      </Card>
    </div>
  );
}

// v2.x long-lived agent tokens (`ahat_`). The spawned Coder MCP subprocess uses
// the operator's agent token instead of the 1h Supabase JWT (which was silently
// dying every hour). Renderer never sees the secret EXCEPT in the mint response
// — copy-once modal, then the plaintext is gone (only metadata stays in the list).
function AgentTokensSection() {
  const [tokens, setTokens] = useState<AgentTokenLite[] | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [minted, setMinted] = useState<{ label: string; token: string; prefix: string } | null>(null);
  const [copied, setCopied] = useState(false);

  const refresh = async () => {
    setBusy(true);
    setLoadErr(null);
    try {
      const res = await ah().agentTokens.list();
      setTokens(res.tokens || []);
      if (res.error) setLoadErr(res.error);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => { refresh(); }, []);

  const mint = async () => {
    setBusy(true);
    try {
      const res = await ah().agentTokens.mint();
      if (res.ok && res.token) {
        setMinted({ label: res.label || 'token', token: res.token, prefix: res.prefix || '' });
        await refresh();
      } else {
        setLoadErr(res.error || 'mint failed');
      }
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (id: string) => {
    setBusy(true);
    try {
      const res = await ah().agentTokens.revoke(id);
      if (!res.ok) setLoadErr(res.error || 'revoke failed');
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const copyToken = async () => {
    if (!minted) return;
    try {
      await navigator.clipboard.writeText(minted.token);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* clipboard unavailable */ }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <Label className="flex items-center gap-1.5">
            <KeyRound className="h-3.5 w-3.5" />
            Agent tokens
          </Label>
          <p className="mt-1 text-xs text-muted-foreground normal-case tracking-normal">
            Long-lived bearers for coder agents — replace the 1h Supabase token so coders don't time out. Tenant-bound, never expires. Auto-minted on sign-in.
          </p>
        </div>
        <div className="flex items-center gap-1.5">
          <Button size="sm" variant="ghost" onClick={refresh} disabled={busy}>
            <RefreshCw className={busy ? 'h-3.5 w-3.5 animate-spin' : 'h-3.5 w-3.5'} />
          </Button>
          <Button size="sm" onClick={mint} disabled={busy}>
            <KeyRound className="h-3.5 w-3.5" /> Mint new
          </Button>
        </div>
      </div>

      {loadErr && <p className="text-[11px] text-destructive">{loadErr}</p>}

      {tokens === null ? (
        <div className="text-[12px] text-muted-foreground">Loading…</div>
      ) : tokens.length === 0 ? (
        <div className="rounded-md border border-border/60 bg-card/40 px-3 py-2.5 text-[12px] text-muted-foreground">
          No tokens yet. Sign in with Supabase and one mints automatically.
        </div>
      ) : (
        <ul className="space-y-1.5">
          {tokens.map((t) => (
            <li key={t.id} className="flex items-center gap-3 rounded-md border border-border/60 bg-card/40 px-3 py-2.5 text-sm">
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="truncate font-medium">{t.label}</span>
                  <code className="shrink-0 rounded bg-input/60 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">ahat_{t.prefix}…</code>
                  {t.revoked && <Badge variant="muted" className="normal-case">revoked</Badge>}
                </div>
                <div className="truncate text-[11px] text-muted-foreground">
                  {t.last_used_at ? `last used ${new Date(t.last_used_at).toLocaleString()}` : 'never used'}
                  {t.created_at ? ` · created ${new Date(t.created_at).toLocaleDateString()}` : ''}
                </div>
              </div>
              {!t.revoked && (
                <Button size="sm" variant="ghost" onClick={() => revoke(t.id)} disabled={busy}>
                  Revoke
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}

      {minted && (
        <div className="rounded-md border border-accent/40 bg-accent/5 p-3 text-sm">
          <div className="mb-2 flex items-center gap-2">
            <KeyRound className="h-3.5 w-3.5 text-accent" />
            <span className="font-medium">New token: {minted.label}</span>
          </div>
          <p className="mb-2 text-[11px] text-muted-foreground">
            Copy this now — you won't see the full value again. It's already stored on this machine and your coders will use it; this is for backups + other machines.
          </p>
          <div className="flex items-center gap-2 rounded border border-border bg-input/60 p-2">
            <code className="min-w-0 flex-1 select-all break-all font-mono text-[11px]">{minted.token}</code>
            <Button size="sm" variant="ghost" onClick={copyToken}>
              <Copy className="h-3.5 w-3.5" /> {copied ? 'Copied' : 'Copy'}
            </Button>
          </div>
          <div className="mt-2 flex justify-end">
            <Button size="sm" variant="ghost" onClick={() => setMinted(null)}>
              Done
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

// Opt-in Cloud Sync toggle. Enabled only when the signed-in tenant is entitled
// (cloud_sync resolved server-side). Default OFF even when entitled — explicit
// opt-in. The choice persists per-tenant in the durable userData store.
function CloudSyncSection() {
  const [ent, setEnt] = useState<Entitlements | null | undefined>(undefined); // undefined = loading
  const [optIn, setOptInState] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      const e = await getEntitlements();
      if (!alive) return;
      setEnt(e);
      if (e) setOptInState(await getOptIn(e.sub));
    })();
    return () => { alive = false; };
  }, []);

  const toggle = async (v: boolean) => {
    if (!ent || !ent.cloud_sync) return;
    setSaving(true);
    await setOptIn(ent.sub, v);
    setOptInState(v);
    setSaving(false);
  };

  return (
    <div className="space-y-3">
      <div>
        <Label className="flex items-center gap-1.5">
          {optIn && ent?.cloud_sync ? <Cloud className="h-3.5 w-3.5 text-accent" /> : <CloudOff className="h-3.5 w-3.5" />}
          Cloud Sync
        </Label>
        <p className="mt-1 text-xs text-muted-foreground normal-case tracking-normal">
          Off: conversations stay on this device. On: conversations sync to your account for cross-device + webapp access.
        </p>
      </div>

      {ent === undefined ? (
        <div className="text-[12px] text-muted-foreground">Checking your account…</div>
      ) : ent === null ? (
        <div className="rounded-md border border-border/60 bg-card/40 px-3 py-2.5 text-[12px] text-muted-foreground">
          Sign in with Supabase to enable Cloud Sync.
        </div>
      ) : (
        <label className={`flex items-center gap-2 text-sm ${ent.cloud_sync ? 'cursor-pointer' : 'cursor-not-allowed opacity-70'}`}>
          <Checkbox checked={optIn && ent.cloud_sync} disabled={!ent.cloud_sync || saving} onCheckedChange={(v) => toggle(Boolean(v))} />
          <span>Sync this account's conversations to the cloud</span>
          {!ent.cloud_sync && (
            <Badge variant="muted" className="ml-1 normal-case">paid add-on</Badge>
          )}
        </label>
      )}
      {ent && !ent.cloud_sync && (
        <p className="text-[11px] text-muted-foreground">
          Cloud Sync is a paid add-on for your account. Conversations stay on this device until it's enabled.
        </p>
      )}
    </div>
  );
}

function ToolsSection() {
  const [tools, setTools] = useState<{ gh: ToolStatus; railway: ToolStatus; vercel: ToolStatus } | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const refresh = async () => {
    setRefreshing(true);
    try {
      const t = await ah().tools.status();
      setTools(t);
    } finally {
      setRefreshing(false);
    }
  };

  useEffect(() => { refresh(); }, []);

  const connect = async (tool: 'gh' | 'railway' | 'vercel') => {
    await ah().tools.connect(tool);
    // Auth opens a terminal/browser — give it a beat then re-check.
    setTimeout(refresh, 4000);
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <Label>Connected tools</Label>
          <p className="mt-1 text-xs text-muted-foreground normal-case tracking-normal">
            CLIs your agents can use. Per-user — not per-project.
          </p>
        </div>
        <Button size="sm" variant="ghost" onClick={refresh} disabled={refreshing}>
          <RefreshCw className={refreshing ? 'h-3.5 w-3.5 animate-spin' : 'h-3.5 w-3.5'} />
        </Button>
      </div>

      <div className="space-y-1.5">
        <ToolRow icon={<Github className="h-4 w-4" />} name="GitHub CLI" cmd="gh" status={tools?.gh} loading={!tools} onConnect={() => connect('gh')} />
        <ToolRow icon={<Train className="h-4 w-4" />} name="Railway CLI" cmd="railway" status={tools?.railway} loading={!tools} onConnect={() => connect('railway')} />
        <ToolRow icon={<Triangle className="h-4 w-4" />} name="Vercel CLI" cmd="vercel" status={tools?.vercel} loading={!tools} onConnect={() => connect('vercel')} />
      </div>
    </div>
  );
}

function ToolRow({
  icon, name, cmd, status, loading, onConnect,
}: {
  icon: React.ReactNode;
  name: string;
  cmd: string;
  status: ToolStatus | undefined;
  loading: boolean;
  onConnect: () => void;
}) {
  if (loading) {
    return (
      <div className="flex items-center gap-3 rounded-md border border-border/60 bg-card/40 px-3 py-2.5 text-sm">
        <span className="text-muted-foreground">{icon}</span>
        <div className="flex-1">
          <div className="font-medium">{name}</div>
          <div className="text-[11px] text-muted-foreground">checking…</div>
        </div>
      </div>
    );
  }
  const s = status!;
  const stateBadge = !s.installed ? (
    <Badge variant="err">Not installed</Badge>
  ) : s.authenticated ? (
    <Badge variant="ok"><CheckCircle2 className="mr-1 h-3 w-3" /> Connected</Badge>
  ) : (
    <Badge variant="muted"><XCircle className="mr-1 h-3 w-3" /> Not logged in</Badge>
  );
  const helpText = !s.installed
    ? `Install with: ${cmd === 'gh' ? 'winget install GitHub.cli (or brew install gh)' : cmd === 'railway' ? 'npm i -g @railway/cli' : 'npm i -g vercel'}`
    : s.authenticated
      ? s.identity || 'authenticated'
      : `Click Connect to run \`${cmd} login\``;
  return (
    <div className="flex items-center gap-3 rounded-md border border-border/60 bg-card/40 px-3 py-2.5 text-sm">
      <span className="text-accent">{icon}</span>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-medium">{name}</span>
          {stateBadge}
        </div>
        <div className="truncate text-[11px] text-muted-foreground">{helpText}</div>
      </div>
      {s.installed && (
        <Button size="sm" variant={s.authenticated ? 'ghost' : 'default'} onClick={onConnect}>
          {s.authenticated ? 'Re-auth' : 'Connect'}
        </Button>
      )}
    </div>
  );
}

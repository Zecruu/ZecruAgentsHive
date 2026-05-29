import { useEffect, useMemo, useState } from 'react';
import { Rocket, FolderOpen } from 'lucide-react';
import {
  ah,
  buildCmd,
  MODEL_OPTIONS,
  EFFORT_OPTIONS,
  type Cli,
  type OsHint,
  type Project,
  type Role,
} from '@/lib/agentshive';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';

export interface LauncherValues {
  role: Role;
  cli: Cli;
  model: string;
  effort: string;
  osHint: OsHint;
  coderId: string;
  skipPerms: boolean;
  resume: boolean;
}

interface Props {
  project: Project;
  folder: string | null;
  hostname: string;
  onLaunch: (v: LauncherValues) => void;
  onPickFolder: () => void;
}

export function LauncherForm({ project, folder, hostname, onLaunch, onPickFolder }: Props) {
  const [role, setRole] = useState<Role>('hivemind');
  const [cli, setCli] = useState<Cli>('claude');
  const [model, setModel] = useState(MODEL_OPTIONS.claude[0].value);
  const [effort, setEffort] = useState('');
  const [osHint, setOsHint] = useState<OsHint>(null);
  const [coderId, setCoderId] = useState('');
  const [skipPerms, setSkipPerms] = useState(false);
  const [resume, setResume] = useState(false);
  // codex's effective model (from ~/.codex/config.toml). ChatGPT-account codex
  // has no selectable model — we show this read-only instead of a dropdown.
  const [codexModel, setCodexModel] = useState<string | null>(null);

  const idPlaceholder = `${(hostname || 'host').toLowerCase().replace(/[^a-z0-9-]+/g, '-')}-${role}`;
  const suggestedCmd = useMemo(() => buildCmd(cli, model || null, effort || null, skipPerms, resume), [cli, model, effort, skipPerms, resume]);
  const noFolder = !folder;

  // Restore + persist per-project launcher prefs.
  useEffect(() => {
    let alive = true;
    (async () => {
      const saved = (await ah().prefs.get(project.slug)) as Record<string, any> | null;
      if (!alive || !saved) return;
      if (saved.role) setRole(saved.role as Role);
      if (saved.cli) setCli(saved.cli as Cli);
      if (typeof saved.model === 'string') setModel(saved.model);
      if (typeof saved.effort === 'string') setEffort(saved.effort);
      if (saved.osHint !== undefined) setOsHint((saved.osHint as OsHint) ?? null);
      if (typeof saved.skipPerms === 'boolean') setSkipPerms(saved.skipPerms);
      if (typeof saved.resume === 'boolean') setResume(saved.resume);
    })();
    return () => {
      alive = false;
    };
  }, [project.slug]);

  useEffect(() => {
    ah().prefs.set(project.slug, { role, cli, model, effort, osHint, skipPerms, resume }).catch(() => {});
  }, [project.slug, role, cli, model, effort, osHint, skipPerms, resume]);

  // Fetch codex's configured default model so the (read-only) model field can
  // show what a ChatGPT-account codex agent will actually run.
  useEffect(() => {
    if (cli !== 'codex') return;
    let alive = true;
    ah().codex.defaultModel().then((m) => { if (alive) setCodexModel(m); }).catch(() => {});
    return () => { alive = false; };
  }, [cli]);

  const launch = () => {
    onLaunch({
      role,
      cli,
      // codex: never pass a model — ChatGPT-account auth only allows the account
      // default (explicit -m 400s); let codex use it.
      model: cli === 'codex' ? '' : model,
      effort,
      osHint,
      coderId: coderId.trim() || idPlaceholder,
      skipPerms,
      resume,
    });
  };

  return (
    <Card className="max-w-3xl">
      <CardHeader>
        <CardTitle>New agent</CardTitle>
        <CardDescription>
          Pick a role + CLI + model. The agent appears in the sidebar — switch between them anytime. Auto-verifies project scope and greets the operator on launch.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="grid grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <Label>Role</Label>
            <Select value={role} onValueChange={(v) => setRole(v as Role)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="hivemind">Hivemind (Planner)</SelectItem>
                <SelectItem value="coder">Coder</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>CLI</Label>
            <Select value={cli} onValueChange={(v) => { const c = v as Cli; setCli(c); setModel(MODEL_OPTIONS[c][0].value); setEffort(''); }}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="claude">Claude Code</SelectItem>
                <SelectItem value="codex">Codex CLI</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>Model</Label>
            {cli === 'codex' ? (
              // ChatGPT-account codex has no selectable model — show the effective
              // one read-only (the account default codex will use).
              <div
                className="flex h-9 items-center truncate rounded-md border border-input bg-input/40 px-3 text-[13px] text-muted-foreground"
                title="ChatGPT-account codex uses the account's default model — it isn't selectable. Reasoning effort is the configurable knob."
              >
                {codexModel ? `${codexModel} · ChatGPT account` : 'ChatGPT account default'}
              </div>
            ) : (
              <Select value={model || '__default'} onValueChange={(v) => setModel(v === '__default' ? '' : v)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {MODEL_OPTIONS[cli].map((o) => (
                    <SelectItem key={o.value || '__default'} value={o.value || '__default'}>{o.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
          <div className="space-y-1.5">
            <Label>Effort</Label>
            <Select value={effort || '__default'} onValueChange={(v) => setEffort(v === '__default' ? '' : v)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {EFFORT_OPTIONS[cli].map((o) => (
                  <SelectItem key={o.value || '__default'} value={o.value || '__default'}>{o.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>OS hint</Label>
            <Select value={osHint || '__default'} onValueChange={(v) => setOsHint(v === '__default' ? null : (v as OsHint))}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__default">(use default)</SelectItem>
                <SelectItem value="windows">windows</SelectItem>
                <SelectItem value="macos">macos</SelectItem>
                <SelectItem value="linux">linux</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>Agent name</Label>
            <Input placeholder={idPlaceholder} value={coderId} onChange={(e) => setCoderId(e.target.value)} maxLength={64} />
          </div>
          <div className="space-y-1.5">
            <Label>Options</Label>
            <div className="space-y-1.5 pt-1">
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <Checkbox checked={resume} onCheckedChange={(v) => setResume(Boolean(v))} /> Resume last session
              </label>
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <Checkbox checked={skipPerms} onCheckedChange={(v) => setSkipPerms(Boolean(v))} /> Skip permission prompts
                <span className="text-[10px] uppercase tracking-wider text-destructive">dangerous</span>
              </label>
            </div>
          </div>
        </div>

        <div className="rounded-md border border-border bg-input/40 px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Suggested command</div>
          <code className="break-all font-mono text-[12px] text-accent">{suggestedCmd}</code>
        </div>

        {noFolder && (
          <div className="flex items-center justify-between rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm">
            <span className="text-destructive-foreground">No local folder set — agents need one to write AGENTS.md + .mcp.json.</span>
            <Button size="sm" variant="outline" onClick={onPickFolder}>
              <FolderOpen className="h-3.5 w-3.5" /> Pick folder
            </Button>
          </div>
        )}

        <div className="flex items-center gap-3 pt-1">
          <Button onClick={launch} disabled={noFolder}>
            <Rocket className="h-4 w-4" /> Launch agent
          </Button>
          <span className="text-xs text-muted-foreground">
            Agent will auto-verify scope and introduce itself before awaiting your first message.
          </span>
        </div>
      </CardContent>
    </Card>
  );
}

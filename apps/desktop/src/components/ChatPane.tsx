import { Fragment, useEffect, useRef, useState } from 'react';
import { Archive, AtSign, ChevronRight, FilePen, FileText, Hexagon, ImageIcon, ListPlus, Loader2, Maximize2, MessageSquare, Minimize2, PanelRight, Paperclip, Send, SlidersHorizontal, StopCircle, TerminalSquare, Wrench, X as XIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { cn } from '@/lib/utils';
import { ah, MODEL_OPTIONS, EFFORT_OPTIONS, basename, changedFiles, type AttachmentData, type SkillItem, type ToolCallData } from '@/lib/agentshive';
import { agentActivity, formatActivity, type AgentRuntime, type MessageRuntime } from '@/lib/useActiveProject';
import { ToolCallCard } from './ToolCallCard';
import { MarkdownBody } from './Markdown';

interface PendingAttachment {
  name: string;
  mime: string;
  dataUrl: string;
}

interface Props {
  agent: AgentRuntime;
  siblings: AgentRuntime[];
  onSend: (prompt: string, attachments?: AttachmentData[]) => void;
  onChangeModelEffort: (model: string | null, effort: string) => void;
  onCancel: () => void;
  onArchive: () => void;
  onSwitchAgent: (a: AgentRuntime) => void;
  projectSlug: string;
  maximized?: boolean;
  onToggleMaximize?: () => void;
  missionsPanelOpen?: boolean;
  onToggleMissionsPanel?: () => void;
  onQueue: (text: string, attachments?: AttachmentData[]) => void;
  onRemoveQueued: (idx: number) => void;
}

export function ChatPane({ agent, siblings, onSend, onChangeModelEffort, onCancel, onArchive, onSwitchAgent, projectSlug, maximized, onToggleMaximize, missionsPanelOpen, onToggleMissionsPanel, onQueue, onRemoveQueued }: Props) {
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState<PendingAttachment[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const [busy, setBusy] = useState(false);
  // Per-agent model/effort tuning popover (changes apply on the next turn).
  const [tuneOpen, setTuneOpen] = useState(false);
  // Whether this project's folder is a git repo — gates the per-turn Undo action.
  const [gitRepo, setGitRepo] = useState(false);
  // `/` slash-command autocomplete. Skills/commands live under ~/.claude; they
  // expand in claude's headless --print mode (codex doesn't expand them, but we
  // still surface the list for reference + quick insert, per the operator).
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [skillMenuOpen, setSkillMenuOpen] = useState(false);
  const [skillIdx, setSkillIdx] = useState(0);
  const messagesRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Load once per project (not gated on cli — a codex agent shouldn't hide the
  // skill list; the prior cli gate is why `/` showed nothing on codex agents).
  useEffect(() => {
    let alive = true;
    ah().skills.list(projectSlug)
      .then((s) => { if (alive) setSkills(s || []); })
      .catch((e) => { console.warn('skills.list failed', e); if (alive) setSkills([]); });
    return () => { alive = false; };
  }, [projectSlug]);

  // Is the project folder a git repo? Gates the per-turn Undo action.
  useEffect(() => {
    let alive = true;
    ah().files.isGitRepo(projectSlug).then((r) => { if (alive) setGitRepo(Boolean(r)); }).catch(() => {});
    return () => { alive = false; };
  }, [projectSlug]);

  const slashQuery =
    skillMenuOpen && draft.startsWith('/') && !draft.slice(1).includes(' ')
      ? draft.slice(1).toLowerCase()
      : null;
  const filteredSkills =
    slashQuery !== null ? skills.filter((s) => s.name.toLowerCase().includes(slashQuery)).slice(0, 8) : [];
  const showSkillMenu = slashQuery !== null && filteredSkills.length > 0;

  const onDraftChange = (val: string) => {
    setDraft(val);
    if (val.startsWith('/') && !val.slice(1).includes(' ')) {
      setSkillMenuOpen(true);
      setSkillIdx(0);
    } else {
      setSkillMenuOpen(false);
    }
  };

  const chooseSkill = (item: SkillItem) => {
    setDraft('/' + item.name + ' ');
    setSkillMenuOpen(false);
    requestAnimationFrame(() => textareaRef.current?.focus());
  };

  useEffect(() => {
    const el = messagesRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [agent.messages.length, agent.messages[agent.messages.length - 1]?.text, agent.id]);

  useEffect(() => { textareaRef.current?.focus(); }, [agent.id]);

  const readAsDataUrl = (file: File): Promise<string> => {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result));
      r.onerror = () => reject(r.error);
      r.readAsDataURL(file);
    });
  };

  const intakeFiles = async (files: FileList | File[]) => {
    const list = Array.from(files).filter((f) => f.type.startsWith('image/'));
    if (list.length === 0) return;
    const added: PendingAttachment[] = [];
    for (const f of list) {
      const dataUrl = await readAsDataUrl(f);
      added.push({ name: f.name || `pasted-${Date.now()}.png`, mime: f.type, dataUrl });
    }
    setPending((p) => [...p, ...added]);
  };

  const onPaste = async (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files: File[] = [];
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      if (it.type.startsWith('image/')) {
        const f = it.getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length) {
      e.preventDefault();
      await intakeFiles(files);
    }
  };

  const onDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer?.files?.length) {
      await intakeFiles(e.dataTransfer.files);
    }
  };

  const removePending = (idx: number) => setPending((p) => p.filter((_, i) => i !== idx));

  // Persist any pending images to disk → AttachmentData[] (path + dataUrl).
  const savePending = async (): Promise<AttachmentData[]> => {
    const saved: AttachmentData[] = [];
    for (const p of pending) {
      const r = await ah().attachments.save({ agentId: agent.id, projectSlug, name: p.name, dataUrl: p.dataUrl });
      saved.push({ name: p.name, path: r.path, dataUrl: p.dataUrl, mime: p.mime });
    }
    return saved;
  };

  const send = async () => {
    if (agent.readOnly) return; // view-only conversation materialized from another device
    const t = draft.trim();
    if (busy) return;
    if (!t && pending.length === 0) return;
    setBusy(true);
    try {
      const saved = await savePending();
      setDraft('');
      setPending([]);
      // P4: while a turn is in-flight, QUEUE the follow-up — now WITH its
      // attachments (saved to disk + carried on the queued entry), so an image
      // attached while busy is sent when the queue drains instead of dropped.
      if (agent.inFlight) {
        onQueue(t, saved.length ? saved : undefined);
      } else {
        onSend(t, saved.length ? saved : undefined);
      }
    } finally {
      setBusy(false);
    }
  };

  const handleKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (showSkillMenu) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setSkillIdx((i) => Math.min(i + 1, filteredSkills.length - 1)); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); setSkillIdx((i) => Math.max(i - 1, 0)); return; }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        chooseSkill(filteredSkills[Math.min(skillIdx, filteredSkills.length - 1)]);
        return;
      }
      if (e.key === 'Escape') { e.preventDefault(); setSkillMenuOpen(false); return; }
    }
    // Shift+Enter: smart list continuation. If the current line is an ordered
    // ("3. ") or unordered ("- "/"* ") item, continue the list; an empty item
    // ends it. Non-list lines fall through to the default newline.
    if (e.key === 'Enter' && e.shiftKey && continueListOnShiftEnter(e)) {
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  // Returns true if it handled the event (list continuation / end), false to let
  // the default newline happen.
  const continueListOnShiftEnter = (e: React.KeyboardEvent<HTMLTextAreaElement>): boolean => {
    const ta = textareaRef.current;
    if (!ta || ta.selectionStart !== ta.selectionEnd) return false; // only a plain caret
    const caret = ta.selectionStart ?? draft.length;
    const before = draft.slice(0, caret);
    const after = draft.slice(caret);
    const lineStart = before.lastIndexOf('\n') + 1;
    const line = before.slice(lineStart);
    const ordered = line.match(/^(\s*)(\d+)\.\s+(.*)$/);
    const unordered = line.match(/^(\s*)([-*])\s+(.*)$/);
    if (!ordered && !unordered) return false;

    const setAndCaret = (value: string, pos: number) => {
      setDraft(value);
      requestAnimationFrame(() => { ta.focus(); ta.setSelectionRange(pos, pos); });
    };

    // Empty item (just the marker) → end the list: clear the marker on this line.
    const content = (ordered ? ordered[3] : unordered![3]);
    if (content.trim() === '') {
      e.preventDefault();
      setAndCaret(draft.slice(0, lineStart) + after, lineStart);
      return true;
    }
    e.preventDefault();
    const indent = ordered ? ordered[1] : unordered![1];
    const marker = ordered ? `${parseInt(ordered[2], 10) + 1}. ` : `${unordered![2]} `;
    const insert = `\n${indent}${marker}`;
    setAndCaret(before + insert + after, (before + insert).length);
    return true;
  };

  const mention = (s: AgentRuntime) => {
    const ta = textareaRef.current;
    const ref = `@${s.coderId || s.label}`;
    if (!ta) {
      setDraft((d) => (d ? d + ' ' + ref + ' ' : ref + ' '));
      return;
    }
    const start = ta.selectionStart ?? draft.length;
    const end = ta.selectionEnd ?? draft.length;
    const before = draft.slice(0, start);
    const after = draft.slice(end);
    const needsSpaceBefore = before && !before.endsWith(' ');
    const insert = (needsSpaceBefore ? ' ' : '') + ref + ' ';
    setDraft(before + insert + after);
    requestAnimationFrame(() => {
      const pos = (before + insert).length;
      ta.focus();
      ta.setSelectionRange(pos, pos);
    });
  };

  const empty = agent.messages.length === 0;
  // Per-TURN changed files: index of each turn's LAST entry → files that turn
  // edited (aggregated across its split entries, deduped). Drives the docked bar.
  const turnEndFiles: Record<number, string[]> = {};
  {
    const msgs = agent.messages;
    let acc: ToolCallData[] = [];
    for (let i = 0; i < msgs.length; i++) {
      if (msgs[i].role === 'user') { acc = []; continue; } // new turn — reset
      const tc = msgs[i].toolCalls;
      if (tc && tc.length) acc = acc.concat(tc);
      const lastOfTurn = i === msgs.length - 1 || msgs[i + 1].role === 'user';
      if (lastOfTurn) {
        const cf = changedFiles(acc);
        if (cf.length) turnEndFiles[i] = cf;
        acc = [];
      }
    }
  }
  // Per-agent usage: running total tokens (sum of per-turn input+output) + the
  // number of completed assistant turns.
  const usageTokens = agent.messages.reduce((sum, m) => sum + (m.tokens ? m.tokens.input + m.tokens.output : 0), 0);
  const usageTurns = agent.messages.filter((m) => m.role === 'assistant' && m.tokens).length;
  // CONTEXT FILL = the LATEST turn's total input (incl. cache_read), point-in-time
  // — what the CLI shows as "% context used". The headline; the cumulative session
  // total above goes in the tooltip. Window % only when we KNOW the model's window.
  let latestContext = 0;
  for (let i = agent.messages.length - 1; i >= 0; i--) {
    const c = agent.messages[i].tokens?.context;
    if (typeof c === 'number' && c > 0) { latestContext = c; break; }
  }
  const ctxWindow = contextWindowFor(agent.model);
  // Live activity: while in-flight, show the current action + a ticking elapsed
  // timer (the parent re-renders every second via the hook's activity ticker),
  // so a long tool call / long generation reads as ALIVE rather than frozen.
  const activity = agentActivity(agent, Date.now());
  const statusDotClass =
    agent.inFlight ? 'bg-warn animate-pulse-ring'
    : activity.state === 'err' ? 'bg-destructive'
    : activity.state === 'idle' ? 'bg-muted-foreground'
    : 'bg-success';
  const statusBadge = agent.inFlight ? (
    <Badge variant="warn" className="gap-1 normal-case">
      <Loader2 className="h-3 w-3 animate-spin" />
      {formatActivity(activity)}
    </Badge>
  ) : activity.state === 'err' ? <Badge variant="err">error</Badge>
    : activity.state === 'idle' ? <Badge variant="muted">idle</Badge>
    : <Badge variant="ok">ready</Badge>;

  const orderedSiblings = [...siblings].sort((a, b) => {
    const aMatch = agent.role === 'hivemind' ? a.role === 'coder' : a.role === 'hivemind';
    const bMatch = agent.role === 'hivemind' ? b.role === 'coder' : b.role === 'hivemind';
    return Number(bMatch) - Number(aMatch);
  });

  return (
    <div
      className={cn(
        'flex h-full flex-col glass',
        // Full-bleed when maximized (fills the window); framed card otherwise.
        maximized
          ? 'rounded-none border-0 shadow-none'
          : 'rounded-lg border border-border/70 shadow-[0_1px_0_hsl(0_0%_100%/0.04)_inset,0_12px_40px_-20px_hsl(222_60%_0%/0.6)]',
      )}
      onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
      onDragLeave={() => setDragOver(false)}
      onDrop={onDrop}
    >
      <div className="flex flex-none flex-col border-b border-border/60 bg-card/35">
        <div className="flex min-h-[58px] items-center justify-between gap-4 px-5 py-3">
          <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span
              className={cn('h-2.5 w-2.5 flex-none rounded-full', statusDotClass)}
              style={{ boxShadow: '0 0 8px -1px currentColor' }}
              title={agent.inFlight ? 'Running' : activity.state}
            />
            <h3 className="truncate text-[16px] font-semibold tracking-tight">{agent.label}</h3>
          </div>
          <div className="mt-1 font-mono text-[11px] text-muted-foreground">
            {agent.role} · {agent.cli}{agent.model ? ` · ${agent.model}` : ''}
            {/* codex always runs --dangerously-bypass-approvals-and-sandbox, so it's
                unsandboxed regardless of the skip-perms checkbox — surface that. */}
            {agent.cli === 'codex' ? ' · unsandboxed' : (agent.skipPerms ? ' · skip-perms' : '')}
          </div>
        </div>
          <div className="flex shrink-0 items-center gap-2">
          {agent.readOnly && (
            <Badge variant="muted" className="normal-case">from another device · read-only</Badge>
          )}
          {latestContext > 0 && (
            <span
              className="rounded-full border border-border bg-input/50 px-2 py-0.5 text-[10px] text-muted-foreground"
              title={`Context: ${latestContext.toLocaleString()} tokens${ctxWindow ? ` of ~${ctxWindow.toLocaleString()}` : ''}\nSession: ${usageTokens.toLocaleString()} new tokens across ${usageTurns} turn${usageTurns > 1 ? 's' : ''}`}
            >
              {fmtTokens(latestContext)} ctx{ctxWindow ? ` · ${Math.round((latestContext / ctxWindow) * 100)}%` : ''}
            </span>
          )}
          {statusBadge}
          <div className="relative">
            <Button
              variant="ghost"
              size="icon"
              className={cn('h-8 w-8', tuneOpen && 'text-accent')}
              onClick={() => setTuneOpen((o) => !o)}
              title="Change model & effort (applies next turn)"
            >
              <SlidersHorizontal className="h-4 w-4" />
            </Button>
            {tuneOpen && (
              <>
                {/* click-away backdrop */}
                <button
                  className="fixed inset-0 z-20 cursor-default"
                  aria-hidden
                  onClick={() => setTuneOpen(false)}
                />
                <div className="absolute right-0 top-full z-30 mt-1.5 w-60 space-y-3 rounded-lg border border-border bg-popover/95 p-3 shadow-[0_12px_40px_-16px_hsl(222_60%_0%/0.7)] backdrop-blur">
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                    Model &amp; effort
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-[11px]">Model</Label>
                    <Select
                      value={agent.model || '__default'}
                      onValueChange={(v) => onChangeModelEffort(v === '__default' ? null : v, agent.effort)}
                    >
                      <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        {MODEL_OPTIONS[agent.cli].map((o) => (
                          <SelectItem key={o.value || '__default'} value={o.value || '__default'}>{o.label}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-1.5">
                    <Label className="text-[11px]">Effort</Label>
                    <Select
                      value={agent.effort || '__default'}
                      onValueChange={(v) => onChangeModelEffort(agent.model, v === '__default' ? '' : v)}
                    >
                      <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        {EFFORT_OPTIONS[agent.cli].map((o) => (
                          <SelectItem key={o.value || '__default'} value={o.value || '__default'}>{o.label}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <p className="text-[10px] leading-snug text-muted-foreground">
                    {agent.inFlight ? 'Applies to the next turn (current turn keeps its settings).' : 'Applies to the next turn.'}
                  </p>
                </div>
              </>
            )}
          </div>
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
              title={maximized ? 'Exit full-width chat' : 'Maximize chat (hide sidebar)'}
            >
              {maximized ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
            </Button>
          )}
          <Button variant="ghost" size="sm" onClick={onArchive}>
            <Archive className="h-3.5 w-3.5" /> Archive
          </Button>
          </div>
        </div>
        <div className="flex min-h-[36px] items-center justify-between gap-3 border-t border-border/40 px-5 py-2">
          <div className="flex min-w-0 items-center gap-2 text-[11px] text-muted-foreground">
            <TerminalSquare className="h-3.5 w-3.5 flex-none" />
            <span className="truncate">Next turn uses current model settings; in-flight work keeps its launch settings.</span>
          </div>
          <div className="hidden shrink-0 items-center gap-2 lg:flex">
            {latestContext > 0 && (
              <span
                className="rounded-full border border-border bg-input/55 px-2.5 py-1 text-[10px] font-medium text-muted-foreground"
                title={`Context: ${latestContext.toLocaleString()} tokens${ctxWindow ? ` of ~${ctxWindow.toLocaleString()}` : ''}\nSession: ${usageTokens.toLocaleString()} new tokens across ${usageTurns} turn${usageTurns > 1 ? 's' : ''}`}
              >
                {fmtTokens(latestContext)} ctx{ctxWindow ? ` · ${Math.round((latestContext / ctxWindow) * 100)}%` : ''}
              </span>
            )}
            {statusBadge}
          </div>
        </div>
      </div>

      <div ref={messagesRef} className="flex-1 overflow-y-auto scrollbar-thin px-4 py-5">
        {empty ? (
          <div className="mx-auto flex max-w-md flex-col items-center py-20 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-lg border border-accent/25 bg-accent/10 text-accent">
              <Hexagon className="h-6 w-6" />
            </div>
            <div className="mt-4 text-sm font-medium">Open a turn with {agent.label}</div>
            <p className="mt-1 text-[12px] leading-relaxed text-muted-foreground">
              Reasoning, tool calls, and results render as cards — not raw TTY.
            </p>
            <div className="mt-4 flex flex-wrap justify-center gap-2">
              <EmptyChip icon={<MessageSquare className="h-3 w-3" />} label="message" />
              <EmptyChip icon={<ImageIcon className="h-3 w-3" />} label="image" />
              <EmptyChip icon={<TerminalSquare className="h-3 w-3" />} label="/ skills" />
            </div>
          </div>
        ) : (
          <div className="mx-auto flex w-full max-w-4xl flex-col gap-4">
            {agent.messages.map((m, i) => (
              <Fragment key={i}>
                <MessageBubble message={m} />
                {turnEndFiles[i] && <ChangedFilesBar files={turnEndFiles[i]} projectSlug={projectSlug} canUndo={gitRepo} />}
              </Fragment>
            ))}
          </div>
        )}
      </div>

      <div className="flex-none border-t border-border/60 bg-card/35 px-4 py-3">
        <div className="mx-auto w-full max-w-4xl">
        {orderedSiblings.length > 0 && (
          <div className="mb-2 flex flex-wrap items-center gap-1.5 rounded-md border border-border/50 bg-input/25 px-2 py-1.5">
            <span className="mr-1 text-[10px] uppercase tracking-wider text-muted-foreground">Mention</span>
            {orderedSiblings.map((s) => (
              <button
                key={s.id}
                onClick={() => mention(s)}
                onDoubleClick={() => onSwitchAgent(s)}
                title="Click to @-mention · double-click to switch to this agent"
                className={cn(
                  'group inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] transition-all',
                  s.role === 'coder'
                    ? 'border-primary/30 bg-primary/5 text-primary hover:bg-primary/15'
                    : 'border-accent/30 bg-accent/5 text-accent hover:bg-accent/15',
                )}
              >
                <AtSign className="h-2.5 w-2.5" />
                {s.coderId || s.label}
                <span className="text-[9px] text-muted-foreground group-hover:text-current">{s.role}</span>
              </button>
            ))}
          </div>
        )}

        {pending.length > 0 && (
          <div className="mb-2 flex gap-2 overflow-x-auto rounded-md border border-border/50 bg-input/25 p-2 scrollbar-thin">
            {pending.map((p, i) => (
              <div key={i} className="relative inline-flex shrink-0 items-center gap-2 rounded-md border border-border bg-input/65 p-1 pr-2">
                <img src={p.dataUrl} alt={p.name} className="h-10 w-10 rounded object-cover" />
                <div className="flex flex-col">
                  <span className="max-w-[180px] truncate text-[11px]">{p.name}</span>
                  <span className="text-[10px] text-muted-foreground">{p.mime}</span>
                </div>
                <button onClick={() => removePending(i)} className="ml-1 rounded-full p-0.5 hover:bg-destructive/20" title="Remove">
                  <XIcon className="h-3 w-3 text-muted-foreground hover:text-destructive" />
                </button>
              </div>
            ))}
          </div>
        )}

        {agent.queue.length > 0 && (
          <div className="mb-2 space-y-1.5 rounded-md border border-border/50 bg-input/25 p-2">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Queued ({agent.queue.length}) — sends after the current turn
            </span>
            {agent.queue.map((q, i) => (
              <div key={i} className="flex items-center gap-2 rounded-md border border-border/60 bg-input/40 px-2 py-1 text-[12px]">
                <ListPlus className="h-3 w-3 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1 truncate" title={q.text}>{q.text || <span className="text-muted-foreground italic">(image only)</span>}</span>
                {q.attachments && q.attachments.length > 0 && (
                  <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border bg-input/50 px-1.5 text-[10px] text-muted-foreground" title={`${q.attachments.length} image${q.attachments.length > 1 ? 's' : ''} attached`}>
                    <Paperclip className="h-2.5 w-2.5" />{q.attachments.length}
                  </span>
                )}
                <button
                  onClick={() => onRemoveQueued(i)}
                  className="shrink-0 rounded-full p-0.5 hover:bg-destructive/20"
                  title="Remove from queue"
                >
                  <XIcon className="h-3 w-3 text-muted-foreground hover:text-destructive" />
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="relative">
        {showSkillMenu && (
          <div className="absolute bottom-full left-0 right-0 z-20 mb-2 max-h-64 overflow-y-auto scrollbar-thin rounded-lg border border-border bg-popover/95 p-1.5 shadow-[0_12px_40px_-16px_hsl(222_60%_0%/0.7)] backdrop-blur">
            <div className="px-2 py-1 text-[10px] uppercase tracking-wider text-muted-foreground">
              Skills &amp; commands · ↑↓ to navigate · Enter to insert
            </div>
            {filteredSkills.map((s, i) => (
              <button
                key={`${s.source}:${s.name}`}
                // mousedown (not click) so selection fires before the textarea blurs
                onMouseDown={(e) => { e.preventDefault(); chooseSkill(s); }}
                onMouseEnter={() => setSkillIdx(i)}
                className={cn(
                  'flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left transition-colors',
                  i === Math.min(skillIdx, filteredSkills.length - 1) ? 'bg-primary/15' : 'hover:bg-secondary/60',
                )}
              >
                <span className="mt-0.5 font-mono text-[12px] font-medium text-primary">/{s.name}</span>
                <span className="flex-1 truncate text-[11px] text-muted-foreground" title={s.description}>
                  {s.description}
                </span>
                <span className="shrink-0 rounded-full border border-border px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-muted-foreground">
                  {s.kind}
                </span>
              </button>
            ))}
          </div>
        )}
        <div
          className={cn(
            'flex items-end gap-2 rounded-lg border border-input bg-input/75 p-2 shadow-[0_1px_0_hsl(0_0%_100%/0.04)_inset] transition-all',
            'focus-within:border-ring focus-within:bg-input/90 focus-within:ring-2 focus-within:ring-ring/30',
            dragOver && 'border-primary ring-2 ring-primary/30',
          )}
        >
          <Button
            type="button"
            size="icon"
            variant="ghost"
            className="h-8 w-8 shrink-0"
            onClick={() => fileInputRef.current?.click()}
            title="Attach image"
          >
            <Paperclip className="h-4 w-4" />
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files) intakeFiles(e.target.files);
              if (fileInputRef.current) fileInputRef.current.value = '';
            }}
          />
          <Textarea
            ref={textareaRef}
            value={draft}
            onChange={(e) => onDraftChange(e.target.value)}
            onKeyDown={handleKey}
            onPaste={onPaste}
            disabled={agent.readOnly}
            placeholder={agent.readOnly ? 'Read-only — this conversation was synced from another device.' : dragOver ? 'Drop image to attach…' : `Message ${agent.label} — Enter to send, Shift+Enter for newline · paste/drop images`}
            className="min-h-[72px] max-h-[480px] resize-y border-0 bg-transparent p-1 text-[13.5px] leading-relaxed shadow-none focus-visible:ring-0 focus-visible:border-0"
            rows={3}
          />
          <div className="flex flex-col gap-1.5">
            <Button
              size="sm"
              onClick={send}
              disabled={agent.readOnly || busy || (!draft.trim() && pending.length === 0)}
              title={agent.readOnly ? 'Read-only — synced from another device' : agent.inFlight ? 'Queue this message — sends when the current turn finishes' : 'Send'}
            >
              {agent.inFlight ? <ListPlus className="h-3.5 w-3.5" /> : <Send className="h-3.5 w-3.5" />}
              {busy ? 'Saving…' : agent.inFlight ? 'Queue' : 'Send'}
            </Button>
            {agent.inFlight && (
              <Button size="sm" variant="ghost" onClick={onCancel}>
                <StopCircle className="h-3.5 w-3.5" /> Stop
              </Button>
            )}
          </div>
        </div>
        </div>
      </div>
      </div>
    </div>
  );
}

// Known context-window sizes by model. Only Claude 4.x is confirmed (200k) — for
// unknown models (codex/gpt-5, window unconfirmed) we return null and show the raw
// context size with NO % rather than guess. Matched by model-id substring.
function contextWindowFor(model: string | null): number | null {
  const m = (model || '').toLowerCase();
  if (m.includes('opus') || m.includes('sonnet') || m.includes('haiku')) return 200_000;
  return null;
}

// Compact token count: 850 → "850", 4200 → "4.2k", 1_500_000 → "1.5M".
function fmtTokens(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(n >= 10_000 ? 0 : 1) + 'k';
  return String(n);
}

function EmptyChip({ icon, label }: { icon: React.ReactNode; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-border/70 bg-input/45 px-2.5 py-1 text-[10px] uppercase tracking-wide text-muted-foreground">
      {icon}
      {label}
    </span>
  );
}

function MessageBubble({ message }: { message: MessageRuntime }) {
  // FIX 2: a tool-group entry (tool calls + no text) renders as JUST the group —
  // no empty assistant bubble. OLD persisted/Cloud-Sync-pulled messages that
  // carry BOTH text and tool calls still render both (the normal path below), so
  // existing/synced conversations are unchanged — no migration.
  if (message.role === 'assistant' && (message.toolCalls?.length ?? 0) > 0 && !message.text) {
    return (
      <div className="animate-fade-up flex w-full justify-start">
        <div className="w-full">
          <ToolCallGroup calls={message.toolCalls!} />
        </div>
      </div>
    );
  }

  const roleClasses =
    message.role === 'user'
      ? 'text-primary bg-primary/15'
      : message.role === 'assistant'
        ? 'text-accent bg-accent/15'
        : 'text-muted-foreground bg-muted/40';
  const bodyClasses =
    message.role === 'user'
      ? 'border-primary/30 bg-gradient-to-b from-primary/14 to-primary/5'
      : message.role === 'assistant'
        ? 'border-accent/20 bg-card/70'
        : 'border-border/40 bg-muted/20 text-muted-foreground text-[12.5px]';
  const isUser = message.role === 'user';
  const isAssistant = message.role === 'assistant';

  return (
    <div className={cn('animate-fade-up flex w-full', isUser ? 'justify-end' : 'justify-start')}>
      <div className={cn('space-y-1.5', isUser ? 'w-fit max-w-[78%]' : 'w-full')}>
      <div className={cn('flex items-center gap-2 text-[11px] text-muted-foreground', isUser && 'justify-end')}>
        <span className={cn('rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider', roleClasses)}>
          {message.role}
        </span>
        {message.tokens && (
          <span
            className="rounded-full border border-border bg-input/50 px-2 py-0.5 text-[10px] text-muted-foreground"
            title={`${message.tokens.input.toLocaleString()} in · ${message.tokens.output.toLocaleString()} out`}
          >
            {fmtTokens(message.tokens.input + message.tokens.output)} tok
          </span>
        )}
      </div>
      <div className={cn('relative break-words rounded-lg border px-3.5 py-3 text-[13.5px] leading-relaxed shadow-[0_1px_0_hsl(0_0%_100%/0.03)_inset]', bodyClasses, isAssistant && 'pl-4')}>
        {isAssistant && <span className="absolute bottom-2 left-0 top-2 w-0.5 rounded-full bg-accent/55" />}
        {message.text
          ? (message.role === 'system'
              ? <span className="whitespace-pre-wrap">{message.text}</span>
              : <MarkdownBody text={message.text} />)
          : (message.role === 'assistant' && <span className="text-muted-foreground">…</span>)}
        {message.attachments && message.attachments.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-2">
            {message.attachments.map((a, i) => (
              <a
                key={i}
                href={a.dataUrl}
                target="_blank"
                rel="noreferrer"
                className="block overflow-hidden rounded-md border border-border/60 transition hover:border-primary"
                title={a.path}
              >
                <img src={a.dataUrl} alt={a.name} className="max-h-48 max-w-xs object-contain" />
              </a>
            ))}
          </div>
        )}
      </div>
      {message.toolCalls && message.toolCalls.length > 0 && (
        <ToolCallGroup calls={message.toolCalls} />
      )}
      </div>
    </div>
  );
}

// One collapsible group for a turn's tool calls. Collapsed by default so the
// conversation reads clean; auto-expands while any call is still running so live
// progress is visible, then collapses to the summary once the turn completes.
// A manual toggle overrides the auto behavior.
function ToolCallGroup({ calls }: { calls: ToolCallData[] }) {
  const [userToggled, setUserToggled] = useState<boolean | null>(null);
  const total = calls.length;
  const running = calls.filter((c) => !c.completed).length;
  const errored = calls.filter((c) => c.completed && c.isError).length;
  const anyRunning = running > 0;
  const open = userToggled !== null ? userToggled : anyRunning;

  const status: { variant: 'ok' | 'err' | 'warn'; label: string } =
    anyRunning ? { variant: 'warn', label: `${running} running…` }
    : errored ? { variant: 'err', label: `${errored} error${errored > 1 ? 's' : ''}` }
    : { variant: 'ok', label: 'done' };

  return (
    <div className="pt-1">
      <button
        type="button"
        onClick={() => setUserToggled(!open)}
        className="flex w-full items-center gap-2 rounded-md border border-border/60 bg-input/45 px-3 py-2 text-left text-xs transition-colors hover:bg-secondary/40"
      >
        <ChevronRight className={cn('h-3 w-3 shrink-0 text-muted-foreground transition-transform', open && 'rotate-90')} />
        <Wrench className="h-3 w-3 shrink-0 text-muted-foreground" />
        <span className="font-medium">{total} tool call{total > 1 ? 's' : ''}</span>
        <Badge variant={status.variant} className="ml-auto gap-1">
          {anyRunning && <Loader2 className="h-3 w-3 animate-spin" />}
          {status.label}
        </Badge>
      </button>
      {open && (
        <div className="mt-1.5 space-y-1.5">
          {calls.map((tc) => (
            <ToolCallCard key={tc.id} call={tc} />
          ))}
        </div>
      )}
    </div>
  );
}

// Per-TURN aggregate changed-files bar (Cursor-style), docked at the end of a
// turn: all files the turn EDITED/WROTE, deduped. Review expands the list; Undo
// reverts them to git HEAD (git-repo-gated, current contents backed up first);
// Keep dismisses the bar.
function ChangedFilesBar({ files, projectSlug, canUndo }: { files: string[]; projectSlug: string; canUndo: boolean }) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<'idle' | 'undoing' | 'reverted' | 'error'>('idle');
  const [note, setNote] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState(false);

  if (dismissed) return null;

  const undo = async () => {
    if (!canUndo || state === 'undoing' || state === 'reverted') return;
    setState('undoing');
    try {
      const r = await ah().files.undoEdits(projectSlug, files);
      if (r.ok) {
        setState('reverted');
        setNote(r.skipped && r.skipped.length ? `${r.skipped.length} untracked skipped` : null);
      } else {
        setState('error');
        setNote(r.reason || 'undo failed');
      }
    } catch {
      setState('error');
      setNote('undo failed');
    }
  };

  return (
    <div className="mx-auto w-full max-w-4xl">
      <div className="flex items-center gap-2 rounded-md border border-border/60 bg-input/35 px-3 py-1.5 text-[11px]">
        <button type="button" onClick={() => setOpen((v) => !v)} className="flex min-w-0 items-center gap-2 text-left transition-colors hover:text-foreground" title="Files changed this turn">
          <FilePen className="h-3 w-3 shrink-0 text-accent" />
          <span className="font-medium">{files.length} File{files.length > 1 ? 's' : ''}</span>
          <span className="text-muted-foreground">Review</span>
          <ChevronRight className={cn('h-3 w-3 shrink-0 text-muted-foreground transition-transform', open && 'rotate-90')} />
        </button>
        <div className="ml-auto flex shrink-0 items-center gap-1.5">
          {state === 'reverted' ? (
            <span className="text-success">Reverted{note ? ` · ${note}` : ''}</span>
          ) : state === 'error' ? (
            <span className="text-destructive" title={note || ''}>Undo failed</span>
          ) : (
            <>
              {canUndo && (
                <button
                  type="button"
                  onClick={undo}
                  disabled={state === 'undoing'}
                  title="Revert this turn's files to the last commit (not the exact pre-turn state). Current contents are backed up first."
                  className="rounded border border-border px-1.5 py-0.5 text-muted-foreground transition-colors hover:bg-secondary/50 hover:text-foreground disabled:opacity-50"
                >
                  {state === 'undoing' ? 'Undoing…' : 'Undo'}
                </button>
              )}
              <button
                type="button"
                onClick={() => setDismissed(true)}
                title="Dismiss — keep the changes"
                className="rounded border border-border px-1.5 py-0.5 text-muted-foreground transition-colors hover:bg-secondary/50 hover:text-foreground"
              >
                Keep
              </button>
            </>
          )}
        </div>
      </div>
      {open && (
        <div className="mt-1 space-y-0.5 pl-2.5">
          {files.map((p) => (
            <div key={p} className="flex items-center gap-1.5 text-[11px]" title={p}>
              <FileText className="h-3 w-3 shrink-0 text-accent" />
              <span className="font-mono">{basename(p)}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-muted-foreground opacity-70">{p}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

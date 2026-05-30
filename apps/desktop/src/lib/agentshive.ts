// Typed wrapper around the window.agentshive bridge exposed by preload.js.
// The IPC contract is defined in apps/desktop/electron/preload.js — keep
// these types in sync if you change it.

export type Role = 'hivemind' | 'coder';
export type Cli = 'claude' | 'codex';
export type OsHint = 'windows' | 'macos' | 'linux' | null;
export type AgentStatus = 'ready' | 'thinking' | 'idle' | 'err' | 'rate-limited';

// Reasoning/effort levels. CLAUDE supports `--effort` (low|medium|high|xhigh|max),
// verified via `claude --help`. CODEX maps effort to `-c model_reasoning_effort`
// but ONLY honors it under an OpenAI API-key login — a ChatGPT-account login
// rejects model/effort overrides — so codex effort is gated on a model being set.
export type Effort = 'low' | 'medium' | 'high' | 'xhigh' | 'max';

// Dashboard state payload (subset of fields we use for wake-detection).
// Server returns more; we ignore anything we don't need.
export interface InboxMessageLite {
  id?: string | number;
  created_at?: string;
  target_coder_id?: string | null;
  coder_id?: string | null;
  body?: string;
  direction?: 'coder_to_planner' | 'planner_to_coder';
}

export interface PendingQuestionLite {
  id?: string | number;
  created_at?: string;
  coder_id?: string | null;
  question?: string;
}

export interface PendingSummaryLite {
  id?: string | number;
  created_at?: string;
  coder_id?: string | null;
  summary?: string;
  status?: string;
}

// Mission A: AgentPresence row as surfaced in /api/dashboard/state.agent_presence.
// state is the EFFECTIVE state (server-side lazy promotion applied — stale at 5
// min of heartbeat silence on non-idle, dead at 30 min). declared_state is what
// the agent actually claimed via set_my_state. agent_key = "planner" for the
// hivemind or the slug-normalized coder_id for coders.
export interface AgentPresenceLite {
  agent_key: string;
  role: 'planner' | 'coder' | string;
  state: 'idle' | 'working' | 'waiting_on_planner' | 'waiting_on_coder' | 'waiting_on_user' | 'blocked' | 'stale' | 'dead' | string;
  declared_state?: string;
  detail?: string | null;
  expected_done_at?: string | null;
  transitioned_at?: string | null;
  last_heartbeat_at?: string | null;
  seconds_since_heartbeat?: number | null;
  source?: string;
}

// v2.x long-lived agent token row as returned by GET /web/agent-tokens. Never
// includes the secret value; the operator sees it once via the mint response.
export interface AgentTokenLite {
  id: string;
  label: string;
  prefix: string;
  created_at?: string | null;
  last_used_at?: string | null;
  revoked_at?: string | null;
  revoked?: boolean;
}

export interface DashboardState {
  active_mission?: unknown;
  // Field names match the server JSON keys (dashboard.py:_build_state_payload).
  // Earlier code read pending_q/pending_s/p2c/c2p which were UNDEFINED in the
  // response — silently disabling every dashboard-poll wake branch + the ffe902c
  // Planner-wake signature. Reading the right names is the fix.
  pending_questions?: PendingQuestionLite[];
  pending_summaries?: PendingSummaryLite[];
  // Undelivered planner_to_coder messages for the active mission — drives the
  // desktop's per-coder wake fallback (2.0.21). target_coder_id NULL = broadcast.
  pending_from_planner?: Array<{ id?: string | number; target_coder_id?: string | null; created_at?: string }>;
  // Mission A: per-agent declared/promoted state for this project.
  agent_presence?: AgentPresenceLite[];
  messages?: {
    coder_to_planner?: InboxMessageLite[];
    planner_to_coder?: InboxMessageLite[];
  };
  connected_coders?: Array<{ coder_id: string; last_seen?: string; os_hint?: string | null }>;
  inbox?: InboxMessageLite[];
  // ... server returns more; we tolerate unknown fields
}

export interface Project {
  slug: string;
  name: string;
  description?: string | null;
  archived_at?: string | null;
}

export interface ConfigState {
  baseUrl: string;
  apiKeyMasked: string;
  apiKeyConfigured: boolean;
  legacyKeyEnabled: boolean;
  defaultOsHint: string;
  platform: string;
}

// VS-Code-style workspace state persisted app-globally in config.json.
export interface WorkspaceState {
  openedProjects: string[]; // project slugs, in sidebar order
  collapsed: Record<string, boolean>; // slug -> true if folder collapsed
  lastActive: string | null; // slug of last active project
}

export interface ToolCallData {
  id: string;
  name: string;
  input: unknown;
  completed?: boolean;
  result?: unknown;
  isError?: boolean;
}

export interface AttachmentData {
  name: string;
  path: string;
  dataUrl: string; // small enough for inline display; full bytes also on disk
  mime: string;
}

// Token usage for one turn (the result event's usage).
// - input/output: NEW tokens that turn (input excludes cache_read) — summed
//   across turns for the cumulative session total.
// - context: the turn's TOTAL input incl. cache_read = the point-in-time context
//   FILL (what the CLIs show as "% context used"); NOT summed across turns.
export interface TokenUsage {
  input: number;
  output: number;
  context?: number;
}

export interface MessageData {
  role: 'user' | 'assistant' | 'system';
  text: string;
  at?: string;
  toolCalls?: ToolCallData[];
  tokens?: TokenUsage;
  attachments?: AttachmentData[];
  // v2.x Cloud Sync: stable client-generated id, assigned at first persist and
  // reused on every re-push — the LWW dedupe key for transcript sync.
  uuid?: string;
  // True for an assistant THINKING/reasoning entry (extended-thinking content
  // blocks) — rendered as a collapsed "Thinking" disclosure.
  thinking?: boolean;
}

export interface AgentData {
  id: string;
  label: string;
  role: Role;
  cli: Cli;
  model?: string | null;
  effort?: string;
  skipPerms?: boolean;
  coderId?: string;
  osHint?: OsHint;
  sessionId?: string | null;
  status?: AgentStatus;
  createdAt?: string;
  updatedAt?: string;
  messages: MessageData[];
  // v2.x Cloud Sync: true for a conversation materialized from ANOTHER device's
  // pulled transcript — view-only (no local session/model), so the composer is
  // disabled to avoid forking a fresh local session.
  readOnly?: boolean;
}

export interface LaunchPayload {
  role: Role;
  cli: Cli;
  projectSlug: string;
  coderId?: string;
  osHint?: OsHint;
  suggestedCmd?: string;
  model?: string | null;
  skipPerms?: boolean;
  cwd?: string | null;
}

export interface SiblingAgent {
  label: string;
  role: Role;
  cli: Cli;
  coderId?: string;
  status?: AgentStatus;
}

export interface ChatSendPayload {
  chatId: string;
  prompt: string;
  sessionId?: string | null;
  projectSlug: string;
  coderId?: string;
  osHint?: OsHint;
  cli?: Cli;
  model?: string | null;
  effort?: string | null;
  skipPerms?: boolean;
  agentLabel?: string;
  agentRole?: Role;
  bootstrap?: boolean;
  siblings?: SiblingAgent[];
  // v2.x: Supabase access token to use as the MCP bearer (tenant identity).
  // When present it replaces the legacy shared key for this agent's MCP auth.
  authToken?: string | null;
  requireAuthToken?: boolean;
}

export interface ChatEvent {
  type: string;
  subtype?: string;
  session_id?: string;
  message?: {
    content?: Array<
      | { type: 'text'; text: string }
      | { type: 'thinking'; thinking: string }
      | { type: 'tool_use'; id: string; name: string; input: unknown }
      | { type: 'tool_result'; tool_use_id: string; content: unknown; is_error?: boolean }
    >;
  };
  total_cost_usd?: number;
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    cache_creation_input_tokens?: number;
    cache_read_input_tokens?: number;
  };
  text?: string;
}

declare global {
  interface Window {
    agentshive: {
      config: {
        get: () => Promise<ConfigState>;
        set: (patch: { baseUrl?: string; apiKey?: string; defaultOsHint?: string | null }) => Promise<ConfigState>;
      };
      projects: {
        list: () => Promise<{ projects: Project[] } | Project[]>;
        create: (slug: string, name: string) => Promise<unknown>;
      };
      paths: {
        get: (slug: string) => Promise<string | null>;
        set: (slug: string, path: string | null) => Promise<string | null>;
        pick: (slug: string) => Promise<string | null>;
      };
      prefs: {
        get: (slug: string) => Promise<Record<string, unknown> | null>;
        set: (slug: string, prefs: Record<string, unknown>) => Promise<Record<string, unknown>>;
      };
      workspace: {
        get: () => Promise<WorkspaceState>;
        set: (patch: Partial<WorkspaceState>) => Promise<WorkspaceState>;
      };
      agents: {
        list: (slug: string) => Promise<AgentData[]>;
        save: (slug: string, agent: AgentData) => Promise<{ ok: boolean }>;
        delete: (slug: string, id: string) => Promise<{ ok: boolean }>;
      };
      agent: {
        launch: (payload: LaunchPayload) => Promise<{ ok: boolean; error?: string; cwd?: string | null }>;
        embed: (payload: LaunchPayload) => Promise<{ id: string }>;
      };
      chat: {
        send: (payload: ChatSendPayload) => Promise<{ started: boolean; pid: number }>;
        cancel: (chatId: string) => Promise<void>;
        onEvent: (chatId: string, cb: (ev: ChatEvent) => void) => () => void;
        onStderr: (chatId: string, cb: (t: string) => void) => () => void;
        onDone: (chatId: string, cb: (p: { code: number }) => void) => () => void;
        onError: (chatId: string, cb: (p: { message: string }) => void) => () => void;
      };
      dashboard: {
        url: (slug: string) => Promise<string | null>;
        open: (slug: string) => Promise<boolean>;
        state: (slug: string) => Promise<DashboardState>;
      };
      app: {
        hostname: () => Promise<string>;
        version: () => Promise<string>;
      };
      auth: {
        setToken: (token: string | null) => Promise<{ ok: boolean }>;
      };
      authStore: {
        get: (key: string) => Promise<string | null>;
        set: (key: string, value: string) => Promise<{ ok: boolean }>;
        remove: (key: string) => Promise<{ ok: boolean }>;
      };
      files: {
        isGitRepo: (projectSlug: string) => Promise<boolean>;
        undoEdits: (projectSlug: string, paths: string[]) => Promise<UndoEditsResult>;
      };
      codex: {
        // The codex CLI's configured default model (~/.codex/config.toml). null
        // when codex isn't configured / no model line. Used to label the
        // effective model for ChatGPT-account codex agents (no -m is passed).
        defaultModel: () => Promise<string | null>;
      };
      // v2.x long-lived agent tokens (`ahat_`). Operator-minted per machine,
      // tenant-bound, never expires — replaces the 1h Supabase-JWT spawn bearer.
      // The plaintext value lives in cfg (userData) and is used by main for
      // spawn env injection; the renderer NEVER sees the secret value EXCEPT in
      // the `mint` response (so the UI can show a copy-once modal).
      agentTokens: {
        ensure: () => Promise<{ ok: boolean; id?: string; label?: string; prefix?: string; mintedAt?: string }>;
        list: () => Promise<{ tokens: AgentTokenLite[]; error?: string }>;
        revoke: (id: string) => Promise<{ ok: boolean; error?: string }>;
        mint: (label?: string) => Promise<{ ok: boolean; id?: string; label?: string; prefix?: string; token?: string; error?: string }>;
      };
      // Mission B P1: forward the renderer's batched observed-presence to
      // /api/dashboard/presence with the operator's agent token. Empty/missing
      // agents array no-ops cleanly. Failure (no bearer, network) returns
      // {ok:false} but never throws.
      presence: {
        publish: (project: string, agents: Array<{
          agent_key: string;
          state: 'idle' | 'working' | 'dead';
          detail?: string | null;
          observed_at: string;
        }>) => Promise<{ ok: boolean; status?: number; skipped?: boolean; error?: string }>;
      };
      skills: {
        list: (projectSlug: string) => Promise<SkillItem[]>;
      };
      admin: {
        listUsers: (token: string) => Promise<{ users: AdminUser[] }>;
        setBanned: (token: string, sub: string, banned: boolean) => Promise<{ ok: boolean }>;
        setPlan: (token: string, sub: string, plan: string) => Promise<{ ok: boolean }>;
        removeUser: (token: string, sub: string) => Promise<{ ok: boolean; deleted?: Record<string, number> }>;
      };
      missions: {
        syncDocs: (projectSlug: string) => Promise<{ ok: boolean; written?: number; total?: number; reason?: string }>;
        export: (projectSlug: string) => Promise<MissionsExport>;
      };
      web: {
        presence: (token: string, project: string, agents: WebRosterAgent[]) => Promise<{ ok: boolean; count?: number }>;
        inbound: (token: string) => Promise<{ messages: WebInboundMessage[] }>;
        ack: (token: string, messageId: string) => Promise<{ ok?: boolean; error?: string }>;
        relay: (token: string, parentId: string | null, project: string, agentKey: string | null, body: string) => Promise<unknown>;
        // v2.x Cloud Sync (opt-in).
        me: (token: string) => Promise<Entitlements>;
        syncPush: (token: string, payload: SyncPushPayload) => Promise<{ ok?: boolean; synced?: number; gated?: boolean; error?: string }>;
        syncPull: (token: string, project: string, since: string | null) => Promise<SyncPullResult>;
      };
      tools: {
        status: () => Promise<{
          gh: ToolStatus;
          railway: ToolStatus;
          vercel: ToolStatus;
        }>;
        connect: (tool: 'gh' | 'railway' | 'vercel') => Promise<{ ok: boolean }>;
      };
      attachments: {
        save: (payload: { agentId: string; projectSlug: string; name: string; dataUrl: string }) => Promise<{ path: string; bytes: number }>;
      };
      updates: {
        onAvailable: (cb: (info: UpdateInfo) => void) => () => void;
        onProgress: (cb: (p: UpdateProgress) => void) => () => void;
        onDownloaded: (cb: (info: UpdateInfo) => void) => () => void;
        onError: (cb: (p: { message: string }) => void) => () => void;
        quitAndInstall: () => Promise<void>;
      };
    };
  }
}

// Auto-update payloads bridged from electron-updater (main process).
export interface UpdateInfo {
  version?: string;
}
export interface UpdateProgress {
  percent?: number;
}

export interface ToolStatus {
  tool: 'gh' | 'railway' | 'vercel';
  installed: boolean;
  authenticated: boolean;
  identity: string | null;
}

// A file-based slash command available in the chat input's `/` autocomplete.
export interface SkillItem {
  name: string;
  description: string;
  source: 'user-skill' | 'user-command' | 'project-command';
  kind: 'skill' | 'command';
}

// Companion-webapp relay shapes (desktop side).
export interface WebRosterAgent {
  agent_key: string;
  label: string;
  role: string;
  cli: string;
  status: string;
}
export interface WebInboundMessage {
  message_id: string;
  body: string;
  agent_key: string | null;
  project_slug: string | null;
  created_at: string;
}

// v2.x Cloud Sync (opt-in) shapes.
export interface Entitlements {
  sub: string;
  email: string | null;
  plan: string;
  cloud_sync: boolean; // resolved entitlement (flag OR pro_unlimited)
}
export interface SyncMessagePayload {
  uuid: string;
  idx: number;
  role: 'user' | 'assistant' | 'system';
  text: string;
  tool_calls?: ToolCallData[] | null;
  tokens?: TokenUsage | null;
  created_at?: string;
}
export interface SyncPushPayload {
  project: string;
  agent_id: string;
  label?: string | null;
  role?: string | null;
  cli?: string | null;
  messages: SyncMessagePayload[];
}
export interface SyncedConversationDTO {
  agent_id: string;
  project_slug: string;
  label: string | null;
  role: string | null;
  cli: string | null;
  updated_at: string;
  messages: Array<SyncMessagePayload & { updated_at?: string }>;
}
export interface SyncPullResult {
  conversations?: SyncedConversationDTO[];
  cursor?: string | null;
  gated?: boolean;
  error?: string;
}

// Full mission export (read-only) backing the right-side missions panel + docs.
export interface FoundationMission {
  name: string;
  spec: string | null;
  set_at: string | null;
}
export interface ExportedMission {
  mission_id: string;
  name: string;
  spec: string;
  status: string;
  created_at: string;
  done_at: string | null;
  summaries?: Array<{ body: string; response: string | null; created_at?: string; coder_id?: string | null }>;
  questions?: Array<{ body: string; answer: string | null }>;
}
export interface MissionsExport {
  project: { slug: string; name: string } | null;
  foundation: FoundationMission | null;
  missions: ExportedMission[];
}

// A user row in the secret admin panel (Supabase user JOIN our Tenant row).
export interface AdminUser {
  sub: string;
  email: string | null;
  role: string | null;
  plan: string;
  subscription_status: string;
  trial_reports_used: number;
  banned: boolean;
  created_at: string | null;
  project_count: number;
  mission_count: number;
}

// Result of a git-restore Undo of a turn's changed files.
export interface UndoEditsResult {
  ok: boolean;
  reverted?: string[];
  skipped?: string[];
  reason?: string;
  backupDir?: string;
}

export const ah = () => window.agentshive;

// --- file-edit visibility (Cursor-style "what files the agent touched") -------
// Built-in claude tools that touch a file carry the path in their input. Edit/
// Write/MultiEdit/NotebookEdit CHANGE the file; Read just reads it. (codex shell
// calls have no file_path → null.)
const FILE_CHANGE_TOOLS = new Set(['Edit', 'Write', 'MultiEdit', 'NotebookEdit']);

export function toolFileTarget(call: ToolCallData): { path: string; changed: boolean } | null {
  const name = (call.name || '').split('__').pop() || call.name || '';
  const input = (call.input || {}) as Record<string, unknown>;
  const raw = input.file_path ?? input.notebook_path;
  const path = typeof raw === 'string' && raw ? raw : null;
  if (!path) return null;
  if (FILE_CHANGE_TOOLS.has(name)) return { path, changed: true };
  if (name === 'Read') return { path, changed: false };
  return null;
}

/** Deduped set of files CHANGED (not just read) by a group of tool calls, in
 *  first-touch order. */
export function changedFiles(calls: ToolCallData[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const c of calls) {
    const t = toolFileTarget(c);
    if (t && t.changed && !seen.has(t.path)) {
      seen.add(t.path);
      out.push(t.path);
    }
  }
  return out;
}

// Line-count diff for an edit tool call. Edit = old→new line delta; MultiEdit =
// summed over edits[]; Write = new content as all-insertions. A sensible
// line-count delta (not a true LCS diff) — enough for the "+N -M" affordance.
export interface EditStat {
  added: number;
  removed: number;
}
function _lineCount(s: unknown): number {
  return typeof s === 'string' && s.length ? s.split('\n').length : 0;
}
export function editStats(call: ToolCallData): EditStat | null {
  const name = (call.name || '').split('__').pop() || call.name || '';
  const input = (call.input || {}) as Record<string, any>;
  if (name === 'Edit') {
    return { removed: _lineCount(input.old_string), added: _lineCount(input.new_string) };
  }
  if (name === 'MultiEdit') {
    const edits = Array.isArray(input.edits) ? input.edits : [];
    let added = 0;
    let removed = 0;
    for (const e of edits) { removed += _lineCount(e?.old_string); added += _lineCount(e?.new_string); }
    return { added, removed };
  }
  if (name === 'Write') {
    return { added: _lineCount(input.content), removed: 0 };
  }
  return null;
}

// Changed files for a group of tool calls WITH aggregated +/- line stats per
// path (deduped, first-touch order).
export interface FileChange {
  path: string;
  added: number;
  removed: number;
}
export function changedFilesWithStats(calls: ToolCallData[]): FileChange[] {
  const byPath = new Map<string, FileChange>();
  const order: string[] = [];
  for (const c of calls) {
    const t = toolFileTarget(c);
    if (!t || !t.changed) continue;
    if (!byPath.has(t.path)) { byPath.set(t.path, { path: t.path, added: 0, removed: 0 }); order.push(t.path); }
    const st = editStats(c);
    if (st) {
      const fc = byPath.get(t.path)!;
      fc.added += st.added;
      fc.removed += st.removed;
    }
  }
  return order.map((p) => byPath.get(p)!);
}

export function basename(p: string): string {
  const parts = p.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || p;
}

/** The shell command for a command tool call (claude Bash / codex shell), else null. */
export function toolCommand(call: ToolCallData): string | null {
  const name = (call.name || '').split('__').pop() || call.name || '';
  if (name !== 'Bash' && name !== 'shell') return null;
  const cmd = (call.input as Record<string, unknown> | null)?.command;
  return typeof cmd === 'string' && cmd ? cmd : null;
}

export function unwrapProjects(raw: { projects: Project[] } | Project[]): Project[] {
  if (Array.isArray(raw)) return raw;
  if (raw && Array.isArray(raw.projects)) return raw.projects;
  return [];
}

export function slugify(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 42);
}

export function validateSlug(s: string): string | null {
  if (!s) return 'Slug is required.';
  if (s.length > 42) return 'Slug too long (max 42 chars).';
  if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(s)) {
    return 'Slug must be lowercase letters/digits with internal hyphens.';
  }
  return null;
}

export const MODEL_OPTIONS: Record<Cli, Array<{ value: string; label: string }>> = {
  // Opus is the default (latest). The `opus` alias resolves to the newest Opus
  // the installed claude CLI knows about, so this never hard-fails on a version
  // that doesn't exist yet — it tracks "latest Opus" automatically.
  claude: [
    { value: 'opus', label: 'Opus 4.8 (default)' },
    { value: 'sonnet', label: 'Sonnet 4.6' },
    { value: 'haiku', label: 'Haiku 4.5' },
  ],
  // ChatGPT-account codex has NO selectable model — only the account's default
  // works (verified on codex 0.124: gpt-5, gpt-5.1, gpt-5-codex, gpt-5.5-codex
  // all 400 with "not supported when using Codex with a ChatGPT account"). We
  // pass no -m and let codex use the account default; the UI shows it as a
  // read-only label (sourced from ~/.codex/config.toml via ah().codex.defaultModel),
  // not a dropdown. The single empty-value entry keeps callers that read
  // MODEL_OPTIONS.codex[0].value defaulting to "" (no -m).
  codex: [
    { value: '', label: 'ChatGPT account default' },
  ],
};

// Effort/reasoning levels per CLI. Claude exposes the full set via `--effort`.
// Codex reasoning effort DOES apply on a ChatGPT-account login (verified on
// codex 0.124 — this overrides the old "needs API key" caveat): low/medium/high/
// xhigh all work via `-c model_reasoning_effort`, even with no -m. `minimal` is
// excluded (codex 400s it when the image_gen/web_search tools are enabled).
export const EFFORT_OPTIONS: Record<Cli, Array<{ value: string; label: string }>> = {
  claude: [
    { value: '', label: 'default' },
    { value: 'low', label: 'Low' },
    { value: 'medium', label: 'Medium' },
    { value: 'high', label: 'High' },
    { value: 'xhigh', label: 'Xhigh' },
    { value: 'max', label: 'Max' },
  ],
  codex: [
    { value: '', label: 'default' },
    { value: 'low', label: 'Low' },
    { value: 'medium', label: 'Medium' },
    { value: 'high', label: 'High' },
    { value: 'xhigh', label: 'Xhigh' },
  ],
};

export function buildCmd(
  cli: Cli,
  model: string | null | undefined,
  effort: string | null | undefined,
  skipPerms: boolean,
  resume = false,
): string {
  const parts: string[] = [cli];
  if (cli === 'claude') {
    if (model) parts.push('--model', model);
    if (effort) parts.push('--effort', effort);
    if (resume) parts.push('--continue');
    if (skipPerms) parts.push('--dangerously-skip-permissions');
  } else if (cli === 'codex') {
    if (model) parts.push('-m', model);
    // Reasoning effort applies on a ChatGPT-account login too (verified codex
    // 0.124), even with no -m — codex applies it to the account's default model.
    if (effort) parts.push('-c', `model_reasoning_effort="${effort}"`);
    if (skipPerms) parts.push('--dangerously-bypass-approvals-and-sandbox');
    if (resume) return 'codex resume';
  }
  return parts.join(' ');
}

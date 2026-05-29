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

export interface DashboardState {
  active_mission?: unknown;
  pending_q?: PendingQuestionLite[];
  pending_s?: PendingSummaryLite[];
  c2p?: InboxMessageLite[];
  p2c?: InboxMessageLite[];
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
  // ChatGPT-account codex logins must NOT pass an explicit -m (both gpt-5 and
  // gpt-5-codex are rejected with a 400 by ChatGPT-account auth) — the empty
  // default sends no -m so codex uses the account's allowed model. gpt-5-codex
  // only works with an OpenAI API-key login (codex login with an API key).
  codex: [
    { value: '', label: 'default (ChatGPT account)' },
    { value: 'gpt-5-codex', label: 'gpt-5-codex (needs OpenAI API key)' },
  ],
};

// Effort/reasoning levels per CLI. Claude exposes the full set via `--effort`.
// Codex only honors reasoning effort with an OpenAI API-key login (and only the
// standard low/medium/high values), so its options carry that caveat in-label
// and are no-ops on the ChatGPT-account default (no model selected).
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
    { value: 'low', label: 'Low (needs OpenAI API key)' },
    { value: 'medium', label: 'Medium (needs OpenAI API key)' },
    { value: 'high', label: 'High (needs OpenAI API key)' },
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
    // Reasoning effort only applies with an explicit model (OpenAI API-key
    // login); a ChatGPT-account login (no -m) rejects model/effort overrides.
    if (model && effort) parts.push('-c', `model_reasoning_effort="${effort}"`);
    if (skipPerms) parts.push('--dangerously-bypass-approvals-and-sandbox');
    if (resume) return 'codex resume';
  }
  return parts.join(' ');
}

// AgentsHive Desktop main process.
//
// Owns: window lifecycle, config persistence (userData/config.json), HTTP calls
// to the AgentsHive server, and child-process spawning for Claude/Codex CLI
// agents in external terminals.
//
// The renderer talks to us strictly through the `agentshive` bridge declared
// in preload.js. The API key never leaves the main process via IPC return
// values — it's only used to build the Authorization header server-side.

const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const { autoUpdater } = require('electron-updater');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { spawn, execFile } = require('child_process');
// node-pty for embedded terminals. We use the @homebridge prebuilt fork
// since the upstream node-pty needs Visual Studio Build Tools on Windows.
let pty = null;
try {
  pty = require('@homebridge/node-pty-prebuilt-multiarch');
} catch (err) {
  console.error('node-pty failed to load — embedded terminals disabled:', err.message);
}

// In-memory map of active embedded PTYs.
const ptys = new Map();
let nextPtyId = 1;

const CONFIG_FILE = () => path.join(app.getPath('userData'), 'config.json');
const AGENTS_DIR = (slug) => path.join(app.getPath('userData'), 'agents', encodeURIComponent(slug));

function safeSlug(s) {
  return String(s || '').replace(/[^a-z0-9-]/gi, '-').slice(0, 80);
}

const DEFAULT_CONFIG = {
  baseUrl: 'https://agentshive-production.up.railway.app',
  apiKey: '',
  defaultOsHint: detectOsHint(),
  // Per-project local folders. Stored client-side (not on the server) because
  // the same project slug maps to different paths on Mac vs Windows.
  projectPaths: {},
  // Per-project launcher prefs (role, cli, model, skipPerms, resume, osHint).
  projectPrefs: {},
  // VS-Code-style workspace state: which projects are "opened" in the sidebar,
  // their collapse state, and which one was active last. App-global (not
  // per-project) so it lives here rather than in projectPrefs.
  workspace: {
    openedProjects: [], // string[] of project slugs, in sidebar order
    collapsed: {},      // { [slug]: boolean } — true = folder collapsed
    lastActive: null,   // slug of the last active project, or null
  },
};

function legacyKeyEnabled() {
  return String(process.env.AGENTSHIVE_LEGACY_KEY_ENABLED || '1').trim().toLowerCase() !== '0';
}

const APP_ICON = path.join(
  __dirname,
  '..',
  'assets',
  'icons',
  process.platform === 'win32' ? 'agentshive-icon.ico' : 'agentshive-icon-512.png',
);

function detectOsHint() {
  switch (process.platform) {
    case 'win32': return 'windows';
    case 'darwin': return 'macos';
    case 'linux': return 'linux';
    default: return null;
  }
}

function readConfig() {
  try {
    const raw = fs.readFileSync(CONFIG_FILE(), 'utf8');
    return { ...DEFAULT_CONFIG, ...JSON.parse(raw) };
  } catch {
    return { ...DEFAULT_CONFIG };
  }
}

function writeConfig(patch) {
  const merged = { ...readConfig(), ...patch };
  fs.mkdirSync(path.dirname(CONFIG_FILE()), { recursive: true });
  fs.writeFileSync(CONFIG_FILE(), JSON.stringify(merged, null, 2), 'utf8');
  return merged;
}

async function apiFetch(pathAndQuery, init = {}) {
  const cfg = readConfig();
  if (!cfg.baseUrl) throw new Error('baseUrl not configured');
  if (!cfg.apiKey) throw new Error('apiKey not configured');
  const url = cfg.baseUrl.replace(/\/$/, '') + pathAndQuery;
  const headers = {
    Authorization: `Bearer ${cfg.apiKey}`,
    Origin: cfg.baseUrl,
    ...(init.headers || {}),
  };
  if (init.body && typeof init.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(init.body);
  }
  const res = await fetch(url, { ...init, headers });
  const text = await res.text();
  let parsed = null;
  try { parsed = text ? JSON.parse(text) : null; } catch { parsed = text; }
  if (!res.ok) {
    const err = new Error(`HTTP ${res.status}: ${typeof parsed === 'string' ? parsed.slice(0, 200) : JSON.stringify(parsed).slice(0, 200)}`);
    err.status = res.status;
    err.body = parsed;
    throw err;
  }
  return parsed;
}

// v2.x: the operator's Supabase access token, pushed from the renderer on every
// auth change (auth:setToken). Lets main-process dashboard READS (missions
// export + the cross-machine state poll) query the operator's OWN tenant when
// signed in — the dashboard endpoints now accept a Supabase JWT. Falls back to
// the legacy shared key when signed out. Keeps the renderer IPC signatures
// unchanged (callers don't pass a token).
let _supabaseToken = null;

// A dashboard request that uses the operator's Supabase tenant when signed in,
// else the legacy shared key. Defaults to GET; pass {method, body} for writes
// (e.g. projects:create) so a signed-in user's writes land under THEIR tenant —
// not the legacy tenant — matching where their agents/MCP connection resolve.
function dashboardFetch(pathAndQuery, opts = {}) {
  const method = opts.method || 'GET';
  if (_supabaseToken) return adminApiFetch(method, pathAndQuery, _supabaseToken, opts.body);
  return apiFetch(pathAndQuery, method === 'GET' ? {} : { method, body: opts.body });
}

// Build the env vars an agent CLI needs to talk to AgentsHive for this
// project. Returns a plain object — callers compose into spawn env.
function agentEnv({ projectSlug, coderId, osHint, authToken, requireAuthToken }) {
  const cfg = readConfig();
  // v2.x: when the renderer passes a Supabase access token, it becomes the MCP
  // bearer (tenant identity) for BOTH claude (--mcp-config ${AGENTSHIVE_API_KEY})
  // and codex (bearer_token_env_var=AGENTSHIVE_API_KEY). We keep the env var NAME
  // so neither CLI's MCP wiring changes — only the value. Falls back to the legacy
  // shared key when no token is supplied (transitional, pre-cutover).
  const bearer = authToken && String(authToken).trim();
  if (requireAuthToken && !bearer) {
    throw new Error('Supabase session is active but no access token is available. Sign in again before launching agents.');
  }
  if (!bearer && !legacyKeyEnabled()) {
    throw new Error('Legacy shared-key auth is disabled. Sign in with Supabase before launching agents.');
  }
  const finalBearer = bearer || cfg.apiKey;
  if (!finalBearer) throw new Error('No AgentsHive auth token configured.');
  return {
    AGENTSHIVE_BASE_URL: cfg.baseUrl,
    AGENTSHIVE_API_KEY: finalBearer,
    AGENTSHIVE_PROJECT: projectSlug,
    AGENTSHIVE_CODER_ID: coderId || '',
    AGENTSHIVE_OS_HINT: osHint || cfg.defaultOsHint || '',
  };
}

// Minimal AGENTS.md template — drops the protocol into the project folder so
// claude/codex pick up the rules without us prepending a long preamble. Kept
// terse on purpose; the full skill lives at ~/.claude/skills/agentshive.
const AGENTS_MD_TEMPLATE = `<!-- AGENTSHIVE_PROJECT_SLUG: {slug} -->
# AgentsHive coordination — \`{slug}\`

This project uses **AgentsHive** so multiple AI agents coordinate via a shared MCP bridge instead of routing every decision through the human.

> Generated by AgentsHive Desktop. Edit freely — agents read what's here.

## Step 0 — Verify project scope (do this FIRST, every new conversation)

Before ANY mutating tool call, call \`mcp__agentshive__get_project_info()\` and confirm the returned \`slug\` matches the sentinel at the top of this file (\`{slug}\`).

If they match — proceed.
If they mismatch — STOP. Call \`mcp__agentshive__send_to_user\` describing actual vs expected slug. Do NOT call \`create_mission\`, \`answer_question\`, \`submit_progress\`, etc.

## Roles

- **Hivemind** (Planner) — orchestrates. Doesn't write code. One per project.
- **Coder** — implements. Reports to the Hivemind. Multiple allowed.

## Hivemind workflow

1. Talk with the user to understand what they want built.
2. Call \`create_mission(brief)\` once scope is clear (include: goal, acceptance criteria, constraints, definition of done).
3. Long-poll Coder activity:
   - \`wait_for_next_question(timeout=240)\` → answer with \`answer_question(question_id, response)\`
   - \`wait_for_next_summary(timeout=240)\` → respond with \`respond_to_summary(summary_id, response)\` or course-correct via \`send_to_coder(message)\`
4. \`mark_mission_done(mission_id)\` when complete.

Only one mission active at a time. Creating a new one supersedes the previous.

## Coder workflow

1. Call \`get_active_mission()\` first — read the brief.
2. Implement. Don't ask the human for things the Hivemind can answer:
   - Stuck on requirements? → \`ask_planner(question)\` then \`wait_for_answer(question_id, timeout=240)\`
   - Truly user-only info (credentials, personal preferences)? → ask the user.
3. At milestones: \`submit_progress(summary, status)\` (status: in_progress|blocked|done), then \`wait_for_summary_response(summary_id, timeout=240)\`.

## Project context

- **Slug**: \`{slug}\` — every agent MUST use this exact slug.
- **Server**: \`{server}\`
- **MCP URL**: \`{server}/mcp?project={slug}\`

The \`.mcp.json\` in this folder is auto-generated by AgentsHive Desktop. The API key is referenced via \`\${AGENTSHIVE_API_KEY}\` so no secret is on disk.

## Full skill

See \`~/.claude/skills/agentshive/SKILL.md\` for the complete protocol reference.
`;

function ensureAgentsMd({ cwd, slug, baseUrl }) {
  if (!cwd || !slug) return { written: false };
  const target = path.join(cwd, 'AGENTS.md');
  const body = AGENTS_MD_TEMPLATE.replace(/\{slug\}/g, slug).replace(/\{server\}/g, (baseUrl || '').replace(/\/$/, ''));
  try {
    if (fs.existsSync(target)) {
      const existing = fs.readFileSync(target, 'utf8');
      if (existing.includes('AgentsHive coordination')) {
        return { written: false, reason: 'already has AgentsHive section', path: target };
      }
      fs.writeFileSync(target, existing.replace(/\s+$/, '') + '\n\n---\n\n' + body, 'utf8');
      return { written: true, appended: true, path: target };
    }
    fs.writeFileSync(target, body, 'utf8');
    return { written: true, appended: false, path: target };
  } catch (err) {
    return { written: false, reason: err.message };
  }
}

function buildSiblingBlock(siblings, selfRole) {
  if (!Array.isArray(siblings) || siblings.length === 0) return '';
  // Lead with the most relevant counterparties: for a Hivemind, Coders matter
  // most (they're the targets for send_to_coder/answer_question). For a Coder,
  // the Hivemind matters most (they're who you ask_planner / submit_progress to).
  const lines = siblings
    .map((s) => `  - ${s.label} (${s.role}, ${s.cli}${s.coderId ? `, coder_id="${s.coderId}"` : ''})`)
    .join('\n');
  const hint =
    selfRole === 'hivemind'
      ? 'When you call send_to_coder / answer_question / respond_to_summary, target Coders by their coder_id from the list above.'
      : 'These are your collaborators on this project. The Hivemind is who you ask_planner / submit_progress to.';
  return `\n\n[AgentsHive Desktop — sidebar context]\nOther agents currently in the operator's sidebar for project "${siblings[0] ? '' : ''}":\n${lines}\n${hint}\n\n`;
}

function buildRoleBriefing({ label, role, slug, cwd, isBootstrap }) {
  const isHivemind = role === 'hivemind';
  const cheatsheet = isHivemind
    ? '- create_mission(brief) when scope is clear\n- wait_for_next_question/summary to long-poll Coders\n- answer_question / respond_to_summary / send_to_coder to course-correct\n- mark_mission_done when complete'
    : '- get_active_mission() to read the brief\n- ask_planner / wait_for_answer when stuck on requirements\n- submit_progress(summary, status) at milestones, then wait_for_summary_response';
  const header = `You are an AgentsHive agent.

Identity:
- Name: ${label}
- Role: ${role}${isHivemind ? ' (Planner — you orchestrate, do not write code yourself)' : ' (you implement; you report to the Hivemind)'}
- Project: ${slug}
- Working folder: ${cwd}

You have the AgentsHive MCP server wired up — tools are prefixed \`mcp__agentshive__\`. The full protocol is in AGENTS.md in this folder.

Cheatsheet for your role:
${cheatsheet}

Project north-star: this project has a durable FOUNDATION MISSION (its ultimate goal). Call \`mcp__agentshive__get_foundation_mission()\` — or read the \`foundation\` field in get_project_info / get_active_mission — to re-ground on the project's purpose, especially if you've lost prior conversation context. The foundation persists and is never superseded by the rotating active mission.

`;
  if (isBootstrap) {
    return header + `## Your task right now (no operator message yet)

Do exactly these three things, in order:

1. Call \`mcp__agentshive__get_project_info()\` and confirm the returned slug equals "${slug}". If it doesn't, STOP and report the mismatch — do NOT call any other AgentsHive tool.
2. Read AGENTS.md (use the Read tool) so you know the protocol.
3. Send the operator a brief greeting: one sentence introducing yourself by name and role, and one sentence confirming you've verified scope. Then await their instructions.

Do NOT start working on tasks yet — just verify scope, read the protocol, and greet.
`;
  }
  // Non-bootstrap path (legacy): briefing followed by operator message.
  return header + `Before responding, call \`mcp__agentshive__get_project_info()\` to confirm scope (slug should equal "${slug}"). If mismatched, STOP and report.

---
Operator's message follows:

`;
}

// Write a project-scoped .mcp.json into cwd so Claude Code auto-wires the
// agentshive MCP server when it boots in this folder. Idempotent: merges
// into any existing .mcp.json without clobbering other servers. The API key
// is referenced via ${AGENTSHIVE_API_KEY} substitution so the secret is NOT
// written to disk — Claude Code resolves the env var at request time, and
// we set AGENTSHIVE_API_KEY in the terminal's env before invoking claude.
// Per-agent MCP config in userData. Each agent gets its own isolated config
// file so two agents in the same folder targeting different projects can't
// stomp on each other's .mcp.json. We pass `--mcp-config <path>` to claude.
function writeAgentMcpConfig({ agentId, projectSlug, baseUrl }) {
  const dir = AGENTS_DIR(projectSlug);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, safeSlug(agentId) + '.mcp.json');
  const url = `${(baseUrl || '').replace(/\/$/, '')}/mcp?project=${encodeURIComponent(projectSlug)}`;
  const config = {
    mcpServers: {
      agentshive: {
        type: 'http',
        url,
        headers: { Authorization: 'Bearer ${AGENTSHIVE_API_KEY}' },
      },
    },
  };
  fs.writeFileSync(file, JSON.stringify(config, null, 2) + '\n', 'utf8');
  return file;
}

function deleteAgentMcpConfig({ agentId, projectSlug }) {
  const dir = AGENTS_DIR(projectSlug);
  const file = path.join(dir, safeSlug(agentId) + '.mcp.json');
  try { fs.unlinkSync(file); } catch { /* already gone */ }
}

function ensureMcpConfig({ cwd, baseUrl, projectSlug }) {
  if (!cwd) return { written: false, reason: 'no folder set — MCP not auto-registered' };
  const mcpPath = path.join(cwd, '.mcp.json');
  let existing = { mcpServers: {} };
  try {
    const raw = fs.readFileSync(mcpPath, 'utf8');
    existing = JSON.parse(raw);
    if (!existing || typeof existing !== 'object') existing = { mcpServers: {} };
    if (!existing.mcpServers || typeof existing.mcpServers !== 'object') existing.mcpServers = {};
  } catch {
    // No existing file or unparseable — start fresh.
  }
  const url = `${baseUrl.replace(/\/$/, '')}/mcp?project=${encodeURIComponent(projectSlug)}`;
  existing.mcpServers.agentshive = {
    type: 'http',
    url,
    headers: { Authorization: 'Bearer ${AGENTSHIVE_API_KEY}' },
  };
  try {
    fs.writeFileSync(mcpPath, JSON.stringify(existing, null, 2) + '\n', 'utf8');
    return { written: true, path: mcpPath };
  } catch (err) {
    return { written: false, reason: `write failed: ${err.message}` };
  }
}

// Spawn an external terminal pre-wired with the agent env vars + a hint
// command to run. We DON'T auto-execute the CLI — the user sees what's about
// to run and presses Enter. This avoids surprises if the CLI is missing or
// needs interactive auth on first use.
function launchAgent({ role, cli, projectSlug, coderId, osHint, cwd, suggestedCmd }) {
  const cfg = readConfig();
  const mcp = ensureMcpConfig({ cwd, baseUrl: cfg.baseUrl, projectSlug });
  const env = agentEnv({ projectSlug, coderId, osHint });
  const banner = buildBanner({ role, cli, projectSlug, coderId, osHint, cwd, mcp });
  // Suggested-cmd comes pre-built from the renderer (it knows about model
  // flags, resume, skip-perms). Fallback: bare cli name.
  const cliCmd = suggestedCmd || (cli === 'codex' ? 'codex' : 'claude');

  if (process.platform === 'win32') {
    return launchWindowsTerminal(env, banner, cliCmd, cwd);
  }
  if (process.platform === 'darwin') {
    return launchMacTerminal(env, banner, cliCmd, cwd);
  }
  return launchLinuxTerminal(env, banner, cliCmd, cwd);
}

function buildBanner({ role, cli, projectSlug, coderId, osHint, cwd, mcp }) {
  const mcpLine = mcp && mcp.written
    ? `  MCP: agentshive auto-wired in ${mcp.path}`
    : (mcp ? `  MCP: not auto-registered (${mcp.reason})` : null);
  const lines = [
    '============================================================',
    `  AgentsHive agent: ${role.toUpperCase()} (${cli})`,
    `  project: ${projectSlug}`,
    coderId ? `  coder_id: ${coderId}` : null,
    osHint ? `  os_hint: ${osHint}` : null,
    cwd ? `  cwd: ${cwd}` : null,
    mcpLine,
    '  Env pre-wired: AGENTSHIVE_BASE_URL, AGENTSHIVE_API_KEY,',
    '                 AGENTSHIVE_PROJECT, AGENTSHIVE_CODER_ID, AGENTSHIVE_OS_HINT',
    '============================================================',
    role === 'hivemind'
      ? '  HIVEMIND: Run `claude` (or your CLI) in this terminal.'
      : '  CODER: ready to go — run the CLI to start working.',
    '============================================================',
  ].filter(Boolean).join('\n');
  return lines;
}

function launchWindowsTerminal(env, banner, cliCmd, cwd) {
  // Build a PowerShell command that prints the banner and stays open. Don't
  // auto-run the CLI — let the user invoke it explicitly so missing-CLI
  // errors are visible.
  const setEnvLines = Object.entries(env)
    .map(([k, v]) => `$env:${k} = ${powerShellQuote(v)}`)
    .join('; ');
  const cdLine = cwd ? `Set-Location -LiteralPath ${powerShellQuote(cwd)}; ` : '';
  const echoBanner = banner.split('\n').map(l => `Write-Host ${powerShellQuote(l)}`).join('; ');
  const hint = `Write-Host ''; Write-Host 'Suggested next command:' -ForegroundColor Cyan; Write-Host ('  ' + ${powerShellQuote(cliCmd)}) -ForegroundColor Yellow; Write-Host ''`;
  const psCmd = `${setEnvLines}; ${cdLine}${echoBanner}; ${hint}`;

  // Prefer Windows Terminal (`wt.exe`); fall back to powershell in cmd. We
  // also pass `--startingDirectory` to wt as belt-and-suspenders, since `cd`
  // inside the PS command also handles it if wt ignores the flag.
  const wtArgs = ['new-tab'];
  if (cwd) wtArgs.push('--startingDirectory', cwd);
  wtArgs.push('powershell.exe', '-NoExit', '-Command', psCmd);
  const child = spawn('wt.exe', wtArgs, { detached: true, stdio: 'ignore', shell: false });
  child.on('error', () => {
    spawn('cmd.exe', ['/c', 'start', 'powershell.exe', '-NoExit', '-Command', psCmd], {
      detached: true, stdio: 'ignore', shell: false,
    });
  });
  child.unref();
}

function powerShellQuote(s) {
  // Single-quoted PowerShell string: escape ' as ''
  return `'${String(s).replace(/'/g, "''")}'`;
}

function launchMacTerminal(env, banner, cliCmd, cwd) {
  const exports = Object.entries(env)
    .map(([k, v]) => `export ${k}=${shQuote(v)}`)
    .join('; ');
  const cdLine = cwd ? `cd ${shQuote(cwd)}; ` : '';
  const echoBanner = `printf '%s\\n' ${shQuote(banner)}`;
  const hint = `printf '\\nSuggested next command:\\n  %s\\n\\n' ${shQuote(cliCmd)}`;
  const innerCmd = `${exports}; ${cdLine}${echoBanner}; ${hint}; exec $SHELL`;
  const osascript = `tell application "Terminal" to do script ${appleScriptQuote(innerCmd)}`;
  spawn('osascript', ['-e', osascript], { detached: true, stdio: 'ignore' }).unref();
}

function shQuote(s) {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

function appleScriptQuote(s) {
  return `"${String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
}

function launchLinuxTerminal(env, banner, cliCmd, cwd) {
  const exports = Object.entries(env)
    .map(([k, v]) => `export ${k}=${shQuote(v)}`)
    .join('; ');
  const cdLine = cwd ? `cd ${shQuote(cwd)}; ` : '';
  const echoBanner = `printf '%s\\n' ${shQuote(banner)}`;
  const hint = `printf '\\nSuggested next command:\\n  %s\\n\\n' ${shQuote(cliCmd)}`;
  const innerCmd = `${exports}; ${cdLine}${echoBanner}; ${hint}; exec $SHELL`;

  const candidates = [
    ['gnome-terminal', ['--', 'bash', '-c', innerCmd]],
    ['konsole', ['-e', 'bash', '-c', innerCmd]],
    ['xterm', ['-e', 'bash', '-c', innerCmd]],
  ];
  tryNext(candidates, 0);

  function tryNext(list, i) {
    if (i >= list.length) return;
    const [cmd, args] = list[i];
    const child = spawn(cmd, args, { detached: true, stdio: 'ignore' });
    child.on('error', () => tryNext(list, i + 1));
    child.unref();
  }
}

// --- IPC handlers ----------------------------------------------------------

ipcMain.handle('config:get', () => {
  const cfg = readConfig();
  // Don't leak the raw API key length to the renderer beyond a masked preview.
  return {
    baseUrl: cfg.baseUrl,
    apiKeyMasked: cfg.apiKey ? `${cfg.apiKey.slice(0, 4)}…${cfg.apiKey.slice(-4)}` : '',
    apiKeyConfigured: Boolean(cfg.apiKey),
    legacyKeyEnabled: legacyKeyEnabled(),
    defaultOsHint: cfg.defaultOsHint,
    platform: process.platform,
  };
});

ipcMain.handle('config:set', (_e, patch) => {
  const cleaned = {};
  if (typeof patch.baseUrl === 'string') cleaned.baseUrl = patch.baseUrl.trim();
  if (typeof patch.apiKey === 'string' && patch.apiKey.length > 0) cleaned.apiKey = patch.apiKey.trim();
  if (typeof patch.defaultOsHint === 'string') cleaned.defaultOsHint = patch.defaultOsHint;
  const merged = writeConfig(cleaned);
  return {
    baseUrl: merged.baseUrl,
    apiKeyConfigured: Boolean(merged.apiKey),
    legacyKeyEnabled: legacyKeyEnabled(),
    defaultOsHint: merged.defaultOsHint,
  };
});

// Tenant-aware (dashboardFetch): when signed in, list/create projects under the
// operator's Supabase tenant — matching where their agents/MCP connection resolve
// — instead of the legacy tenant. A legacy-only project row would be invisible to
// a signed-in Planner ("project does not exist"). Falls back to the legacy key
// when signed out. Renderer signatures unchanged.
ipcMain.handle('projects:list', async () => {
  return dashboardFetch('/api/dashboard/projects');
});

ipcMain.handle('projects:create', async (_e, { slug, name }) => {
  return dashboardFetch('/api/dashboard/projects', { method: 'POST', body: { slug, name } });
});

// --- embedded PTY IPC ----------------------------------------------------

ipcMain.handle('pty:available', () => Boolean(pty));

// --- agentic chat (claude --output-format=stream-json) -------------------
// One turn = one `claude --print --output-format=stream-json` subprocess.
// Streams JSONL events to the renderer; renderer renders them as cards.
// Multi-turn uses --resume <session_id> from the previous turn's init event.

const activeChats = new Map(); // chatId -> { child }

// --- codex embedded-chat support ------------------------------------------
// Codex's `exec --json` emits a different JSONL schema than claude's
// `stream-json`. We normalize codex events into the SAME ChatEvent shape the
// renderer (useActiveProject.handleEvent) already parses for claude — so the
// existing message/tool-call cards render with zero codex-specific branching
// in the UI. Codex MCP is wired per-invocation via `-c mcp_servers.*` overrides
// (the streamable-HTTP transport AgentsHive exposes), analogous to claude's
// per-agent --mcp-config; the bearer token is read from AGENTSHIVE_API_KEY in
// the spawn env (we never write the secret to disk).

function buildCodexExecArgs({ sessionId, model, effort, skipPerms, projectSlug, baseUrl }) {
  const url = `${(baseUrl || '').replace(/\/$/, '')}/mcp?project=${encodeURIComponent(projectSlug)}`;
  // Bareword TOML values: codex parses the value as TOML and falls back to a
  // literal string when that fails, so an unquoted URL / env-var name is taken
  // verbatim — which keeps these as space-free argv tokens (no shell quoting).
  const mcp = [
    '-c', `mcp_servers.agentshive.url=${url}`,
    '-c', 'mcp_servers.agentshive.bearer_token_env_var=AGENTSHIVE_API_KEY',
  ];
  // AgentsHive codex coders MUST reach the agentshive MCP — that's their whole
  // job. Verified that MCP tool calls FAIL under --full-auto headless ("user
  // cancelled MCP tool call": the workspace sandbox blocks the MCP call and the
  // approval auto-denies with no TTY), but SUCCEED under full bypass. So codex
  // always runs bypassed — these are autonomous agents the operator explicitly
  // launched (same intent as claude's --dangerously-skip-permissions). `skipPerms`
  // is therefore moot for codex.
  void skipPerms;
  const sandbox = '--dangerously-bypass-approvals-and-sandbox';
  const common = ['--json', '--skip-git-repo-check', sandbox, ...mcp];
  // Reasoning effort works on a ChatGPT-account login too (verified codex 0.124),
  // even with NO -m — codex applies it to the account's default model, and it's
  // honored on `resume` as well (exec-level -c precedes the subcommand). This is
  // the only configurable knob for ChatGPT-account codex (model is fixed to the
  // account default; explicit -m other than that 400s).
  const effortFlags = effort ? ['-c', `model_reasoning_effort=${effort}`] : [];
  if (sessionId) {
    // exec-level options must precede the `resume` subcommand; `-` makes resume
    // read the follow-up prompt from stdin. Pass effort so a live per-agent
    // effort change takes effect on the next (resumed) turn.
    return ['exec', ...common, ...effortFlags, 'resume', String(sessionId), '-'];
  }
  const modelFlags = model ? ['-m', String(model)] : [];
  // `-` → read the prompt from stdin (sidesteps argv quoting for the prompt).
  return ['exec', ...common, ...modelFlags, ...effortFlags, '-'];
}

// Normalize codex `exec --json` lines into claude-shaped ChatEvents. Verified
// against the real codex 0.124 thread/item schema:
//   {"type":"thread.started","thread_id":"<uuid>"}      → session id (resume)
//   {"type":"turn.started"} / {"type":"turn.completed","usage":{…}}
//   {"type":"item.started","item":{id,type,…}}          → tool_use begin
//   {"type":"item.completed","item":{id,type:"agent_message",text}}  → assistant text
//   {"type":"item.completed","item":{type:"command_execution",command,aggregated_output,exit_code}}
//   {"type":"item.completed","item":{type:"mcp_tool_call",…}}        → tool_result
//   {"type":"error",…} / {"type":"turn.failed",error:{message}}
function makeCodexEventParser() {
  // Track tool ids already announced via item.started so item.completed doesn't
  // emit a duplicate tool_use card for the same call.
  const startedIds = new Set();
  const toolName = (server, tool) => (server ? `mcp__${server}__${tool}` : (tool || 'tool'));

  // Friendlier guidance for the common ChatGPT-account / model-auth mismatch.
  const errorHint = (raw) => {
    const s = String(raw || '');
    if (/not supported when using Codex with a ChatGPT account/i.test(s) || /gpt-5-codex/i.test(s)) {
      return s + "\n\n→ This model needs an OpenAI API key. Pick the default model (which works with a ChatGPT account), or run `codex login` with an API key to use gpt-5-codex.";
    }
    return s;
  };

  const itemToolUse = (item) => {
    const id = item.id || ('codex-' + Math.random().toString(36).slice(2));
    if (item.type === 'command_execution') {
      return { type: 'assistant', message: { content: [{ type: 'tool_use', id, name: 'shell', input: { command: item.command || '' } }] } };
    }
    if (item.type === 'mcp_tool_call') {
      const name = toolName(item.server, item.tool);
      const input = item.arguments != null ? item.arguments : (item.input || {});
      return { type: 'assistant', message: { content: [{ type: 'tool_use', id, name, input }] } };
    }
    return null;
  };

  const errMsg = (item) => (item.error && (item.error.message || item.error)) || null;

  const itemToolResult = (item) => {
    const id = item.id;
    if (!id) return null;
    if (item.type === 'command_execution') {
      const isErr = (item.exit_code != null && item.exit_code !== 0) || item.status === 'failed';
      const content = errMsg(item) || (item.aggregated_output != null ? item.aggregated_output : (item.output || ''));
      return { type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: id, content: String(content), is_error: isErr }] } };
    }
    if (item.type === 'mcp_tool_call') {
      const isErr = Boolean(errMsg(item) || item.is_error || item.status === 'failed');
      let content;
      if (errMsg(item)) {
        content = String(errMsg(item));
      } else {
        const res = item.result != null ? item.result : (item.output || '');
        content = typeof res === 'string' ? res : JSON.stringify(res);
      }
      return { type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: id, content, is_error: isErr }] } };
    }
    return null;
  };

  return {
    parseLine(line) {
      let obj;
      try { obj = JSON.parse(line); } catch { return [{ type: 'raw', text: line }]; }
      const type = obj && obj.type;
      if (!type) return [];
      const out = [];
      switch (type) {
        case 'thread.started': {
          if (obj.thread_id) out.push({ type: 'system', subtype: 'init', session_id: String(obj.thread_id) });
          break;
        }
        case 'item.started': {
          const tu = obj.item ? itemToolUse(obj.item) : null;
          if (tu) { out.push(tu); if (obj.item.id) startedIds.add(obj.item.id); }
          break;
        }
        case 'item.completed': {
          const item = obj.item || {};
          if (item.type === 'agent_message') {
            const t = item.text != null ? item.text : '';
            if (t) out.push({ type: 'assistant', message: { content: [{ type: 'text', text: String(t) }] } });
          } else if (item.type === 'command_execution' || item.type === 'mcp_tool_call') {
            // Emit the tool_use card only if item.started didn't already (fast ops
            // can arrive completed-only); always emit the result.
            if (!item.id || !startedIds.has(item.id)) {
              const tu = itemToolUse(item);
              if (tu) out.push(tu);
            }
            const tr = itemToolResult(item);
            if (tr) out.push(tr);
          }
          // reasoning / file_change / todo_list / web_search items are ignored for
          // now (kept out of the chat to reduce noise).
          break;
        }
        case 'turn.completed': {
          // Best-effort token usage so codex agents show a token count too.
          // Field names vary across codex versions — tolerate the common ones.
          const u = obj.usage;
          if (u) {
            out.push({
              type: 'result',
              usage: {
                input_tokens: u.input_tokens || 0,
                output_tokens: u.output_tokens || 0,
                cache_read_input_tokens: u.cached_input_tokens || u.cache_read_input_tokens || 0,
              },
            });
          }
          break;
        }
        case 'error': {
          out.push({ type: 'raw', text: 'codex: ' + errorHint(obj.message || JSON.stringify(obj)) });
          break;
        }
        case 'turn.failed': {
          const m = (obj.error && obj.error.message) || JSON.stringify(obj);
          out.push({ type: 'raw', text: 'codex: ' + errorHint(m) });
          break;
        }
        default:
          break;
      }
      return out;
    },
  };
}

ipcMain.handle('chat:send', (event, { chatId, prompt, sessionId, cwd, projectSlug, coderId, osHint, cli, model, effort, skipPerms, agentLabel, agentRole, bootstrap, siblings, authToken, requireAuthToken }) => {
  // Ensure AGENTS.md + .mcp.json + env so claude's MCP catalog picks up agentshive.
  const cfgNow = readConfig();
  if (!cwd && projectSlug) cwd = (cfgNow.projectPaths || {})[projectSlug] || null;
  if (!cwd) throw new Error('set a local folder for this project first');
  if (!fs.existsSync(cwd)) throw new Error(`folder does not exist: ${cwd}`);
  ensureAgentsMd({ cwd, slug: projectSlug, baseUrl: cfgNow.baseUrl });
  ensureMcpConfig({ cwd, baseUrl: cfgNow.baseUrl, projectSlug });

  // Bootstrap turn (fired automatically right after agent creation): use a
  // briefing that IS the task — verify scope, read AGENTS.md, greet the
  // operator. No user message needed.
  // Subsequent turns (--resume) just send the operator's prompt verbatim.
  // First *operator* turn after bootstrap: briefing was already absorbed, so
  // prompt goes through as-is.
  const siblingBlock = buildSiblingBlock(siblings, agentRole);
  let finalPrompt = prompt;
  if (bootstrap && agentLabel && agentRole) {
    finalPrompt = buildRoleBriefing({ label: agentLabel, role: agentRole, slug: projectSlug, cwd, isBootstrap: true }) + siblingBlock;
  } else if (!sessionId && agentLabel && agentRole) {
    // Legacy path — bootstrap didn't run, fall back to briefing-prefix.
    finalPrompt = buildRoleBriefing({ label: agentLabel, role: agentRole, slug: projectSlug, cwd, isBootstrap: false }) + siblingBlock + prompt;
  } else if (siblingBlock) {
    // Subsequent turns: inject a fresh sibling-context block so the agent
    // knows about Coders that joined/left between turns.
    finalPrompt = siblingBlock + prompt;
  }

  const env = { ...process.env, ...agentEnv({ projectSlug, coderId, osHint, authToken, requireAuthToken }) };
  // Pipe the prompt via stdin instead of an argv string. Multi-line briefings
  // with quotes/backticks/asterisks get mangled by cmd.exe on Windows when
  // spawn(...,{shell:true}) assembles the command line. Stdin sidesteps shell
  // quoting entirely (both claude and codex read the prompt from stdin).
  const isCodex = cli === 'codex';
  let bin, args;
  if (isCodex) {
    // Codex MCP is wired via per-invocation `-c mcp_servers.*` overrides (no
    // file on disk); see buildCodexExecArgs. Resume uses codex's session id.
    bin = 'codex';
    args = buildCodexExecArgs({ sessionId, model, effort, skipPerms, projectSlug, baseUrl: cfgNow.baseUrl });
  } else {
    // Per-agent MCP config — isolated from the project-folder .mcp.json so two
    // agents in the same folder for different projects don't stomp each other.
    bin = 'claude';
    const agentMcpPath = writeAgentMcpConfig({ agentId: chatId, projectSlug, baseUrl: cfgNow.baseUrl });
    args = ['--print', '--output-format=stream-json', '--verbose', '--include-partial-messages', '--mcp-config', agentMcpPath];
    if (sessionId) args.push('--resume', sessionId);
    if (model) args.push('--model', model);
    if (effort) args.push('--effort', effort);
    if (skipPerms) args.push('--dangerously-skip-permissions');
  }

  // On Windows, claude/codex are .cmd shims node can't exec directly; shell:true
  // lets PATH find them. Safe because the prompt is on stdin and codex's args
  // are space-free barewords.
  const child = spawn(bin, args, {
    cwd,
    env,
    shell: process.platform === 'win32',
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  try {
    child.stdin.write(finalPrompt);
    child.stdin.end();
  } catch (err) {
    // Child may have died before we could write — handled via 'error' event.
  }
  activeChats.set(chatId, { child });

  const webContents = event.sender;
  let buffer = '';
  const emit = (kind, payload) => {
    if (!webContents.isDestroyed()) webContents.send(`chat:${kind}:${chatId}`, payload);
  };

  // Codex emits its own JSONL schema; normalize each line into claude-shaped
  // ChatEvents so the renderer needs no codex-specific handling.
  const codexParser = isCodex ? makeCodexEventParser() : null;
  const emitLine = (line) => {
    if (codexParser) {
      for (const ev of codexParser.parseLine(line)) emit('event', ev);
    } else {
      try { emit('event', JSON.parse(line)); } catch { emit('event', { type: 'raw', text: line }); }
    }
  };
  child.stdout.on('data', (chunk) => {
    buffer += chunk.toString('utf8');
    let nl;
    while ((nl = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (!line) continue;
      emitLine(line);
    }
  });
  child.stderr.on('data', (chunk) => emit('stderr', chunk.toString('utf8')));
  child.on('error', (err) => emit('error', { message: err.message }));
  child.on('exit', (code) => {
    if (buffer.trim()) emitLine(buffer.trim());
    emit('done', { code });
    activeChats.delete(chatId);
  });
  return { started: true, pid: child.pid };
});

// Terminate a spawned CLI AND its descendants. On Windows the child was spawned
// with shell:true, so child.pid is the cmd/shell wrapper — child.kill() reaps
// only that shim and leaves the real CLI (claude/codex) and ITS children (e.g.
// codex-acp.exe) alive, which is how an "archived" agent became a zombie that
// kept editing files. taskkill /T /F kills the whole tree by pid; on posix we
// signal the process directly.
function killChildTree(child) {
  if (!child) return;
  try {
    if (process.platform === 'win32' && child.pid) {
      spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
    } else {
      child.kill('SIGTERM');
    }
  } catch { /* already gone */ }
}

ipcMain.handle('chat:cancel', (_e, { chatId }) => {
  const entry = activeChats.get(chatId);
  if (entry && entry.child) killChildTree(entry.child);
});

// The codex CLI's configured default model from ~/.codex/config.toml. We pass no
// -m for codex (ChatGPT-account auth only allows the account default; explicit
// models 400), so this is the effective model — surfaced read-only in the UI.
// Returns null when codex isn't configured or has no model line.
ipcMain.handle('codex:defaultModel', () => {
  try {
    const txt = fs.readFileSync(path.join(os.homedir(), '.codex', 'config.toml'), 'utf8');
    const m = txt.match(/^\s*model\s*=\s*"?([^"\r\n]+)"?/m);
    return m ? m[1].trim() : null;
  } catch {
    return null;
  }
});

// One-call embedded agent launcher: writes .mcp.json, spawns PTY with full
// AgentsHive env (including API key — which renderer can't see), returns
// {id, suggestedCmd, mcp, cwd}. Renderer then attaches xterm and types the
// suggested command into the PTY.
ipcMain.handle('agent:embed', (event, payload) => {
  if (!pty) throw new Error('node-pty not loaded');
  const { projectSlug, coderId, osHint, suggestedCmd } = payload;
  const cfgNow = readConfig();
  let cwd = payload.cwd;
  if (!cwd && projectSlug) cwd = (cfgNow.projectPaths || {})[projectSlug] || null;
  if (!cwd) throw new Error('set a local folder for this project first');
  if (!fs.existsSync(cwd)) throw new Error(`folder does not exist: ${cwd}`);

  ensureAgentsMd({ cwd, slug: projectSlug, baseUrl: cfgNow.baseUrl });
  const mcp = ensureMcpConfig({ cwd, baseUrl: cfgNow.baseUrl, projectSlug });
  const extraEnv = agentEnv({ projectSlug, coderId, osHint });

  const id = String(nextPtyId++);
  const shellExe = process.platform === 'win32'
    ? (process.env.COMSPEC || 'powershell.exe')
    : (process.env.SHELL || '/bin/bash');
  const shellArgs = process.platform === 'win32' ? ['-NoLogo'] : [];
  const child = pty.spawn(shellExe, shellArgs, {
    name: 'xterm-256color',
    cols: payload.cols || 100,
    rows: payload.rows || 28,
    cwd,
    env: { ...process.env, ...extraEnv, TERM: 'xterm-256color' },
  });
  ptys.set(id, child);
  const webContents = event.sender;
  child.onData((data) => {
    if (!webContents.isDestroyed()) webContents.send(`pty:data:${id}`, data);
  });
  child.onExit(({ exitCode, signal }) => {
    if (!webContents.isDestroyed()) webContents.send(`pty:exit:${id}`, { exitCode, signal });
    ptys.delete(id);
  });
  return { id, pid: child.pid, suggestedCmd: suggestedCmd || 'claude', mcp, cwd };
});

ipcMain.handle('pty:spawn', (event, { cwd, env: extraEnv, cols, rows, shell }) => {
  if (!pty) throw new Error('node-pty not available');
  const id = String(nextPtyId++);
  const shellExe = shell || (process.platform === 'win32'
    ? (process.env.COMSPEC || 'powershell.exe')
    : (process.env.SHELL || '/bin/bash'));
  const shellArgs = process.platform === 'win32' ? ['-NoLogo'] : [];
  const child = pty.spawn(shellExe, shellArgs, {
    name: 'xterm-256color',
    cols: cols || 80,
    rows: rows || 24,
    cwd: cwd || process.env.HOME || process.cwd(),
    env: { ...process.env, ...(extraEnv || {}), TERM: 'xterm-256color' },
  });
  ptys.set(id, child);
  const webContents = event.sender;
  child.onData((data) => {
    if (!webContents.isDestroyed()) webContents.send(`pty:data:${id}`, data);
  });
  child.onExit(({ exitCode, signal }) => {
    if (!webContents.isDestroyed()) webContents.send(`pty:exit:${id}`, { exitCode, signal });
    ptys.delete(id);
  });
  return { id, pid: child.pid };
});

ipcMain.handle('pty:write', (_e, { id, data }) => {
  const child = ptys.get(id);
  if (child) child.write(data);
});

ipcMain.handle('pty:resize', (_e, { id, cols, rows }) => {
  const child = ptys.get(id);
  if (child) {
    try { child.resize(cols, rows); } catch { /* ignore resize-after-exit */ }
  }
});

ipcMain.handle('pty:kill', (_e, { id }) => {
  const child = ptys.get(id);
  if (child) {
    try { child.kill(); } catch { /* already exited */ }
    ptys.delete(id);
  }
});

ipcMain.handle('pty:send-cmd', (_e, { id, text }) => {
  // Convenience: write text + newline. Used by the launcher to type the
  // suggested command into a freshly-spawned terminal without auto-pressing
  // enter (we DO press enter here — caller decides whether to use this or
  // pty:write).
  const child = ptys.get(id);
  if (child) child.write(text + '\r');
});

// --- agent persistence ---------------------------------------------------
// One file per agent at userData/agents/<slug>/<agentId>.json. Renderer
// hydrates the sidebar from disk on project open, and writes back on every
// meaningful state change (create, user msg, turn done, archive).

ipcMain.handle('agents:list', (_e, { projectSlug }) => {
  const dir = AGENTS_DIR(projectSlug);
  try {
    if (!fs.existsSync(dir)) return [];
    const files = fs.readdirSync(dir).filter(f => f.endsWith('.json'));
    const agents = [];
    for (const f of files) {
      try {
        const raw = fs.readFileSync(path.join(dir, f), 'utf8');
        const parsed = JSON.parse(raw);
        if (parsed && parsed.id) agents.push(parsed);
      } catch { /* skip corrupt file */ }
    }
    agents.sort((a, b) => (a.createdAt || '').localeCompare(b.createdAt || ''));
    return agents;
  } catch {
    return [];
  }
});

ipcMain.handle('agents:save', (_e, { projectSlug, agent }) => {
  if (!agent || !agent.id) throw new Error('agent.id required');
  const dir = AGENTS_DIR(projectSlug);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, safeSlug(agent.id) + '.json');
  // Atomic write: stage to .tmp then rename.
  const tmp = file + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(agent, null, 2), 'utf8');
  fs.renameSync(tmp, file);
  return { ok: true };
});

ipcMain.handle('agents:delete', (_e, { projectSlug, agentId }) => {
  const dir = AGENTS_DIR(projectSlug);
  const file = path.join(dir, safeSlug(agentId) + '.json');
  try { fs.unlinkSync(file); } catch { /* already gone */ }
  // Also clean up the per-agent MCP config file.
  deleteAgentMcpConfig({ agentId, projectSlug });
  return { ok: true };
});

// --- workspace (opened projects + collapse + last active) ----------------
// App-global sidebar state. Channels: workspace:get returns the full shape;
// workspace:set shallow-merges a patch ({ openedProjects?, collapsed?,
// lastActive? }) so callers can update one field without clobbering the rest.

ipcMain.handle('workspace:get', () => {
  const cfg = readConfig();
  const ws = cfg.workspace || {};
  return {
    openedProjects: Array.isArray(ws.openedProjects) ? ws.openedProjects : [],
    collapsed: ws.collapsed && typeof ws.collapsed === 'object' ? ws.collapsed : {},
    lastActive: typeof ws.lastActive === 'string' ? ws.lastActive : null,
  };
});

ipcMain.handle('workspace:set', (_e, patch) => {
  const cfg = readConfig();
  const cur = cfg.workspace || {};
  const next = {
    openedProjects: Array.isArray(cur.openedProjects) ? cur.openedProjects : [],
    collapsed: cur.collapsed && typeof cur.collapsed === 'object' ? cur.collapsed : {},
    lastActive: typeof cur.lastActive === 'string' ? cur.lastActive : null,
  };
  if (patch && Array.isArray(patch.openedProjects)) next.openedProjects = patch.openedProjects;
  if (patch && patch.collapsed && typeof patch.collapsed === 'object') next.collapsed = patch.collapsed;
  if (patch && 'lastActive' in patch) next.lastActive = patch.lastActive || null;
  writeConfig({ workspace: next });
  return next;
});

ipcMain.handle('prefs:get', (_e, { projectSlug }) => {
  const cfg = readConfig();
  return (cfg.projectPrefs || {})[projectSlug] || null;
});

ipcMain.handle('prefs:set', (_e, { projectSlug, prefs }) => {
  const cfg = readConfig();
  const all = { ...(cfg.projectPrefs || {}) };
  all[projectSlug] = { ...(all[projectSlug] || {}), ...prefs };
  writeConfig({ projectPrefs: all });
  return all[projectSlug];
});

ipcMain.handle('paths:get', (_e, { projectSlug }) => {
  const cfg = readConfig();
  return (cfg.projectPaths || {})[projectSlug] || null;
});

ipcMain.handle('paths:set', (_e, { projectSlug, path: p }) => {
  const cfg = readConfig();
  const paths = { ...(cfg.projectPaths || {}) };
  if (p) paths[projectSlug] = p;
  else delete paths[projectSlug];
  writeConfig({ projectPaths: paths });
  return paths[projectSlug] || null;
});

ipcMain.handle('paths:pick', async (_e, { projectSlug }) => {
  const cfg = readConfig();
  const current = (cfg.projectPaths || {})[projectSlug];
  const win = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0];
  const res = await dialog.showOpenDialog(win, {
    title: `Select local folder for "${projectSlug}"`,
    defaultPath: current || app.getPath('home'),
    properties: ['openDirectory'],
  });
  if (res.canceled || !res.filePaths || res.filePaths.length === 0) return null;
  const picked = res.filePaths[0];
  const paths = { ...(cfg.projectPaths || {}) };
  paths[projectSlug] = picked;
  writeConfig({ projectPaths: paths });
  // Auto-bootstrap: drop AGENTS.md + .mcp.json so agents pick up the protocol
  // and the MCP wiring immediately — no manual init script required.
  ensureAgentsMd({ cwd: picked, slug: projectSlug, baseUrl: cfg.baseUrl });
  ensureMcpConfig({ cwd: picked, baseUrl: cfg.baseUrl, projectSlug });
  return picked;
});

ipcMain.handle('agent:launch', (_e, payload) => {
  try {
    let cwd = payload.cwd;
    if (!cwd && payload.projectSlug) {
      const cfg = readConfig();
      cwd = (cfg.projectPaths || {})[payload.projectSlug] || null;
    }
    if (cwd && !fs.existsSync(cwd)) {
      return { ok: false, error: `folder does not exist: ${cwd}` };
    }
    if (cwd && payload.projectSlug) {
      const cfg = readConfig();
      ensureAgentsMd({ cwd, slug: payload.projectSlug, baseUrl: cfg.baseUrl });
    }
    launchAgent({ ...payload, cwd: cwd || null });
    return { ok: true, cwd: cwd || null };
  } catch (err) {
    return { ok: false, error: String(err && err.message || err) };
  }
});

// --- external CLI tools (gh / railway / vercel) --------------------------
// Detect installed + authenticated state for the common CLIs agents reach for.
// Pure status check — no auth state is changed here. The "Connect" path spawns
// an external terminal running the CLI's interactive login.

function runCommand(cmd, args, opts = {}) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, {
      shell: process.platform === 'win32',
      env: process.env,
      ...opts,
    });
    let stdout = '';
    let stderr = '';
    if (child.stdout) child.stdout.on('data', (d) => { stdout += d.toString('utf8'); });
    if (child.stderr) child.stderr.on('data', (d) => { stderr += d.toString('utf8'); });
    let done = false;
    const finish = (code) => {
      if (done) return;
      done = true;
      resolve({ code, stdout, stderr });
    };
    child.on('exit', (code) => finish(code));
    child.on('error', (err) => finish(-1));
    // 8s hard cap — these are local commands, should finish fast.
    setTimeout(() => { try { child.kill(); } catch {} finish(-2); }, 8000);
  });
}

async function checkGh() {
  const v = await runCommand('gh', ['--version']);
  if (v.code !== 0) return { tool: 'gh', installed: false, authenticated: false, identity: null };
  const a = await runCommand('gh', ['auth', 'status']);
  const text = (a.stderr || '') + '\n' + (a.stdout || '');
  // gh's "auth status" prints "Logged in to github.com account <user>" or similar.
  const m = text.match(/Logged in to [^\s]+ (?:account |as )([^\s(]+)/i)
        || text.match(/account ([^\s(]+) /i);
  return {
    tool: 'gh',
    installed: true,
    authenticated: a.code === 0,
    identity: m ? m[1] : null,
  };
}

async function checkRailway() {
  const v = await runCommand('railway', ['--version']);
  if (v.code !== 0) return { tool: 'railway', installed: false, authenticated: false, identity: null };
  const a = await runCommand('railway', ['whoami']);
  // railway whoami: "Logged in as <email>" on success.
  const out = (a.stdout || '').trim();
  const m = out.match(/Logged in as\s+(.+)$/im) || out.match(/^👋\s+(.+)$/m);
  return {
    tool: 'railway',
    installed: true,
    authenticated: a.code === 0 && Boolean(out),
    identity: m ? m[1].trim() : (a.code === 0 ? out.split('\n').pop() : null),
  };
}

async function checkVercel() {
  const v = await runCommand('vercel', ['--version']);
  if (v.code !== 0) return { tool: 'vercel', installed: false, authenticated: false, identity: null };
  const a = await runCommand('vercel', ['whoami']);
  const id = (a.stdout || '').trim().split('\n').pop() || null;
  return {
    tool: 'vercel',
    installed: true,
    authenticated: a.code === 0 && Boolean(id),
    identity: a.code === 0 ? id : null,
  };
}

// --- attachments (image pastes/drops in chat) ----------------------------
// Save a base64 data URL to disk inside userData. Returns the absolute path
// so the renderer can reference it in prompts (claude reads the file via its
// native Read tool — same path-based vision flow as anywhere else).

ipcMain.handle('attachments:save', async (_e, { agentId, projectSlug, name, dataUrl }) => {
  if (!agentId || !dataUrl || !name) throw new Error('agentId, name, dataUrl required');
  const m = String(dataUrl).match(/^data:([^;]+);base64,(.+)$/);
  if (!m) throw new Error('expected base64 data URL');
  const buf = Buffer.from(m[2], 'base64');
  const slug = projectSlug ? safeSlug(projectSlug) : 'shared';
  const agentDir = path.join(app.getPath('userData'), 'attachments', slug, safeSlug(agentId));
  fs.mkdirSync(agentDir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const safe = String(name).replace(/[^a-z0-9._-]/gi, '_').slice(0, 80);
  const file = path.join(agentDir, `${stamp}-${safe}`);
  fs.writeFileSync(file, buf);
  return { path: file, bytes: buf.length };
});

ipcMain.handle('tools:status', async () => {
  const [gh, railway, vercel] = await Promise.all([checkGh(), checkRailway(), checkVercel()]);
  return { gh, railway, vercel };
});

ipcMain.handle('tools:connect', (_e, { tool }) => {
  // Map to the CLI's interactive login command. We launch in an external
  // terminal because most of these flows open a browser + paste a token.
  const cmd = { gh: 'gh auth login', railway: 'railway login', vercel: 'vercel login' }[tool];
  if (!cmd) throw new Error('unknown tool: ' + tool);
  openExternalTerminalWithCommand(cmd);
  return { ok: true };
});

function openExternalTerminalWithCommand(cmd) {
  if (process.platform === 'win32') {
    const ps = `Write-Host 'Running: ${cmd}' -ForegroundColor Cyan; ${cmd}; Write-Host ''; Write-Host 'When done, close this window.' -ForegroundColor Yellow`;
    const wtArgs = ['new-tab', 'powershell.exe', '-NoExit', '-Command', ps];
    const child = spawn('wt.exe', wtArgs, { detached: true, stdio: 'ignore', shell: false });
    child.on('error', () => {
      spawn('cmd.exe', ['/c', 'start', 'powershell.exe', '-NoExit', '-Command', ps], {
        detached: true, stdio: 'ignore', shell: false,
      });
    });
    child.unref();
  } else if (process.platform === 'darwin') {
    const safe = cmd.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    const osascript = `tell application "Terminal" to do script "${safe}"`;
    spawn('osascript', ['-e', osascript], { detached: true, stdio: 'ignore' }).unref();
  } else {
    const inner = `${cmd}; echo ''; echo 'When done, close this window.'; exec $SHELL`;
    spawn('gnome-terminal', ['--', 'bash', '-c', inner], { detached: true, stdio: 'ignore' }).unref();
  }
}

ipcMain.handle('dashboard:state', async (_e, { projectSlug }) => {
  return dashboardFetch(`/api/dashboard/state?project=${encodeURIComponent(projectSlug)}`);
});

// --- v2.x admin/superuser API (routed through main so the admin's Supabase
// token is the bearer, and to avoid renderer CORS). Server enforces is_admin()
// on every endpoint; these just relay. token = the admin's Supabase access token.
async function adminApiFetch(method, pathAndQuery, token, body) {
  const cfg = readConfig();
  if (!cfg.baseUrl) throw new Error('baseUrl not configured');
  if (!token) throw new Error('not signed in as admin');
  const url = cfg.baseUrl.replace(/\/$/, '') + pathAndQuery;
  const headers = { Authorization: `Bearer ${token}`, Origin: cfg.baseUrl };
  const init = { method, headers };
  if (body) { headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
  const res = await fetch(url, init);
  const text = await res.text();
  let parsed = null;
  try { parsed = text ? JSON.parse(text) : null; } catch { parsed = text; }
  if (!res.ok) {
    const err = new Error(`HTTP ${res.status}: ${typeof parsed === 'string' ? parsed.slice(0, 200) : JSON.stringify(parsed).slice(0, 200)}`);
    err.status = res.status;
    throw err;
  }
  return parsed;
}

// --- v2.x companion-webapp relay (desktop side). All authed as the operator's
// Supabase tenant (token from the renderer) so they share a tenant with the
// webapp — the legacy key is NOT used here (it's a different tenant).
ipcMain.handle('web:presence', (_e, { token, project, agents }) =>
  adminApiFetch('POST', '/web/presence', token, { project, agents }));
ipcMain.handle('web:inbound', (_e, { token }) =>
  adminApiFetch('GET', '/web/inbound', token));
ipcMain.handle('web:ack', (_e, { token, messageId }) =>
  adminApiFetch('POST', '/web/ack', token, { message_id: messageId }));
ipcMain.handle('web:relay', (_e, { token, parentId, project, agentKey, body }) =>
  adminApiFetch('POST', '/web/relay', token, { parent_id: parentId, project, agent_key: agentKey, body }));

// v2.x Cloud Sync (opt-in). Same operator-Supabase-tenant auth as the other
// /web/* relays. me = entitlements; syncPush/syncPull = tenant transcript sync.
ipcMain.handle('web:me', (_e, { token }) => adminApiFetch('GET', '/web/me', token));
ipcMain.handle('web:syncPush', (_e, { token, payload }) =>
  adminApiFetch('POST', '/web/sync/push', token, payload));
ipcMain.handle('web:syncPull', (_e, { token, project, since }) =>
  adminApiFetch(
    'GET',
    `/web/sync/pull?project=${encodeURIComponent(project || '')}` + (since ? `&since=${encodeURIComponent(since)}` : ''),
    token,
  ));

ipcMain.handle('admin:listUsers', (_e, { token }) => adminApiFetch('GET', '/admin/users', token));
ipcMain.handle('admin:setBanned', (_e, { token, sub, banned }) =>
  adminApiFetch('POST', `/admin/users/${encodeURIComponent(sub)}/${banned ? 'ban' : 'unban'}`, token));
ipcMain.handle('admin:setPlan', (_e, { token, sub, plan }) =>
  adminApiFetch('POST', `/admin/users/${encodeURIComponent(sub)}/plan`, token, { plan }));
ipcMain.handle('admin:removeUser', (_e, { token, sub }) =>
  adminApiFetch('POST', `/admin/users/${encodeURIComponent(sub)}/remove`, token));

// --- v2.x persistent mission docs → <projectFolder>/agentsmissions/ ---------
// Auto-write a durable markdown record per mission (+ FOUNDATION.md) so the
// operator OR a fresh-context Planner can read what each mission was and the
// coder's responses. Source = the read-only server export (full spec + all
// summaries/responses + questions). Idempotent: write-only-if-changed, stable
// filename per mission, so re-syncing updates in place instead of duplicating.

function _slugName(s) {
  return String(s || 'mission').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 50) || 'mission';
}

function _missionDocFilename(m) {
  const created = String(m.created_at || '').replace(/[:.]/g, '-').replace('T', '_').replace(/[^0-9A-Za-z_-]/g, '');
  return `${created || 'mission'}-${_slugName(m.name)}.md`;
}

function _renderMissionMd(m) {
  const lines = [];
  lines.push(`# ${m.name || '(untitled mission)'}`, '');
  lines.push(`- **Mission ID:** ${m.mission_id}`);
  lines.push(`- **Status:** ${m.status}`);
  lines.push(`- **Created:** ${m.created_at || '—'}`);
  lines.push(`- **Done:** ${m.done_at || '—'}`);
  lines.push('', '## Spec', '', m.spec || '_(no spec)_', '');
  lines.push('## Progress', '');
  const summaries = Array.isArray(m.summaries) ? m.summaries : [];
  if (summaries.length === 0) {
    lines.push('_No coder reports yet._', '');
  } else {
    for (const s of summaries) {
      const who = s.coder_id ? ` (${s.coder_id})` : '';
      lines.push(`### Coder report — ${s.created_at || ''}${who}`, '', s.body || '', '');
      lines.push(`**Planner response${s.responded_at ? ` (${s.responded_at})` : ''}:** ${s.response || '_(awaiting response)_'}`, '');
    }
  }
  const questions = Array.isArray(m.questions) ? m.questions : [];
  if (questions.length > 0) {
    lines.push('## Questions', '');
    for (const q of questions) {
      const who = q.coder_id ? ` (${q.coder_id})` : '';
      lines.push(`- **Q — ${q.created_at || ''}${who}:** ${q.body || ''}`);
      lines.push(`  **A${q.answered_at ? ` (${q.answered_at})` : ''}:** ${q.answer || '_(unanswered)_'}`);
    }
    lines.push('');
  }
  lines.push('---', '_Generated by AgentsHive Desktop. Edit freely — re-syncing only updates when the source changes._', '');
  return lines.join('\n');
}

function _renderFoundationMd(project, foundation) {
  const lines = [];
  lines.push(`# Foundation mission — ${(project && project.name) || ''}`, '');
  lines.push('> The project\'s durable north-star goal. It is never superseded by the rotating active mission.', '');
  lines.push(`- **Set at:** ${foundation.set_at || '—'}`, '');
  lines.push(`## ${foundation.name || '(unnamed)'}`, '', foundation.spec || '_(no spec)_', '');
  return lines.join('\n');
}

// Read-only full mission export for the renderer (missions panel). Tenant-scoped
// server-side; uses the legacy-key apiFetch like the other dashboard reads.
ipcMain.handle('missions:export', async (_e, { projectSlug }) => {
  if (!projectSlug) return { project: null, foundation: null, missions: [] };
  return dashboardFetch(`/api/dashboard/missions/export?project=${encodeURIComponent(projectSlug)}`);
});

ipcMain.handle('missions:syncDocs', async (_e, { projectSlug }) => {
  const cfg = readConfig();
  const cwd = projectSlug ? (cfg.projectPaths || {})[projectSlug] : null;
  if (!cwd) return { ok: false, reason: 'no folder set for this project' };
  if (!fs.existsSync(cwd)) return { ok: false, reason: 'project folder does not exist' };
  let exp;
  try {
    exp = await apiFetch(`/api/dashboard/missions/export?project=${encodeURIComponent(projectSlug)}`);
  } catch (err) {
    return { ok: false, reason: 'export failed: ' + (err && err.message || err) };
  }
  const dir = path.join(cwd, 'agentsmissions');
  fs.mkdirSync(dir, { recursive: true });
  const writeIfChanged = (file, content) => {
    try {
      if (fs.existsSync(file) && fs.readFileSync(file, 'utf8') === content) return false;
      fs.writeFileSync(file, content, 'utf8');
      return true;
    } catch { return false; }
  };
  let written = 0;
  if (exp && exp.foundation) {
    if (writeIfChanged(path.join(dir, 'FOUNDATION.md'), _renderFoundationMd(exp.project, exp.foundation))) written++;
  }
  const missions = (exp && exp.missions) || [];
  for (const m of missions) {
    if (writeIfChanged(path.join(dir, _missionDocFilename(m)), _renderMissionMd(m))) written++;
  }
  return { ok: true, written, total: missions.length };
});

ipcMain.handle('dashboard:url', (_e, { projectSlug }) => {
  const cfg = readConfig();
  if (!cfg.baseUrl || !cfg.apiKey) return null;
  // The dashboard uses cookie auth, so we can't deep-link with a bearer token
  // in a webview. Open in the user's default browser for now (v2.0-beta will
  // mint a short-lived dashboard session cookie via a new endpoint).
  return `${cfg.baseUrl.replace(/\/$/, '')}/dashboard?project=${encodeURIComponent(projectSlug)}`;
});

ipcMain.handle('dashboard:open', (_e, { projectSlug }) => {
  const cfg = readConfig();
  if (!cfg.baseUrl) return false;
  const url = `${cfg.baseUrl.replace(/\/$/, '')}/dashboard?project=${encodeURIComponent(projectSlug)}`;
  shell.openExternal(url);
  return true;
});

ipcMain.handle('app:hostname', () => os.hostname());

// The running app's version (from the packaged/app package.json). Drives the
// header version badge so it always matches the deployed release — no manual
// bump of a hardcoded string.
ipcMain.handle('app:version', () => app.getVersion());

// Renderer pushes the operator's Supabase access token here on every auth change
// (and clears it on sign-out) so dashboard reads can be tenant-correct. Cached in
// main; never returned to the renderer.
ipcMain.handle('auth:setToken', (_e, { token }) => {
  _supabaseToken = (token && String(token)) || null;
  return { ok: true };
});

// --- durable auth store (Supabase session persistence across updates) -------
// The renderer's localStorage for a file:// origin is not a reliable home for
// the Supabase session across installs/updates (an update can land the renderer
// on a fresh storage area, signing the user out). userData IS preserved across
// updates, so we back supabase-js's auth storage with a small JSON file there.
const AUTH_STORE_FILE = () => path.join(app.getPath('userData'), 'auth-store.json');
function readAuthStore() {
  try { return JSON.parse(fs.readFileSync(AUTH_STORE_FILE(), 'utf8')) || {}; } catch { return {}; }
}
function writeAuthStore(obj) {
  fs.mkdirSync(path.dirname(AUTH_STORE_FILE()), { recursive: true });
  fs.writeFileSync(AUTH_STORE_FILE(), JSON.stringify(obj), 'utf8');
}
// --- file-edit Undo/Keep (git-restore) --------------------------------------
// Revert a turn's changed files to their git HEAD. SECURITY: git runs via
// execFile with an ARGS ARRAY (no shell), and the file_paths come from agent
// tool inputs (untrusted) — the `--` separator + no-shell exec prevents any
// argument/shell injection. Only TRACKED paths inside the project's git worktree
// are touched; we back up current contents to userData first so Undo is itself
// recoverable.
function _projectCwd(projectSlug) {
  const cfg = readConfig();
  const cwd = projectSlug ? (cfg.projectPaths || {})[projectSlug] : null;
  return cwd && fs.existsSync(cwd) ? cwd : null;
}
function _runGit(cwd, args) {
  return new Promise((resolve) => {
    execFile('git', args, { cwd, windowsHide: true, timeout: 15000, maxBuffer: 4 * 1024 * 1024 }, (err, stdout, stderr) => {
      resolve({ code: err ? (typeof err.code === 'number' ? err.code : 1) : 0, stdout: String(stdout || ''), stderr: String(stderr || '') });
    });
  });
}
async function _isGitRepo(cwd) {
  const r = await _runGit(cwd, ['rev-parse', '--is-inside-work-tree']);
  return r.code === 0 && r.stdout.trim() === 'true';
}

ipcMain.handle('files:isGitRepo', async (_e, { projectSlug }) => {
  const cwd = _projectCwd(projectSlug);
  if (!cwd) return false;
  return _isGitRepo(cwd);
});

ipcMain.handle('files:undoEdits', async (_e, { projectSlug, paths }) => {
  const cwd = _projectCwd(projectSlug);
  if (!cwd) return { ok: false, reason: 'no folder set for this project' };
  if (!Array.isArray(paths) || paths.length === 0) return { ok: false, reason: 'no paths' };
  if (!(await _isGitRepo(cwd))) return { ok: false, reason: 'not-a-git-repo' };

  // Keep only paths that are TRACKED in this worktree (untracked/new files have
  // no HEAD to restore to — report them as skipped rather than touching them).
  const clean = paths.filter((p) => typeof p === 'string' && p.length > 0);
  const tracked = [];
  const skipped = [];
  for (const p of clean) {
    const r = await _runGit(cwd, ['ls-files', '--error-unmatch', '--', p]);
    if (r.code === 0) tracked.push(p);
    else skipped.push(p);
  }
  if (tracked.length === 0) return { ok: false, reason: 'no tracked files to revert', skipped };

  // Back up current contents BEFORE reverting (recoverable Undo).
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const backupDir = path.join(app.getPath('userData'), 'edit-backups', safeSlug(projectSlug || 'project'), stamp);
  const manifest = [];
  try {
    fs.mkdirSync(backupDir, { recursive: true });
    tracked.forEach((p, i) => {
      try {
        if (fs.existsSync(p)) {
          const dest = path.join(backupDir, `${i}__${String(p).replace(/[^a-z0-9._-]/gi, '_').slice(-80)}`);
          fs.copyFileSync(p, dest);
          manifest.push({ path: p, backup: dest });
        }
      } catch { /* skip unreadable */ }
    });
    fs.writeFileSync(path.join(backupDir, 'manifest.json'), JSON.stringify(manifest, null, 2), 'utf8');
  } catch { /* backup is best-effort; proceed with the revert */ }

  // Revert tracked paths to HEAD — execFile, args array, `--` guard. No shell.
  const res = await _runGit(cwd, ['restore', '--source=HEAD', '--', ...tracked]);
  if (res.code !== 0) {
    return { ok: false, reason: (res.stderr || 'git restore failed').trim().slice(0, 300), backupDir };
  }
  return { ok: true, reverted: tracked, skipped, backupDir };
});

ipcMain.handle('authstore:get', (_e, { key }) => {
  const s = readAuthStore();
  return Object.prototype.hasOwnProperty.call(s, key) ? s[key] : null;
});
ipcMain.handle('authstore:set', (_e, { key, value }) => {
  const s = readAuthStore();
  s[key] = value;
  writeAuthStore(s);
  return { ok: true };
});
ipcMain.handle('authstore:remove', (_e, { key }) => {
  const s = readAuthStore();
  delete s[key];
  writeAuthStore(s);
  return { ok: true };
});

// --- slash-command / skill catalog ---------------------------------------
// Enumerate file-based skills + prompt commands for the chat input's `/`
// autocomplete. Only PROMPT-EXPANSION items are returned (skills + command
// .md files) — these are exactly the ones that actually do something in
// `--print` headless mode. Interactive/client-side built-ins (clear, config,
// login, …) are NOT files, so they never enter this list; the menu stays free
// of silent no-ops.

function parseFrontmatter(text) {
  const m = text.match(/^---\s*\r?\n([\s\S]*?)\r?\n---/);
  if (!m) return {};
  const out = {};
  for (const line of m[1].split('\n')) {
    const mm = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (mm) out[mm[1].trim()] = mm[2].trim().replace(/^["']|["']$/g, '');
  }
  return out;
}

function listSkillDir(dir, source) {
  const res = [];
  let entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return res; }
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const skillMd = path.join(dir, entry.name, 'SKILL.md');
    if (!fs.existsSync(skillMd)) continue;
    let fm = {};
    try { fm = parseFrontmatter(fs.readFileSync(skillMd, 'utf8')); } catch {}
    res.push({ name: fm.name || entry.name, description: fm.description || '', source, kind: 'skill' });
  }
  return res;
}

function listCommandDir(dir, source) {
  const res = [];
  const walk = (d, prefix) => {
    let entries;
    try { entries = fs.readdirSync(d, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      const full = path.join(d, e.name);
      if (e.isDirectory()) {
        walk(full, prefix ? `${prefix}:${e.name}` : e.name);
      } else if (e.name.endsWith('.md')) {
        const base = e.name.slice(0, -3);
        const name = prefix ? `${prefix}:${base}` : base;
        let fm = {};
        try { fm = parseFrontmatter(fs.readFileSync(full, 'utf8')); } catch {}
        res.push({ name, description: fm.description || '', source, kind: 'command' });
      }
    }
  };
  walk(dir, '');
  return res;
}

ipcMain.handle('skills:list', (_e, { projectSlug }) => {
  const home = app.getPath('home');
  const cfg = readConfig();
  const cwd = projectSlug ? (cfg.projectPaths || {})[projectSlug] : null;
  const items = [
    ...listSkillDir(path.join(home, '.claude', 'skills'), 'user-skill'),
    ...listCommandDir(path.join(home, '.claude', 'commands'), 'user-command'),
  ];
  if (cwd) items.push(...listCommandDir(path.join(cwd, '.claude', 'commands'), 'project-command'));
  // De-dupe by name (project entries pushed last win over user ones).
  const byName = new Map();
  for (const it of items) byName.set(it.name, it);
  return Array.from(byName.values()).sort((a, b) => a.name.localeCompare(b.name));
});

// --- auto-update (electron-updater + GitHub releases feed) -----------------
// The app auto-DOWNLOADS a new release in the background; the renderer shows a
// "Restart to update" control once it's downloaded; clicking it installs +
// relaunches. Guarded by app.isPackaged so dev runs never touch the updater
// (in dev there's no app-update.yml feed, so checks would error noisily).

let mainWindow = null;

function sendToRenderer(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, payload);
  }
}

function setupAutoUpdater() {
  if (!app.isPackaged) return; // dev: no packaged feed — stay quiet.
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on('update-available', (info) => sendToRenderer('update:available', { version: info && info.version }));
  autoUpdater.on('download-progress', (p) => sendToRenderer('update:progress', { percent: p && p.percent }));
  autoUpdater.on('update-downloaded', (info) => sendToRenderer('update:downloaded', { version: info && info.version }));
  autoUpdater.on('error', (err) => sendToRenderer('update:error', { message: String((err && err.message) || err) }));

  const check = () => { autoUpdater.checkForUpdates().catch((err) => console.warn('update check failed', err && err.message)); };
  check();                              // on launch
  setInterval(check, 60 * 60 * 1000);  // and hourly thereafter
}

// Renderer asks to install the downloaded update + relaunch into the new version.
ipcMain.handle('update:quitAndInstall', () => {
  if (!app.isPackaged) return;
  // (isSilent, isForceRunAfter): silent runs the one-click NSIS installer with no
  // wizard UI, forceRunAfter relaunches into the new version — Zed-style close+reopen.
  autoUpdater.quitAndInstall(true, true);
});

// --- window ---------------------------------------------------------------

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 920,
    minHeight: 620,
    backgroundColor: '#0f1115',
    icon: APP_ICON,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow = win;
  win.on('closed', () => { if (mainWindow === win) mainWindow = null; });
  win.removeMenu();
  // Dev mode: load from Vite dev server. Production: load Vite-built bundle.
  const devUrl = process.env.VITE_DEV_SERVER_URL;
  if (devUrl) {
    win.loadURL(devUrl);
    // win.webContents.openDevTools({ mode: 'detach' });
  } else {
    win.loadFile(path.join(__dirname, '..', 'dist', 'index.html'));
  }
  win.once('ready-to-show', () => win.show());
}

app.whenReady().then(() => {
  createWindow();
  setupAutoUpdater();
});

// On quit, tree-kill every still-running CLI turn so none are orphaned. With
// per-project runtimes now persisting in the background (in-flight turns survive
// project switches), several chats can be live at once — a single active one is
// no longer the only subprocess. Same teardown path as chat:cancel.
app.on('before-quit', () => {
  for (const [, entry] of activeChats) {
    if (entry && entry.child) { try { killChildTree(entry.child); } catch { /* already gone */ } }
  }
  activeChats.clear();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

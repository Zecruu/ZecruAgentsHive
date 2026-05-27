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
const path = require('path');
const fs = require('fs');
const os = require('os');
const { spawn } = require('child_process');

const CONFIG_FILE = () => path.join(app.getPath('userData'), 'config.json');

const DEFAULT_CONFIG = {
  baseUrl: 'https://agentshive-production.up.railway.app',
  apiKey: '',
  defaultOsHint: detectOsHint(),
};

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

// Build the env vars an agent CLI needs to talk to AgentsHive for this
// project. Returns a plain object — callers compose into spawn env.
function agentEnv({ projectSlug, coderId, osHint }) {
  const cfg = readConfig();
  return {
    AGENTSHIVE_BASE_URL: cfg.baseUrl,
    AGENTSHIVE_API_KEY: cfg.apiKey,
    AGENTSHIVE_PROJECT: projectSlug,
    AGENTSHIVE_CODER_ID: coderId || '',
    AGENTSHIVE_OS_HINT: osHint || cfg.defaultOsHint || '',
  };
}

// Spawn an external terminal pre-wired with the agent env vars + a hint
// command to run. We DON'T auto-execute the CLI — the user sees what's about
// to run and presses Enter. This avoids surprises if the CLI is missing or
// needs interactive auth on first use.
function launchAgent({ role, cli, projectSlug, coderId, osHint }) {
  const env = agentEnv({ projectSlug, coderId, osHint });
  const banner = buildBanner({ role, cli, projectSlug, coderId, osHint });
  const cliCmd = cli === 'codex' ? 'codex' : 'claude';

  if (process.platform === 'win32') {
    return launchWindowsTerminal(env, banner, cliCmd);
  }
  if (process.platform === 'darwin') {
    return launchMacTerminal(env, banner, cliCmd);
  }
  return launchLinuxTerminal(env, banner, cliCmd);
}

function buildBanner({ role, cli, projectSlug, coderId, osHint }) {
  const lines = [
    '============================================================',
    `  AgentsHive agent: ${role.toUpperCase()} (${cli})`,
    `  project: ${projectSlug}`,
    coderId ? `  coder_id: ${coderId}` : null,
    osHint ? `  os_hint: ${osHint}` : null,
    '  Env pre-wired: AGENTSHIVE_BASE_URL, AGENTSHIVE_API_KEY,',
    '                 AGENTSHIVE_PROJECT, AGENTSHIVE_CODER_ID, AGENTSHIVE_OS_HINT',
    '============================================================',
    role === 'hivemind'
      ? '  HIVEMIND: Run `claude` (or your CLI) in this terminal.'
      : '  CODER: cd into your project, then run the CLI.',
    '============================================================',
  ].filter(Boolean).join('\n');
  return lines;
}

function launchWindowsTerminal(env, banner, cliCmd) {
  // Build a PowerShell command that prints the banner and stays open. Don't
  // auto-run the CLI — let the user invoke it explicitly so missing-CLI
  // errors are visible.
  const setEnvLines = Object.entries(env)
    .map(([k, v]) => `$env:${k} = ${powerShellQuote(v)}`)
    .join('; ');
  const echoBanner = banner.split('\n').map(l => `Write-Host ${powerShellQuote(l)}`).join('; ');
  const hint = `Write-Host ''; Write-Host 'Suggested next command:' -ForegroundColor Cyan; Write-Host '  ${cliCmd}' -ForegroundColor Yellow; Write-Host ''`;
  const psCmd = `${setEnvLines}; ${echoBanner}; ${hint}`;

  // Prefer Windows Terminal (`wt.exe`); fall back to powershell in cmd.
  const wtArgs = ['new-tab', 'powershell.exe', '-NoExit', '-Command', psCmd];
  const child = spawn('wt.exe', wtArgs, { detached: true, stdio: 'ignore', shell: false });
  child.on('error', () => {
    // Fallback: cmd.exe /k powershell -NoExit -Command "..."
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

function launchMacTerminal(env, banner, cliCmd) {
  const exports = Object.entries(env)
    .map(([k, v]) => `export ${k}=${shQuote(v)}`)
    .join('; ');
  const echoBanner = `printf '%s\\n' ${shQuote(banner)}`;
  const hint = `printf '\\nSuggested next command:\\n  %s\\n\\n' ${shQuote(cliCmd)}`;
  const innerCmd = `${exports}; ${echoBanner}; ${hint}; exec $SHELL`;
  // osascript opens Terminal.app and runs the command. The escaping is
  // tricky: we wrap the bash command in double quotes for AppleScript, then
  // escape the inner double quotes.
  const osascript = `tell application "Terminal" to do script ${appleScriptQuote(innerCmd)}`;
  spawn('osascript', ['-e', osascript], { detached: true, stdio: 'ignore' }).unref();
}

function shQuote(s) {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

function appleScriptQuote(s) {
  return `"${String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
}

function launchLinuxTerminal(env, banner, cliCmd) {
  const exports = Object.entries(env)
    .map(([k, v]) => `export ${k}=${shQuote(v)}`)
    .join('; ');
  const echoBanner = `printf '%s\\n' ${shQuote(banner)}`;
  const hint = `printf '\\nSuggested next command:\\n  %s\\n\\n' ${shQuote(cliCmd)}`;
  const innerCmd = `${exports}; ${echoBanner}; ${hint}; exec $SHELL`;

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
    defaultOsHint: merged.defaultOsHint,
  };
});

ipcMain.handle('projects:list', async () => {
  return apiFetch('/api/dashboard/projects');
});

ipcMain.handle('projects:create', async (_e, { slug, name }) => {
  return apiFetch('/api/dashboard/projects', { method: 'POST', body: { slug, name } });
});

ipcMain.handle('agent:launch', (_e, payload) => {
  try {
    launchAgent(payload);
    return { ok: true };
  } catch (err) {
    return { ok: false, error: String(err && err.message || err) };
  }
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

// --- window ---------------------------------------------------------------

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 920,
    minHeight: 620,
    backgroundColor: '#0f1115',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  win.removeMenu();
  win.loadFile(path.join(__dirname, '..', 'src', 'index.html'));
  win.once('ready-to-show', () => win.show());
}

app.whenReady().then(createWindow);

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

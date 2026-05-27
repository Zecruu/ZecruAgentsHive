// AgentsHive Desktop renderer.
//
// Single-page UI with three views, swapped into <main id="app">:
//   - settings (first run, or when the ⚙ button is clicked)
//   - project-picker (list/create projects on the AgentsHive server)
//   - project-view (role + CLI dropdowns + Launch button)
//
// Talks to main process via window.agentshive (preload.js).

const api = window.agentshive;
const app = document.getElementById('app');
const statusPill = document.getElementById('server-status');
const openSettingsBtn = document.getElementById('open-settings');

let cachedConfig = null;
let currentProject = null;

function setStatus(text, kind = '') {
  statusPill.textContent = text;
  statusPill.className = 'status-pill' + (kind ? ' ' + kind : '');
}

function tpl(id) {
  const t = document.getElementById(id);
  return t.content.cloneNode(true);
}

function clear() { app.replaceChildren(); }

async function boot() {
  cachedConfig = await api.config.get();
  if (!cachedConfig.apiKeyConfigured) {
    showSettings({ firstRun: true });
    return;
  }
  setStatus(`connected · ${shortHost(cachedConfig.baseUrl)}`, 'ok');
  showProjectPicker();
}

function shortHost(url) {
  try { return new URL(url).host; } catch { return url; }
}

// --- Settings view --------------------------------------------------------

function showSettings({ firstRun = false } = {}) {
  clear();
  app.appendChild(tpl('tpl-settings'));
  const baseInput = document.getElementById('cfg-base-url');
  const keyInput = document.getElementById('cfg-api-key');
  const osSelect = document.getElementById('cfg-os-hint');
  const apiStatus = document.getElementById('cfg-api-status');
  const saveBtn = document.getElementById('cfg-save');
  const cancelBtn = document.getElementById('cfg-cancel');

  baseInput.value = cachedConfig.baseUrl || '';
  osSelect.value = cachedConfig.defaultOsHint || '';
  if (cachedConfig.apiKeyConfigured) {
    keyInput.placeholder = `current: ${cachedConfig.apiKeyMasked} (leave blank to keep)`;
    apiStatus.textContent = 'API key already set — leave blank to keep, paste a new one to replace.';
  }
  if (!firstRun) cancelBtn.hidden = false;

  cancelBtn.addEventListener('click', () => {
    if (currentProject) showProjectView(currentProject);
    else showProjectPicker();
  });

  saveBtn.addEventListener('click', async () => {
    saveBtn.disabled = true;
    try {
      const patch = {
        baseUrl: baseInput.value,
        defaultOsHint: osSelect.value || null,
      };
      if (keyInput.value.trim()) patch.apiKey = keyInput.value;
      const res = await api.config.set(patch);
      cachedConfig = await api.config.get();
      if (!res.apiKeyConfigured) {
        apiStatus.textContent = 'API key still missing.';
        apiStatus.style.color = 'var(--danger)';
        saveBtn.disabled = false;
        return;
      }
      setStatus(`connected · ${shortHost(cachedConfig.baseUrl)}`, 'ok');
      showProjectPicker();
    } catch (err) {
      apiStatus.textContent = 'Save failed: ' + err.message;
      apiStatus.style.color = 'var(--danger)';
      saveBtn.disabled = false;
    }
  });
}

// --- Project picker -------------------------------------------------------

async function showProjectPicker() {
  clear();
  app.appendChild(tpl('tpl-project-picker'));
  const list = document.getElementById('project-list');
  const refresh = document.getElementById('refresh-projects');
  const createBtn = document.getElementById('create-project');
  const slugIn = document.getElementById('new-project-slug');
  const nameIn = document.getElementById('new-project-name');

  refresh.addEventListener('click', () => loadProjects(list));
  createBtn.addEventListener('click', async () => {
    const slug = slugIn.value.trim();
    const name = nameIn.value.trim();
    if (!slug || !name) {
      createBtn.disabled = false;
      return alert('Need both slug and name.');
    }
    createBtn.disabled = true;
    try {
      await api.projects.create(slug, name);
      slugIn.value = '';
      nameIn.value = '';
      await loadProjects(list);
    } catch (err) {
      alert('Create failed: ' + err.message);
    } finally {
      createBtn.disabled = false;
    }
  });

  await loadProjects(list);
}

async function loadProjects(list) {
  list.innerHTML = '<li class="muted">Loading…</li>';
  try {
    const projects = await api.projects.list();
    if (!projects || projects.length === 0) {
      list.innerHTML = '<li class="muted">No projects yet — create one below.</li>';
      return;
    }
    list.innerHTML = '';
    for (const p of projects) {
      const li = document.createElement('li');
      const left = document.createElement('div');
      const name = document.createElement('div');
      name.textContent = p.name || p.slug;
      const slug = document.createElement('div');
      slug.className = 'slug';
      slug.textContent = p.slug;
      left.appendChild(name);
      left.appendChild(slug);
      const right = document.createElement('div');
      right.className = 'muted';
      right.textContent = '→';
      li.appendChild(left);
      li.appendChild(right);
      li.addEventListener('click', () => {
        currentProject = p;
        showProjectView(p);
      });
      list.appendChild(li);
    }
  } catch (err) {
    list.innerHTML = `<li class="muted" style="color: var(--danger)">Failed to load: ${escapeHtml(err.message)}</li>`;
    setStatus('error · check settings', 'err');
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// --- Project view (the agent launcher) ------------------------------------

async function showProjectView(p) {
  clear();
  app.appendChild(tpl('tpl-project-view'));
  document.getElementById('pv-name').textContent = p.name || p.slug;
  document.getElementById('pv-slug').textContent = p.slug;
  document.getElementById('pv-switch').addEventListener('click', () => {
    currentProject = null;
    showProjectPicker();
  });
  document.getElementById('pv-dashboard').addEventListener('click', () => api.dashboard.open(p.slug));

  // Pre-populate coder-id with hostname-shortid for convenience.
  const coderIdInput = document.getElementById('launch-coder-id');
  const hostname = await api.app.hostname().catch(() => 'host');
  coderIdInput.placeholder = `${hostname.toLowerCase().replace(/[^a-z0-9-]+/g, '-')}-coder`;

  const roleSelect = document.getElementById('launch-role');
  const cliSelect = document.getElementById('launch-cli');
  const osSelect = document.getElementById('launch-os-hint');
  const launchBtn = document.getElementById('launch-go');
  const launchStatus = document.getElementById('launch-status');

  // Hivemind is always claude for now (Hivemind = planner in Claude Desktop /
  // Claude Code). Force cli=claude when role=hivemind.
  roleSelect.addEventListener('change', () => {
    if (roleSelect.value === 'hivemind') {
      cliSelect.value = 'claude';
      cliSelect.disabled = true;
    } else {
      cliSelect.disabled = false;
    }
  });

  launchBtn.addEventListener('click', async () => {
    launchBtn.disabled = true;
    launchStatus.textContent = 'launching…';
    launchStatus.className = 'muted';
    const payload = {
      role: roleSelect.value,
      cli: cliSelect.value,
      projectSlug: p.slug,
      coderId: coderIdInput.value.trim() || coderIdInput.placeholder,
      osHint: osSelect.value || null,
    };
    const res = await api.agent.launch(payload);
    if (res.ok) {
      launchStatus.textContent = `launched ${payload.role} (${payload.cli}) → terminal opened`;
      launchStatus.className = 'ok';
    } else {
      launchStatus.textContent = 'launch failed: ' + (res.error || 'unknown');
      launchStatus.className = 'err';
    }
    launchBtn.disabled = false;
  });
}

// --- wire top-bar ---------------------------------------------------------

openSettingsBtn.addEventListener('click', () => showSettings({ firstRun: false }));

// --- go -------------------------------------------------------------------

boot().catch((err) => {
  setStatus('error', 'err');
  app.innerHTML = `<section class="card"><h2>Failed to start</h2><pre>${escapeHtml(err.stack || err.message)}</pre></section>`;
});

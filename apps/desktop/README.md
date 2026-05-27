# AgentsHive Desktop (v2.0-alpha)

Cursor-style control center for AgentsHive: pick a project, pick a role, launch
Claude Code or Codex CLI agents pre-wired to AgentsHive MCP — the way Zed's
agent panel boots up agents, but pointed at our protocol instead.

## Status: alpha

What works today (v2.0-alpha):

- First-run config UI (server URL + API key, persisted to user data dir).
- Project picker (lists projects from `/api/dashboard/projects`, create new).
- Role + CLI dropdown + Launch button.
- Launch spawns a new terminal window (Windows Terminal / Terminal.app /
  gnome-terminal) with `AGENTSHIVE_BASE_URL`, `AGENTSHIVE_API_KEY`,
  `AGENTSHIVE_PROJECT`, `AGENTSHIVE_CODER_ID`, `AGENTSHIVE_OS_HINT` pre-set.
- Open-dashboard button (bounces to browser; in-app embed lands in beta).

What's queued for v2.0-beta:

- Embedded terminals via xterm.js + node-pty (no external window).
- Hivemind dynamic role assignment (coder / designer / marketer).
- In-app dashboard view (no browser bounce).
- Per-project `AGENTS.md` auto-generation from `init_project.py`.

## Run from source

```
cd apps/desktop
npm install
npm start
```

Requires Node 18+ (tested on 24.11). Electron will download a binary on first
`npm install`.

## How it relates to the rest of AgentsHive

- The server is unchanged — this app is a pure client of the existing HTTP +
  MCP surface.
- API key is stored at `%APPDATA%\agentshive-desktop\config.json` (Windows),
  `~/Library/Application Support/agentshive-desktop/config.json` (macOS), or
  `~/.config/agentshive-desktop/config.json` (Linux). v2.0-beta moves it to
  the OS keychain via `keytar`.
- Project context flows to launched agents via env vars (NOT command-line
  args), so any CLI that reads `AGENTSHIVE_*` from env Just Works.

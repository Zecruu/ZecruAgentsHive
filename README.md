# Zecru AgentsHive

An MCP bridge so AI coders (Claude Code, Codex CLI) can ask AI planners (Claude or Codex desktop/mobile) instead of stopping to ask the human.

## How it works

1. **You + the Planner** lock the spec inside Claude.ai or ChatGPT/Codex (desktop or mobile). The Planner has AgentsHive connected as a connector.
2. The Planner calls `create_mission` — the spec is now live in AgentsHive.
3. **You start your Coder** (Claude Code, Codex CLI) with AgentsHive configured as an MCP server.
4. The Coder fetches the spec, builds, and routes every "I need to ask the human" moment to the Planner via AgentsHive. The Coder blocks until the Planner answers.
5. The Coder reports progress as natural-language summaries (no raw code — saves tokens). The Planner reviews and replies with direction or "done."
6. Loop until the Planner calls `mark_mission_done`.

The human is out of the loop after step 1. The Planner is the authority during build.

## MCP tools

**Planner-side** (call from Claude/Codex app via the AgentsHive connector):
- `create_mission(name, spec)` — start a new mission (becomes the active one)
- `list_pending_questions()` — see what the Coder is waiting on
- `answer_question(question_id, answer)` — unblock the Coder
- `list_pending_summaries()` — see new progress reports
- `respond_to_summary(summary_id, response)` — send direction or approval
- `get_active_mission()` — read the current mission
- `mark_mission_done()` — declare it shipped; the Coder stops

**Coder-side** (call from Claude Code / Codex CLI via the AgentsHive MCP server):
- `fetch_mission()` — get the active spec
- `ask_planner(question)` — block until the Planner answers (returns answer, or a `pending` sentinel + `question_id` if the MCP client times out first — call `wait_for_answer` to keep waiting)
- `wait_for_answer(question_id)` — continue blocking on a previously-asked question
- `submit_progress(summary)` — push a progress summary; blocks until the Planner responds
- `wait_for_summary_response(summary_id)` — continue blocking on a previously-submitted summary
- `is_mission_done()` — check whether the Planner has marked the mission complete

## Local development

```bash
pip install -e .
cp .env.example .env  # then edit AGENTSHIVE_API_KEY
python -m agentshive.main
```

The server listens on `http://localhost:8000/mcp` by default. SQLite database is created in the working directory.

## Deployment

Hosted on Railway. Provision a Postgres plugin (auto-injects `DATABASE_URL`) and set `AGENTSHIVE_API_KEY` to a long random string. `railway.toml` handles the build and start command.

## Auth

Two parallel paths, both backed by the same MCP server:

**1. Shared bearer key (`AGENTSHIVE_API_KEY`)** — original v1.0–v1.6 mechanism, still works forever. Any caller that sends `Authorization: Bearer <key>` is authenticated. This is what Claude Code CLI, the test suite, and direct `curl` against `/mcp` use.

**2. OAuth 2.1 (v1.7+)** — required to register AgentsHive as a Claude Desktop "custom connector" (the desktop UI does not accept bearer-token fields). AgentsHive runs as a self-contained OAuth 2.1 authorization server with PKCE + Dynamic Client Registration (RFC 7591) + Protected Resource Metadata (RFC 9728) + token revocation (RFC 7009). The Claude Desktop "Add custom connector" dialog discovers our metadata, registers itself via DCR, opens our consent page in a browser, exchanges the code for tokens, and uses the access token going forward — no manual client config.

Set `AGENTSHIVE_BASE_URL` to the public HTTPS URL of your deployment (e.g. `https://agentshive-production.up.railway.app`) so issued tokens have the correct audience claim per RFC 8707. Without it, the server defaults to `http://localhost:{PORT}` and Claude Desktop will refuse the issued tokens.

### Adding AgentsHive as a Claude Desktop custom connector

1. **Claude Desktop → Settings → Connectors → Add custom connector**
2. **URL:** `https://<your-railway-domain>/mcp`
3. **Client ID / Client Secret:** leave blank (DCR registers the client automatically)
4. A browser tab opens to `/oauth/consent`. If you have a valid dashboard session cookie, just click **Approve**. Otherwise, paste `AGENTSHIVE_API_KEY` in the form and click **Approve**.
5. The connector shows a green "Connected" badge. The access token lasts 1 hour and silently refreshes for up to 30 days.

### OAuth surface

| Endpoint | Purpose |
| --- | --- |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 AS metadata (issuer, endpoints, supported scopes) |
| `GET /.well-known/oauth-protected-resource/mcp` | RFC 9728 PRM — Claude Desktop reads this to find the AS |
| `POST /register` | RFC 7591 Dynamic Client Registration. Soft cap of 100 clients with LRU eviction (by `last_used_at`) |
| `GET /authorize` | OAuth 2.1 PKCE authorization endpoint. Validates the `resource` indicator matches this server before redirecting to `/oauth/consent` |
| `GET /oauth/consent` | Browser-facing consent page. Shows API-key entry field when no dashboard cookie is present, otherwise just Approve/Deny |
| `POST /oauth/consent` | Mints the authorization code on Approve; 302s to `redirect_uri` with `error=access_denied` on Deny |
| `POST /token` | Exchanges code for tokens or refreshes (RFC 6749). Refresh tokens **rotate** on every exchange per OAuth 2.1 BCP |
| `POST /revoke` | RFC 7009 token revocation. Takes effect on the very next `/mcp` request |

Tokens are stored as SHA-256 hashes — the raw bearer values exist only in transit and in the requesting client. Access tokens are 1 hour, refresh tokens 30 days, authorization codes 10 minutes single-use.

### Audience validation

The server validates RFC 8707 audience in two places to defeat confused-deputy:
- `/authorize` refuses to start a flow whose `resource` doesn't match this server's canonical MCP URL.
- `load_access_token` refuses to return any token whose stored `resource` claim mismatches the canonical URL — so a token issued for a different audience cannot authenticate against `/mcp`.

The legacy bearer key path is unaffected by audience validation (it's pre-OAuth and uses the canonical audience by definition).

## Using AgentsHive for multiple projects (v1.9+)

AgentsHive scopes every mission, question, summary, message, and dashboard view by **project**. One AgentsHive deployment can host many independent projects — Zecru Widget, internal tools, side projects — without their missions or chats colliding. A v1.0–v1.8 deployment with no project awareness still works: all legacy data lives in a reserved `default` project, and any client that doesn't supply a project parameter lands there automatically.

### Adding a Coder MCP entry for a project

Append `?project=<slug>` to the `/mcp` URL when registering the connector:

```bash
claude mcp add agentshive-zecru-widget \
  https://<your-railway>.up.railway.app/mcp?project=zecru-widget \
  --header "Authorization: Bearer $AGENTSHIVE_API_KEY"
```

One MCP entry per project per Claude Code window. The Coder window is bound to one project for its lifetime — to switch projects, add a different `agentshive-<slug>` entry pointing at the new project's URL. This is intrinsic to FastMCP transport semantics (one connection per server URL).

### Dashboard project switcher

The dashboard header has a **Project** dropdown that lists every non-archived project. Switching reloads the page to the new project's URL (`?project=<slug>`), which:

- Re-subscribes the SSE channel to the new project (so only that project's events arrive live).
- Re-fetches `/api/dashboard/state` so the page shows only the new project's missions, questions, summaries, inbox.
- Inline **+ New project** and **Archive** buttons next to the dropdown handle creation and soft-delete from the dashboard directly.

### Project lifecycle

| Endpoint | Purpose |
| --- | --- |
| `GET /api/dashboard/projects` | List non-archived projects (add `?include_archived=true` to see them all) |
| `POST /api/dashboard/projects` | Body `{slug, name, description?}`. Slug validated against `^[a-z0-9](?:[a-z0-9-]{0,40}[a-z0-9])?$` (kebab-case, 1-42 chars). The `default` slug is reserved. Returns 201 on success, 400 on validation error, 409 on slug collision |
| `POST /api/dashboard/projects/<slug>/archive` | Soft-delete: sets `archived_at`, hides from switcher, preserves data. The `default` project cannot be archived |

OAuth tokens are project-orthogonal: an OAuth client issued during a consent flow for project A works for project B too. Project scope is a request-level concern (read from the URL), not an authentication concern.

## AgentsHive Desktop (v1.10+)

Downloadable Windows desktop app that bundles the AgentsHive server, runs it locally on `127.0.0.1:8765`, and renders the dashboard in a native window via pywebview / Edge WebView2. Detects local Claude Desktop + Claude Code CLI installs and exposes per-project **Launch Planner** / **Launch Coder** buttons that spawn each pre-wired for the right project — collapsing the "create project → add MCP entry to Claude Desktop → add MCP entry in your editor → start coding" dance into one click.

**Two deployments coexist:** the Railway-hosted server (`https://agentshive-production.up.railway.app/dashboard`) for cross-device / mobile access via OAuth, and the desktop app for local-first power-user workflow on the same machine you code from. They share zero state — desktop's database is `%LOCALAPPDATA%\AgentsHive\data.db`, Railway's is its Postgres.

### Install

1. Download `AgentsHive-Setup-1.10.0.exe` from the [latest GitHub release](https://github.com/Zecruu/ZecruAgentsHive/releases/latest).
2. Run the installer. **Windows SmartScreen will warn** that the publisher is unverified (we don't ship with an EV code-signing cert yet — that's queued for v1.10.x once we have telemetry showing user friction). Click **More info** → **Run anyway**.
3. Per-user install (no admin / UAC prompt). Installs to `%LOCALAPPDATA%\Programs\AgentsHive\` and creates a Start Menu shortcut.
4. Launch from Start Menu or desktop icon. First launch generates a local bearer key at `%LOCALAPPDATA%\AgentsHive\local.key` (never rotated; delete the file to regenerate) and a SQLite database at `%LOCALAPPDATA%\AgentsHive\data.db`.

### What you can do

- **Create projects from the dashboard** with the native **Pick Folder…** button that opens the OS folder dialog (pywebview's JS bridge → `window.create_file_dialog`).
- **Launch Planner** opens Claude Desktop pre-configured for the project's MCP endpoint. For the Microsoft Store Claude install (which silently drops `mcpServers` config — see the v1.7 OAuth section), the MCP URL is copied to your clipboard and you paste it into **Settings → Connectors → Add custom connector**. For the .exe installer variant, the connector is written into `claude_desktop_config.json` automatically (untested on dev machine; queued for v1.10.x VM verification).
- **Launch Coder** opens a new terminal in the project's bound folder, runs `claude mcp add agentshive-<slug> ...`, then `claude --dangerously-skip-permissions`. The Coder is bound to that project for its lifetime.
- **Install status pill** in the header shows ✓/✗ for Claude Desktop and Claude Code CLI detection — instant feedback on what's available before you click Launch.

### Caveats

- **Windows-only in v1.10.** Mac/Linux is queued for v1.11+ (same architecture, different OS-specific branches for paths + detection + installer).
- **WebView2 runtime** ships with Windows 11 and recent Windows 10 updates. If you're on a very old Win10, install the [WebView2 Evergreen Runtime](https://developer.microsoft.com/microsoft-edge/webview2/).
- **Single instance.** Launching the installed app twice raises the existing window instead of spawning a second server (file lock at `%LOCALAPPDATA%\AgentsHive\instance.lock` + named-pipe IPC).
- **Local bearer key never rotates** (Planner Q3 decision). It stays in `%LOCALAPPDATA%\AgentsHive\local.key` so `claude mcp add` registrations don't break across restarts. Delete the file manually to regenerate (will invalidate existing MCP entries).
- **Microsoft Store Claude Desktop** silently drops `mcpServers` config files (the v1.7 OAuth flow was the real fix for this; the desktop launcher uses clipboard handoff as the UX workaround in v1.10).

### Building the installer

```bat
:: From repo root, with pywebview + pyinstaller installed via pip install -e .[desktop]:
scripts\build_desktop.bat
```

Produces `dist\AgentsHive-Setup-1.10.0.exe`. Requires [Inno Setup 6](https://jrsoftware.org/isinfo.php) on PATH or at the default install location.

## Dashboard (v1.4+)

A read-only web view of the unified Planner / Coder state, served by the same Starlette app at:

```
https://<your-railway>.up.railway.app/dashboard
```

Sign in by pasting your `AGENTSHIVE_API_KEY` (same value as `Authorization: Bearer ...`). Session cookie is signed (the key derives from your API key, so rotating the env var auto-invalidates all sessions) and good for 12 hours.

What you see, all on one page:

- **Header card** — active mission name, status badge, Coder heartbeat (color-coded by freshness: <30s green, <60s yellow, >60s red, "not connected" gray), spec preview with expand/collapse, server version + tools catalog hash, logout button.
- **Pending Questions** — questions the Coder is blocked on waiting for the Planner to answer.
- **Pending Summaries** — progress summaries the Coder has submitted, awaiting your response.
- **Messages** — two columns (Coder→Planner, Planner→Coder), with `unacked`/`acked` badges and a redelivery-count chip when applicable.

Auto-refreshes every 3 seconds. Read-only in v1.4 — write actions (answer/respond/ack from the UI) ship in v1.5+.

A status banner appears at the top if the browser loses connection to the server; it retries automatically and clears the banner on success.

## Troubleshooting: MCP client doesn't see new tools after a redeploy

If you've redeployed AgentsHive (e.g., `railway up`) and your MCP client still doesn't show new tools you know shipped, this is a **client-side cache**, not a server bug.

Two diagnostic tools (v1.3+):

- `get_server_info()` — returns `server_version`, `tools_catalog_hash`, `started_at`. Pure read. If `tools_catalog_hash` differs from what your client cached, the catalog drifted.
- `refresh_tool_catalog()` — emits a `notifications/tools/list_changed` MCP notification to your session. Spec-compliant clients respond by re-fetching the tool list automatically.

**Recovery path:**

1. Call `get_server_info` — note the `tools_catalog_hash` and `server_version`.
2. If those don't match what you expect, call `refresh_tool_catalog`. Compliant clients refresh their tool list immediately.
3. If your client still doesn't show new tools — it's caching aggressively across reconnects and is ignoring `tools/list_changed`. Manual fix: **disconnect and reconnect the MCP server in your client.**
   - Claude Code: close the Claude Code app and reopen it (or run `claude mcp remove agentshive` then re-add).
   - Claude.ai / Claude desktop connector: toggle the connector off in Settings → Connectors, then back on.
   - ChatGPT/Codex connector: same toggle-off-toggle-on flow.

This limitation is a property of how each MCP client implements its tool cache; the server emits the right signal but cannot force a non-cooperating client to refresh.

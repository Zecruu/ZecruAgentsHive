# Zecru AgentsHive

An MCP bridge so AI coders (Claude Code, Codex CLI) can ask AI planners (Claude or Codex desktop/mobile) instead of stopping to ask the human.

## Quick start — `agentshive init` (Zed / Claude Code / Codex CLI multi-agent)

Run from your project root to drop in `AGENTS.md` (cross-tool agent rules) plus `.zed/settings.json` (Zed MCP wiring):

```bash
# macOS / Linux — recommended (uses a wrapper script that survives shell-piping quirks)
curl -fsSL https://raw.githubusercontent.com/Zecruu/ZecruAgentsHive/main/scripts/install.sh | bash -s -- my-project
```

```powershell
# Windows PowerShell — recommended (uses install.ps1 wrapper)
iex "& { $(iwr -useb https://raw.githubusercontent.com/Zecruu/ZecruAgentsHive/main/scripts/install.ps1) } my-project"
```

You'll be prompted for the AgentsHive server URL (defaults to the Railway-hosted one) and your API key (or set `AGENTSHIVE_API_KEY` env to skip the prompt).

After init, in Zed's agent panel:
- One thread: *"You are the Hivemind for project `<slug>`. Read AGENTS.md."*
- Other threads: *"You are a Coder for project `<slug>`. Read AGENTS.md."*

The Hivemind orchestrates, Coders implement and ask the Hivemind (not you) when stuck. Pair this with the [`agentshive` skill](https://app.noticomax.com) for full protocol context inside Claude Code CLI.

## Testing (v1.17+)

Start a server, then run all suites sequentially with `tests/runner.py`:

```bash
# Start once, leave running
AGENTSHIVE_API_KEY=test-key PORT=8000 \
    DATABASE_URL=sqlite:///./agentshive_test_runner.db \
    AGENTSHIVE_BASE_URL=http://localhost:8000 \
    TOOL_BLOCK_TIMEOUT_SECONDS=2 \
    python -m agentshive.main &

# Run all suites sequentially (mandatory — see Why below)
AGENTSHIVE_API_KEY=test-key AGENTSHIVE_BASE=http://localhost:8000 \
    python tests/runner.py
```

Single suite:
```bash
python tests/runner.py test_v1_16_scope_guard
```

Skip flaky/slow ones:
```bash
python tests/runner.py --skip test_dashboard_sse test_oauth
```

**Why sequential and not parallel:** several legacy suites (test_v1_1, test_v1_2, test_v1_3, test_supersede, test_inbox, test_dashboard*) assume sole ownership of the default project's active-mission state. Running them concurrently against the same server causes state contamination — questions/missions created by suite A leak into suite B's wait assertions. v1.18+ may refactor each legacy suite to use its own scoped project (Option A from the v1.14 spec) so parallel runs become safe. Until then, `tests/runner.py` is the canonical way to run the full suite.

## Cross-device setup (v1.15+) — Windows + Mac + Linux + mobile

AgentsHive is server-hosted on Railway, so the same dashboard, missions, and message queue are visible from any device with internet + an AgentsHive client. You can run a Hivemind Claude Desktop on Mac, Coder threads on Windows + Linux, and check progress from your phone in claude.ai/code — all on one mission.

### Setting up a fresh machine

Run the install one-liner from any project dir on the new device — same project slug as your other machines, same `AGENTSHIVE_API_KEY`:

| Platform | One-liner |
| --- | --- |
| macOS / Linux | `curl -fsSL https://raw.githubusercontent.com/Zecruu/ZecruAgentsHive/main/scripts/install.sh \| bash -s -- my-project` |
| Windows | `iex "& { $(iwr -useb https://raw.githubusercontent.com/Zecruu/ZecruAgentsHive/main/scripts/install.ps1) } my-project"` |

The init script writes platform-aware Zed config, registers the project on the server (idempotent — running it on the second machine just no-ops), and prints `--role coder` boot prompts pre-filled with the local `os_hint` ("windows" / "macos" / "linux").

### Per-machine Coder identity

When you spawn a Coder thread on each device, pass an `os_hint` matching the local platform on every Coder-side tool call:

```python
ask_planner(question="…", coder_id="dell-xps-coder", os_hint="windows")
submit_progress(summary="…", coder_id="dell-xps-coder", os_hint="windows")
ask_planner(question="…", coder_id="macbook-pro-coder", os_hint="macos")
```

The dashboard's "Connected Coders" panel (v1.13+) now renders an OS icon next to each Coder:

| Icon | Platform |
| --- | --- |
| 🪟 | Windows |
| 🍎 | macOS |
| 🐧 | Linux |
| (none) | unknown / cloud Coder via OAuth (claude.ai web) |

You can see at a glance which machine each Coder is on, sort by last-seen, watch live activity across devices.

### Mobile / web Planner

If you want to drive a mission from your phone, add the AgentsHive custom connector to claude.ai (Settings → Connectors → Add custom connector → `https://agentshive-production.up.railway.app/mcp?project=<slug>`). The v1.7+ OAuth flow handles auth without a bearer field; you sign in once and the Planner can create missions, answer questions, and review summaries from any browser.

### Caveats

- **Same project slug everywhere.** If your Windows machine uses `?project=foo` and your Mac uses `?project=bar`, they're invisible to each other — different namespaces. Run `agentshive init` with the SAME slug on each device, or set the slug in your dashboard before spawning Coders.
- **The bearer key is shared.** All your devices use the same `AGENTSHIVE_API_KEY`. Rotate it (`railway variables --service agentshive --set ...`) if any machine is compromised, then re-run init on the survivors.
- **No automatic device discovery yet.** Setting up a fresh machine is still a deliberate `install.sh` / `install.ps1` step. v1.16+ may add a `agentshive pair` flow that picks up your account-bound config from claude.ai.

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

## Multi-Coder coordination (v1.11+)

Inside one project you can run a single Hivemind (Planner) alongside several Coder agents — e.g., one Coder per subsystem (`coder-server`, `coder-client`, `coder-tests`). All Coders share the active mission; per-Coder identity keeps their questions, summaries, and chat attributed and addressable.

**Coders self-identify.** Every Coder-side tool accepts an optional `coder_id`:

```python
ask_planner(question="0-indexed?", coder_id="coder-server")
submit_progress(summary="phase 1 done", coder_id="coder-server")
send_to_planner(body="fyi I'm starting tests", coder_id="coder-tests")
```

`coder_id` is validated against the same kebab-case regex as project slugs (`[a-z0-9](?:[a-z0-9-]{0,40}[a-z0-9])?`). It surfaces on every question / summary / message in `list_pending_*`, `wait_for_next_*`, and the dashboard JSON, so the Hivemind always knows who asked.

**Hivemind addresses one Coder or broadcasts.** `send_to_coder` takes an optional `target_coder_id`:

```python
send_to_coder(body="use Postgres, not SQLite")                    # broadcast — every Coder sees it
send_to_coder(body="rebase your branch", target_coder_id="coder-server")  # only coder-server sees it
```

**Coders filter their inbox by identity.** `wait_for_planner_message` takes an optional `coder_id`:

| Sender's `target_coder_id` | Coder's `wait_for_planner_message(coder_id=…)` | Delivered? |
| --- | --- | --- |
| `None` (broadcast) | any value (or `None`) | yes |
| `"a"` | `"a"` | yes |
| `"a"` | `"b"` | no — belongs to another Coder |
| `"a"` | `None` (legacy Coder) | no — legacy Coders only see broadcasts |

The last row is the safety property: a legacy single-Coder setup that never declares `coder_id` stays in broadcast-only mode and never silently intercepts a targeted message meant for someone else.

**Backwards compatibility.** `coder_id` is optional everywhere. Omit it and you get pre-v1.11 behavior (no attribution, no targeting). Existing single-Coder workflows need zero changes.

**Dashboard pills.** Each question, summary, and message renders a small `Coder: <id>` pill in a deterministic color (so the same Coder always reads as the same color across sessions). Targeted Planner→Coder messages also show a `→ <target>` pill; broadcasts show `→ broadcast`.

### Connected Coders panel + crash resume (v1.13+)

The dashboard now has a **Connected Coders** card directly under the active mission summary. For every `coder_id` active on the current mission within the last 5 minutes, it shows:

- the v1.11 color-stable pill (same hue as the Coder's Q/S/M rows)
- relative-time of last activity
- a `q N · s N · m N` row of question / summary / message counts on this mission

A Coder counts as "active" by either source: an explicit heartbeat write (any Coder-side tool call that passed `coder_id` bumps a `CoderHeartbeat(project_id, coder_id)` row, throttled to ≤6 writes/min) **or** the `MAX(created_at)` across their questions/summaries/messages on the mission. Heartbeat-only Coders surface even before they've produced any protocol output — useful for the "I'm in Step 1 research, haven't asked yet" case. Legacy Coders (no `coder_id`) aggregate into a single `<unidentified>` row.

**Crash resume.** `wait_for_planner_message(since=…)` takes either an ISO 8601 timestamp or a message_id (32-char hex). Only messages created strictly after that point are eligible. Malformed / future / unknown values silently fall back to "no filter" so the Coder never has to special-case errors:

```python
# Pick up where you left off after a process restart
wait_for_planner_message(coder_id="coder-server", since=last_seen_msg_id)
# Or by wall-clock: only messages from after a known checkpoint
wait_for_planner_message(coder_id="coder-server", since="2026-05-26T13:00:00+00:00")
```

The at-least-once delivery, ack contract, and `coder_id` routing matrix all still apply on top of `since` — it's a pure filter, not a replacement.

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

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

v1 uses a single shared bearer token (`AGENTSHIVE_API_KEY`). Both the Planner connector and the Coder MCP client must send `Authorization: Bearer <key>`. Multi-tenancy and per-user auth are deferred.

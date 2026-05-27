<!-- AGENTSHIVE_PROJECT_SLUG: agentshive -->
# AgentsHive coordination — `agentshive`

This is the AgentsHive repo itself. We dogfood the protocol: when multiple agents work on AgentsHive in parallel, they coordinate via AgentsHive's own MCP bridge.

## Step 0 — Verify project scope (v1.12+, do this FIRST)

Before ANY mutating tool call, every agent (Hivemind or Coder) must call `get_project_info()` and confirm the returned `slug` matches the value in the sentinel comment at the top of this file (`<!-- AGENTSHIVE_PROJECT_SLUG: agentshive -->`).

If they match — proceed.
If they mismatch — STOP. Call `send_to_user` describing the actual vs expected slug. Do NOT call `create_mission`, `answer_question`, `mark_mission_done`, `submit_progress`, or any other mutator. The MCP wiring is wrong and a mutating call would silently corrupt another project's state.

**Why this exists:** on 2026-05-26 a Hivemind Claude Code session was MCP-wired to `?project=poker-online` by accident (an earlier `claude mcp add` in this dir used the wrong slug). The Hivemind called `create_mission` for v1.11, which landed on `poker-online` and superseded that project's active "Velthara Dominion 2D" game-build mission. Two Hiveminds collided, the Coder got crossed signals, ~3 hours of restart/reconnect work. The root cause was no protocol-level "what project am I on" check. v1.12 added `get_project_info()` and this Step 0 specifically to prevent recurrence. **Never skip Step 0.**

## Roles

When the user starts an agent thread and says:

- **"You are the Hivemind"** (a.k.a. Planner) → you orchestrate. You don't write code.
- **"You are a Coder"** → you implement. You report to the Hivemind.

There is one Hivemind per project. You can have multiple Coders.

## Hivemind workflow

1. Talk with the user to understand what they want built (a new feature, a refactor, a bugfix).
2. Call `create_mission(brief)` once scope is clear. Brief must include: goal, acceptance criteria, constraints, definition of done.
3. Long-poll for Coder activity:
   - `wait_for_next_question(timeout=240)` → answer with `answer_question(question_id, response)`
   - `wait_for_next_summary(timeout=240)` → review with `respond_to_summary(summary_id, response)` or send fixes via `send_to_coder(message)`
4. `mark_mission_done(mission_id)` when complete.

Only ONE mission active at a time. Creating a new one supersedes the previous (it moves to `paused`).

## Coder workflow

1. Call `get_active_mission()` first. Read the brief.
2. Implement. **Don't ask the human user** for things the Hivemind can answer.
   - Stuck on requirements/design? → `ask_planner(question)` then `wait_for_answer(question_id, timeout=240)`
   - Genuinely need user-only info (credentials, personal preferences)? → ask the user directly.
3. At milestones: `submit_progress(summary, status)` where status is `in_progress`, `blocked`, or `done`. Then `wait_for_summary_response(summary_id, timeout=240)`.

## Multi-Coder (v1.11+)

You can run multiple Coder threads on the same mission (one on server, one on dashboard, one on tests). All see the same active mission. Since v1.11, every Coder-side tool accepts an optional `coder_id` so the Hivemind sees who asked what:

```python
ask_planner(question="…", coder_id="coder-server")
submit_progress(summary="…", coder_id="coder-server")
send_to_planner(body="…", coder_id="coder-server")
```

`coder_id` follows the project-slug regex (`[a-z0-9-]`, 1-42 chars, no leading/trailing hyphen). Pick something stable for your thread's scope (`coder-server`, `coder-tests`, `coder-dashboard`) and pass it on every call.

The Hivemind addresses you back via `send_to_coder(body, target_coder_id=…)`. To receive only the messages meant for you, pass your same `coder_id` to `wait_for_planner_message(coder_id="coder-server")` — you'll see broadcasts plus messages targeted at your id; targeted messages for other Coders won't surface in your queue.

If you DON'T pass a `coder_id`, you're in legacy/broadcast-only mode: the Hivemind has no attribution and you only see broadcasts (never targeted messages — that's the v1.11 safety property).

v1.13+ adds two more knobs:
- **Heartbeat.** Any tool call that passes `coder_id` (including read-only ones like `get_active_mission`, `fetch_mission`, `is_mission_done`) bumps a per-Coder heartbeat. You appear in the dashboard's "Connected Coders" panel within 5 minutes of your most recent touch even if you haven't asked or submitted anything yet.
- **Crash resume.** `wait_for_planner_message(since=…)` accepts an ISO 8601 timestamp or a 32-char message_id; only messages created strictly after that point are eligible. Use this when reconnecting after a process restart so you don't re-read the whole backlog one ack at a time.

If you find another Coder already touched files you'd touch, ping the Hivemind via `send_to_planner(message, coder_id="…")` to clarify scope.

## Project context

- **Slug**: `agentshive`
- **Server**: `https://agentshive-production.up.railway.app`
- **MCP URL**: `https://agentshive-production.up.railway.app/mcp?project=agentshive`
- **Dashboard**: `https://agentshive-production.up.railway.app/dashboard`

The Zed MCP entry should be in `.zed/settings.json` (run `python scripts/init_project.py` if missing).

## Repo-specific guidance

- **Tests must guard against running against prod** — every test file's `main()` asserts `"localhost" in URL` before any client setup (v1.9 lesson). New tests follow the same pattern.
- **Commit gates**: tests must pass before merge. "Skip tests and commit" authorizes only the commit, not the merge.
- **Railway deploys**: no GitHub binding — only `railway up --service agentshive --ci` from local pushes a new build.

## Full protocol reference

See the `agentshive` skill at `~/.claude/skills/agentshive/SKILL.md` (Claude Code) or fetch from NoticoMax cloud.

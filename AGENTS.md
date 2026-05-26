# AgentsHive coordination — `agentshive`

This is the AgentsHive repo itself. We dogfood the protocol: when multiple agents work on AgentsHive in parallel, they coordinate via AgentsHive's own MCP bridge.

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

## Multi-Coder

You can run multiple Coder threads on the same mission (one on server, one on dashboard, one on tests). All see the same active mission. The Hivemind sees questions in arrival order — questions don't carry per-Coder identity in v1.x, so be explicit about which part of the work you're asking about.

If you find another Coder already touched files you'd touch, ping the Hivemind via `send_to_planner(message)` to clarify scope.

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

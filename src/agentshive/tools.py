import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import Session, select

from .config import Settings
from .db import Mission, Question, Summary, get_engine


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _mission_dict(m: Mission) -> dict[str, Any]:
    return {
        "mission_id": m.id,
        "name": m.name,
        "spec": m.spec,
        "status": m.status,
        "created_at": m.created_at.isoformat(),
        "done_at": m.done_at.isoformat() if m.done_at else None,
        "coder_last_seen": m.coder_last_seen.isoformat() if m.coder_last_seen else None,
    }


def _question_dict(q: Question) -> dict[str, Any]:
    return {
        "question_id": q.id,
        "mission_id": q.mission_id,
        "body": q.body,
        "answer": q.answer,
        "created_at": q.created_at.isoformat(),
        "answered_at": q.answered_at.isoformat() if q.answered_at else None,
    }


def _summary_dict(s: Summary) -> dict[str, Any]:
    return {
        "summary_id": s.id,
        "mission_id": s.mission_id,
        "body": s.body,
        "response": s.response,
        "created_at": s.created_at.isoformat(),
        "responded_at": s.responded_at.isoformat() if s.responded_at else None,
    }


def _active_mission(session: Session) -> Optional[Mission]:
    return session.exec(
        select(Mission).where(Mission.status == "active").order_by(Mission.created_at.desc())
    ).first()


def _touch_coder(session: Session) -> None:
    """Update the active mission's coder_last_seen to now.

    Called once per Coder-side tool invocation so the Planner can see whether the
    Coder process is alive without an explicit ping protocol. Called at the START
    of a tool, not inside per-iteration polling loops — one touch per call is the
    intended granularity ("the Coder placed this tool call N seconds ago").

    No-op if no mission is active.
    """
    mission = _active_mission(session)
    if mission is not None:
        mission.coder_last_seen = _utcnow()
        session.add(mission)
        session.commit()


def register_tools(mcp, settings: Settings) -> None:
    """Register every AgentsHive tool with the given FastMCP instance."""

    poll_interval = settings.poll_interval_seconds
    block_timeout = settings.tool_block_timeout_seconds

    # ---------- Planner-side tools ----------

    @mcp.tool
    def create_mission(name: str, spec: str) -> dict[str, Any]:
        """Start a new AgentsHive mission. The Coder will fetch this spec and begin building.

        Any previously-active mission is marked 'superseded' — there is only one active
        mission at a time. Call this once you and the human have locked the spec.

        Args:
            name: short label for the mission (e.g., "Build the invoice exporter")
            spec: the full natural-language specification the Coder should implement
        """
        with Session(get_engine()) as session:
            current = _active_mission(session)
            if current:
                current.status = "superseded"
                session.add(current)
            mission = Mission(name=name, spec=spec, status="active")
            session.add(mission)
            session.commit()
            session.refresh(mission)
            return _mission_dict(mission)

    @mcp.tool
    def get_active_mission() -> dict[str, Any]:
        """Return the currently active mission (spec + status), or None if none is active."""
        with Session(get_engine()) as session:
            mission = _active_mission(session)
            return _mission_dict(mission) if mission else {"mission": None}

    @mcp.tool
    def list_pending_questions() -> list[dict[str, Any]]:
        """List every question the Coder has asked that you have not yet answered.

        Returns questions for the currently-active mission, oldest first.
        """
        with Session(get_engine()) as session:
            mission = _active_mission(session)
            if not mission:
                return []
            rows = session.exec(
                select(Question)
                .where(Question.mission_id == mission.id, Question.answer.is_(None))
                .order_by(Question.created_at)
            ).all()
            return [_question_dict(q) for q in rows]

    @mcp.tool
    def answer_question(question_id: str, answer: str) -> dict[str, Any]:
        """Answer a pending question from the Coder. The Coder will receive this and resume."""
        with Session(get_engine()) as session:
            q = session.get(Question, question_id)
            if q is None:
                return {"error": f"no question with id {question_id}"}
            if q.answer is not None:
                return {"error": "question already answered", "question": _question_dict(q)}
            q.answer = answer
            q.answered_at = _utcnow()
            session.add(q)
            session.commit()
            session.refresh(q)
            return _question_dict(q)

    @mcp.tool
    def list_pending_summaries() -> list[dict[str, Any]]:
        """List every progress summary the Coder has submitted that you have not yet responded to."""
        with Session(get_engine()) as session:
            mission = _active_mission(session)
            if not mission:
                return []
            rows = session.exec(
                select(Summary)
                .where(Summary.mission_id == mission.id, Summary.response.is_(None))
                .order_by(Summary.created_at)
            ).all()
            return [_summary_dict(s) for s in rows]

    @mcp.tool
    def respond_to_summary(summary_id: str, response: str) -> dict[str, Any]:
        """Respond to a Coder progress summary. Use this to give direction, request changes, or say 'continue'.

        To mark the entire mission as finished, call mark_mission_done — not this.
        """
        with Session(get_engine()) as session:
            s = session.get(Summary, summary_id)
            if s is None:
                return {"error": f"no summary with id {summary_id}"}
            if s.response is not None:
                return {"error": "summary already responded to", "summary": _summary_dict(s)}
            s.response = response
            s.responded_at = _utcnow()
            session.add(s)
            session.commit()
            session.refresh(s)
            return _summary_dict(s)

    @mcp.tool
    def wait_for_next_question(timeout_seconds: Optional[float] = None) -> dict[str, Any]:
        """Block until any unanswered question exists for the currently-active mission.

        Use this instead of polling list_pending_questions in a loop. Mirrors the
        Coder-side ask_planner blocking semantics from the Planner's side: call
        once, the server blocks until a real item arrives.

        On hit: returns the single matching question (same shape as one entry from
        list_pending_questions). Pass `question_id` directly to answer_question.
        Returns the OLDEST unanswered question — answer in arrival order.

        On timeout: returns {status: "pending", message: ...}. The MCP transport
        will time out the call before the configured server-side timeout in most
        clients; just call wait_for_next_question again — there is no question_id
        to track because we are waiting on "whatever shows up next," not a
        specific one.

        Supersede behavior: if the active mission changes mid-wait (someone
        called create_mission), the new active mission's pending items become
        eligible. You wait for whoever's active, never for a specific mission.

        Args:
            timeout_seconds: optional override for how long the server blocks before
                returning "pending". Falls back to TOOL_BLOCK_TIMEOUT_SECONDS.
        """
        block_for = timeout_seconds if timeout_seconds is not None else block_timeout
        deadline = time.monotonic() + block_for
        while True:
            with Session(get_engine()) as session:
                mission = _active_mission(session)
                if mission is not None:
                    q = session.exec(
                        select(Question)
                        .where(Question.mission_id == mission.id, Question.answer.is_(None))
                        .order_by(Question.created_at)
                    ).first()
                    if q is not None:
                        return _question_dict(q)
            if time.monotonic() >= deadline:
                return {
                    "status": "pending",
                    "message": "no questions yet — call wait_for_next_question again to keep waiting",
                }
            time.sleep(poll_interval)

    @mcp.tool
    def wait_for_next_summary(timeout_seconds: Optional[float] = None) -> dict[str, Any]:
        """Block until any progress summary awaiting your response exists for the active mission.

        The summary-side companion to wait_for_next_question. Use instead of
        polling list_pending_summaries.

        On hit: returns the single oldest unresponded summary; pass summary_id
        directly to respond_to_summary.

        On timeout: returns {status: "pending", message: ...}. Call again to
        keep waiting.

        Supersede: same as wait_for_next_question — if the active mission
        changes mid-wait, the new active mission's pending summaries become
        eligible.

        Args:
            timeout_seconds: optional override for the server-side block.
                Falls back to TOOL_BLOCK_TIMEOUT_SECONDS.
        """
        block_for = timeout_seconds if timeout_seconds is not None else block_timeout
        deadline = time.monotonic() + block_for
        while True:
            with Session(get_engine()) as session:
                mission = _active_mission(session)
                if mission is not None:
                    s = session.exec(
                        select(Summary)
                        .where(Summary.mission_id == mission.id, Summary.response.is_(None))
                        .order_by(Summary.created_at)
                    ).first()
                    if s is not None:
                        return _summary_dict(s)
            if time.monotonic() >= deadline:
                return {
                    "status": "pending",
                    "message": "no summaries yet — call wait_for_next_summary again to keep waiting",
                }
            time.sleep(poll_interval)

    @mcp.tool
    def mark_mission_done() -> dict[str, Any]:
        """Mark the active mission as done. The Coder will see this on its next is_mission_done() check and stop."""
        with Session(get_engine()) as session:
            mission = _active_mission(session)
            if mission is None:
                return {"error": "no active mission"}
            mission.status = "done"
            mission.done_at = _utcnow()
            session.add(mission)
            session.commit()
            session.refresh(mission)
            return _mission_dict(mission)

    # ---------- Coder-side tools ----------

    @mcp.tool
    def fetch_mission() -> dict[str, Any]:
        """Fetch the currently-active mission spec from AgentsHive. Call this first when starting work.

        Returns the mission's name, spec, status, and mission_id. If there is no active mission,
        the Planner has not created one yet — wait or stop.
        """
        with Session(get_engine()) as session:
            _touch_coder(session)
            mission = _active_mission(session)
            if mission is None:
                return {"mission": None, "message": "No active mission. The Planner has not started one yet."}
            return _mission_dict(mission)

    def _wait_for_question(question_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + block_timeout
        while True:
            with Session(get_engine()) as session:
                q = session.get(Question, question_id)
                if q is None:
                    return {"error": f"no question with id {question_id}"}
                if q.answer is not None:
                    return {"status": "answered", **_question_dict(q)}
                # The mission this question belongs to may have moved out from under us
                # (Planner called create_mission again, or mark_mission_done). Surface the
                # actual mission.status so the Coder can branch — without this, the Coder
                # would block forever waiting for an answer that no Planner UI can deliver.
                mission = session.get(Mission, q.mission_id)
                if mission is not None and mission.status != "active":
                    return {
                        "status": mission.status,
                        "question_id": question_id,
                        "mission_id": q.mission_id,
                        "message": (
                            "Your mission is no longer active — fetch_mission to get the new "
                            "spec and decide whether to restart."
                            if mission.status == "superseded"
                            else "Your mission is marked done — stop work."
                        ),
                    }
            if time.monotonic() >= deadline:
                return {
                    "status": "pending",
                    "question_id": question_id,
                    "message": (
                        "Planner has not answered yet. Call wait_for_answer(question_id) "
                        "to keep waiting — this is not an error, just a long-running operation."
                    ),
                }
            time.sleep(poll_interval)

    @mcp.tool
    def ask_planner(question: str) -> dict[str, Any]:
        """Ask the Planner a question and wait for the answer.

        Use this whenever you would otherwise stop and ask the human. This is the entire
        point of AgentsHive — the Planner (running in Claude or Codex desktop/mobile) becomes
        your human substitute.

        Behavior: this call blocks until the Planner answers, up to an internal timeout
        (~50s by default). If the timeout is hit before an answer arrives, you get a
        {status: "pending", question_id: ...} response — call wait_for_answer(question_id)
        repeatedly until you get a real answer. Do NOT treat 'pending' as failure.
        """
        with Session(get_engine()) as session:
            _touch_coder(session)
            mission = _active_mission(session)
            if mission is None:
                return {"error": "no active mission — cannot ask"}
            q = Question(mission_id=mission.id, body=question)
            session.add(q)
            session.commit()
            session.refresh(q)
            question_id = q.id
        return _wait_for_question(question_id)

    @mcp.tool
    def wait_for_answer(question_id: str) -> dict[str, Any]:
        """Continue waiting for the Planner to answer a previously-asked question.

        Use this when ask_planner returned status="pending" (the MCP transport timed out
        before the Planner answered). Keep calling until you get status="answered".
        """
        with Session(get_engine()) as session:
            _touch_coder(session)
        return _wait_for_question(question_id)

    def _wait_for_summary(summary_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + block_timeout
        while True:
            with Session(get_engine()) as session:
                s = session.get(Summary, summary_id)
                if s is None:
                    return {"error": f"no summary with id {summary_id}"}
                if s.response is not None:
                    return {"status": "responded", **_summary_dict(s)}
                mission = session.get(Mission, s.mission_id)
                if mission is not None and mission.status != "active":
                    return {
                        "status": mission.status,
                        "summary_id": summary_id,
                        "mission_id": s.mission_id,
                        "message": (
                            "Your mission is no longer active — fetch_mission to get the new "
                            "spec and decide whether to restart."
                            if mission.status == "superseded"
                            else "Your mission is marked done — stop work."
                        ),
                    }
            if time.monotonic() >= deadline:
                return {
                    "status": "pending",
                    "summary_id": summary_id,
                    "message": (
                        "Planner has not responded yet. Call wait_for_summary_response(summary_id) "
                        "to keep waiting."
                    ),
                }
            time.sleep(poll_interval)

    @mcp.tool
    def submit_progress(summary: str) -> dict[str, Any]:
        """Push a natural-language progress summary to the Planner and wait for their response.

        Call this at meaningful checkpoints (after each feature / milestone). Write in plain
        English — the Planner judges your work from this text, NOT from raw code or diffs.
        Be honest about what was done, what wasn't, and any decisions you made along the way.

        Behavior: blocks until the Planner responds. If the MCP transport times out first,
        you get status="pending" + summary_id — call wait_for_summary_response(summary_id)
        to keep waiting.
        """
        with Session(get_engine()) as session:
            _touch_coder(session)
            mission = _active_mission(session)
            if mission is None:
                return {"error": "no active mission — cannot submit progress"}
            s = Summary(mission_id=mission.id, body=summary)
            session.add(s)
            session.commit()
            session.refresh(s)
            summary_id = s.id
        return _wait_for_summary(summary_id)

    @mcp.tool
    def wait_for_summary_response(summary_id: str) -> dict[str, Any]:
        """Continue waiting for the Planner to respond to a previously-submitted summary."""
        with Session(get_engine()) as session:
            _touch_coder(session)
        return _wait_for_summary(summary_id)

    @mcp.tool
    def is_mission_done(mission_id: Optional[str] = None) -> dict[str, Any]:
        """Check the status of a mission.

        Without an argument: backward-compatible behavior — reports on the latest applicable
        mission (active first, else most-recently-done). Useful as a simple "are we shipped?"
        check when the Coder only ever cares about the current top mission.

        With mission_id: report on that specific mission. Use this when you're holding a
        mission_id from an earlier fetch_mission / ask_planner / submit_progress and want
        to know whether the mission you're actually working on is active, superseded by a
        newer one, or done.

        Returns: {done: bool, status: "active"|"done"|"superseded"|None, mission: dict|None}
        - done is True ONLY when status == "done"
        - status carries the literal mission.status so the Coder can branch correctly
          (e.g., distinguish "Planner started a new mission, fetch_mission and restart"
          from "Planner shipped this one, stop")
        """
        with Session(get_engine()) as session:
            _touch_coder(session)
            if mission_id is not None:
                m = session.get(Mission, mission_id)
                if m is None:
                    return {
                        "done": False,
                        "status": None,
                        "mission": None,
                        "error": f"no mission with id {mission_id}",
                    }
                return {
                    "done": m.status == "done",
                    "status": m.status,
                    "mission": _mission_dict(m),
                }

            active = _active_mission(session)
            if active is not None:
                return {"done": False, "status": active.status, "mission": _mission_dict(active)}
            most_recent_done = session.exec(
                select(Mission).where(Mission.status == "done").order_by(Mission.created_at.desc())
            ).first()
            if most_recent_done is not None:
                return {"done": True, "status": "done", "mission": _mission_dict(most_recent_done)}
            return {"done": False, "status": None, "mission": None, "message": "No mission exists yet."}

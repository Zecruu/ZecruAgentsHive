import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .config import Settings
from .db import Message, Mission, Question, Summary, get_engine


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


def _message_dict(m: Message) -> dict[str, Any]:
    # redelivery_count surface is 0-indexed (matches docstring: 0 = first delivery,
    # positive = N readers saw this before you without acking). DB stays 1-indexed
    # internally because writes are simpler that way; we subtract 1 here so callers
    # see the semantic value. max(0, ...) handles the never-returned case where the
    # DB column is still 0 from the default.
    db_count = m.redelivery_count or 0
    return {
        "message_id": m.id,
        "mission_id": m.mission_id,
        "direction": m.direction,
        "body": m.body,
        "created_at": m.created_at.isoformat(),
        # delivered_at semantically means "acked_at" since v1.2 — see Message model docstring
        "delivered_at": m.delivered_at.isoformat() if m.delivered_at else None,
        "redelivery_count": max(0, db_count - 1),
    }


def _active_mission(session: Session) -> Optional[Mission]:
    return session.exec(
        select(Mission).where(Mission.status == "active").order_by(Mission.created_at.desc())
    ).first()


# ---------- Input validation (v1.2 Feature 3) ----------
# Length caps live here, not in config, because they're protocol guarantees rather
# than per-deployment tunables. If anyone hits a wall against these, we'll move
# them to env vars then — until then a single source of truth keeps the surface flat.

MAX_NAME_LEN = 200
MAX_SPEC_LEN = 64 * 1024       # 64 KB
MAX_TEXT_LEN = 16 * 1024       # 16 KB — applies to question, summary, message body,
                               # answer, response. One cap simpler than seven nearly-equal ones.


def _validate_text(value: str, field_name: str, max_len: int) -> Optional[dict]:
    """Return an error dict if value is empty/whitespace or exceeds max_len; None if OK.

    Tool entry points call this and short-circuit on a non-None return so callers
    get a clean error before any DB write happens.
    """
    if not isinstance(value, str) or not value.strip():
        return {"error": f"{field_name} must be a non-empty string"}
    if len(value) > max_len:
        return {
            "error": (
                f"{field_name} exceeds maximum length of {max_len} characters "
                f"(got {len(value)})"
            )
        }
    return None


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

    # ---------- Long-poll helpers (v1.2 Feature 4 — DRY up 6 near-identical wait loops) ----------
    #
    # Two helpers, not one. The wait sites split into two patterns that don't combine cleanly:
    #   Pattern A — wait on a SPECIFIC row by id, return on terminal state, surface parent
    #               mission's status if it left "active" mid-wait. Async state machine.
    #               Used by _wait_for_question and _wait_for_summary.
    #   Pattern B — wait on the OLDEST matching row for the active mission, return when one
    #               appears. No lifecycle branch (the row IS for active mission by query
    #               construction). Pure pull-from-queue. Optional on_hit side-effect lets
    #               message tools increment redelivery_count without auto-acking.
    #               Used by wait_for_next_question, wait_for_next_summary,
    #               wait_for_coder_message, wait_for_planner_message.
    #
    # A previous draft tried a single helper with lifecycle_check=None — confusing conditional
    # paths defeated the abstraction. Two helpers, each does one thing.

    def _wait_specific(
        row_id,
        model_cls,
        id_key,
        is_terminal,
        terminal_status,
        to_dict,
        pending_msg,
    ):
        deadline = time.monotonic() + block_timeout
        while True:
            with Session(get_engine()) as session:
                row = session.get(model_cls, row_id)
                if row is None:
                    return {"error": f"no {id_key.replace('_id', '')} with id {row_id}"}
                if is_terminal(row):
                    return {"status": terminal_status, **to_dict(row)}
                mission = session.get(Mission, row.mission_id)
                if mission is not None and mission.status != "active":
                    return {
                        "status": mission.status,
                        id_key: row_id,
                        "mission_id": row.mission_id,
                        "message": (
                            "Your mission is no longer active — fetch_mission to get the new "
                            "spec and decide whether to restart."
                            if mission.status == "superseded"
                            else "Your mission is marked done — stop work."
                        ),
                    }
            if time.monotonic() >= deadline:
                return {"status": "pending", id_key: row_id, "message": pending_msg}
            time.sleep(poll_interval)

    def _wait_for_active(
        query_fn,
        to_dict,
        pending_msg,
        block_for,
        on_hit_mutate=None,
    ):
        deadline = time.monotonic() + block_for
        while True:
            with Session(get_engine()) as session:
                mission = _active_mission(session)
                if mission is not None:
                    row = query_fn(session, mission)
                    if row is not None:
                        if on_hit_mutate is not None:
                            on_hit_mutate(row, session)
                        return to_dict(row)
            if time.monotonic() >= deadline:
                return {"status": "pending", "message": pending_msg}
            time.sleep(poll_interval)

    # ---------- Planner-side tools ----------

    @mcp.tool
    def create_mission(name: str, spec: str) -> dict[str, Any]:
        """Start a new AgentsHive mission. The Coder will fetch this spec and begin building.

        Any previously-active mission is marked 'superseded' — there is only one active
        mission at a time. Call this once you and the human have locked the spec.

        Args:
            name: short label for the mission (e.g., "Build the invoice exporter").
                Must be non-empty, max {MAX_NAME_LEN} characters.
            spec: the full natural-language specification the Coder should implement.
                Must be non-empty, max {MAX_SPEC_LEN // 1024} KB.
        """
        err = _validate_text(name, "name", MAX_NAME_LEN) or _validate_text(spec, "spec", MAX_SPEC_LEN)
        if err:
            return err
        # Belt-and-suspenders against concurrent create_mission races on Postgres:
        # the partial unique index `one_active_mission ON mission(status) WHERE
        # status='active'` enforces the invariant at the DB level. On collision we
        # treat the racing winner as a "supersede target" and retry once — semantically
        # faithful to create_mission's contract ("new mission, prior is superseded").
        # One retry only: sustained contention should surface as an error so the caller
        # decides what to do instead of us spinning.
        for attempt in range(2):
            try:
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
            except IntegrityError:
                if attempt == 0:
                    continue
                return {
                    "error": (
                        "create_mission contention: another concurrent creator beat us "
                        "twice in a row. The active mission belongs to someone else right "
                        "now — call get_active_mission to see it, then create_mission again "
                        "if you still want to supersede."
                    )
                }

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

        Transport note: list returns are wrapped by FastMCP's structured_content layer
        under a "result" key in the MCP message envelope. Most clients unwrap this
        automatically; if yours doesn't, look for {"result": [...]}. Prefer
        wait_for_next_question for a push-style loop that returns one item at a time.
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
        err = _validate_text(answer, "answer", MAX_TEXT_LEN)
        if err:
            return err
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
        """List every progress summary the Coder has submitted that you have not yet responded to.

        Transport note: list returns are wrapped by FastMCP's structured_content layer
        under a "result" key in the MCP message envelope. Most clients unwrap this
        automatically. Prefer wait_for_next_summary for a push-style loop.
        """
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
        err = _validate_text(response, "response", MAX_TEXT_LEN)
        if err:
            return err
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
        return _wait_for_active(
            lambda session, mission: session.exec(
                select(Question)
                .where(Question.mission_id == mission.id, Question.answer.is_(None))
                .order_by(Question.created_at)
            ).first(),
            _question_dict,
            "no questions yet — call wait_for_next_question again to keep waiting",
            timeout_seconds if timeout_seconds is not None else block_timeout,
        )

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
        return _wait_for_active(
            lambda session, mission: session.exec(
                select(Summary)
                .where(Summary.mission_id == mission.id, Summary.response.is_(None))
                .order_by(Summary.created_at)
            ).first(),
            _summary_dict,
            "no summaries yet — call wait_for_next_summary again to keep waiting",
            timeout_seconds if timeout_seconds is not None else block_timeout,
        )

    @mcp.tool
    def send_to_coder(body: str) -> dict[str, Any]:
        """Planner-side: send a free-form message TO the Coder. Fire-and-forget.

        Use this for casual "fyi…" / "while you're at it…" / "I noticed X" updates that
        don't need a structured response. The Coder reads via wait_for_planner_message().
        For structured Q&A or progress review, use answer_question / respond_to_summary
        as before — this is the chat-style channel, not a replacement.

        Inserts a Message addressed to the Coder against the currently-active mission.
        Returns the message_id immediately; does NOT block.
        """
        err = _validate_text(body, "body", MAX_TEXT_LEN)
        if err:
            return err
        with Session(get_engine()) as session:
            mission = _active_mission(session)
            if mission is None:
                return {"error": "no active mission — cannot send"}
            m = Message(mission_id=mission.id, direction="planner_to_coder", body=body)
            session.add(m)
            session.commit()
            session.refresh(m)
            return _message_dict(m)

    @mcp.tool
    def wait_for_coder_message(timeout_seconds: Optional[float] = None) -> dict[str, Any]:
        """Planner-side: block until an unacked Coder→Planner message exists for the active mission.

        AT-LEAST-ONCE SEMANTICS (v1.2): returns the OLDEST unacked message but does NOT
        stamp delivered_at. Until you call ack_message(message_id), subsequent calls to
        this tool keep returning the same row (with redelivery_count incrementing each
        time). Reader pattern: wait → process → ack. If you crash before ack, you'll see
        the row again on next call — exactly the safety property you want.

        On timeout: {status: "pending", message: ...}. Call again to keep waiting.

        Args:
            timeout_seconds: optional override for the server-side block. Falls back to
                TOOL_BLOCK_TIMEOUT_SECONDS.
        """
        def _bump(m, session):
            m.redelivery_count = (m.redelivery_count or 0) + 1
            session.add(m)
            session.commit()
            session.refresh(m)
        return _wait_for_active(
            lambda session, mission: session.exec(
                select(Message)
                .where(
                    Message.mission_id == mission.id,
                    Message.direction == "coder_to_planner",
                    Message.delivered_at.is_(None),
                )
                .order_by(Message.created_at)
            ).first(),
            _message_dict,
            "no messages yet — call wait_for_coder_message again to keep waiting",
            timeout_seconds if timeout_seconds is not None else block_timeout,
            on_hit_mutate=_bump,
        )

    @mcp.tool
    def send_to_planner(body: str) -> dict[str, Any]:
        """Coder-side: send a free-form message TO the Planner. Fire-and-forget.

        Use this for "fyi…" / "I made a small tangential decision" / "here's an
        observation about AgentsHive itself" updates that don't warrant a full
        submit_progress checkpoint. The Planner reads via wait_for_coder_message().

        Inserts a Message addressed to the Planner against the active mission. Also
        bumps the Coder heartbeat. Returns the message_id immediately; does NOT block.
        """
        err = _validate_text(body, "body", MAX_TEXT_LEN)
        if err:
            return err
        with Session(get_engine()) as session:
            _touch_coder(session)
            mission = _active_mission(session)
            if mission is None:
                return {"error": "no active mission — cannot send"}
            m = Message(mission_id=mission.id, direction="coder_to_planner", body=body)
            session.add(m)
            session.commit()
            session.refresh(m)
            return _message_dict(m)

    @mcp.tool
    def wait_for_planner_message(timeout_seconds: Optional[float] = None) -> dict[str, Any]:
        """Coder-side: block until an unacked Planner→Coder message exists for the active mission.

        AT-LEAST-ONCE SEMANTICS (v1.2): returns the OLDEST unacked message but does NOT
        stamp delivered_at. Until you call ack_message(message_id), subsequent calls keep
        returning the same row (with redelivery_count incrementing). Reader pattern:
        wait → process → ack. If you crash before ack, you'll see the row again next call.

        Also bumps the Coder heartbeat (single touch on entry, not per poll iteration).

        On timeout: {status: "pending", message: ...}. Call again to keep waiting.

        Args:
            timeout_seconds: optional override for the server-side block. Falls back to
                TOOL_BLOCK_TIMEOUT_SECONDS.
        """
        with Session(get_engine()) as session:
            _touch_coder(session)
        def _bump(m, session):
            m.redelivery_count = (m.redelivery_count or 0) + 1
            session.add(m)
            session.commit()
            session.refresh(m)
        return _wait_for_active(
            lambda session, mission: session.exec(
                select(Message)
                .where(
                    Message.mission_id == mission.id,
                    Message.direction == "planner_to_coder",
                    Message.delivered_at.is_(None),
                )
                .order_by(Message.created_at)
            ).first(),
            _message_dict,
            "no messages yet — call wait_for_planner_message again to keep waiting",
            timeout_seconds if timeout_seconds is not None else block_timeout,
            on_hit_mutate=_bump,
        )

    @mcp.tool
    def ack_message(message_id: str) -> dict[str, Any]:
        """Acknowledge receipt of a message returned by wait_for_coder_message or wait_for_planner_message.

        Idempotent: calling ack on an already-acked message is a no-op that returns the
        same message dict. Both Planner and Coder use this — the symmetric design avoids
        a "who should ack this?" handshake on top of the role split.

        Reader pattern (v1.2 at-least-once):
            msg = wait_for_*_message(...)
            ... process msg ...
            ack_message(msg["message_id"])

        If you skip the ack, the next wait_for_*_message call returns the same row with
        redelivery_count incremented. If you crashed between wait and ack, that's the
        feature — the message lives on for the next reader instead of vanishing into
        the void of "delivered but never seen."

        Returns the full message dict with the (possibly newly stamped) delivered_at.
        """
        with Session(get_engine()) as session:
            m = session.get(Message, message_id)
            if m is None:
                return {"error": f"no message with id {message_id}"}
            if m.delivered_at is not None:
                # idempotent: already acked, just return the row as-is
                return _message_dict(m)
            m.delivered_at = _utcnow()
            session.add(m)
            session.commit()
            session.refresh(m)
            return _message_dict(m)

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
        return _wait_specific(
            question_id,
            Question,
            "question_id",
            lambda q: q.answer is not None,
            "answered",
            _question_dict,
            (
                "Planner has not answered yet. Call wait_for_answer(question_id) "
                "to keep waiting — this is not an error, just a long-running operation."
            ),
        )

    @mcp.tool
    def ask_planner(question: str) -> dict[str, Any]:
        """Ask the Planner a question and wait for the answer.

        Use this whenever you would otherwise stop and ask the human. This is the entire
        point of AgentsHive — the Planner (running in Claude or Codex desktop/mobile) becomes
        your human substitute.

        Behavior: this call blocks until the Planner answers, up to an internal timeout
        (~4 minutes by default; controlled by TOOL_BLOCK_TIMEOUT_SECONDS). If the timeout
        is hit before an answer arrives, you get a {status: "pending", question_id: ...}
        response — call wait_for_answer(question_id) repeatedly until you get a real
        answer. Do NOT treat 'pending' as failure.
        """
        err = _validate_text(question, "question", MAX_TEXT_LEN)
        if err:
            return err
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
        return _wait_specific(
            summary_id,
            Summary,
            "summary_id",
            lambda s: s.response is not None,
            "responded",
            _summary_dict,
            (
                "Planner has not responded yet. Call wait_for_summary_response(summary_id) "
                "to keep waiting."
            ),
        )

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
        err = _validate_text(summary, "summary", MAX_TEXT_LEN)
        if err:
            return err
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

"""Durable Mad Scientist graph.

The mission row and parent_run_id stay compatibility metadata. Scheduling,
dependency fan-in, fixer/retry attempts, and step status live in the
mad_scientist_* tables.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import config
import models
import run_approval
import run_chain

INFLIGHT_STEP_STATUSES = {"queued", "running", "awaiting_approval"}
TERMINAL_RUN_STATUSES = {"done", "failed", "rejected", "cancelled"}
EVALUATOR_ROLES = {"reviewer", "tester"}
VERIFIER_ROLES = EVALUATOR_ROLES
BLOCKED_HUMAN = "BLOCKED_HUMAN"
_RUN_TO_STEP = {
    "pending": "queued",
    "running": "running",
    "awaiting_approval": "awaiting_approval",
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def has_graph(mission_id: int) -> bool:
    return get_by_mission_id(mission_id) is not None


def get_by_mission_id(mission_id: int):
    with models.get_db() as conn:
        return conn.execute(
            "SELECT * FROM mad_scientist_missions WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()


def get_step(step_id: int):
    with models.get_db() as conn:
        return conn.execute(
            "SELECT * FROM mad_scientist_steps WHERE id = ?",
            (step_id,),
        ).fetchone()


def get_attempt_by_run(run_id: int):
    with models.get_db() as conn:
        return conn.execute(
            "SELECT * FROM mad_scientist_attempts WHERE run_id = ?",
            (run_id,),
        ).fetchone()


def list_steps(graph_mission_id: int):
    with models.get_db() as conn:
        return conn.execute(
            """
            SELECT * FROM mad_scientist_steps
            WHERE graph_mission_id = ?
            ORDER BY position, id
            """,
            (graph_mission_id,),
        ).fetchall()


def open_graph(
    mission_id: int,
    workspace_id: int,
    goal: str,
    scout_name: str,
    scout_task: str,
    scout_provider: str | None,
    spend_cap_usd: float | None = None,
    max_parallel_steps: int | None = None,
    max_attempts: int | None = None,
    token_cap: int | None = None,
) -> tuple[int, int]:
    """Create the graph mission and its scout step. Returns (graph_id, scout_step_id)."""
    if spend_cap_usd is None:
        spend_cap_usd = config.DEFAULT_SPEND_CAP_USD
    if spend_cap_usd < 0:
        raise ValueError("Spend cap cannot be negative")
    if token_cap is None:
        token_cap = config.DEFAULT_TOKEN_CAP
    if token_cap < 0:
        raise ValueError("Token cap cannot be negative")
    if max_attempts is None:
        max_attempts = config.DEFAULT_MAX_FIX_LOOPS
    if max_attempts < 1:
        raise ValueError("Max attempts per step must be at least 1")
    if max_parallel_steps is not None and max_parallel_steps < 1:
        raise ValueError("Max parallel steps must be at least 1")
    now = _now()
    with models.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO mad_scientist_missions
                (mission_id, workspace_id, status, goal, spend_cap_usd, spend_usd,
                 max_parallel_steps, token_cap, tokens_used, max_attempts,
                 created_at, updated_at)
            VALUES (?, ?, 'running', ?, ?, 0, ?, ?, 0, ?, ?, ?)
            """,
            (
                mission_id,
                workspace_id,
                goal,
                spend_cap_usd,
                max_parallel_steps,
                token_cap,
                max_attempts,
                now,
                now,
            ),
        )
        graph_id = cur.lastrowid
        cur = conn.execute(
            """
            INSERT INTO mad_scientist_steps
                (graph_mission_id, name, role, provider, task, write_allowed,
                 success_criteria, status, position, created_at, updated_at)
            VALUES (?, ?, 'scout', ?, ?, 0, ?, 'planned', 0, ?, ?)
            """,
            (
                graph_id,
                scout_name,
                scout_provider,
                scout_task,
                json.dumps(["A parseable execution plan grounded in this repository"]),
                now,
                now,
            ),
        )
        return graph_id, cur.lastrowid


def record_attempt(step_id: int, run_id: int, kind: str, status: str = "pending") -> int:
    now = _now()
    step_status = _RUN_TO_STEP.get(status, "queued")
    with models.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO mad_scientist_attempts
                (step_id, run_id, attempt_kind, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (step_id, run_id, kind, status, now, now),
        )
        conn.execute(
            """
            UPDATE mad_scientist_steps
            SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (step_status, now, step_id),
        )
        return cur.lastrowid


def assert_acyclic(steps: list[dict]) -> None:
    """Reject a plan whose dependency edges contain a cycle.

    Missing dependency names are not edges. A cycle among named steps
    fails closed before those rows are inserted.
    """
    deps = {step["name"]: list(step.get("depends_on") or []) for step in steps}
    visiting = set()
    visited = set()

    def visit(name: str) -> None:
        if name in visited or name not in deps:
            return
        if name in visiting:
            raise ValueError(f"Cyclic dependency involving '{name}'")
        visiting.add(name)
        for dep in deps.get(name, []):
            visit(dep)
        visiting.remove(name)
        visited.add(name)

    for step_name in deps:
        visit(step_name)


def materialize_plan(graph_mission_id: int, plan: dict) -> None:
    """Insert planned steps and dependency edges. Safe to call again."""
    assert_acyclic(plan.get("steps") or [])
    existing = list_steps(graph_mission_id)
    if any(step["position"] > 0 for step in existing):
        return

    known = {step["name"]: step["id"] for step in existing}
    items = []
    for index, item in enumerate(plan.get("steps") or [], start=1):
        name = item.get("name")
        if not name or name in known:
            continue
        items.append((index, item))

    now = _now()
    with models.get_db() as conn:
        for index, item in items:
            criteria = item.get("success_criteria") or []
            cur = conn.execute(
                """
                INSERT INTO mad_scientist_steps
                    (graph_mission_id, name, role, provider, task, write_allowed,
                     success_criteria, status, position, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)
                """,
                (
                    graph_mission_id,
                    item["name"],
                    item.get("role") or "builder",
                    item.get("provider"),
                    item.get("task") or "",
                    1 if item.get("write_allowed") else 0,
                    json.dumps(criteria),
                    index,
                    now,
                    now,
                ),
            )
            known[item["name"]] = cur.lastrowid

        for _index, item in items:
            step_id = known[item["name"]]
            unresolved = []
            for dep_name in item.get("depends_on") or []:
                dep_id = known.get(dep_name)
                if dep_id is None:
                    unresolved.append(dep_name)
                    continue
                if dep_id == step_id:
                    unresolved.append(dep_name)
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO mad_scientist_step_dependencies
                        (step_id, depends_on_step_id)
                    VALUES (?, ?)
                    """,
                    (step_id, dep_id),
                )
            if unresolved:
                conn.execute(
                    """
                    UPDATE mad_scientist_steps
                    SET unresolved_dependencies = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (json.dumps(unresolved), now, step_id),
                )

        summary = (plan.get("summary") or "").strip() or None
        conn.execute(
            """
            UPDATE mad_scientist_missions
            SET summary = COALESCE(?, summary), updated_at = ?
            WHERE id = ?
            """,
            (summary, now, graph_mission_id),
        )


def _unresolved(step) -> list:
    raw = step["unresolved_dependencies"]
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return [raw]
    return list(parsed) if isinstance(parsed, list) else []


def dependency_ids(step_id: int) -> list[int]:
    with models.get_db() as conn:
        rows = conn.execute(
            """
            SELECT depends_on_step_id FROM mad_scientist_step_dependencies
            WHERE step_id = ?
            ORDER BY depends_on_step_id
            """,
            (step_id,),
        ).fetchall()
    return [row["depends_on_step_id"] for row in rows]


def dependency_names(step_id: int) -> list[str]:
    with models.get_db() as conn:
        rows = conn.execute(
            """
            SELECT s.name
            FROM mad_scientist_step_dependencies d
            JOIN mad_scientist_steps s ON s.id = d.depends_on_step_id
            WHERE d.step_id = ?
            ORDER BY s.position, s.id
            """,
            (step_id,),
        ).fetchall()
    return [row["name"] for row in rows]


def ready_steps(graph_mission_id: int) -> list:
    steps = list_steps(graph_mission_id)
    done = {step["id"] for step in steps if step["status"] == "done"}
    ready = []
    for step in steps:
        if step["status"] != "planned":
            continue
        if _unresolved(step):
            continue
        deps = dependency_ids(step["id"])
        if all(dep_id in done for dep_id in deps):
            ready.append(step)
    return ready


def list_attempts(step_id: int):
    with models.get_db() as conn:
        return conn.execute(
            """
            SELECT * FROM mad_scientist_attempts
            WHERE step_id = ?
            ORDER BY id
            """,
            (step_id,),
        ).fetchall()


def count_attempts(step_id: int) -> int:
    return len(list_attempts(step_id))


def attempt_cap(graph_row) -> int:
    raw = None if graph_row is None else graph_row["max_attempts"]
    if raw is None:
        raw = config.DEFAULT_MAX_FIX_LOOPS
    return int(raw)


def count_inflight(graph_mission_id: int) -> int:
    with models.get_db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM mad_scientist_attempts a
            JOIN mad_scientist_steps s ON s.id = a.step_id
            WHERE s.graph_mission_id = ?
              AND a.status IN ('pending', 'running', 'awaiting_approval')
            """,
            (graph_mission_id,),
        ).fetchone()
    return row["n"]


def all_steps_done(graph_mission_id: int) -> bool:
    steps = list_steps(graph_mission_id)
    return bool(steps) and all(step["status"] == "done" for step in steps)


def _latest_completed_attempt(step_id: int):
    with models.get_db() as conn:
        return conn.execute(
            """
            SELECT * FROM mad_scientist_attempts
            WHERE step_id = ? AND status = 'done' AND attempt_kind != 'fixer'
            ORDER BY run_id DESC LIMIT 1
            """,
            (step_id,),
        ).fetchone()


def newest_dependency_run_id(step_id: int) -> int | None:
    newest = None
    for dep_id in dependency_ids(step_id):
        attempt = _latest_completed_attempt(dep_id)
        if attempt is None:
            continue
        if newest is None or attempt["run_id"] > newest:
            newest = attempt["run_id"]
    return newest


def dependency_context(step_id: int) -> str:
    """Localized output from every direct dependency, not only the newest parent."""
    sections = []
    dep_ids = dependency_ids(step_id)
    if not dep_ids:
        return ""
    budget = max(1000, config.CHAIN_RUN_MAX_BYTES // len(dep_ids))
    for dep_id in dep_ids:
        step = get_step(dep_id)
        attempt = _latest_completed_attempt(dep_id)
        if step is None or attempt is None:
            continue
        run = models.get_run(attempt["run_id"])
        output = ""
        if run is not None:
            output = run_chain.extract_run_output(run, max_bytes=budget) or ""
        if not output:
            output = "(no extracted output)"
        sections.append(
            f"Direct dependency: {step['name']} "
            f"(run #{attempt['run_id']}, {attempt['status']}, {attempt['attempt_kind']})\n"
            f"{output}"
        )
    return "\n\n".join(sections)


def sync_run_status(run_id: int) -> None:
    """Copy a run's status onto its attempt and the logical step.

    awaiting_approval is a real step state. Leaving the step queued while the
    run waits for approval is the desync this function closes.
    """
    attempt = get_attempt_by_run(run_id)
    if attempt is None:
        return
    run = models.get_run(run_id)
    if run is None:
        return
    status = run["status"]
    now = _now()
    with models.get_db() as conn:
        latest = conn.execute(
            """
            SELECT id FROM mad_scientist_attempts
            WHERE step_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (attempt["step_id"],),
        ).fetchone()
        conn.execute(
            """
            UPDATE mad_scientist_attempts
            SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, now, attempt["id"]),
        )
        if latest is None or latest["id"] != attempt["id"]:
            return
        step = conn.execute(
            "SELECT status FROM mad_scientist_steps WHERE id = ?",
            (attempt["step_id"],),
        ).fetchone()
        if step is not None and step["status"] == BLOCKED_HUMAN:
            return
        if status in _RUN_TO_STEP:
            step_status = _RUN_TO_STEP[status]
        elif status == "done" and attempt["attempt_kind"] == "fixer":
            step_status = "fixer_succeeded"
        elif status == "done":
            step_status = "done"
        elif status in ("failed", "rejected", "cancelled"):
            step_status = status
        else:
            return
        conn.execute(
            """
            UPDATE mad_scientist_steps
            SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (step_status, now, attempt["step_id"]),
        )


def charge_run_spend(run_id: int) -> None:
    """Add provider-reported cost_usd once. Missing cost records as zero."""
    attempt = get_attempt_by_run(run_id)
    if attempt is None or attempt["cost_usd"] is not None:
        return
    run = models.get_run(run_id)
    if run is None or run["status"] not in TERMINAL_RUN_STATUSES:
        return
    payload = models.get_run_payload(run_id) or {}
    raw = payload.get("cost_usd")
    amount = 0.0
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        amount = float(raw)
    now = _now()
    with models.get_db() as conn:
        conn.execute(
            """
            UPDATE mad_scientist_attempts
            SET cost_usd = ?, updated_at = ?
            WHERE id = ? AND cost_usd IS NULL
            """,
            (amount, now, attempt["id"]),
        )
        if amount:
            conn.execute(
                """
                UPDATE mad_scientist_missions
                SET spend_usd = spend_usd + ?, updated_at = ?
                WHERE id = (
                    SELECT graph_mission_id FROM mad_scientist_steps WHERE id = ?
                )
                """,
                (amount, now, attempt["step_id"]),
            )


def spend_block_reason(graph_row) -> str | None:
    if graph_row is None or graph_row["spend_cap_usd"] is None:
        return None
    spent = float(graph_row["spend_usd"] or 0)
    cap = float(graph_row["spend_cap_usd"])
    if spent + 1e-9 < cap:
        return None
    return f"Spend cap reached (${spent:.4f} of ${cap:.4f})"


def _tokens_were_reported(graph_mission_id: int) -> bool:
    with models.get_db() as conn:
        row = conn.execute(
            """
            SELECT 1 AS ok
            FROM mad_scientist_attempts a
            JOIN mad_scientist_steps s ON s.id = a.step_id
            WHERE s.graph_mission_id = ? AND a.tokens IS NOT NULL
            LIMIT 1
            """,
            (graph_mission_id,),
        ).fetchone()
    return row is not None


def token_block_reason(graph_row) -> str | None:
    """Stop only when a provider has reported tokens and the cap is crossed.

    A missing token count is not zero. Providers that never report usage
    leave tokens NULL, and this breaker stays idle.
    """
    if graph_row is None or graph_row["token_cap"] is None:
        return None
    if not _tokens_were_reported(graph_row["id"]):
        return None
    used = int(graph_row["tokens_used"] or 0)
    cap = int(graph_row["token_cap"])
    if used < cap:
        return None
    return f"Token cap reached ({used} of {cap})"


def mission_stop_reason(graph_row) -> str | None:
    if graph_row is None:
        return None
    return spend_block_reason(graph_row) or token_block_reason(graph_row)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_attempt_usage(run_id: int) -> None:
    """Store provider tokens and patch/error hashes once per attempt."""
    attempt = get_attempt_by_run(run_id)
    if attempt is None:
        return
    run = models.get_run(run_id)
    if run is None or run["status"] not in TERMINAL_RUN_STATUSES:
        return
    payload = models.get_run_payload(run_id) or {}
    raw_tokens = payload.get("total_tokens")
    tokens = None
    if isinstance(raw_tokens, int) and not isinstance(raw_tokens, bool) and raw_tokens >= 0:
        tokens = raw_tokens
    patch_hash = attempt["patch_hash"]
    error_hash = attempt["error_hash"]
    patches = payload.get("patches") or []
    if patch_hash is None and isinstance(patches, list) and patches:
        patch_hash = _sha256(json.dumps(patches, sort_keys=True, default=str))
    if error_hash is None and run["status"] in ("failed", "rejected", "cancelled"):
        text = (run["error"] or payload.get("summary") or run["status"] or "").strip()
        if text:
            error_hash = _sha256(text)
    now = _now()
    with models.get_db() as conn:
        conn.execute(
            """
            UPDATE mad_scientist_attempts
            SET tokens = COALESCE(tokens, ?),
                patch_hash = COALESCE(patch_hash, ?),
                error_hash = COALESCE(error_hash, ?),
                updated_at = ?
            WHERE id = ?
            """,
            (tokens, patch_hash, error_hash, now, attempt["id"]),
        )
        if tokens and attempt["tokens"] is None:
            conn.execute(
                """
                UPDATE mad_scientist_missions
                SET tokens_used = tokens_used + ?, updated_at = ?
                WHERE id = (
                    SELECT graph_mission_id FROM mad_scientist_steps WHERE id = ?
                )
                """,
                (tokens, now, attempt["step_id"]),
            )


def repeated_mistake_reason(step_id: int, attempt_id: int) -> str | None:
    with models.get_db() as conn:
        current = conn.execute(
            "SELECT patch_hash, error_hash FROM mad_scientist_attempts WHERE id = ?",
            (attempt_id,),
        ).fetchone()
        if current is None:
            return None
        priors = conn.execute(
            """
            SELECT patch_hash, error_hash FROM mad_scientist_attempts
            WHERE step_id = ? AND id != ?
            """,
            (step_id, attempt_id),
        ).fetchall()
    step = get_step(step_id)
    name = step["name"] if step is not None else str(step_id)
    if current["patch_hash"] and any(row["patch_hash"] == current["patch_hash"] for row in priors):
        return (
            f"Repeated patch hash on step '{name}'. "
            "Retries halted so the same patch is not applied again."
        )
    if current["error_hash"] and any(row["error_hash"] == current["error_hash"] for row in priors):
        return (
            f"Repeated error hash on step '{name}'. "
            "Retries halted so the same failure is not repeated."
        )
    return None


def block_mission(mission_id: int, reason: str) -> None:
    now = _now()
    with models.get_db() as conn:
        conn.execute(
            """
            UPDATE mad_scientist_missions
            SET status = ?, blocked_reason = ?, updated_at = ?
            WHERE mission_id = ?
            """,
            (BLOCKED_HUMAN, reason, now, mission_id),
        )


def block_step(step_id: int, reason: str) -> None:
    now = _now()
    with models.get_db() as conn:
        conn.execute(
            """
            UPDATE mad_scientist_steps
            SET status = ?, blocked_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (BLOCKED_HUMAN, reason, now, step_id),
        )


def note_stop_reason(mission_id: int, reason: str, step_id: int | None = None) -> None:
    """Store why automation stopped without changing a non-block status."""
    now = _now()
    with models.get_db() as conn:
        conn.execute(
            """
            UPDATE mad_scientist_missions
            SET blocked_reason = ?, updated_at = ?
            WHERE mission_id = ?
            """,
            (reason, now, mission_id),
        )
        if step_id is not None:
            conn.execute(
                """
                UPDATE mad_scientist_steps
                SET blocked_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (reason, now, step_id),
            )


def set_status(
    mission_id: int,
    status: str,
    summary: str | None = None,
    blocked_reason: str | None = None,
) -> None:
    now = _now()
    assignments = ["status = ?", "updated_at = ?"]
    values: list = [status, now]
    if summary is not None:
        assignments.append("summary = ?")
        values.append(summary)
    if blocked_reason is not None or status in ("running", "done"):
        assignments.append("blocked_reason = ?")
        values.append(blocked_reason)
    values.append(mission_id)
    with models.get_db() as conn:
        conn.execute(
            f"""
            UPDATE mad_scientist_missions
            SET {", ".join(assignments)}
            WHERE mission_id = ?
            """,
            values,
        )


def payload_has_verification(payload: dict | None, role: str | None = None) -> bool:
    """True when a tester or reviewer payload ran a real command that exited 0."""
    if role not in VERIFIER_ROLES:
        return True
    if not payload:
        return False
    post = payload.get("post_actions") or {}
    command = str(post.get("test_command") or "").strip()
    has_test = bool(post.get("run_tests")) and bool(command)
    has_diff = bool(post.get("run_git_diff"))
    exit_code = payload.get("tool_exit_code")
    return bool(has_test or has_diff) and exit_code == 0


def verification_failure_reason(run, step) -> str | None:
    """A model summary does not complete a tester or reviewer step.

    Subprocess providers that store no local_agent payload are judged by
    the process exit code already copied onto the run status. A payload
    without a test, build, or diff command and a zero tool exit code fails.
    """
    if run is None or step is None or step["role"] not in VERIFIER_ROLES:
        return None
    if run["status"] != "done":
        return None
    payload = models.get_run_payload(run["id"])
    if payload is None:
        return None
    if payload_has_verification(payload, step["role"]):
        return None
    return (
        f"Step '{step['name']}' ({step['role']}) cannot complete on a model summary. "
        "A real test, build, or diff command must run and exit 0."
    )


def record_evaluation_if_applicable(run_id: int) -> None:
    attempt = get_attempt_by_run(run_id)
    if attempt is None:
        return
    run = models.get_run(run_id)
    step = get_step(attempt["step_id"])
    if run is None or step is None:
        return
    if step["role"] not in EVALUATOR_ROLES:
        return
    if run["status"] not in TERMINAL_RUN_STATUSES:
        return
    graph_row = None
    with models.get_db() as conn:
        graph_row = conn.execute(
            "SELECT * FROM mad_scientist_missions WHERE id = ?",
            (step["graph_mission_id"],),
        ).fetchone()
    if graph_row is None:
        return
    payload = models.get_run_payload(run_id) or {}
    critique = (payload.get("summary") or run["error"] or f"status {run['status']}").strip()
    verified = run["status"] == "done" and payload_has_verification(payload, step["role"])
    if run["status"] == "done" and not verified:
        critique = verification_failure_reason(run, step) or critique
    subject = run["parent_run_id"] or newest_dependency_run_id(step["id"])
    models.insert_evaluation(
        graph_row["workspace_id"],
        "pass" if verified else "fail",
        mission_id=graph_row["mission_id"],
        subject_run_id=subject,
        evaluator_run_id=run_id,
        step_id=step["id"],
        evaluator_provider=run["provider"],
        critique=critique,
    )


def step_public_dict(step) -> dict:
    criteria = []
    if step["success_criteria"]:
        try:
            criteria = json.loads(step["success_criteria"])
        except json.JSONDecodeError:
            criteria = []
    return {
        "name": step["name"],
        "role": step["role"],
        "provider": step["provider"],
        "task": step["task"],
        "write_allowed": bool(step["write_allowed"]),
        "success_criteria": criteria,
        "depends_on": dependency_names(step["id"]),
    }


def _display_status(step, done_ids: set[int]) -> str:
    status = step["status"]
    if status == BLOCKED_HUMAN:
        return "blocked"
    if status == "planned":
        if _unresolved(step):
            return "planned"
        deps = dependency_ids(step["id"])
        if all(dep_id in done_ids for dep_id in deps):
            return "ready"
    return status


def _attempt_public(attempt) -> dict:
    run = models.get_run(attempt["run_id"])
    approval = run_approval.approval_state(run) if run is not None else {
        "run_id": attempt["run_id"],
        "approval_available": False,
        "response_type": None,
        "patch_count": 0,
    }
    status = run["status"] if run is not None else attempt["status"]
    error = ""
    if run is not None:
        error = str(run["error"] or "").strip()
    return {
        "id": attempt["id"],
        "run_id": attempt["run_id"],
        "attempt_kind": attempt["attempt_kind"],
        "status": status,
        "error": error,
        "approval": approval,
        "approval_available": bool(approval.get("approval_available")),
    }


def mission_view(mission_id: int) -> dict | None:
    graph_row = get_by_mission_id(mission_id)
    if graph_row is None:
        return None
    evaluations = {
        row["step_id"]: row
        for row in models.list_evaluations_for_mission(mission_id)
    }
    raw_steps = list_steps(graph_row["id"])
    done_ids = {step["id"] for step in raw_steps if step["status"] == "done"}
    steps = []
    scout_failed = False
    non_scout_started = False
    waiting_approvals = []
    for step in raw_steps:
        attempts = [_attempt_public(item) for item in list_attempts(step["id"])]
        actionable = next((item for item in reversed(attempts) if item["approval_available"]), None)
        awaiting = next(
            (
                item for item in reversed(attempts)
                if item["status"] == "awaiting_approval"
            ),
            None,
        )
        evaluation = evaluations.get(step["id"])
        display_status = _display_status(step, done_ids)
        failure_reason = ""
        for item in reversed(attempts):
            if item.get("status") in ("failed", "rejected") and item.get("error"):
                failure_reason = item["error"]
                break
        if actionable is not None:
            waiting_approvals.append(actionable)
        steps.append({
            "id": step["id"],
            "name": step["name"],
            "role": step["role"],
            "status": step["status"],
            "display_status": display_status,
            "depends_on": dependency_names(step["id"]),
            "unresolved": _unresolved(step),
            "blocked_reason": step["blocked_reason"],
            "failure_reason": failure_reason,
            "attempt_count": len(attempts),
            "attempts": attempts,
            "awaiting_run_id": actionable["run_id"] if actionable else None,
            "approval_available": actionable is not None,
            "approval_pending_without_payload": (
                awaiting is not None and actionable is None
            ),
            "evaluation": (
                {"verdict": evaluation["verdict"], "critique": evaluation["critique"]}
                if evaluation is not None else None
            ),
        })
        if step["role"] == "scout" and step["status"] in ("failed", "rejected", "cancelled"):
            scout_failed = True
        if step["role"] != "scout" and step["status"] != "planned":
            non_scout_started = True
    mission = models.get_mission(mission_id)
    can_retry_scout = bool(
        mission
        and mission["status"] in ("failed", "aborted")
        and scout_failed
        and not non_scout_started
        and count_inflight(graph_row["id"]) == 0
    )
    return {
        "status": graph_row["status"],
        "summary": graph_row["summary"],
        "blocked_reason": graph_row["blocked_reason"],
        "spend_usd": graph_row["spend_usd"],
        "spend_cap_usd": graph_row["spend_cap_usd"],
        "tokens_used": graph_row["tokens_used"],
        "token_cap": graph_row["token_cap"],
        "tokens_reported": _tokens_were_reported(graph_row["id"]),
        "max_attempts": graph_row["max_attempts"],
        "waiting_approvals": waiting_approvals,
        "steps": steps,
        "can_retry_scout": can_retry_scout,
    }

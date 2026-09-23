"""Durable Mad Scientist graph.

The mission row and parent_run_id stay compatibility metadata. Scheduling,
dependency fan-in, fixer/retry attempts, and step status live in the
mad_scientist_* tables.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import config
import models
import run_chain

INFLIGHT_STEP_STATUSES = {"queued", "running", "awaiting_approval"}
TERMINAL_RUN_STATUSES = {"done", "failed", "rejected", "cancelled"}
EVALUATOR_ROLES = {"reviewer", "tester"}
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
) -> tuple[int, int]:
    """Create the graph mission and its scout step. Returns (graph_id, scout_step_id)."""
    if spend_cap_usd is not None and spend_cap_usd < 0:
        raise ValueError("Spend cap cannot be negative")
    now = _now()
    with models.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO mad_scientist_missions
                (mission_id, workspace_id, status, goal, spend_cap_usd, spend_usd, created_at, updated_at)
            VALUES (?, ?, 'running', ?, ?, 0, ?, ?)
            """,
            (mission_id, workspace_id, goal, spend_cap_usd, now, now),
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


def materialize_plan(graph_mission_id: int, plan: dict) -> None:
    """Insert planned steps and dependency edges. Safe to call again."""
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


def set_status(mission_id: int, status: str, summary: str | None = None) -> None:
    now = _now()
    with models.get_db() as conn:
        if summary is None:
            conn.execute(
                """
                UPDATE mad_scientist_missions
                SET status = ?, updated_at = ?
                WHERE mission_id = ?
                """,
                (status, now, mission_id),
            )
        else:
            conn.execute(
                """
                UPDATE mad_scientist_missions
                SET status = ?, summary = ?, updated_at = ?
                WHERE mission_id = ?
                """,
                (status, summary, now, mission_id),
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
    subject = run["parent_run_id"] or newest_dependency_run_id(step["id"])
    models.insert_evaluation(
        graph_row["workspace_id"],
        "pass" if run["status"] == "done" else "fail",
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


def mission_view(mission_id: int) -> dict | None:
    graph_row = get_by_mission_id(mission_id)
    if graph_row is None:
        return None
    evaluations = {
        row["step_id"]: row
        for row in models.list_evaluations_for_mission(mission_id)
    }
    steps = []
    scout_failed = False
    non_scout_started = False
    for step in list_steps(graph_row["id"]):
        awaiting_run_id = None
        if step["status"] == "awaiting_approval":
            with models.get_db() as conn:
                row = conn.execute(
                    """
                    SELECT run_id FROM mad_scientist_attempts
                    WHERE step_id = ? AND status = 'awaiting_approval'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (step["id"],),
                ).fetchone()
            if row is not None:
                awaiting_run_id = row["run_id"]
        evaluation = evaluations.get(step["id"])
        steps.append({
            "id": step["id"],
            "name": step["name"],
            "role": step["role"],
            "status": step["status"],
            "depends_on": dependency_names(step["id"]),
            "unresolved": _unresolved(step),
            "awaiting_run_id": awaiting_run_id,
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
        "spend_usd": graph_row["spend_usd"],
        "spend_cap_usd": graph_row["spend_cap_usd"],
        "steps": steps,
        "can_retry_scout": can_retry_scout,
    }

#!/usr/bin/env python3
"""SQLite-only validation for the Mad Scientist graph.

Covers storage, dependency fan-in, fixer/retry attempts on the same step,
spend cap, approval sync, restart sweep, and workspace ownership checks.
Does not call a model or bind a port.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="tank-graph-")
REPO = Path(TMP) / "repo"
REPO.mkdir()
(REPO / "README.md").write_text("tank graph validation repo\n", encoding="utf-8")
WORKSPACES = Path(TMP) / "workspaces.yaml"
WORKSPACES.write_text("workspaces: []\n", encoding="utf-8")

os.environ["TANK_DATA_DIR"] = TMP
os.environ["TANK_WORKSPACES_FILE"] = str(WORKSPACES)
os.environ["TANK_PROVIDERS_FILE"] = str(ROOT / "providers.yaml")
os.environ["TANK_MISSION_TEMPLATES_FILE"] = str(ROOT / "mission_templates.yaml")
os.environ["TANK_MAX_PARALLEL_RUNS"] = "0"
os.environ["TANK_MAX_PARALLEL_RUNS_PER_MISSION"] = "0"

import yaml  # noqa: E402

import config  # noqa: E402
import mad_scientist  # noqa: E402
import mad_scientist_graph as graph  # noqa: E402
import models  # noqa: E402
import session_manager  # noqa: E402

SCOUT = mad_scientist.SCOUT_STAGE
FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"ok: {label}")
        return
    FAILURES.append(label)
    print(f"FAIL: {label}")


def plan_payload(plan: dict, summary: str = "scout summary", cost: float | None = None) -> str:
    payload = {
        "response_type": "plan",
        "summary": summary,
        "plan": json.dumps(plan),
    }
    if cost is not None:
        payload["cost_usd"] = cost
    return json.dumps(payload)


def run_payload(summary: str, cost: float | None = None) -> str:
    payload = {"response_type": "plan", "summary": summary, "plan": summary}
    if cost is not None:
        payload["cost_usd"] = cost
    return json.dumps(payload)


def finish(
    run_id: int,
    status: str = "done",
    summary: str = "",
    cost: float | None = None,
    payload: dict | None = None,
    error: str | None = None,
) -> None:
    if payload is None:
        body = run_payload(summary, cost)
    else:
        body_dict = dict(payload)
        if cost is not None and "cost_usd" not in body_dict:
            body_dict["cost_usd"] = cost
        body = json.dumps(body_dict)
    if error is None and status != "done":
        error = status
    models.update_run(
        run_id,
        status=status,
        agent_payload=body,
        finished_at=models._now(),
        error=error,
    )
    mad_scientist.advance_mission(run_id)


def verified_tester_payload(summary: str, command: str = "pytest -q") -> dict:
    return {
        "response_type": "plan",
        "summary": summary,
        "plan": summary,
        "patches": [],
        "post_actions": {
            "run_tests": True,
            "test_command": command,
            "run_git_diff": False,
        },
        "tool_exit_code": 0,
    }


def mission_runs(mission_id: int):
    return models.list_mission_runs(mission_id)


def attempts_for(step_id: int):
    with models.get_db() as conn:
        return conn.execute(
            "SELECT * FROM mad_scientist_attempts WHERE step_id = ? ORDER BY id",
            (step_id,),
        ).fetchall()


def step_named(graph_id: int, name: str):
    for step in graph.list_steps(graph_id):
        if step["name"] == name:
            return step
    return None


DIAMOND = {
    "summary": "diamond plan",
    "project_profile": {
        "important_paths": ["README.md"],
        "test_commands": [],
        "implementation_risks": [],
    },
    "steps": [
        {
            "name": "Alpha",
            "role": "builder",
            "provider": "local_qwen",
            "task": "change alpha",
            "depends_on": [],
            "write_allowed": True,
            "success_criteria": ["alpha done"],
        },
        {
            "name": "Beta",
            "role": "builder",
            "provider": "local_qwen",
            "task": "change beta",
            "depends_on": [],
            "write_allowed": True,
            "success_criteria": ["beta done"],
        },
        {
            "name": "Join",
            "role": "integrator",
            "provider": "local_qwen",
            "task": "combine alpha and beta",
            "depends_on": ["Alpha", "Beta"],
            "write_allowed": True,
            "success_criteria": ["joined"],
        },
        {
            "name": "Check",
            "role": "tester",
            "provider": "local_qwen",
            "task": "verify the join",
            "depends_on": ["Join"],
            "write_allowed": False,
            "success_criteria": ["verified"],
        },
    ],
}


def main() -> int:
    print(f"PyYAML {yaml.__version__}")
    check(yaml.__version__ == "6.0.3", "pinned PyYAML 6.0.3 is the runtime")
    loaded = yaml.safe_load((ROOT / "providers.yaml").read_text(encoding="utf-8"))
    check(isinstance(loaded, dict) and "providers" in loaded, "providers.yaml parses")
    loaded_ws = yaml.safe_load((ROOT / "workspaces.yaml").read_text(encoding="utf-8"))
    check(isinstance(loaded_ws, dict), "workspaces.yaml parses")

    models.init_db()
    slug_a = models.create_workspace("Graph A", str(REPO), slug="graph-a", default_provider="local_qwen")
    slug_b = models.create_workspace("Graph B", str(REPO), slug="graph-b", default_provider="local_qwen")
    ws_a = models.get_workspace(slug_a)
    ws_b = models.get_workspace(slug_b)

    with models.get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_roles (workspace_id, slug, name, system_prompt, created_at)
            VALUES (?, 'reviewer', 'Reviewer', 'review', ?)
            """,
            (ws_b["id"], models._now()),
        )
        foreign_role_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    try:
        models.create_run(ws_a["id"], foreign_role_id, "should not attach")
        check(False, "role from another workspace is rejected")
    except ValueError:
        check(True, "role from another workspace is rejected")

    try:
        models.create_schedule(ws_a["id"], foreign_role_id, "nope", "* * * * *")
        check(False, "schedule role from another workspace is rejected")
    except ValueError:
        check(True, "schedule role from another workspace is rejected")

    mission_id = mad_scientist.start_mission(
        ws_a["id"],
        "Build the diamond",
        provider="local_qwen",
        max_steps=6,
        max_fix_loops=3,
    )
    mission = models.get_mission(mission_id)
    graph_row = graph.get_by_mission_id(mission_id)
    check(graph_row is not None, "graph mission row exists")
    check(float(graph_row["spend_cap_usd"]) == 5.0, "default spend cap is $5")
    check(int(graph_row["token_cap"]) == 500_000, "default token cap is 500000")
    check(int(graph_row["max_attempts"]) == 3, "attempt cap defaults to 3 per step")
    parent = models.get_run(mission["parent_run_id"])
    check(parent is not None and parent["provider"] == "mad_scientist", "compatibility parent run exists")
    scout_runs = [run for run in mission_runs(mission_id) if run["stage_name"] == SCOUT]
    check(len(scout_runs) == 1, "one scout attempt is queued")
    scout = scout_runs[0]
    scout_step = step_named(graph_row["id"], SCOUT)
    check(scout_step is not None and scout_step["status"] == "queued", "scout step starts queued")

    models.update_run(
        scout["id"],
        status="awaiting_approval",
        agent_payload=plan_payload(DIAMOND),
    )
    mad_scientist.advance_mission(scout["id"])
    scout_step = step_named(graph_row["id"], SCOUT)
    check(
        scout_step["status"] == "awaiting_approval",
        "awaiting_approval run updates the logical step",
    )
    check(models.get_mission(mission_id)["status"] == "running", "approval pause keeps the mission running")
    check(len(attempts_for(scout_step["id"])) == 1, "approval pause does not create another attempt")

    models.update_run(
        scout["id"],
        status="done",
        agent_payload=plan_payload(DIAMOND, summary="scout summary"),
        finished_at=models._now(),
    )
    mad_scientist.advance_mission(scout["id"])
    alpha = step_named(graph_row["id"], "Alpha")
    beta = step_named(graph_row["id"], "Beta")
    join = step_named(graph_row["id"], "Join")
    check(alpha and beta and join, "plan steps are stored")
    alpha_runs = [run for run in mission_runs(mission_id) if run["stage_name"] == "Alpha"]
    beta_runs = [run for run in mission_runs(mission_id) if run["stage_name"] == "Beta"]
    join_runs = [run for run in mission_runs(mission_id) if run["stage_name"] == "Join"]
    check(len(alpha_runs) == 1 and len(beta_runs) == 1, "ready roots are both queued")
    check(join_runs == [], "join waits for both dependencies")

    finish(alpha_runs[0]["id"], summary="ALPHA_TOKEN")
    join_runs = [run for run in mission_runs(mission_id) if run["stage_name"] == "Join"]
    check(join_runs == [], "one finished dependency does not release join")
    finish(beta_runs[0]["id"], summary="BETA_TOKEN")
    join_runs = [run for run in mission_runs(mission_id) if run["stage_name"] == "Join"]
    check(len(join_runs) == 1, "join queues after both dependencies finish")
    join_run = models.get_run(join_runs[0]["id"])
    check("ALPHA_TOKEN" in join_run["task"] and "BETA_TOKEN" in join_run["task"], "fan-in context includes every dependency")
    check(join_run["parent_run_id"] == beta_runs[0]["id"], "parent_run_id is the newest dependency run")

    finish(join_run["id"], status="failed", summary="join failed")
    join = step_named(graph_row["id"], "Join")
    join_attempts = attempts_for(join["id"])
    check(
        len(join_attempts) == 2 and join_attempts[-1]["attempt_kind"] == "fixer",
        "failure queues a fixer attempt on the same step",
    )
    fixer_run = models.get_run(join_attempts[-1]["run_id"])
    check(fixer_run["stage_name"] == "Join", "fixer keeps the logical step name")
    check(models.get_mission(mission_id)["fix_loop_count"] == 1, "fix-loop counter increments")

    finish(fixer_run["id"], summary="fixer repaired join")
    join_attempts = attempts_for(join["id"])
    check(
        len(join_attempts) == 3 and join_attempts[-1]["attempt_kind"] == "retry",
        "fixer success queues a retry on the same step",
    )
    retry_run = models.get_run(join_attempts[-1]["run_id"])
    finish(retry_run["id"], summary="join retried cleanly")
    join = step_named(graph_row["id"], "Join")
    check(join["status"] == "done", "retry success completes the logical step")
    join_view = next(
        step for step in graph.mission_view(mission_id)["steps"] if step["name"] == "Join"
    )
    check(
        [item["attempt_kind"] for item in join_view["attempts"]] == ["execute", "fixer", "retry"],
        "attempt stack lists execute, fixer, and retry",
    )
    check(join_view["attempt_count"] == 3, "attempt stack count is 3")

    check_runs = [run for run in mission_runs(mission_id) if run["stage_name"] == "Check"]
    check(len(check_runs) == 1, "tester queues after join completes")
    finish(
        check_runs[0]["id"],
        summary="checks passed",
        payload=verified_tester_payload("checks passed"),
    )
    evaluations = models.list_evaluations_for_mission(mission_id)
    check(
        len(evaluations) == 1 and evaluations[0]["verdict"] == "pass",
        "tester completion records an evaluation",
    )
    check(
        evaluations[0]["subject_run_id"] == retry_run["id"],
        "evaluation subject is the direct dependency run",
    )
    check(models.get_mission(mission_id)["status"] == "done", "mission completes when every step is done")

    blocked_id = mad_scientist.start_mission(
        ws_a["id"], "Blocked plan", provider="local_qwen", max_steps=4, max_fix_loops=1
    )
    blocked_scout = [run for run in mission_runs(blocked_id) if run["stage_name"] == SCOUT][0]
    blocked_plan = {
        "summary": "blocked",
        "steps": [{
            "name": "Ghost",
            "role": "builder",
            "provider": "local_qwen",
            "task": "cannot start",
            "depends_on": ["Missing"],
            "write_allowed": True,
            "success_criteria": ["nope"],
        }],
    }
    models.update_run(
        blocked_scout["id"],
        status="done",
        agent_payload=plan_payload(blocked_plan),
        finished_at=models._now(),
    )
    mad_scientist.advance_mission(blocked_scout["id"])
    check(models.get_mission(blocked_id)["status"] == "failed", "unresolved dependency blocks the mission")

    cap_id = mad_scientist.start_mission(
        ws_a["id"],
        "Stay under budget",
        provider="local_qwen",
        max_steps=4,
        max_fix_loops=1,
        spend_cap_usd=1.0,
    )
    cap_scout = [run for run in mission_runs(cap_id) if run["stage_name"] == SCOUT][0]
    models.update_run(
        cap_scout["id"],
        status="done",
        agent_payload=plan_payload(DIAMOND, cost=2.0),
        finished_at=models._now(),
    )
    mad_scientist.advance_mission(cap_scout["id"])
    cap_graph = graph.get_by_mission_id(cap_id)
    execute_attempts = []
    for step in graph.list_steps(cap_graph["id"]):
        if step["role"] == "scout":
            continue
        execute_attempts.extend(attempts_for(step["id"]))
    check(models.get_mission(cap_id)["status"] == "BLOCKED_HUMAN", "spend cap stops the mission")
    check(
        "Spend cap" in (graph.get_by_mission_id(cap_id)["blocked_reason"] or ""),
        "spend cap stores a BLOCKED_HUMAN reason",
    )
    check(float(cap_graph["spend_usd"]) == 2.0, "reported cost is accumulated")
    check(execute_attempts == [], "spend cap does not queue more steps")

    config.MAX_PARALLEL_RUNS_PER_MISSION = 1
    limited_id = mad_scientist.start_mission(
        ws_a["id"], "One at a time", provider="local_qwen", max_steps=4, max_fix_loops=1
    )
    limited_scout = [run for run in mission_runs(limited_id) if run["stage_name"] == SCOUT][0]
    models.update_run(
        limited_scout["id"],
        status="done",
        agent_payload=plan_payload(DIAMOND),
        finished_at=models._now(),
    )
    mad_scientist.advance_mission(limited_scout["id"])
    limited_graph = graph.get_by_mission_id(limited_id)
    queued_names = [
        step["name"]
        for step in graph.list_steps(limited_graph["id"])
        if step["status"] == "queued" and step["role"] != "scout"
    ]
    check(queued_names == ["Alpha"], "per-mission cap queues one ready step")
    limited_view = graph.mission_view(limited_id)
    beta_view = next(step for step in limited_view["steps"] if step["name"] == "Beta")
    check(beta_view["display_status"] == "ready", "capped sibling stays ready")
    config.MAX_PARALLEL_RUNS_PER_MISSION = 0

    sweep_id = mad_scientist.start_mission(
        ws_a["id"], "Interrupted step", provider="local_qwen", max_steps=4, max_fix_loops=2
    )
    sweep_mission = models.get_mission(sweep_id)
    sweep_scout = [run for run in mission_runs(sweep_id) if run["stage_name"] == SCOUT][0]
    models.update_run(
        sweep_scout["id"],
        status="done",
        agent_payload=plan_payload(DIAMOND),
        finished_at=models._now(),
    )
    mad_scientist.advance_mission(sweep_scout["id"])
    sweep_alpha = [run for run in mission_runs(sweep_id) if run["stage_name"] == "Alpha"][0]
    models.update_run(sweep_alpha["id"], status="running", started_at=models._now())
    swept = session_manager.sweep_orphaned_runs()
    check(sweep_alpha["id"] in swept, "startup sweep catches a running attempt")
    swept_alpha = models.get_run(sweep_alpha["id"])
    check(
        swept_alpha["status"] == "failed" and swept_alpha["error"] == session_manager.INTERRUPTED_ERROR,
        "swept run is marked interrupted",
    )
    sweep_graph = graph.get_by_mission_id(sweep_id)
    sweep_step = step_named(sweep_graph["id"], "Alpha")
    sweep_attempts = attempts_for(sweep_step["id"])
    check(
        any(item["attempt_kind"] == "fixer" for item in sweep_attempts),
        "swept failure follows the normal fixer path",
    )
    parent_after = models.get_run(sweep_mission["parent_run_id"])
    check(parent_after["status"] == "running", "swept compatibility parent is revived while the mission continues")
    check(
        models.get_run(sweep_mission["parent_run_id"])["id"] in swept,
        "sweep includes the synthetic parent run",
    )

    import app

    client = app.app.test_client()
    other_mission = models.create_mission(ws_b["id"], "build_fix", "other goal", "plan")
    resume = client.post(f"/workspaces/{slug_a}/missions/{other_mission}/resume")
    check(resume.status_code == 404, "resume rejects a mission from another workspace")
    retry = client.post(f"/workspaces/{slug_b}/mad-scientist/{blocked_id}/retry-scout")
    check(retry.status_code == 404, "retry-scout rejects a mission from another workspace")

    retry_id = mad_scientist.start_mission(
        ws_a["id"], "Scout again", provider="local_qwen", max_steps=3, max_fix_loops=1
    )
    retry_scout_run = [run for run in mission_runs(retry_id) if run["stage_name"] == SCOUT][0]
    models.update_run(
        retry_scout_run["id"],
        status="failed",
        error="scout failed",
        finished_at=models._now(),
    )
    mad_scientist.advance_mission(retry_scout_run["id"])
    check(models.get_mission(retry_id)["status"] == "failed", "failed scout fails the mission")
    page = client.get(f"/workspaces/{slug_a}")
    check(page.status_code == 200 and b"Graph failed" in page.data, "workspace page renders the graph status banner")
    check(b"depends on" in page.data and b"Retry scout" in page.data, "graph UI shows edges and scout retry")
    check(b"graph-attempt-stack" in page.data, "graph card lists the attempt stack")
    check(b"execute" in page.data and b"fixer" in page.data and b"retry" in page.data, "attempt stack shows kind and status")
    retried = client.post(f"/workspaces/{slug_a}/mad-scientist/{retry_id}/retry-scout")
    check(retried.status_code == 200, "retry-scout on the owning workspace is accepted")
    scout_again = [run for run in mission_runs(retry_id) if run["stage_name"] == SCOUT]
    check(len(scout_again) == 2 and scout_again[-1]["status"] == "pending", "retry-scout queues a new scout attempt")
    check(models.get_mission(retry_id)["status"] == "running", "retried scout mission is running again")

    try:
        mad_scientist._validate_plan(
            {
                "summary": "cycle",
                "steps": [
                    {"name": "A", "role": "builder", "task": "a", "depends_on": ["B"]},
                    {"name": "B", "role": "builder", "task": "b", "depends_on": ["A"]},
                ],
            },
            "local_qwen",
            4,
        )
        check(False, "cyclic plan is rejected before materialize")
    except ValueError as exc:
        check("Cyclic dependency" in str(exc), "cyclic plan is rejected before materialize")

    cycle_id = mad_scientist.start_mission(
        ws_a["id"], "Reject a cycle", provider="local_qwen", max_steps=4, max_fix_loops=1
    )
    cycle_scout = [run for run in mission_runs(cycle_id) if run["stage_name"] == SCOUT][0]
    models.update_run(
        cycle_scout["id"],
        status="done",
        agent_payload=plan_payload({
            "summary": "cycle",
            "steps": [
                {
                    "name": "A",
                    "role": "builder",
                    "provider": "local_qwen",
                    "task": "a",
                    "depends_on": ["B"],
                    "write_allowed": True,
                    "success_criteria": ["a"],
                },
                {
                    "name": "B",
                    "role": "builder",
                    "provider": "local_qwen",
                    "task": "b",
                    "depends_on": ["A"],
                    "write_allowed": True,
                    "success_criteria": ["b"],
                },
            ],
        }),
        finished_at=models._now(),
    )
    mad_scientist.advance_mission(cycle_scout["id"])
    check(models.get_mission(cycle_id)["status"] == "failed", "cyclic plan fails the mission closed")
    check(
        "Cyclic dependency" in (models.get_mission(cycle_id)["note"] or ""),
        "cyclic plan failure names the cycle",
    )
    check(
        step_named(graph.get_by_mission_id(cycle_id)["id"], "A") is None,
        "cyclic plan does not materialize steps",
    )

    cap_step_id = mad_scientist.start_mission(
        ws_a["id"], "One attempt", provider="local_qwen", max_steps=2, max_fix_loops=1
    )
    cap_step_scout = [run for run in mission_runs(cap_step_id) if run["stage_name"] == SCOUT][0]
    models.update_run(
        cap_step_scout["id"],
        status="done",
        agent_payload=plan_payload({
            "summary": "single",
            "steps": [{
                "name": "Only",
                "role": "builder",
                "provider": "local_qwen",
                "task": "do the thing",
                "depends_on": [],
                "write_allowed": True,
                "success_criteria": ["done"],
            }],
        }),
        finished_at=models._now(),
    )
    mad_scientist.advance_mission(cap_step_scout["id"])
    only_run = [run for run in mission_runs(cap_step_id) if run["stage_name"] == "Only"][0]
    finish(only_run["id"], status="failed", summary="boom", error="boom")
    only_step = step_named(graph.get_by_mission_id(cap_step_id)["id"], "Only")
    check(only_step["status"] == "BLOCKED_HUMAN", "attempt cap blocks the step")
    check(len(attempts_for(only_step["id"])) == 1, "attempt cap does not queue another attempt")
    check(
        models.get_mission(cap_step_id)["status"] == "BLOCKED_HUMAN",
        "attempt cap blocks the mission when nothing else can run",
    )
    check(
        "Attempt cap" in (only_step["blocked_reason"] or ""),
        "attempt cap reason is stored on the step",
    )

    hash_id = mad_scientist.start_mission(
        ws_a["id"], "Same error twice", provider="local_qwen", max_steps=2, max_fix_loops=3
    )
    hash_scout = [run for run in mission_runs(hash_id) if run["stage_name"] == SCOUT][0]
    models.update_run(
        hash_scout["id"],
        status="done",
        agent_payload=plan_payload({
            "summary": "hash",
            "steps": [{
                "name": "Repeat",
                "role": "builder",
                "provider": "local_qwen",
                "task": "try once",
                "depends_on": [],
                "write_allowed": True,
                "success_criteria": ["done"],
            }],
        }),
        finished_at=models._now(),
    )
    mad_scientist.advance_mission(hash_scout["id"])
    repeat_run = [run for run in mission_runs(hash_id) if run["stage_name"] == "Repeat"][0]
    finish(repeat_run["id"], status="failed", summary="same-boom", error="same-boom")
    repeat_step = step_named(graph.get_by_mission_id(hash_id)["id"], "Repeat")
    repeat_attempts = attempts_for(repeat_step["id"])
    check(
        len(repeat_attempts) == 2 and repeat_attempts[-1]["attempt_kind"] == "fixer",
        "first failure still queues a fixer",
    )
    finish(repeat_attempts[-1]["run_id"], status="failed", summary="same-boom", error="same-boom")
    repeat_step = step_named(graph.get_by_mission_id(hash_id)["id"], "Repeat")
    check(repeat_step["status"] == "BLOCKED_HUMAN", "repeated error hash blocks the step")
    check(len(attempts_for(repeat_step["id"])) == 2, "repeated error hash does not queue again")
    check(
        "Repeated error hash" in (repeat_step["blocked_reason"] or ""),
        "repeated error hash reason is stored",
    )

    token_id = mad_scientist.start_mission(
        ws_a["id"],
        "Too many tokens",
        provider="local_qwen",
        max_steps=2,
        max_fix_loops=1,
        token_cap=100,
    )
    token_scout = [run for run in mission_runs(token_id) if run["stage_name"] == SCOUT][0]
    models.update_run(
        token_scout["id"],
        status="done",
        agent_payload=json.dumps({
            "response_type": "plan",
            "summary": "scout",
            "plan": json.dumps({
                "summary": "token",
                "steps": [{
                    "name": "After",
                    "role": "builder",
                    "provider": "local_qwen",
                    "task": "should not start",
                    "depends_on": [],
                    "write_allowed": True,
                    "success_criteria": ["nope"],
                }],
            }),
            "total_tokens": 150,
        }),
        finished_at=models._now(),
    )
    mad_scientist.advance_mission(token_scout["id"])
    token_graph = graph.get_by_mission_id(token_id)
    check(models.get_mission(token_id)["status"] == "BLOCKED_HUMAN", "token cap blocks the mission")
    check(int(token_graph["tokens_used"]) == 150, "reported tokens are accumulated")
    check(
        step_named(token_graph["id"], "After")["status"] == "planned",
        "token cap does not queue the next step",
    )
    check(
        "Token cap" in (token_graph["blocked_reason"] or ""),
        "token cap reason is stored",
    )

    summary_id = mad_scientist.start_mission(
        ws_a["id"], "Summary is not a test", provider="local_qwen", max_steps=2, max_fix_loops=2
    )
    summary_scout = [run for run in mission_runs(summary_id) if run["stage_name"] == SCOUT][0]
    models.update_run(
        summary_scout["id"],
        status="done",
        agent_payload=plan_payload({
            "summary": "verify",
            "steps": [{
                "name": "Verify",
                "role": "tester",
                "provider": "local_qwen",
                "task": "run the tests",
                "depends_on": [],
                "write_allowed": False,
                "success_criteria": ["tests pass"],
            }],
        }),
        finished_at=models._now(),
    )
    mad_scientist.advance_mission(summary_scout["id"])
    verify_run = [run for run in mission_runs(summary_id) if run["stage_name"] == "Verify"][0]
    finish(verify_run["id"], summary="passed")
    verify_step = step_named(graph.get_by_mission_id(summary_id)["id"], "Verify")
    verify_attempts = attempts_for(verify_step["id"])
    check(verify_step["status"] != "done", "tester summary alone does not complete the step")
    check(
        len(verify_attempts) == 2 and verify_attempts[-1]["attempt_kind"] == "fixer",
        "tester summary alone is a failed attempt",
    )
    check(
        "model summary" in (models.get_run(verify_run["id"])["error"] or ""),
        "tester summary failure explains the missing command",
    )
    summary_evals = models.list_evaluations_for_mission(summary_id)
    check(
        summary_evals and summary_evals[0]["verdict"] == "fail",
        "tester summary alone records a failing evaluation",
    )

    patch_id = mad_scientist.start_mission(
        ws_a["id"], "Same patch twice", provider="local_qwen", max_steps=2, max_fix_loops=3
    )
    patch_scout = [run for run in mission_runs(patch_id) if run["stage_name"] == SCOUT][0]
    models.update_run(
        patch_scout["id"],
        status="done",
        agent_payload=plan_payload({
            "summary": "patch",
            "steps": [{
                "name": "Patch",
                "role": "builder",
                "provider": "local_qwen",
                "task": "edit",
                "depends_on": [],
                "write_allowed": True,
                "success_criteria": ["edited"],
            }],
        }),
        finished_at=models._now(),
    )
    mad_scientist.advance_mission(patch_scout["id"])
    same_patch = {
        "response_type": "patch",
        "summary": "first try",
        "plan": "",
        "patches": [{"path": "auth.py", "content": "same bytes"}],
    }
    patch_run = [run for run in mission_runs(patch_id) if run["stage_name"] == "Patch"][0]
    finish(patch_run["id"], status="failed", summary="first", error="first-boom", payload=same_patch)
    patch_step = step_named(graph.get_by_mission_id(patch_id)["id"], "Patch")
    patch_attempts = attempts_for(patch_step["id"])
    check(len(patch_attempts) == 2, "a new patch failure still queues a fixer")
    second_patch = dict(same_patch)
    second_patch["summary"] = "second try"
    finish(
        patch_attempts[-1]["run_id"],
        status="failed",
        summary="second",
        error="second-boom",
        payload=second_patch,
    )
    patch_step = step_named(graph.get_by_mission_id(patch_id)["id"], "Patch")
    check(patch_step["status"] == "BLOCKED_HUMAN", "repeated patch hash blocks the step")
    check(len(attempts_for(patch_step["id"])) == 2, "repeated patch hash does not queue again")
    check(
        "Repeated patch hash" in (patch_step["blocked_reason"] or ""),
        "repeated patch hash reason is stored",
    )

    config.MAX_PARALLEL_RUNS_PER_MISSION = 1
    sibling_id = mad_scientist.start_mission(
        ws_a["id"], "Block one branch", provider="local_qwen", max_steps=4, max_fix_loops=1
    )
    sibling_scout = [run for run in mission_runs(sibling_id) if run["stage_name"] == SCOUT][0]
    models.update_run(
        sibling_scout["id"],
        status="done",
        agent_payload=plan_payload({
            "summary": "two roots",
            "steps": [
                {
                    "name": "Left",
                    "role": "builder",
                    "provider": "local_qwen",
                    "task": "left",
                    "depends_on": [],
                    "write_allowed": True,
                    "success_criteria": ["left"],
                },
                {
                    "name": "Right",
                    "role": "builder",
                    "provider": "local_qwen",
                    "task": "right",
                    "depends_on": [],
                    "write_allowed": True,
                    "success_criteria": ["right"],
                },
            ],
        }),
        finished_at=models._now(),
    )
    mad_scientist.advance_mission(sibling_scout["id"])
    left_runs = [run for run in mission_runs(sibling_id) if run["stage_name"] == "Left"]
    right_runs = [run for run in mission_runs(sibling_id) if run["stage_name"] == "Right"]
    check(len(left_runs) == 1 and right_runs == [], "sibling fixture queues one root")
    finish(left_runs[0]["id"], status="failed", summary="left failed", error="left failed")
    sibling_graph = graph.get_by_mission_id(sibling_id)
    left_step = step_named(sibling_graph["id"], "Left")
    right_step = step_named(sibling_graph["id"], "Right")
    check(left_step["status"] == "BLOCKED_HUMAN", "attempt cap blocks only the exhausted step")
    check(right_step["status"] == "queued", "the other ready step still starts")
    check(
        models.get_mission(sibling_id)["status"] == "running",
        "a blocked step does not freeze an independent branch",
    )
    config.MAX_PARALLEL_RUNS_PER_MISSION = 0

    approve_id = mad_scientist.start_mission(
        ws_a["id"], "Needs a payload", provider="local_qwen", max_steps=2, max_fix_loops=1
    )
    approve_scout = [run for run in mission_runs(approve_id) if run["stage_name"] == SCOUT][0]
    models.update_run(
        approve_scout["id"],
        status="awaiting_approval",
        agent_payload=plan_payload({
            "summary": "hold",
            "steps": [{
                "name": "Held",
                "role": "builder",
                "provider": "local_qwen",
                "task": "wait",
                "depends_on": [],
                "write_allowed": True,
                "success_criteria": ["held"],
            }],
        }),
    )
    mad_scientist.advance_mission(approve_scout["id"])
    approval_page = client.get(f"/workspaces/{slug_a}")
    approve_label = f"Approve #{approve_scout['id']}".encode()
    check(approve_label in approval_page.data, "awaiting plan payload offers Approve")
    models.update_run(approve_scout["id"], status="awaiting_approval", agent_payload="")
    graph.sync_run_status(approve_scout["id"])
    empty_page = client.get(f"/workspaces/{slug_a}")
    check(approve_label not in empty_page.data, "awaiting run without a payload hides Approve")
    check(
        b"no plan or patch payload" in empty_page.data,
        "missing approval payload is explained",
    )

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("\nmad scientist graph validation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

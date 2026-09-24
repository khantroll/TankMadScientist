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


def finish(run_id: int, status: str = "done", summary: str = "", cost: float | None = None) -> None:
    models.update_run(
        run_id,
        status=status,
        agent_payload=run_payload(summary, cost),
        finished_at=models._now(),
        error=None if status == "done" else status,
    )
    mad_scientist.advance_mission(run_id)


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
        max_fix_loops=2,
    )
    mission = models.get_mission(mission_id)
    graph_row = graph.get_by_mission_id(mission_id)
    check(graph_row is not None, "graph mission row exists")
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

    check_runs = [run for run in mission_runs(mission_id) if run["stage_name"] == "Check"]
    check(len(check_runs) == 1, "tester queues after join completes")
    finish(check_runs[0]["id"], summary="checks passed")
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
    check(models.get_mission(cap_id)["status"] == "failed", "spend cap stops the mission")
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
    retried = client.post(f"/workspaces/{slug_a}/mad-scientist/{retry_id}/retry-scout")
    check(retried.status_code == 200, "retry-scout on the owning workspace is accepted")
    scout_again = [run for run in mission_runs(retry_id) if run["stage_name"] == SCOUT]
    check(len(scout_again) == 2 and scout_again[-1]["status"] == "pending", "retry-scout queues a new scout attempt")
    check(models.get_mission(retry_id)["status"] == "running", "retried scout mission is running again")

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("\nmad scientist graph validation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Validation for mission approval UX, debrief, arsenal, and blocked-step resume.

Does not call a model or bind a port. Uses a temp data dir.
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

TMP = tempfile.mkdtemp(prefix="tank-obs-")
REPO = Path(TMP) / "repo"
REPO.mkdir()
(REPO / "README.md").write_text("tank observability validation repo\n", encoding="utf-8")
WORKSPACES = Path(TMP) / "workspaces.yaml"
WORKSPACES.write_text("workspaces: []\n", encoding="utf-8")

os.environ["TANK_DATA_DIR"] = TMP
os.environ["TANK_WORKSPACES_FILE"] = str(WORKSPACES)
os.environ["TANK_PROVIDERS_FILE"] = str(ROOT / "providers.yaml")
os.environ["TANK_MISSION_TEMPLATES_FILE"] = str(ROOT / "mission_templates.yaml")
os.environ["TANK_MAX_PARALLEL_RUNS"] = "0"
os.environ["TANK_MAX_PARALLEL_RUNS_PER_MISSION"] = "0"

import arsenal  # noqa: E402
import dependency_recovery  # noqa: E402
import mad_scientist  # noqa: E402
import mad_scientist_graph as graph  # noqa: E402
import mission_debrief  # noqa: E402
import models  # noqa: E402
import pattern_synthesis  # noqa: E402
import session_manager  # noqa: E402
import verification_display  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"ok: {label}")
        return
    FAILURES.append(label)
    print(f"FAIL: {label}")


def make_workspace(slug: str = "obs") -> int:
    created = models.create_workspace(
        name=f"Obs {slug}",
        repo_path=str(REPO),
        slug=slug,
        default_provider="local_qwen",
    )
    ws = models.get_workspace(created)
    return int(ws["id"])


def open_mission(workspace_id: int, goal: str = "Add JWT authentication"):
    mission_id = models.create_mission(
        workspace_id,
        "mad_scientist",
        goal,
        "scout",
        provider="local_qwen",
    )
    graph_id, scout_id = graph.open_graph(
        mission_id,
        workspace_id,
        goal,
        scout_name="scout",
        scout_task="scout the repo",
        scout_provider="local_qwen",
        max_attempts=3,
    )
    return mission_id, graph_id, scout_id


def finish_run(run_id: int, status="done", payload=None, error=None):
    models.update_run(
        run_id,
        status=status,
        agent_payload=json.dumps(payload or {"response_type": "plan", "summary": "ok", "plan": "{}"}),
        finished_at=models._now(),
        error=error,
    )
    graph.sync_run_status(run_id)
    if status in ("done", "failed", "rejected", "cancelled"):
        graph.record_attempt_usage(run_id)


def seed_plan(graph_id: int):
    plan = {
        "summary": "JWT plan",
        "project_profile": {"framework": "Flask", "language": "Python"},
        "steps": [
            {
                "name": "Add JWT utils",
                "role": "builder",
                "provider": "local_qwen",
                "task": "add jwt helpers",
                "write_allowed": True,
                "depends_on": [],
            },
            {
                "name": "Run tests",
                "role": "tester",
                "provider": "local_qwen",
                "task": "run pytest",
                "write_allowed": False,
                "depends_on": ["Add JWT utils"],
            },
        ],
    }
    graph.materialize_plan(graph_id, plan)
    return plan


def awaiting_patch(run_id: int, path: str, content: str = "x = 1\n"):
    payload = {
        "response_type": "patch",
        "summary": f"patch {path}",
        "patches": [{"path": path, "content": content}],
        "post_actions": {"run_tests": False, "run_git_diff": False},
    }
    models.update_run(
        run_id,
        status="awaiting_approval",
        agent_payload=json.dumps(payload),
    )
    graph.sync_run_status(run_id)


def test_bulk_approval_mission_scoped():
    print("\n== bulk approval ==")
    ws = make_workspace("bulk-main")
    mission_id, graph_id, scout_id = open_mission(ws, "bulk jwt")
    scout_run = models.create_run(ws, None, "scout", provider="local_qwen", mission_id=mission_id, stage_name="scout")
    graph.record_attempt(scout_id, scout_run, "scout")
    finish_run(scout_run)
    seed_plan(graph_id)
    steps = graph.list_steps(graph_id)
    builder = next(s for s in steps if s["name"] == "Add JWT utils")
    tester = next(s for s in steps if s["name"] == "Run tests")

    r1 = models.create_run(ws, None, "p1", provider="local_qwen", mission_id=mission_id, stage_name="Add JWT utils")
    r2 = models.create_run(ws, None, "p2", provider="local_qwen", mission_id=mission_id, stage_name="Add JWT utils")
    graph.record_attempt(builder["id"], r1, "execute")
    awaiting_patch(r1, "jwt_utils.py", "TOKEN=1\n")
    # Force-create a second awaiting approval tied to mission without advancing graph oddly:
    # mark builder done without clearing approval — instead attach r2 as attempt on tester with awaiting.
    finish_run(r1, payload={
        "response_type": "patch",
        "summary": "applied",
        "patches": [{"path": "jwt_utils.py", "content": "TOKEN=1\n"}],
    })
    # Individual approval path still works
    r3 = models.create_run(ws, None, "p3", provider="local_qwen", mission_id=mission_id, stage_name="Add JWT utils")
    graph.record_attempt(builder["id"], r3, "retry")
    awaiting_patch(r3, "jwt_utils.py", "TOKEN=2\n")
    check(session_manager.approve_run(r3) is True, "individual approve_run still works")
    run3 = models.get_run(r3)
    check(run3["status"] in ("done", "failed"), "individual approve moved run out of awaiting_approval")

    # Two pending on mission + one on other mission
    r4 = models.create_run(ws, None, "p4", provider="local_qwen", mission_id=mission_id, stage_name="Run tests")
    r5 = models.create_run(ws, None, "p5", provider="local_qwen", mission_id=mission_id, stage_name="Run tests")
    graph.record_attempt(tester["id"], r4, "execute")
    awaiting_patch(r4, "test_jwt.py", "def test_ok():\n    assert True\n")
    # Second awaiting: recreate builder attempt
    builder_steps = graph.get_step(builder["id"])
    # Put builder back to awaiting via new attempt
    r6 = models.create_run(ws, None, "p6", provider="local_qwen", mission_id=mission_id, stage_name="Add JWT utils")
    graph.record_attempt(builder["id"], r6, "fixer")
    awaiting_patch(r6, "auth.py", "def login():\n    return 1\n")

    other_ws = make_workspace("bulk-other")
    other_mission, other_graph, other_scout = open_mission(other_ws, "other mission")
    other_scout_run = models.create_run(other_ws, None, "scout", provider="local_qwen", mission_id=other_mission, stage_name="scout")
    graph.record_attempt(other_scout, other_scout_run, "scout")
    finish_run(other_scout_run)
    seed_plan(other_graph)
    other_builder = next(s for s in graph.list_steps(other_graph) if s["name"] == "Add JWT utils")
    other_run = models.create_run(other_ws, None, "other", provider="local_qwen", mission_id=other_mission, stage_name="Add JWT utils")
    graph.record_attempt(other_builder["id"], other_run, "execute")
    awaiting_patch(other_run, "other.py", "y=1\n")

    pending = session_manager.pending_approval_run_ids(mission_id)
    check(r4 in pending and r6 in pending, "pending approvals listed for mission")
    check(len(pending) >= 2, "pending approval count includes every awaiting run")
    check(other_run not in pending, "pending approvals do not cross mission boundaries")
    view = graph.mission_view(mission_id)
    check(view["waiting_approval_count"] == len(pending), "mission_view pending count matches run list")

    result = session_manager.approve_mission_pending(mission_id)
    check(result["approved_count"] >= 2, "mission bulk approve approved pending runs")
    check(models.get_run(other_run)["status"] == "awaiting_approval", "bulk approve did not touch other mission")
    check(models.get_run(r4)["status"] != "awaiting_approval", "bulk-approved run left awaiting_approval")
    check(models.get_run(r6)["status"] != "awaiting_approval", "second bulk-approved run left awaiting_approval")

    # HTML surface
    import app as tank_app

    client = tank_app.app.test_client()
    # Ensure workspace page can render with approvals banner for a fresh awaiting set
    r7 = models.create_run(ws, None, "p7", provider="local_qwen", mission_id=mission_id, stage_name="Run tests")
    graph.record_attempt(tester["id"], r7, "retry")
    awaiting_patch(r7, "more.py", "z=1\n")
    slug = models.get_workspace_by_id(ws)["slug"]
    html = client.get(f"/workspaces/{slug}").get_data(as_text=True)
    check("ACTION REQUIRED" in html, "mission card shows ACTION REQUIRED")
    check("Approve all" in html, "mission card shows Approve all control")
    check("Review &amp; Approve" in html or "Review & Approve" in html, "mission card shows Review & Approve")


def test_debrief_and_arsenal():
    print("\n== debrief + arsenal ==")
    ws = make_workspace("debrief")
    mission_id, graph_id, scout_id = open_mission(ws, "Add JWT authentication")
    scout_run = models.create_run(ws, None, "scout", provider="local_qwen", mission_id=mission_id, stage_name="scout")
    graph.record_attempt(scout_id, scout_run, "scout")
    finish_run(scout_run, payload={
        "response_type": "plan",
        "summary": "Flask app scouted",
        "plan": "{}",
    })
    seed_plan(graph_id)
    models.upsert_workspace_memory(ws, "mad_scientist", {
        "project_profile": {"framework": "Flask", "language": "Python"},
        "test_command": "python -m pytest -q",
        "known_pitfalls": [],
        "prior_successful_patterns": [],
        "updated_at": models._now(),
    })
    builder = next(s for s in graph.list_steps(graph_id) if s["name"] == "Add JWT utils")
    tester = next(s for s in graph.list_steps(graph_id) if s["name"] == "Run tests")

    # Pattern synthesis decision: generate because incompatible crew
    synthesis = {
        "version": 1,
        "goal": "Add JWT authentication",
        "crew": {
            "action": "generate",
            "name": "JwtBuilderCrew",
            "slug": "jwt-builder-crew",
            "rationale": "Rejected closest crew CityConsoleAuditorCrew (partial role cover).",
            "closest_rejected": {"kind": "crew", "name": "CityConsoleAuditorCrew", "id": "city"},
            "roles": [],
            "tools": [],
            "provider_preferences": {},
            "changes": [],
            "confirmation": "pending",
        },
        "specialists": {
            "builder": {
                "action": "generate",
                "name": "JwtBuilder",
                "rationale": "generated",
                "changes": [],
            },
            "tester": {
                "action": "reuse",
                "name": "DefaultTester",
                "rationale": "reused",
                "changes": [],
            },
        },
        "inspected": {
            "crews": [{"name": "CityConsoleAuditorCrew"}],
            "roles": [],
        },
    }
    with models.get_db() as conn:
        conn.execute(
            "UPDATE mad_scientist_missions SET synthesis_json = ? WHERE mission_id = ?",
            (json.dumps(synthesis), mission_id),
        )
    arsenal.record_synthesis_lessons(ws, mission_id, pattern_synthesis.public_view(mission_id))

    br = models.create_run(ws, None, "build", provider="local_qwen", mission_id=mission_id, stage_name="Add JWT utils")
    graph.record_attempt(builder["id"], br, "execute")
    finish_run(br, payload={
        "response_type": "patch",
        "summary": "JWT utils",
        "patches": [{"path": "jwt_utils.py", "content": "ok\n"}],
    })
    with models.get_db() as conn:
        conn.execute(
            "UPDATE mad_scientist_steps SET status = 'done' WHERE id = ?",
            (builder["id"],),
        )

    tr = models.create_run(ws, None, "test", provider="local_qwen", mission_id=mission_id, stage_name="Run tests")
    graph.record_attempt(tester["id"], tr, "execute")
    fail_payload = {
        "response_type": "plan",
        "summary": "tests",
        "plan": "",
        "post_actions": {
            "run_tests": True,
            "test_command": r"C:\Program Files\Python314\python.exe -m pytest -q",
            "run_git_diff": False,
        },
        "tool_exit_code": 1,
    }
    err = (
        "Verification command exited 1: "
        r"C:\Program Files\Python314\python.exe: No module named pytest"
    )
    finish_run(tr, status="failed", payload=fail_payload, error=err)
    graph.block_step(tester["id"], "Repeated error hash on step 'Run tests'.")
    graph.block_mission(mission_id, "Repeated error hash on step 'Run tests'.")
    models.update_mission(mission_id, status=graph.BLOCKED_HUMAN, note="Repeated error hash")
    arsenal.record_verification_lesson(ws, models.get_run(tr), mission_id=mission_id, blocked_reason="Repeated error hash")

    debrief = mission_debrief.build_debrief(mission_id)
    check(debrief is not None and debrief["show"], "debrief available for blocked mission")
    check(debrief["objective"].startswith("Add JWT"), "debrief includes objective")
    check(any(s["name"] == "Add JWT utils" for s in debrief["completed_steps"]), "debrief lists completed steps")
    check(any(s["name"] == "Run tests" for s in debrief["failed_or_blocked_steps"]), "debrief lists blocked steps")
    check(debrief["specialists_generated"] == 1, "debrief counts generated specialists")
    check(debrief["specialists_reused"] == 1, "debrief counts reused specialists")
    check(debrief["crews_generated"] == 1, "debrief counts generated crew")
    check(debrief["rejected_crew"] == "CityConsoleAuditorCrew", "debrief surfaces rejected crew")
    check(debrief["files_touched"] and "jwt_utils.py" in debrief["files_touched"], "debrief lists files touched")
    check(debrief["verification_attempts"], "debrief includes verification attempts")
    check(debrief["circuit_events"], "debrief includes circuit breaker events")

    view = verification_display.verification_view(models.get_run(tr))
    check(view is not None, "verification view present")
    check(view["classification"] == "missing_dependency", "pytest missing classified as missing_dependency")
    check(view["interpreter"] and "Python314" in view["interpreter"], "interpreter extracted from command")
    check(view["exit_code"] == 1, "exit code surfaced")

    arsenal_view = arsenal.arsenal_view(ws)
    check(arsenal_view["asset_count"] >= 1, "arsenal has assets (recipe/adapter)")
    check(any(a["type"] == "verification_recipe" or a["type"] == "repo_adapter" for a in arsenal_view["assets"])
          or arsenal_view["lesson_count"] > 0, "arsenal separates durable content")
    check(any("pytest" in (lesson["text"] or "").lower() for lesson in arsenal_view["lessons"]),
          "arsenal lessons include pytest environment fact")
    check(any(lesson["category"] == "environment" for lesson in arsenal_view["lessons"]),
          "arsenal lessons categorized")
    # Assets vs lessons are separate lists
    check("assets" in arsenal_view and "lessons" in arsenal_view, "arsenal separates assets from lessons")

    # Pattern synthesis still rejects partial crew cover
    decision = pattern_synthesis._decide_crew(
        "Add JWT authentication",
        [
            {"role": "builder", "name": "build", "provider": "local_qwen"},
            {"role": "tester", "name": "test", "provider": "local_qwen"},
        ],
        [{
            "id": "city",
            "name": "CityConsoleAuditorCrew",
            "goal": "audit city console",
            "roles": [
                {"slug": "reviewer", "name": "Reviewer", "provider": "local_qwen", "tools": []},
            ],
        }],
    )
    check(decision[0] == "generate", "Pattern Synthesis does not force-fit incompatible crew")


def test_resume_blocked_preserves_history():
    print("\n== resume blocked step ==")
    ws = make_workspace("resume")
    mission_id, graph_id, scout_id = open_mission(ws, "resume jwt")
    scout_run = models.create_run(ws, None, "scout", provider="local_qwen", mission_id=mission_id, stage_name="scout")
    graph.record_attempt(scout_id, scout_run, "scout")
    finish_run(scout_run)
    seed_plan(graph_id)
    builder = next(s for s in graph.list_steps(graph_id) if s["name"] == "Add JWT utils")
    tester = next(s for s in graph.list_steps(graph_id) if s["name"] == "Run tests")

    br = models.create_run(ws, None, "build", provider="local_qwen", mission_id=mission_id, stage_name="Add JWT utils")
    graph.record_attempt(builder["id"], br, "execute")
    finish_run(br, payload={"response_type": "plan", "summary": "built", "plan": "ok"})
    with models.get_db() as conn:
        conn.execute("UPDATE mad_scientist_steps SET status='done' WHERE id=?", (builder["id"],))

    # Two failed tester attempts with same error → block via repeated hash
    err = "Verification command exited 1: No module named pytest"
    payload = {
        "response_type": "plan",
        "summary": "fail",
        "plan": "",
        "post_actions": {"run_tests": True, "test_command": "python -m pytest -q", "run_git_diff": False},
        "tool_exit_code": 1,
    }
    t1 = models.create_run(ws, None, "t1", provider="local_qwen", mission_id=mission_id, stage_name="Run tests")
    graph.record_attempt(tester["id"], t1, "execute")
    finish_run(t1, status="failed", payload=payload, error=err)
    t2 = models.create_run(ws, None, "t2", provider="local_qwen", mission_id=mission_id, stage_name="Run tests")
    graph.record_attempt(tester["id"], t2, "retry")
    finish_run(t2, status="failed", payload=payload, error=err)
    reason = graph.repeated_mistake_reason(tester["id"], graph.get_attempt_by_run(t2)["id"])
    check(reason is not None and "Repeated error hash" in reason, "circuit breaker detects repeated error")
    graph.block_step(tester["id"], reason)
    graph.block_mission(mission_id, reason)
    models.update_mission(mission_id, status=graph.BLOCKED_HUMAN, note=reason)

    attempts_before = graph.list_attempts(tester["id"])
    check(len(attempts_before) >= 2, "retry history present before resume")
    done_builder = graph.get_step(builder["id"])
    check(done_builder["status"] == "done", "completed builder remains done before resume")

    view = graph.mission_view(mission_id)
    check(view["can_resume_blocked"], "mission_view marks blocked step resumable")
    check(view["resumable_steps"] and view["resumable_steps"][0]["name"] == "Run tests",
          "resumable step is Run tests")

    new_run_id = mad_scientist.resume_blocked_step(mission_id, step_id=tester["id"])
    check(isinstance(new_run_id, int) and new_run_id > 0, "resume queues a new run")
    mission = models.get_mission(mission_id)
    check(mission["status"] == "running", "mission returns to running on resume")
    check(graph.get_step(builder["id"])["status"] == "done", "completed steps are not rerun on resume")
    attempts_after = graph.list_attempts(tester["id"])
    check(len(attempts_after) == len(attempts_before) + 1, "resume adds attempt; history preserved")
    check(all(a["run_id"] in {row["run_id"] for row in attempts_after} for a in attempts_before),
          "prior attempt run ids still listed")
    resumed = graph.get_attempt_by_run(new_run_id)
    check(resumed is not None and resumed["attempt_kind"] == "execute", "resumed step may execute again")
    check(int(resumed["breaker_epoch"] or 0) > 0, "resume bumps breaker epoch")
    # Same error in new epoch should NOT immediately match old epoch priors
    finish_run(new_run_id, status="failed", payload=payload, error=err)
    reason2 = graph.repeated_mistake_reason(tester["id"], resumed["id"])
    check(reason2 is None, "circuit breaker reset for resumed epoch (first failure)")
    # Second failure in new epoch trips again
    t3 = models.create_run(ws, None, "t3", provider="local_qwen", mission_id=mission_id, stage_name="Run tests")
    graph.record_attempt(tester["id"], t3, "retry")
    finish_run(t3, status="failed", payload=payload, error=err)
    reason3 = graph.repeated_mistake_reason(tester["id"], graph.get_attempt_by_run(t3)["id"])
    check(reason3 is not None, "circuit breaker still works inside new epoch")

    # UI contains resume control
    import app as tank_app
    # re-block for UI check
    graph.block_step(tester["id"], reason3 or "blocked")
    graph.block_mission(mission_id, reason3 or "blocked")
    models.update_mission(mission_id, status=graph.BLOCKED_HUMAN, note=reason3)
    slug = models.get_workspace_by_id(ws)["slug"]
    html = tank_app.app.test_client().get(f"/workspaces/{slug}").get_data(as_text=True)
    check("Resume from Run tests" in html, "UI exposes Resume from Run tests")
    check("Mission Debrief" in html, "UI exposes Mission Debrief")
    check("No module named pytest" in html or "missing dependency" in html, "verification failure UX visible")

    arsenal_html = tank_app.app.test_client().get(f"/workspaces/{slug}/arsenal").get_data(as_text=True)
    check("Arsenal" in arsenal_html and "Assets" in arsenal_html and "Lessons" in arsenal_html,
          "Arsenal page separates Assets and Lessons")


def test_module_classification_and_install_gate():
    print("\n== local vs third-party + install gate ==")
    (REPO / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n", encoding="utf-8")
    (REPO / "requirements.txt").write_text("Flask==3.0.3\nPyJWT==2.9.0\n", encoding="utf-8")

    pytest_err = r"C:\Program Files\Python314\python.exe: No module named pytest"
    app_err = "ModuleNotFoundError: No module named 'app'"

    check(
        verification_display.classify_verification_failure(
            pytest_err,
            r"C:\Program Files\Python314\python.exe -m pytest -q",
            repo_path=str(REPO),
        ) == "missing_dependency",
        "No module named pytest → missing_dependency",
    )
    check(
        verification_display.is_installable_module(str(REPO), "pytest") is True,
        "pytest is installable",
    )
    check(
        verification_display.classify_verification_failure(
            app_err,
            r"C:\Program Files\Python314\python.exe -m pytest -q",
            repo_path=str(REPO),
        ) == "import_context_failure",
        "No module named app → import_context_failure",
    )
    check(
        verification_display.is_installable_module(str(REPO), "app") is False,
        "local app module is not installable",
    )
    check(
        verification_display.is_local_project_module(str(REPO), "app") is True,
        "app is detected as local project module",
    )

    ws = make_workspace("install-gate")
    mission_id, graph_id, scout_id = open_mission(ws, "jwt install gate")
    scout_run = models.create_run(ws, None, "scout", provider="local_qwen", mission_id=mission_id, stage_name="scout")
    graph.record_attempt(scout_id, scout_run, "scout")
    finish_run(scout_run)
    seed_plan(graph_id)
    builder = next(s for s in graph.list_steps(graph_id) if s["name"] == "Add JWT utils")
    tester = next(s for s in graph.list_steps(graph_id) if s["name"] == "Run tests")
    br = models.create_run(ws, None, "build", provider="local_qwen", mission_id=mission_id, stage_name="Add JWT utils")
    graph.record_attempt(builder["id"], br, "execute")
    finish_run(br, payload={"response_type": "plan", "summary": "built", "plan": "ok"})
    with models.get_db() as conn:
        conn.execute("UPDATE mad_scientist_steps SET status='done' WHERE id=?", (builder["id"],))

    # Missing pytest should attach install offer and block instead of queuing a fixer.
    tr = models.create_run(ws, None, "test", provider="local_qwen", mission_id=mission_id, stage_name="Run tests")
    graph.record_attempt(tester["id"], tr, "execute")
    payload = {
        "response_type": "plan",
        "summary": "tests",
        "plan": "",
        "post_actions": {
            "run_tests": True,
            "test_command": r"C:\Program Files\Python314\python.exe -m pytest -q",
            "run_git_diff": False,
        },
        "tool_exit_code": 1,
        "verification_cwd": str(REPO),
        "verification_command_resolved": r"C:\Program Files\Python314\python.exe -m pytest -q",
    }
    models.update_run(
        tr,
        status="failed",
        agent_payload=json.dumps(payload),
        finished_at=models._now(),
        error=f"Verification command exited 1: {pytest_err}",
        log_path=str(Path(TMP) / "install-gate.log"),
    )
    Path(TMP, "install-gate.log").write_text("", encoding="utf-8")
    graph.sync_run_status(tr)
    graph.record_attempt_usage(tr)
    mad_scientist.advance_mission(tr)

    offer = dependency_recovery.attach_install_offer(tr, str(REPO))
    check(offer is not None and offer["module"] == "pytest", "missing pytest produces install offer")
    check("pip install" in (offer.get("install_command") or ""), "install command uses pip")
    check(
        "Python314" in (offer.get("install_command") or "")
        or "Python314" in (offer.get("interpreter") or ""),
        "install command uses failing interpreter when possible",
    )
    step = graph.get_step(tester["id"])
    # advance_mission should have blocked for install rather than queued a fixer
    attempts = graph.list_attempts(tester["id"])
    check(
        step["status"] == graph.BLOCKED_HUMAN
        or any((models.get_run_payload(a["run_id"]) or {}).get("install_offer") for a in attempts),
        "installable failure stays approval-gated (blocked or offer stored)",
    )
    check(
        not any(a["attempt_kind"] == "fixer" for a in attempts),
        "installable missing pytest does not auto-queue fixer",
    )

    # Refuse installing local module 'app'
    app_run = models.create_run(ws, None, "appfail", provider="local_qwen", mission_id=mission_id, stage_name="Run tests")
    graph.record_attempt(tester["id"], app_run, "retry")
    app_payload = dict(payload)
    models.update_run(
        app_run,
        status="failed",
        agent_payload=json.dumps(app_payload),
        finished_at=models._now(),
        error=f"Verification command exited 1: {app_err}",
    )
    graph.sync_run_status(app_run)
    no_offer = dependency_recovery.attach_install_offer(app_run, str(REPO))
    check(no_offer is None, "local module app does not produce install offer")
    view = verification_display.verification_view(models.get_run(app_run), repo_path=str(REPO))
    check(view["classification"] == "import_context_failure", "app failure classified as import context")
    check(view.get("cwd") == str(REPO), "verification view includes working directory")
    check(view.get("installable") is False, "app failure is not installable")

    # Fixer context includes cwd + import guidance
    fixer = mad_scientist._fixer_step(
        {"name": "Run tests", "task": "run tests", "provider": "local_qwen"},
        models.get_run(app_run),
        1,
        3,
    )
    check("working_directory" in fixer["task"], "fixer receives working directory")
    check("import/execution-context" in fixer["task"] or "local project" in fixer["task"],
          "fixer receives import-context guidance")
    check("Do NOT treat it as a pip package" in fixer["task"] or "must not" in fixer["task"].lower()
          or "Do not invent a pip install" in fixer["task"],
          "fixer told not to pip-install local modules")

    prior = __import__("run_chain").build_prior_context(app_run)
    check(prior and "working_directory" in prior, "prior context includes verification cwd/diagnostics")

    # Refusing install of app via API helper
    try:
        dependency_recovery.run_approved_install(mission_id, tester["id"], module="app")
        check(False, "install of local app should raise")
    except ValueError as exc:
        check("local" in str(exc).lower() or "refusing" in str(exc).lower(),
              "installation remains refused for local modules")

    # UI surfaces install controls for pytest offer
    graph.block_step(tester["id"], "Missing dependency 'pytest'")
    graph.block_mission(mission_id, "Missing dependency 'pytest'")
    models.update_mission(mission_id, status=graph.BLOCKED_HUMAN, note="Missing pytest")
    dependency_recovery.attach_install_offer(tr, str(REPO))
    import app as tank_app
    slug = models.get_workspace_by_id(ws)["slug"]
    html = tank_app.app.test_client().get(f"/workspaces/{slug}").get_data(as_text=True)
    check("Install pytest and retry" in html or "Install pytest" in html,
          "UI shows Install pytest and retry")
    check("Working directory" in html or str(REPO) in html,
          "UI shows working directory on verification failure")
    check("import / execution context" in html or "import_context" in html or "local import" in html.lower()
          or "Missing module" in html,
          "UI shows classification / missing-module diagnostics")


def main():
    models.init_db()
    test_bulk_approval_mission_scoped()
    test_debrief_and_arsenal()
    test_resume_blocked_preserves_history()
    test_module_classification_and_install_gate()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s):")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("all observability checks passed")
    return 0



if __name__ == "__main__":
    sys.exit(main())

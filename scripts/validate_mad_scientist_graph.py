"""Lightweight Mad Scientist graph validation.

Runs against a temporary SQLite database and does not call any real model
provider. Intended for local smoke checks after graph scheduler changes.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import types


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Keep this script useful even from a bare Python that has not installed
# runtime dependencies yet. The validation uses direct DB rows, not YAML IO.
fake_yaml = types.ModuleType("yaml")
fake_yaml.safe_load = lambda *a, **k: {}
fake_yaml.dump = lambda *a, **k: ""
sys.modules.setdefault("yaml", fake_yaml)


def _payload(summary: str, patches: list[dict] | None = None) -> str:
    return json.dumps({
        "response_type": "patch" if patches else "plan",
        "summary": summary,
        "plan": "",
        "patches": patches or [],
        "post_actions": {},
    })


def _plan_payload(plan) -> str:
    return json.dumps({
        "response_type": "plan",
        "summary": "scouted",
        "plan": plan,
        "patches": [],
        "post_actions": {},
    })


def _seed_workspace(base, repo):
    import models

    with models.get_db() as conn:
        conn.executescript(models.SCHEMA)
        models._migrate_db(conn)
        now = models._now()
        conn.execute(
            """
            INSERT INTO workspaces
                (slug, name, repo_path, default_provider, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("demo", "Demo", str(repo), "local", now),
        )


def _fresh_env():
    base = pathlib.Path(tempfile.mkdtemp())
    repo = base / "repo"
    repo.mkdir()
    (repo / "app.py").write_text('print("hello")\n', encoding="utf-8")
    os.environ["TANK_DATA_DIR"] = str(base / "data")
    return base, repo


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp)
        repo = base / "repo"
        repo.mkdir()
        (repo / "app.py").write_text('print("hello")\n', encoding="utf-8")

        os.environ["TANK_DATA_DIR"] = str(base / "data")

        import config
        import mad_scientist_graph
        import models
        import session_manager

        config.DATA_DIR = str(base / "data")
        config.DB_PATH = str(base / "data" / "tank.db")
        config.LOGS_DIR = str(base / "data" / "logs")
        os.makedirs(config.LOGS_DIR, exist_ok=True)

        _seed_workspace(base, repo)

        session_manager.launch_pending = lambda: None
        ws = models.get_workspace("demo")

        normal_run_id = models.create_run(ws["id"], None, "Normal run still queues", provider="local")
        assert models.get_run(normal_run_id)["status"] == "pending"

        crew_run_id = models.create_adhoc_crew_run(ws["id"], "Crew parent still creates")
        assert models.get_run(crew_run_id)["provider"] == "crew_builder"

        legacy_id = mad_scientist_graph.start_mission(
            ws["id"],
            "Build two pieces then integrate",
            provider="local",
            max_steps=5,
            max_fix_loops=2,
        )
        gm = models.get_mad_scientist_mission_by_legacy(legacy_id)
        assert gm is not None

        scout = models.list_mad_scientist_attempts(gm["id"])[0]
        plan = {
            "summary": "Parallel DAG",
            "project_profile": {
                "project_type": "python",
                "important_paths": ["app.py"],
                "test_commands": ["pytest -q"],
            },
            "steps": [
                {"name": "Build A", "role": "builder", "provider": "local", "task": "A", "write_allowed": True},
                {"name": "Build B", "role": "builder", "provider": "local", "task": "B", "write_allowed": True},
                {
                    "name": "Integrate",
                    "role": "integrator",
                    "provider": "local",
                    "task": "Combine",
                    "depends_on": ["Build A", "Build B"],
                    "write_allowed": True,
                },
            ],
        }
        models.update_run(
            scout["run_id"],
            status="done",
            agent_payload=_plan_payload(json.dumps(plan)),
        )
        mad_scientist_graph.advance_mission(scout["run_id"])

        steps = models.list_mad_scientist_steps(gm["id"])
        assert [s["name"] for s in steps] == ["Build A", "Build B", "Integrate"]
        deps = models.list_mad_scientist_dependencies(gm["id"])
        assert [(d["depends_on_name"], d["step_name"]) for d in deps] == [
            ("Build A", "Integrate"),
            ("Build B", "Integrate"),
        ]

        attempts = models.list_mad_scientist_attempts(gm["id"])
        roots = [a for a in attempts if a["attempt_type"] == "initial"]
        assert [a["step_name"] for a in roots] == ["Build A", "Build B"]

        build_a, build_b = roots
        models.update_run(build_a["run_id"], status="done", agent_payload=_payload("A done", [{"path": "a.txt"}]))
        mad_scientist_graph.advance_mission(build_a["run_id"])
        attempts = models.list_mad_scientist_attempts(gm["id"])
        assert [a["step_name"] for a in attempts if a["attempt_type"] == "initial"] == ["Build A", "Build B"]

        models.update_run(build_b["run_id"], status="done", agent_payload=_payload("B done", [{"path": "b.txt"}]))
        mad_scientist_graph.advance_mission(build_b["run_id"])
        attempts = models.list_mad_scientist_attempts(gm["id"])
        integrate = [a for a in attempts if a["step_name"] == "Integrate"][0]
        integrate_run = models.get_run(integrate["run_id"])
        assert integrate_run["parent_run_id"] == build_b["run_id"]
        assert "Dependency step: Build A" in integrate_run["task"]
        assert "Dependency step: Build B" in integrate_run["task"]

        models.update_run(integrate["run_id"], status="failed")
        mad_scientist_graph.advance_mission(integrate["run_id"])
        attempts = models.list_mad_scientist_attempts(gm["id"])
        fixer = [a for a in attempts if a["step_name"] == "Integrate" and a["attempt_type"] == "fixer"][0]
        assert fixer["step_id"] == integrate["step_id"]

        models.update_run(fixer["run_id"], status="done", agent_payload=_payload("fixed", [{"path": "app.py"}]))
        mad_scientist_graph.advance_mission(fixer["run_id"])
        attempts = models.list_mad_scientist_attempts(gm["id"])
        retry = [a for a in attempts if a["step_name"] == "Integrate" and a["attempt_type"] == "retry"][0]
        retry_run = models.get_run(retry["run_id"])
        assert retry["step_id"] == integrate["step_id"]
        assert retry_run["parent_run_id"] == fixer["run_id"]

        models.update_run(retry["run_id"], status="done", agent_payload=_payload("integrated", [{"path": "app.py"}]))
        mad_scientist_graph.advance_mission(retry["run_id"])
        gm = models.get_mad_scientist_mission(gm["id"])
        assert gm["status"] == "done"

    # Scout parsing and failure visibility checks.
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp)
        repo = base / "repo"
        repo.mkdir()
        (repo / "app.py").write_text('print("hello")\n', encoding="utf-8")
        os.environ["TANK_DATA_DIR"] = str(base / "data")

        import config
        import mad_scientist_graph
        import models
        import session_manager

        config.DATA_DIR = str(base / "data")
        config.DB_PATH = str(base / "data" / "tank.db")
        config.LOGS_DIR = str(base / "data" / "logs")
        os.makedirs(config.LOGS_DIR, exist_ok=True)
        _seed_workspace(base, repo)
        session_manager.launch_pending = lambda: None
        ws = models.get_workspace("demo")

        def start_and_finish_scout(plan_text=None, status="done", error=None, log_text=None):
            legacy_id = mad_scientist_graph.start_mission(ws["id"], "Scout hardening", provider="local")
            gm = models.get_mad_scientist_mission_by_legacy(legacy_id)
            scout = models.list_mad_scientist_attempts(gm["id"])[0]
            run = models.get_run(scout["run_id"])
            log_path = os.path.join(config.LOGS_DIR, f"run-{run['id']}.log")
            if log_text is not None:
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(log_text)
            fields = {"status": status, "log_path": log_path}
            if error:
                fields["error"] = error
            if plan_text is not None:
                fields["agent_payload"] = _plan_payload(plan_text)
            models.update_run(run["id"], **fields)
            mad_scientist_graph.advance_mission(run["id"])
            return models.get_mad_scientist_mission(gm["id"]), run["id"]

        gm, _ = start_and_finish_scout(plan_text="")
        assert gm["status"] == "blocked"
        assert "Scout did not return valid execution-plan JSON" in gm["blocked_reason"]

        gm, _ = start_and_finish_scout(plan_text="I inspected the repo and would build two files.")
        assert gm["status"] == "blocked"
        assert "Response excerpt" in gm["blocked_reason"]

        fenced_plan = {
            "summary": "Fence OK",
            "project_profile": {"project_type": "python"},
            "steps": [{"name": "Build", "role": "builder", "task": "Build", "write_allowed": True}],
        }
        gm, _ = start_and_finish_scout(plan_text=f"```json\n{json.dumps(fenced_plan)}\n```")
        assert gm["status"] == "running"
        assert models.list_mad_scientist_steps(gm["id"])[0]["name"] == "Build"

        gm, failed_run_id = start_and_finish_scout(
            status="failed",
            error="HTTP 401 Unauthorized",
            log_text="[tank] provider error: provider=local type=local_agent model=x base_url=http://127.0.0.1\n[tank] exception: ValueError: bad key\n",
        )
        assert gm["status"] == "failed"
        assert "HTTP 401 Unauthorized" in gm["blocked_reason"]
        entries = mad_scientist_graph.graph_entries_for_workspace(ws["id"])
        failed_entry = [entry for entry in entries if entry["mission"]["id"] == gm["id"]][0]
        assert failed_entry["latest_scout_attempt"]["run_id"] == failed_run_id
        assert "bad key" in failed_entry["latest_scout_attempt"]["failure_reason"]

    print("Mad Scientist graph validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

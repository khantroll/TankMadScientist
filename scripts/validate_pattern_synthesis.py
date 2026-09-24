#!/usr/bin/env python3
"""Validators for Lab pattern synthesis (Reuse → Adapt → Generate).

Proves a fitting crew is reused, a generated crew records the rejected
closest match, an unconfirmed crew stays on the mission, a confirmed crew
lands in durable workspace memory, and Lab-staffed steps are ordinary
Mission Control attempts. Does not call a model or bind a port.
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

TMP = tempfile.mkdtemp(prefix="tank-pattern-")
REPO = Path(TMP) / "repo"
REPO.mkdir()
(REPO / "README.md").write_text("pattern synthesis validation repo\n", encoding="utf-8")
WORKSPACES = Path(TMP) / "workspaces.yaml"
WORKSPACES.write_text("workspaces: []\n", encoding="utf-8")
PROVIDERS = Path(TMP) / "providers.yaml"

os.environ["TANK_DATA_DIR"] = TMP
os.environ["TANK_WORKSPACES_FILE"] = str(WORKSPACES)
os.environ["TANK_PROVIDERS_FILE"] = str(PROVIDERS)
os.environ["TANK_MISSION_TEMPLATES_FILE"] = str(ROOT / "mission_templates.yaml")
os.environ["TANK_MAX_PARALLEL_RUNS"] = "0"
os.environ["TANK_MAX_PARALLEL_RUNS_PER_MISSION"] = "0"
os.environ["TANK_DEFAULT_PROVIDER"] = "local_qwen"

import yaml  # noqa: E402

import config  # noqa: E402
import mad_scientist  # noqa: E402
import mad_scientist_graph as graph  # noqa: E402
import mission  # noqa: E402
import models  # noqa: E402
import pattern_synthesis  # noqa: E402
import providers  # noqa: E402
import session_manager  # noqa: E402

FAILURES: list[str] = []
GOAL = "Add JWT bearer auth"


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"ok: {label}")
        return
    FAILURES.append(label)
    print(f"FAIL: {label}")


def write_providers(doc: dict) -> None:
    PROVIDERS.write_text(yaml.safe_dump(doc), encoding="utf-8")
    providers.load_providers()


def leaf_providers() -> dict:
    return {
        "local_qwen": {"type": "local_agent", "label": "Local Qwen"},
        "openrouter_agent": {"type": "local_agent", "label": "OpenRouter agent"},
    }


def powershell_crew() -> dict:
    return {
        "type": "crew",
        "label": "Powershell_expert",
        "process": "sequential",
        "agents": [
            {"provider": "openrouter_agent", "role_slug": "powershell_expert_step_1"},
        ],
    }


def coding_crew() -> dict:
    return {
        "type": "crew",
        "label": "Coding Crew",
        "process": "sequential",
        "agents": [
            {"provider": "local_qwen", "role_slug": "builder"},
            {"provider": "local_qwen", "role_slug": "tester"},
        ],
    }


def broad_crew() -> dict:
    return {
        "type": "crew",
        "label": "Broad Coding Crew",
        "process": "sequential",
        "agents": [
            {
                "provider": "openrouter_agent",
                "role_slug": "builder",
                "tools": ["read_file", "search", "write_file", "shell", "web"],
            },
            {
                "provider": "openrouter_agent",
                "role_slug": "tester",
                "tools": ["read_file", "search", "run_tests", "shell"],
            },
            {"provider": "openrouter_agent", "role_slug": "reviewer"},
        ],
    }


def role(slug: str, name: str, **extra) -> dict:
    item = {
        "slug": slug,
        "name": name,
        "system_prompt": extra.get("system_prompt") or f"You are {name}.",
        "goal": extra.get("goal") or "",
        "tools": extra.get("tools") or [],
        "provider": extra.get("provider") or "",
        "source": extra.get("source") or "agent_role",
    }
    return item


def crew(name: str, agents: list[dict], source: str = "provider") -> dict:
    return pattern_synthesis._crew_from_provider({
        "id": "crew_" + pattern_synthesis._slugify(name),
        "label": name,
        "agents": agents,
    }) | {"source": source}


def plan_steps() -> list[dict]:
    return [
        {
            "name": "Implement",
            "role": "builder",
            "provider": "local_qwen",
            "task": "implement jwt bearer checks",
            "depends_on": [],
            "write_allowed": True,
            "success_criteria": ["code"],
        },
        {
            "name": "Verify",
            "role": "tester",
            "provider": "local_qwen",
            "task": "verify jwt bearer auth",
            "depends_on": ["Implement"],
            "write_allowed": False,
            "success_criteria": ["tests"],
        },
    ]


def add_role(workspace_id: int, slug: str, name: str, tools=None) -> int:
    with models.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO agent_roles
                (workspace_id, slug, name, system_prompt, goal, tools, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                slug,
                name,
                f"You are {name}.",
                "",
                json.dumps(tools) if tools else None,
                models._now(),
            ),
        )
        return cur.lastrowid


def scout_plan(workspace_id: int, goal: str = GOAL, steps: list[dict] | None = None) -> int:
    mission_id = mad_scientist.start_mission(
        workspace_id,
        goal,
        provider="local_qwen",
        max_steps=4,
        max_fix_loops=3,
    )
    scout = next(
        run for run in models.list_mission_runs(mission_id)
        if run["stage_name"] == mad_scientist.SCOUT_STAGE
    )
    payload = {
        "response_type": "plan",
        "summary": "jwt plan",
        "plan": json.dumps({"summary": "jwt plan", "steps": steps if steps is not None else plan_steps()}),
    }
    models.update_run(
        scout["id"],
        status="done",
        agent_payload=json.dumps(payload),
        finished_at=models._now(),
    )
    mad_scientist.advance_mission(scout["id"])
    return mission_id


def runs_named(mission_id: int, stage: str):
    return [
        run for run in models.list_mission_runs(mission_id)
        if run["stage_name"] == stage
    ]


def decision_catalog() -> None:
    fitting = crew("Coding Crew", coding_crew()["agents"])
    unrelated = crew("Powershell_expert", powershell_crew()["agents"])
    catalog = {
        "roles": [role("builder", "Builder"), role("tester", "Tester")],
        "crews": [unrelated, fitting],
    }
    decision = pattern_synthesis.synthesize(1, GOAL, plan_steps(), "local_qwen", catalog)
    check(decision["crew"]["action"] == "reuse", "reuse is chosen when a fitting crew exists")
    check(decision["crew"]["name"] == "Coding Crew", "the fitting crew is the one reused")
    check(
        decision["crew"]["confirmation"] == "not_required",
        "a reused crew does not ask for confirmation",
    )
    check(
        "Powershell_expert" in decision["inspected"]["crews"][0]["name"]
        or any(item["name"] == "Powershell_expert" for item in decision["inspected"]["crews"]),
        "synthesis records the crews it inspected",
    )

    broad = crew("Broad Coding Crew", broad_crew()["agents"])
    adapted = pattern_synthesis.synthesize(
        1, GOAL, plan_steps(), "local_qwen", {"roles": [], "crews": [broad, unrelated]}
    )
    changes = " ".join(adapted["crew"]["changes"])
    check(adapted["crew"]["action"] == "adapt", "a broader covering crew is adapted")
    check("reviewer" in changes, "adapt records the dropped role")
    check("openrouter_agent" in changes, "adapt records the narrowed provider")
    check("shell" in changes, "adapt records the narrowed tools")
    check(adapted["crew"]["confirmation"] == "not_required", "an adapted crew is not a new durable default")

    generated = pattern_synthesis.synthesize(
        1,
        GOAL,
        plan_steps(),
        "local_qwen",
        {"roles": [role("reviewer", "Reviewer")], "crews": [unrelated]},
    )
    rationale = generated["crew"]["rationale"]
    check(generated["crew"]["action"] == "generate", "generate is used when reuse and adapt fit poorly")
    check("Powershell_expert" in rationale, "generate names the closest rejected crew")
    check("Rejected closest crew" in rationale, "generate rationale says the closest crew was rejected")
    check(generated["crew"]["confirmation"] == "pending", "a generated crew waits for confirmation")
    check(generated["crew"]["tools"], "a generated crew carries tools")
    check(
        generated["crew"]["provider_preferences"]["by_role"].get("builder") == "local_qwen",
        "a generated crew carries provider preferences",
    )
    check(generated["crew"]["roles"], "a generated crew carries roles")
    check(
        generated["specialists"]["builder"]["action"] == "generate",
        "a specialist is generated when no role fits",
    )
    check(
        "Reviewer" in generated["specialists"]["builder"]["rationale"],
        "a generated specialist names the closest rejected role",
    )

    half = crew("Half Crew", [{"provider": "local_qwen", "role_slug": "builder"}])
    half_decision = pattern_synthesis.synthesize(
        1, GOAL, plan_steps(), "local_qwen", {"roles": [], "crews": [half]}
    )
    check(half_decision["crew"]["action"] == "generate", "a partial role cover is not adapted into a fit")
    check("Half Crew" in half_decision["crew"]["rationale"], "partial cover is the named rejection")

    empty = pattern_synthesis.synthesize(1, GOAL, plan_steps(), "local_qwen", {"roles": [], "crews": []})
    check(empty["crew"]["action"] == "generate", "an empty catalog generates")
    check(
        "No existing crew or role" in empty["crew"]["rationale"],
        "an empty catalog says nothing was close enough to name",
    )

    reused_inside = pattern_synthesis.synthesize(
        1,
        GOAL,
        [plan_steps()[0]],
        "local_qwen",
        {"roles": [role("builder", "Builder")], "crews": []},
    )
    check(
        reused_inside["crew"]["action"] == "generate"
        and reused_inside["specialists"]["builder"]["action"] == "reuse",
        "a fitting specialist is reused inside a generated crew",
    )


def main() -> int:
    models.init_db()
    decision_catalog()

    write_providers({"default_provider": "local_qwen", "providers": {**leaf_providers(), "crew_powershell_expert": powershell_crew()}})
    slug = models.create_workspace("Pattern A", str(REPO), slug="pattern-a", default_provider="local_qwen")
    ws = models.get_workspace(slug)
    add_role(ws["id"], "reviewer", "Reviewer")

    mission_a = scout_plan(ws["id"])
    view = pattern_synthesis.public_view(mission_a)
    check(view is not None and view["action"] == "generate", "a lab mission generates when nothing fits")
    check(view["needs_confirmation"], "the generated crew is waiting for a person")
    check("Powershell_expert" in view["rationale"], "the mission records the rejected crew")
    check(
        all(item["name"] != view["name"] for item in pattern_synthesis.catalog_preview(ws["id"])["crews"]),
        "an unconfirmed crew is absent from the reuse catalog",
    )
    check(pattern_synthesis.durable_crews(ws["id"]) == [], "an unconfirmed crew is not durable memory")

    implement = runs_named(mission_a, "Implement")
    check(len(implement) == 1, "the generated builder step is one Mission Control run")
    run = models.get_run(implement[0]["id"])
    attempt = graph.get_attempt_by_run(run["id"])
    check(attempt is not None and attempt["attempt_kind"] == "execute", "the run is a graph execute attempt")
    check(run["status"] == "pending", "Lab queues the attempt instead of executing it")
    check(run["provider"] == "local_qwen", "the attempt uses a Mission Control provider")
    check(run["role_id"] is None, "a generated specialist does not attach an unrelated saved role")
    persona = json.loads(run["persona_override"])
    check(bool(persona.get("system_prompt")), "the attempt binds the generated specialist prompt")
    check("run_tests" in (persona.get("tools") or "") or "write_file" in persona.get("tools", ""), "the attempt binds generated tools")

    models.update_run(run["id"], status="failed", error="jwt build failed", finished_at=models._now())
    mad_scientist.advance_mission(run["id"])
    step = next(item for item in graph.list_steps(graph.get_by_mission_id(mission_a)["id"]) if item["name"] == "Implement")
    kinds = [item["attempt_kind"] for item in graph.list_attempts(step["id"])]
    check(kinds == ["execute", "fixer"], "a failed lab step retries through a Mission Control fixer attempt")
    fixer = models.get_run(graph.list_attempts(step["id"])[-1]["run_id"])
    check(fixer["provider"] == "local_qwen" and fixer["persona_override"], "the fixer attempt keeps the specialist binding")

    import app as tank_app

    client = tank_app.app.test_client()
    page = client.get(f"/workspaces/{slug}")
    check(page.status_code == 200, "workspace page renders")
    check(b"Confirm as reusable" in page.data, "the mission card offers Confirm as reusable")
    check(b"Use for this mission only" in page.data, "the mission card offers Use for this mission only")
    check(b"Powershell_expert" in page.data, "the mission card shows the rejection rationale")
    check(b"Reuse candidates Lab will inspect" in page.data, "the Lab form lists reuse candidates")

    posted = client.post(
        f"/workspaces/{slug}/mad-scientist/{mission_a}/crew-confirmation",
        data={"decision": "mission_only"},
    )
    check(posted.status_code == 200, "mission-only confirmation returns the mission list")
    check(b"Using this crew for this mission only" in posted.data, "the card records mission-only")
    check(pattern_synthesis.public_view(mission_a)["mission_only"], "the posted choice is mission-only")
    check(pattern_synthesis.durable_crews(ws["id"]) == [], "mission-only does not write durable memory")
    check(
        pattern_synthesis.binding_for_step(mission_a, {"role": "builder"})["persona_override"],
        "a mission-only crew still binds this mission's attempts",
    )

    mission_b = scout_plan(ws["id"])
    pending = pattern_synthesis.public_view(mission_b)
    check(pending["action"] == "generate" and pending["needs_confirmation"], "the next mission generates until one is confirmed")
    confirmed_post = client.post(
        f"/workspaces/{slug}/mad-scientist/{mission_b}/crew-confirmation",
        data={"decision": "confirm"},
    )
    check(b"Confirmed as a reusable crew in workspace memory" in confirmed_post.data, "the card records durable confirmation")
    confirmed = pattern_synthesis.public_view(mission_b)
    durable = pattern_synthesis.durable_crews(ws["id"])
    check(confirmed["confirmed"], "confirm marks the crew reusable")
    check(len(durable) == 1, "confirm writes one durable crew")
    saved = durable[0]
    check(saved["name"] and saved["roles"] and saved["tools"], "durable crew keeps name, roles, and tools")
    check(saved["provider_preferences"]["by_role"], "durable crew keeps provider preferences")
    check("Powershell_expert" in saved["rationale"], "durable crew keeps the rejection rationale")
    check(
        any(item["name"] == saved["name"] for item in pattern_synthesis.catalog_preview(ws["id"])["crews"]),
        "a confirmed crew is a later reuse candidate",
    )

    preview_roles = {item["slug"] for item in pattern_synthesis.catalog_preview(ws["id"])["roles"]}
    check("reviewer" in preview_roles, "human-configured roles stay in the catalog")

    mission_c = scout_plan(ws["id"])
    reused = pattern_synthesis.public_view(mission_c)
    check(reused["action"] == "reuse" and reused["name"] == saved["name"], "a later mission reuses the confirmed crew")
    check(len(pattern_synthesis.durable_crews(ws["id"])) == 1, "reuse does not append another durable crew")

    try:
        pattern_synthesis.set_crew_confirmation(mission_b, ws["id"], "mission_only")
        check(False, "a confirmed crew cannot be demoted")
    except ValueError:
        check(True, "a confirmed crew cannot be demoted")

    other = models.create_workspace("Pattern Other", str(REPO), slug="pattern-other", default_provider="local_qwen")
    other_ws = models.get_workspace(other)
    try:
        pattern_synthesis.set_crew_confirmation(mission_c, other_ws["id"], "confirm")
        check(False, "confirmation is workspace scoped")
    except ValueError:
        check(True, "confirmation is workspace scoped")

    write_providers({"default_provider": "local_qwen", "providers": {**leaf_providers(), "crew_broad_coding": broad_crew()}})
    broad_slug = models.create_workspace("Pattern Broad", str(REPO), slug="pattern-broad", default_provider="local_qwen")
    broad_ws = models.get_workspace(broad_slug)
    broad_mission = scout_plan(broad_ws["id"])
    broad_view = pattern_synthesis.public_view(broad_mission)
    broad_changes = " ".join(broad_view["changes"])
    check(broad_view["action"] == "adapt", "the live catalog adapts a broader crew")
    check("reviewer" in broad_changes and "shell" in broad_changes, "the live adapt records what changed")
    check(pattern_synthesis.durable_crews(broad_ws["id"]) == [], "an adapted crew stays off durable memory")
    check(len(pattern_synthesis.durable_crews(ws["id"])) == 1, "adapting elsewhere leaves confirmed memory in place")

    write_providers({
        "default_provider": "local_qwen",
        "providers": {
            **leaf_providers(),
            "crew_coding": coding_crew(),
            "crew_powershell_expert": powershell_crew(),
        },
    })
    fit_slug = models.create_workspace("Pattern Fit", str(REPO), slug="pattern-fit", default_provider="local_qwen")
    fit_ws = models.get_workspace(fit_slug)
    builder_id = add_role(fit_ws["id"], "builder", "Builder")
    add_role(fit_ws["id"], "tester", "Tester")
    fit_mission = scout_plan(fit_ws["id"])
    fit_view = pattern_synthesis.public_view(fit_mission)
    check(fit_view["action"] == "reuse" and fit_view["name"] == "Coding Crew", "a live fitting crew is reused ahead of an unrelated one")
    fit_run = models.get_run(runs_named(fit_mission, "Implement")[0]["id"])
    check(fit_run["role_id"] == builder_id, "reuse binds the saved builder role on the attempt")
    check(not fit_run["persona_override"], "reuse does not replace the saved role prompt")
    check(fit_run["status"] == "pending" and fit_run["provider"] == "local_qwen", "a reused crew still schedules a Mission Control attempt")

    yaml_id = mission.start_mission(fit_ws["id"], "plan_build_test_fix", "Keep the human template", provider="local_qwen")
    yaml_mission = models.get_mission(yaml_id)
    yaml_runs = models.list_mission_runs(yaml_id)
    check(yaml_mission["template"] == "plan_build_test_fix", "a human template mission still starts")
    check(len(yaml_runs) == 1 and yaml_runs[0]["status"] == "pending", "a human template mission queues a normal run")
    check(graph.get_by_mission_id(yaml_id) is None, "a human template mission does not open a Lab graph")
    check(pattern_synthesis.load_synthesis(yaml_id) is None, "Lab synthesis stays off the human template path")
    check("pattern_synthesis" not in (ROOT / "mission.py").read_text(encoding="utf-8"), "Mission Control templates do not import Lab synthesis")

    adhoc = models.create_adhoc_crew_run(fit_ws["id"], "human crew task")
    adhoc_run = models.get_run(adhoc)
    check(adhoc_run["provider"] == "crew_builder", "human crew launch still creates a crew_builder run")
    check("launch_crew_builder" in (ROOT / "app.py").read_text(encoding="utf-8"), "the human crew route still uses the crew builder")

    merged = session_manager.merge_persona_override(
        {"system_prompt": "saved", "goal": "saved goal", "tools": "shell"},
        json.dumps({"tools": "read_file, search", "system_prompt": "generated prompt"}),
    )
    check(merged["system_prompt"] == "generated prompt", "attempt persona can carry a generated system prompt")
    check(merged["tools"] == "read_file, search" and merged["goal"] == "saved goal", "persona override still replaces only the provided fields")

    source = (ROOT / "pattern_synthesis.py").read_text(encoding="utf-8")
    for banned in ("start_run", "launch_pending", "run_sequential_crew", "finalize_approved"):
        check(banned not in source, f"pattern synthesis does not call {banned}")

    architect_steps = [{
        "name": "Design",
        "role": "architect",
        "provider": "local_qwen",
        "task": "design refresh token rotation",
        "depends_on": [],
        "write_allowed": False,
        "success_criteria": ["design"],
    }]
    fresh = scout_plan(ws["id"], goal="Ship refresh token rotation", steps=architect_steps)
    fresh_view = pattern_synthesis.public_view(fresh)
    check(
        fresh_view["action"] == "generate",
        "a plan the confirmed crew does not cover still generates",
    )
    before = [item["name"] for item in pattern_synthesis.durable_crews(ws["id"])]
    posted = client.post(
        f"/workspaces/{slug}/mad-scientist/{fresh}/crew-confirmation",
        data={"decision": "mission_only"},
    )
    check(posted.status_code == 200, "a second mission-only post returns the mission list")
    check(pattern_synthesis.public_view(fresh)["mission_only"], "that generated crew stays mission scoped")
    check(
        fresh_view["name"] not in [item["name"] for item in pattern_synthesis.durable_crews(ws["id"])],
        "the mission-scoped crew is not added beside the confirmed one",
    )
    check(before == [item["name"] for item in pattern_synthesis.durable_crews(ws["id"])], "durable crews are unchanged by mission-only")

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("\npattern synthesis validation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

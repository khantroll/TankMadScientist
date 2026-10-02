"""Tank Arsenal: durable assets and learned lessons for a workspace.

Assets are derived from existing specialists, crews, roles, and memory.
Lessons are stored under workspace_memory key \"arsenal\" with provenance,
and also surface distilled facts from mad_scientist memory.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import models
import pattern_synthesis
import verification_display

ARSENAL_KEY = "arsenal"
MEMORY_KEY = "mad_scientist"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lesson_id(text: str, category: str) -> str:
    digest = hashlib.sha256(f"{category}\n{text.strip().lower()}".encode("utf-8")).hexdigest()
    return digest[:16]


def _load_store(workspace_id: int) -> dict:
    raw = models.get_workspace_memory(workspace_id, ARSENAL_KEY) or {}
    if not isinstance(raw, dict):
        return {"lessons": [], "asset_stats": {}}
    lessons = raw.get("lessons") if isinstance(raw.get("lessons"), list) else []
    stats = raw.get("asset_stats") if isinstance(raw.get("asset_stats"), dict) else {}
    return {"lessons": lessons, "asset_stats": stats}


def _save_store(workspace_id: int, store: dict) -> None:
    models.upsert_workspace_memory(workspace_id, ARSENAL_KEY, store)


def record_lesson(
    workspace_id: int,
    text: str,
    category: str,
    *,
    mission_id: int | None = None,
    run_id: int | None = None,
    source: str = "runtime",
    details: dict | None = None,
) -> dict | None:
    """Upsert a lesson. Duplicate text+category updates last_seen / use_count."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return None
    store = _load_store(workspace_id)
    lesson_id = _lesson_id(cleaned, category)
    now = _now()
    for item in store["lessons"]:
        if item.get("id") == lesson_id:
            item["use_count"] = int(item.get("use_count") or 1) + 1
            item["last_seen_at"] = now
            item["updated_at"] = now
            if mission_id is not None:
                item["mission_id"] = mission_id
            if run_id is not None:
                item["run_id"] = run_id
            if details:
                item["details"] = {**(item.get("details") or {}), **details}
            _save_store(workspace_id, store)
            return item
    entry = {
        "id": lesson_id,
        "text": cleaned,
        "category": category,
        "source": source,
        "mission_id": mission_id,
        "run_id": run_id,
        "details": details or {},
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
        "use_count": 1,
    }
    store["lessons"].insert(0, entry)
    store["lessons"] = store["lessons"][:200]
    _save_store(workspace_id, store)
    return entry


def touch_asset(
    workspace_id: int,
    asset_key: str,
    *,
    success: bool = False,
    mission_id: int | None = None,
) -> None:
    store = _load_store(workspace_id)
    stats = store["asset_stats"]
    row = stats.get(asset_key) or {
        "use_count": 0,
        "last_used_at": None,
        "last_success_at": None,
        "last_mission_id": None,
    }
    now = _now()
    row["use_count"] = int(row.get("use_count") or 0) + 1
    row["last_used_at"] = now
    row["last_mission_id"] = mission_id
    if success:
        row["last_success_at"] = now
    stats[asset_key] = row
    store["asset_stats"] = stats
    _save_store(workspace_id, store)


def record_verification_lesson(
    workspace_id: int,
    run,
    *,
    mission_id: int | None = None,
    blocked_reason: str | None = None,
) -> None:
    """Derive durable lessons from a failed verification or circuit break."""
    details = verification_display.verification_view(run) or {}
    command = details.get("command")
    interpreter = details.get("interpreter")
    snippet = details.get("snippet") or (run["error"] if run is not None else "") or ""
    classification = details.get("classification") or "unknown"
    run_id = run["id"] if run is not None else None

    if interpreter:
        record_lesson(
            workspace_id,
            f"Python interpreter on this host: {interpreter}",
            "environment",
            mission_id=mission_id,
            run_id=run_id,
            source="verification",
            details={"interpreter": interpreter},
        )
    module = details.get("missing_module")
    if classification == "missing_dependency" and (snippet or module):
        if module:
            record_lesson(
                workspace_id,
                f"{module} is not installed in the active Python environment",
                "environment",
                mission_id=mission_id,
                run_id=run_id,
                source="verification",
                details={
                    "command": command,
                    "module": module,
                    "install_command": details.get("install_command"),
                    "interpreter": interpreter,
                },
            )
            if module.lower() == "pytest":
                record_lesson(
                    workspace_id,
                    "invoke Python verification through the interpreter (python -m pytest) when pytest is available",
                    "verification",
                    mission_id=mission_id,
                    run_id=run_id,
                    source="verification",
                    details={"command": command},
                )
        else:
            record_lesson(
                workspace_id,
                f"Missing dependency during verification: {snippet[:240]}",
                "environment",
                mission_id=mission_id,
                run_id=run_id,
                source="verification",
                details={"command": command, "classification": classification},
            )
    elif classification == "import_context_failure":
        record_lesson(
            workspace_id,
            (
                f"'{module}' in this repository is a local project module and must not "
                "be treated as an external package candidate"
                if module else
                "Verification failed with a local import/execution-context error"
            ),
            "repo",
            mission_id=mission_id,
            run_id=run_id,
            source="verification",
            details={
                "command": command,
                "cwd": details.get("cwd"),
                "module": module,
                "classification": classification,
                "snippet": snippet[:360] if snippet else None,
            },
        )
        if details.get("cwd"):
            record_lesson(
                workspace_id,
                f"Verification working directory: {details['cwd']}",
                "verification",
                mission_id=mission_id,
                run_id=run_id,
                source="verification",
                details={"cwd": details.get("cwd"), "command": command},
            )
    elif command and (classification in ("code_failure", "environment_failure", "unknown")):
        record_lesson(
            workspace_id,
            f"Verification command failed ({classification}): {command}",
            "verification",
            mission_id=mission_id,
            run_id=run_id,
            source="verification",
            details={
                "command": command,
                "exit_code": details.get("exit_code"),
                "snippet": snippet[:360] if snippet else None,
                "classification": classification,
            },
        )
    if blocked_reason:
        record_lesson(
            workspace_id,
            blocked_reason,
            "failure",
            mission_id=mission_id,
            run_id=run_id,
            source="circuit_breaker",
            details={"blocked_reason": blocked_reason},
        )


def record_synthesis_lessons(workspace_id: int, mission_id: int, synthesis: dict | None) -> None:
    if not synthesis:
        return
    action = synthesis.get("action")
    name = synthesis.get("name")
    rationale = (synthesis.get("rationale") or "").strip()
    if action == "generate" and rationale:
        record_lesson(
            workspace_id,
            rationale,
            "orchestration",
            mission_id=mission_id,
            source="pattern_synthesis",
            details={"crew_action": action, "crew_name": name},
        )
    if action == "reuse" and name:
        touch_asset(workspace_id, f"crew:{name}", success=True, mission_id=mission_id)
        record_lesson(
            workspace_id,
            f"JWT-style or similar missions reused crew {name}",
            "orchestration",
            mission_id=mission_id,
            source="pattern_synthesis",
            details={"crew_action": action, "crew_name": name},
        )
    for spec in synthesis.get("specialists") or []:
        spec_name = spec.get("name") or spec.get("role")
        if not spec_name:
            continue
        key = f"specialist:{spec_name}"
        touch_asset(
            workspace_id,
            key,
            success=spec.get("action") in ("reuse", "adapt", "generate"),
            mission_id=mission_id,
        )
        if spec.get("action") == "generate" and spec.get("closest_rejected"):
            record_lesson(
                workspace_id,
                f"Rejected closest specialist {spec['closest_rejected']} for role {spec.get('role')}",
                "orchestration",
                mission_id=mission_id,
                source="pattern_synthesis",
                details=spec,
            )


def _asset_from_role(role, stats: dict, origin: str) -> dict:
    name = role["name"] if not isinstance(role, dict) else role.get("name")
    slug = role["slug"] if not isinstance(role, dict) else role.get("slug")
    key = f"specialist:{name}"
    meta = stats.get(key) or {}
    tools = []
    raw_tools = role["tools"] if not isinstance(role, dict) else role.get("tools")
    if isinstance(raw_tools, str) and raw_tools.strip():
        try:
            tools = json.loads(raw_tools)
        except json.JSONDecodeError:
            tools = [raw_tools]
    elif isinstance(raw_tools, list):
        tools = raw_tools
    return {
        "key": key,
        "name": name,
        "type": "specialist",
        "description": (role["goal"] if not isinstance(role, dict) else role.get("goal")) or "",
        "origin": origin,
        "identifier": slug,
        "provider": (role["provider"] if not isinstance(role, dict) else role.get("provider")),
        "tools": tools,
        "generated_vs_reused": "catalog",
        "use_count": meta.get("use_count") or 0,
        "last_used_at": meta.get("last_used_at"),
        "last_success_at": meta.get("last_success_at"),
        "last_mission_id": meta.get("last_mission_id"),
    }


def _asset_from_crew(crew: dict, stats: dict, origin: str, generated_vs: str) -> dict:
    name = crew.get("name") or "crew"
    key = f"crew:{name}"
    meta = stats.get(key) or {}
    roles = []
    for member in crew.get("roles") or []:
        if isinstance(member, dict):
            roles.append(member.get("name") or member.get("slug") or "?")
        else:
            roles.append(str(member))
    return {
        "key": key,
        "name": name,
        "type": "crew",
        "description": crew.get("goal") or crew.get("description") or "",
        "origin": origin,
        "identifier": crew.get("id") or name,
        "provider": crew.get("provider"),
        "roles": roles,
        "generated_vs_reused": generated_vs,
        "use_count": meta.get("use_count") or 0,
        "last_used_at": meta.get("last_used_at"),
        "last_success_at": meta.get("last_success_at"),
        "last_mission_id": meta.get("last_mission_id"),
        "mission_id": crew.get("mission_id"),
    }


def list_assets(workspace_id: int) -> list[dict]:
    store = _load_store(workspace_id)
    stats = store["asset_stats"]
    assets = []
    seen = set()

    for role in models.list_roles(workspace_id):
        asset = _asset_from_role(role, stats, origin="workspaces.yaml / agent_roles")
        if asset["key"] not in seen:
            assets.append(asset)
            seen.add(asset["key"])

    for crew in pattern_synthesis.durable_crews(workspace_id):
        asset = _asset_from_crew(
            crew, stats, origin="workspace_memory.pattern_synthesis", generated_vs="confirmed"
        )
        if asset["key"] not in seen:
            assets.append(asset)
            seen.add(asset["key"])

    memory = models.get_workspace_memory(workspace_id, MEMORY_KEY) or {}
    test_command = memory.get("test_command")
    if test_command:
        key = f"recipe:{test_command}"
        meta = stats.get(key) or {}
        assets.append({
            "key": key,
            "name": "Verification recipe",
            "type": "verification_recipe",
            "description": str(test_command),
            "origin": "workspace_memory.mad_scientist",
            "identifier": test_command,
            "generated_vs_reused": "learned",
            "use_count": meta.get("use_count") or 0,
            "last_used_at": meta.get("last_used_at"),
            "last_success_at": meta.get("last_success_at"),
            "last_mission_id": meta.get("last_mission_id"),
        })

    profile = memory.get("project_profile") or {}
    framework = profile.get("framework") or profile.get("language")
    if framework:
        key = f"adapter:{framework}"
        meta = stats.get(key) or {}
        assets.append({
            "key": key,
            "name": f"Repo adapter · {framework}",
            "type": "repo_adapter",
            "description": json.dumps(profile, sort_keys=True)[:400],
            "origin": "workspace_memory.mad_scientist.project_profile",
            "identifier": str(framework),
            "generated_vs_reused": "learned",
            "use_count": meta.get("use_count") or 0,
            "last_used_at": meta.get("last_used_at"),
            "last_success_at": meta.get("last_success_at"),
            "last_mission_id": meta.get("last_mission_id"),
        })

    return assets


def list_lessons(workspace_id: int) -> list[dict]:
    store = _load_store(workspace_id)
    lessons = list(store["lessons"])
    memory = models.get_workspace_memory(workspace_id, MEMORY_KEY) or {}
    seen_ids = {item.get("id") for item in lessons}

    for pitfall in memory.get("known_pitfalls") or []:
        text = str(pitfall).strip()
        if not text:
            continue
        lesson_id = _lesson_id(text, "failure")
        if lesson_id in seen_ids:
            continue
        lessons.append({
            "id": lesson_id,
            "text": text,
            "category": "failure",
            "source": "mad_scientist_memory",
            "mission_id": None,
            "run_id": None,
            "details": {},
            "created_at": memory.get("updated_at"),
            "updated_at": memory.get("updated_at"),
            "last_seen_at": memory.get("updated_at"),
            "use_count": 1,
            "ephemeral": True,
        })
        seen_ids.add(lesson_id)

    for pattern in memory.get("prior_successful_patterns") or []:
        text = str(pattern).strip()
        if not text:
            continue
        lesson_id = _lesson_id(text, "orchestration")
        if lesson_id in seen_ids:
            continue
        lessons.append({
            "id": lesson_id,
            "text": text,
            "category": "orchestration",
            "source": "mad_scientist_memory",
            "mission_id": None,
            "run_id": None,
            "details": {},
            "created_at": memory.get("updated_at"),
            "updated_at": memory.get("updated_at"),
            "last_seen_at": memory.get("updated_at"),
            "use_count": 1,
            "ephemeral": True,
        })
        seen_ids.add(lesson_id)

    profile = memory.get("project_profile") or {}
    if profile:
        framework = profile.get("framework") or profile.get("stack") or profile.get("language")
        if framework:
            text = f"Repository framework/stack: {framework}"
            lesson_id = _lesson_id(text, "repo")
            if lesson_id not in seen_ids:
                lessons.append({
                    "id": lesson_id,
                    "text": text,
                    "category": "repo",
                    "source": "project_profile",
                    "details": {"project_profile": profile},
                    "created_at": memory.get("updated_at"),
                    "updated_at": memory.get("updated_at"),
                    "last_seen_at": memory.get("updated_at"),
                    "use_count": 1,
                    "ephemeral": True,
                })

    lessons.sort(key=lambda item: item.get("last_seen_at") or item.get("updated_at") or "", reverse=True)
    return lessons


def arsenal_view(workspace_id: int) -> dict:
    assets = list_assets(workspace_id)
    lessons = list_lessons(workspace_id)
    return {
        "assets": assets,
        "lessons": lessons,
        "asset_count": len(assets),
        "lesson_count": len(lessons),
    }

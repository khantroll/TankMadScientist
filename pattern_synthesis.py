"""Pattern synthesis for Mad Scientist Lab.

When a Lab plan needs specialists or a whole crew, choose in order:
reuse an existing fit, adapt one by narrowing prompt, tools, or provider,
or generate a new one. Generation is the leftover case.

This module only decides and records. It does not launch runs, call
providers, apply patches, or write durable workspace memory. A generated
crew stays on the mission until a person confirms it.
"""
from __future__ import annotations

import json
import re

import models
import providers

PATTERN_MEMORY_KEY = "pattern_synthesis"
REUSE_MIN = 0.80
ADAPT_MIN = 0.45

CANONICAL_ROLES = (
    "architect",
    "builder",
    "fixer",
    "integrator",
    "reviewer",
    "scout",
    "tester",
)

ROLE_TOOLS = {
    "architect": ["read_file", "search"],
    "builder": ["read_file", "search", "write_file"],
    "fixer": ["read_file", "search", "write_file"],
    "integrator": ["read_file", "search", "write_file"],
    "reviewer": ["read_file", "search"],
    "scout": ["read_file", "search"],
    "tester": ["read_file", "search", "run_tests"],
}

ROLE_BLURB = {
    "architect": "Shape the implementation approach for this step.",
    "builder": "Implement this step with the smallest concrete repository change.",
    "fixer": "Repair the failed step with the smallest concrete change.",
    "integrator": "Combine the finished parent steps into one coherent result.",
    "reviewer": "Review the parent steps and report concrete findings.",
    "scout": "Inspect the repository and return a grounded plan.",
    "tester": "Verify the parent steps with a real test, build, or diff command.",
}

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "into", "is", "it", "of", "on", "or", "over", "that", "the", "this",
    "to", "with", "when", "if", "add", "fix", "make", "create", "update",
    "implement", "using", "via",
}


def inspect_catalog(workspace_id: int) -> dict:
    """Roles and crews Lab may reuse. Unconfirmed mission drafts are omitted."""
    roles = []
    seen = set()
    for row in models.list_roles(workspace_id):
        item = _role_from_mapping(row, source="agent_role")
        roles.append(item)
        seen.add(item["slug"])

    crews = []
    for cfg in providers.list_crew_providers():
        crews.append(_crew_from_provider(cfg))

    memory = models.get_workspace_memory(workspace_id, PATTERN_MEMORY_KEY) or {}
    for saved in memory.get("crews") or []:
        if not isinstance(saved, dict):
            continue
        crews.append(_crew_from_memory(saved))
        for member in saved.get("roles") or []:
            slug = str(member.get("slug") or "").strip()
            if not slug or slug in seen:
                continue
            if member.get("action") == "reused":
                continue
            roles.append(_role_from_mapping(member, source="durable_role"))
            seen.add(slug)
    return {"roles": roles, "crews": crews}


def catalog_preview(workspace_id: int) -> dict:
    """Compact catalog for the Lab form."""
    catalog = inspect_catalog(workspace_id)
    return {
        "roles": [
            {"name": role["name"], "slug": role["slug"], "source": role["source"]}
            for role in catalog["roles"]
        ],
        "crews": [
            {
                "name": crew["name"],
                "id": crew["id"],
                "source": crew["source"],
                "roles": [member["slug"] for member in crew["roles"] if member.get("slug")],
            }
            for crew in catalog["crews"]
        ],
    }


def synthesize(
    workspace_id: int,
    goal: str,
    steps: list[dict],
    default_provider: str | None = None,
    catalog: dict | None = None,
) -> dict:
    """Choose reuse, adapt, or generate for the plan's crew and specialists."""
    catalog = catalog if catalog is not None else inspect_catalog(workspace_id)
    needs = _needs_from_steps(steps, default_provider)
    inspected = {
        "roles": [
            {"name": role["name"], "slug": role["slug"], "source": role["source"]}
            for role in catalog.get("roles") or []
        ],
        "crews": [
            {"name": crew["name"], "id": crew["id"], "source": crew["source"]}
            for crew in catalog.get("crews") or []
        ],
    }
    if not needs:
        return {
            "version": 1,
            "goal": goal,
            "crew": {
                "action": "none",
                "name": "",
                "slug": "",
                "roles": [],
                "tools": [],
                "provider_preferences": {"default": default_provider or "", "by_role": {}},
                "rationale": "No execution steps to staff.",
                "changes": [],
                "confirmation": "not_required",
                "closest_rejected": None,
            },
            "specialists": {},
            "inspected": inspected,
        }

    crew_choice, crew_assessment = _decide_crew(goal, needs, catalog.get("crews") or [])
    if crew_choice == "reuse":
        specialists = {
            need["role"]: _reuse_specialist(need, crew_assessment["by_role"].get(need["role"]))
            for need in _unique_needs(needs)
        }
        crew = _reuse_crew(crew_assessment, specialists)
    elif crew_choice == "adapt":
        specialists = {
            need["role"]: _adapt_member(need, crew_assessment["by_role"].get(need["role"]))
            for need in _unique_needs(needs)
        }
        crew = _adapt_crew(crew_assessment, specialists)
    else:
        specialists = {
            need["role"]: _decide_specialist(need, catalog.get("roles") or [])
            for need in _unique_needs(needs)
        }
        closest = _closest_rejected(
            crew_assessment, catalog.get("roles") or [], needs, specialists
        )
        crew = _generate_crew(goal, needs, specialists, default_provider, closest)

    return {
        "version": 1,
        "goal": goal,
        "crew": crew,
        "specialists": specialists,
        "inspected": inspected,
    }


def bind_plan(workspace, mission, plan: dict) -> dict:
    """Record synthesis for this mission. A second call keeps the first decision."""
    existing = load_synthesis(mission["id"])
    if existing:
        return existing
    decision = synthesize(
        workspace["id"],
        mission["goal"],
        plan.get("steps") or [],
        mission["provider"],
    )
    _save_synthesis(mission["id"], decision)
    _apply_provider_narrowing(mission["id"], decision)
    return decision


def load_synthesis(mission_id: int) -> dict | None:
    row = models.get_mad_scientist_mission_for_mission(mission_id)
    if row is None:
        return None
    raw = row["synthesis_json"] if "synthesis_json" in row.keys() else None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def public_view(mission_id: int) -> dict | None:
    data = load_synthesis(mission_id)
    if not data:
        return None
    crew = data.get("crew") or {}
    confirmation = crew.get("confirmation") or "not_required"
    action = crew.get("action") or "none"
    preferences = crew.get("provider_preferences") or {}
    return {
        "action": action,
        "name": crew.get("name") or "",
        "slug": crew.get("slug") or "",
        "roles": [
            {
                "name": role.get("name") or role.get("slug") or "",
                "slug": role.get("slug") or "",
                "provider": role.get("provider") or "",
                "tools": list(role.get("tools") or []),
                "action": role.get("action") or "",
            }
            for role in crew.get("roles") or []
        ],
        "tools": list(crew.get("tools") or []),
        "provider_preferences": {
            "default": preferences.get("default") or "",
            "by_role": dict(preferences.get("by_role") or {}),
        },
        "rationale": crew.get("rationale") or "",
        "changes": list(crew.get("changes") or []),
        "confirmation": confirmation,
        "closest_rejected": crew.get("closest_rejected"),
        "needs_confirmation": action == "generate" and confirmation == "pending",
        "confirmed": confirmation == "confirmed",
        "mission_only": confirmation == "mission_only",
        "inspected_roles": list((data.get("inspected") or {}).get("roles") or []),
        "inspected_crews": list((data.get("inspected") or {}).get("crews") or []),
        "specialists": [
            {
                "role": slug,
                "action": spec.get("action") or "",
                "name": spec.get("name") or slug,
                "rationale": spec.get("rationale") or "",
                "changes": list(spec.get("changes") or []),
            }
            for slug, spec in (data.get("specialists") or {}).items()
        ],
    }


def binding_for_step(mission_id: int, step: dict) -> dict | None:
    """Persona and provider a Mission Control attempt should bind for this step."""
    data = load_synthesis(mission_id)
    if not data:
        return None
    role = str(step.get("role") or "").strip().lower()
    spec = (data.get("specialists") or {}).get(role)
    if not spec:
        return None
    persona = spec.get("persona")
    return {
        "action": spec.get("action"),
        "slug": spec.get("slug") or role,
        "provider": spec.get("provider") or None,
        "use_saved_role": spec.get("action") != "generate",
        "persona_override": json.dumps(persona, sort_keys=True) if persona else None,
    }


def set_crew_confirmation(mission_id: int, workspace_id: int, decision: str) -> dict:
    """Confirm a generated crew into durable memory, or keep it on this mission."""
    if decision not in ("confirm", "mission_only"):
        raise ValueError("Decision must be confirm or mission_only")
    row = models.get_mad_scientist_mission_for_mission(mission_id)
    if row is None or int(row["workspace_id"]) != int(workspace_id):
        raise ValueError("Mad Scientist mission not found for this workspace")
    data = load_synthesis(mission_id)
    if not data or (data.get("crew") or {}).get("action") != "generate":
        raise ValueError("Only a generated crew can be confirmed")
    crew = data["crew"]
    if crew.get("confirmation") != "pending":
        raise ValueError("Crew confirmation is already recorded")
    if decision == "confirm":
        _store_durable_crew(workspace_id, crew, mission_id)
        crew["confirmation"] = "confirmed"
    else:
        crew["confirmation"] = "mission_only"
    data["crew"] = crew
    _save_synthesis(mission_id, data)
    return public_view(mission_id)


def durable_crews(workspace_id: int) -> list[dict]:
    memory = models.get_workspace_memory(workspace_id, PATTERN_MEMORY_KEY) or {}
    crews = memory.get("crews") or []
    return list(crews) if isinstance(crews, list) else []


def _save_synthesis(mission_id: int, payload: dict) -> None:
    with models.get_db() as conn:
        conn.execute(
            """
            UPDATE mad_scientist_missions
            SET synthesis_json = ?, updated_at = ?
            WHERE mission_id = ?
            """,
            (json.dumps(payload, sort_keys=True), models._now(), mission_id),
        )


def _apply_provider_narrowing(mission_id: int, decision: dict) -> None:
    """Point adapted steps at the narrowed provider. The runner still queues them."""
    import mad_scientist_graph as graph

    graph_row = graph.get_by_mission_id(mission_id)
    if graph_row is None:
        return
    providers_by_role = {}
    for role, spec in (decision.get("specialists") or {}).items():
        if spec.get("action") == "adapt" and spec.get("provider"):
            providers_by_role[role] = spec["provider"]
    if not providers_by_role:
        return
    now = models._now()
    with models.get_db() as conn:
        for step in graph.list_steps(graph_row["id"]):
            narrowed = providers_by_role.get(step["role"])
            if not narrowed or step["provider"] == narrowed:
                continue
            conn.execute(
                """
                UPDATE mad_scientist_steps
                SET provider = ?, updated_at = ?
                WHERE id = ?
                """,
                (narrowed, now, step["id"]),
            )


def _store_durable_crew(workspace_id: int, crew: dict, mission_id: int) -> None:
    memory = models.get_workspace_memory(workspace_id, PATTERN_MEMORY_KEY) or {}
    crews = [item for item in (memory.get("crews") or []) if isinstance(item, dict)]
    slug = crew.get("slug") or _slugify(crew.get("name") or "crew")
    taken = {item.get("slug") for item in crews}
    if slug in taken:
        slug = f"{slug}-{mission_id}"
    crews.append({
        "name": crew.get("name"),
        "slug": slug,
        "roles": crew.get("roles") or [],
        "tools": crew.get("tools") or [],
        "provider_preferences": crew.get("provider_preferences") or {},
        "rationale": crew.get("rationale") or "",
        "closest_rejected": crew.get("closest_rejected"),
        "source_mission_id": mission_id,
        "confirmed_at": models._now(),
    })
    models.upsert_workspace_memory(workspace_id, PATTERN_MEMORY_KEY, {"crews": crews})


def _needs_from_steps(steps: list[dict], default_provider: str | None) -> list[dict]:
    needs = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        role = str(step.get("role") or "builder").strip().lower()
        if role not in CANONICAL_ROLES:
            role = "builder"
        name = str(step.get("name") or role).strip()
        if not name:
            continue
        provider = str(step.get("provider") or default_provider or "").strip()
        needs.append({
            "role": role,
            "name": name,
            "task": str(step.get("task") or "").strip(),
            "provider": provider,
        })
    return needs


def _unique_needs(needs: list[dict]) -> list[dict]:
    chosen = []
    seen = set()
    for need in needs:
        if need["role"] in seen:
            continue
        seen.add(need["role"])
        chosen.append(need)
    return chosen


def _decide_crew(goal: str, needs: list[dict], crews: list[dict]) -> tuple[str, dict | None]:
    ranked = []
    for crew in crews:
        ranked.append(_assess_crew(goal, needs, crew))
    ranked.sort(
        key=lambda item: (item["coverage"], item["goal_overlap"], item["crew"]["name"]),
        reverse=True,
    )
    fitting = [
        item for item in ranked
        if item["coverage"] == 1
        and not item["extra"]
        and not item["provider_mismatch"]
        and not item["tools_broader"]
    ]
    if fitting:
        return "reuse", fitting[0]
    adaptable = [
        item for item in ranked
        if item["coverage"] == 1
        and (item["extra"] or item["provider_mismatch"] or item["tools_broader"])
    ]
    if adaptable:
        adaptable.sort(
            key=lambda item: (
                len(item["extra"]),
                1 if item["provider_mismatch"] else 0,
                1 if item["tools_broader"] else 0,
                -item["goal_overlap"],
            )
        )
        return "adapt", adaptable[0]
    return "generate", ranked[0] if ranked else None


def _assess_crew(goal: str, needs: list[dict], crew: dict) -> dict:
    required = []
    seen = set()
    for need in needs:
        if need["role"] not in seen:
            required.append(need["role"])
            seen.add(need["role"])
    by_role = {}
    extra = []
    for member in crew.get("roles") or []:
        canonical = _canonical_roles(member.get("slug") or "", member.get("name") or "")
        if not canonical:
            extra.append(member)
            continue
        role = canonical[0]
        if role not in seen or role in by_role:
            if role not in seen:
                extra.append(member)
            continue
        by_role[role] = member
    coverage = (len(by_role) / len(required)) if required else 0.0
    provider_mismatch = False
    tools_broader = False
    for need in _unique_needs(needs):
        member = by_role.get(need["role"])
        if member is None:
            continue
        member_provider = (member.get("provider") or "").strip()
        if member_provider and need.get("provider") and member_provider != need["provider"]:
            provider_mismatch = True
        member_tools = set(member.get("tools") or [])
        needed = set(ROLE_TOOLS.get(need["role"], []))
        if member_tools and needed and not member_tools.issubset(needed):
            tools_broader = True
    return {
        "crew": crew,
        "coverage": coverage,
        "extra": extra,
        "provider_mismatch": provider_mismatch,
        "tools_broader": tools_broader,
        "goal_overlap": _jaccard(_tokens(goal), _tokens(crew.get("text") or crew.get("name") or "")),
        "by_role": by_role,
        "required": required,
    }


def _decide_specialist(need: dict, roles: list[dict]) -> dict:
    ranked = []
    for role in roles:
        score, needs_narrowing = _score_specialist(need, role)
        ranked.append((score, needs_narrowing, role))
    ranked.sort(
        key=lambda item: (item[0], 1 if item[2].get("slug") == need["role"] else 0),
        reverse=True,
    )
    if ranked and ranked[0][0] >= REUSE_MIN and not ranked[0][1]:
        return _reuse_specialist(need, ranked[0][2])
    if ranked and ranked[0][0] >= ADAPT_MIN:
        return _adapt_member(need, ranked[0][2], score=ranked[0][0])
    closest = ranked[0][2] if ranked else None
    return _generate_specialist(need, closest)


def _score_specialist(need: dict, candidate: dict) -> tuple[float, bool]:
    role = need["role"]
    parts = set(_canonical_roles(candidate.get("slug") or "", candidate.get("name") or ""))
    exact = role in parts or candidate.get("slug") == role
    if exact:
        role_hit = 1.0
    elif role in _tokens(f"{candidate.get('name') or ''} {candidate.get('slug') or ''}"):
        role_hit = 1.0
    elif role in _tokens(
        f"{candidate.get('system_prompt') or ''} {candidate.get('goal') or ''}"
    ):
        role_hit = 0.55
    else:
        role_hit = 0.0
    overlap = _jaccard(
        _tokens(need.get("task") or ""),
        _tokens(
            " ".join([
                candidate.get("name") or "",
                candidate.get("slug") or "",
                candidate.get("system_prompt") or "",
                candidate.get("goal") or "",
                " ".join(candidate.get("tools") or []),
            ])
        ),
    )
    score = 0.8 * role_hit + 0.2 * overlap
    tools = set(candidate.get("tools") or [])
    needed = set(ROLE_TOOLS.get(role, []))
    tools_broader = bool(tools) and bool(needed) and not tools.issubset(needed)
    tools_missing = bool(tools) and bool(needed - tools)
    provider = (candidate.get("provider") or "").strip()
    provider_mismatch = bool(provider and need.get("provider") and provider != need["provider"])
    if tools_missing and not exact:
        score *= 0.4
    needs_narrowing = (tools_broader or provider_mismatch) and not (tools_missing and not exact)
    if needs_narrowing and score >= REUSE_MIN:
        score = 0.70
    return score, needs_narrowing


def _reuse_specialist(need: dict, member: dict | None) -> dict:
    member = member or {}
    slug = member.get("slug") or need["role"]
    name = member.get("name") or _title_role(need["role"])
    source = member.get("source") or "crew"
    tools = list(member.get("tools") or [])
    # Saved agent roles already live on the run via role_id. A crew or
    # durable-memory role still has to travel in the attempt persona.
    persona = None
    if source != "agent_role":
        persona = {}
        if member.get("system_prompt"):
            persona["system_prompt"] = member["system_prompt"]
        if member.get("goal"):
            persona["goal"] = member["goal"]
        if member.get("backstory"):
            persona["backstory"] = member["backstory"]
        if tools:
            persona["tools"] = ", ".join(tools)
        if not persona:
            persona = None
    return {
        "action": "reuse",
        "slug": slug,
        "name": name,
        "source": source,
        "system_prompt": member.get("system_prompt") or "",
        "goal": member.get("goal") or "",
        "tools": tools,
        "provider": need.get("provider") or member.get("provider") or "",
        "changes": [],
        "rationale": f"Reused existing role '{name}'.",
        "closest_rejected": None,
        "persona": persona,
    }


def _adapt_member(need: dict, member: dict | None, score: float | None = None) -> dict:
    member = member or {}
    slug = member.get("slug") or need["role"]
    name = member.get("name") or _title_role(need["role"])
    source = member.get("source") or "crew"
    needed_tools = ROLE_TOOLS.get(need["role"], [])
    current_tools = list(member.get("tools") or [])
    changes = []
    persona = {}
    tools = current_tools
    if current_tools and needed_tools and not set(current_tools).issubset(set(needed_tools)):
        tools = list(needed_tools)
        changes.append(
            "Narrowed tools for '{name}' from '{before}' to '{after}'.".format(
                name=slug,
                before=", ".join(sorted(current_tools)),
                after=", ".join(tools),
            )
        )
        persona["tools"] = ", ".join(tools)
    provider = need.get("provider") or member.get("provider") or ""
    member_provider = (member.get("provider") or "").strip()
    if member_provider and provider and member_provider != provider:
        changes.append(
            f"Narrowed provider for '{slug}' from '{member_provider}' to '{provider}'."
        )
    if not changes and score is None:
        return _reuse_specialist(need, member)
    if not changes and score is not None:
        changes.append(f"Narrowed '{name}' to this step's provider, tools, and prompt.")
        persona["system_prompt"] = _specialist_prompt(name, need)
        persona["goal"] = _specialist_goal(need)
        if needed_tools:
            tools = list(needed_tools)
            persona["tools"] = ", ".join(tools)
    return {
        "action": "adapt",
        "slug": slug,
        "name": name,
        "source": source,
        "system_prompt": persona.get("system_prompt") or member.get("system_prompt") or "",
        "goal": persona.get("goal") or member.get("goal") or "",
        "tools": tools,
        "provider": provider,
        "changes": changes,
        "rationale": f"Adapted role '{name}'. " + " ".join(changes),
        "closest_rejected": None,
        "persona": persona or None,
    }


def _generate_specialist(need: dict, closest: dict | None) -> dict:
    name = _generated_specialist_name(need)
    slug = _slugify(name)
    tools = list(ROLE_TOOLS.get(need["role"], []))
    prompt = _specialist_prompt(name, need)
    goal = _specialist_goal(need)
    if closest is None:
        rationale = (
            f"Reuse and adapt fit poorly for the {need['role']} specialist. "
            "No existing role was close enough to reject by name."
        )
        rejected = None
    else:
        rationale = (
            f"Reuse and adapt fit poorly for the {need['role']} specialist. "
            f"Rejected closest role '{closest.get('name')}' ({closest.get('slug')})."
        )
        rejected = {
            "kind": "role",
            "name": closest.get("name"),
            "id": closest.get("slug"),
        }
    return {
        "action": "generate",
        "slug": slug,
        "name": name,
        "source": "generated",
        "system_prompt": prompt,
        "goal": goal,
        "tools": tools,
        "provider": need.get("provider") or "",
        "changes": [],
        "rationale": rationale,
        "closest_rejected": rejected,
        "persona": {
            "system_prompt": prompt,
            "goal": goal,
            "backstory": "Generated for this mission because reuse and adapt fit poorly.",
            "tools": ", ".join(tools),
        },
    }


def _reuse_crew(assessment: dict, specialists: dict) -> dict:
    crew = assessment["crew"]
    roles = [_crew_role_from_specialist(spec) for spec in specialists.values()]
    return {
        "action": "reuse",
        "name": crew["name"],
        "slug": crew.get("slug") or _slugify(crew["name"]),
        "source": crew.get("source"),
        "source_id": crew.get("id"),
        "roles": roles,
        "tools": _union_tools(roles),
        "provider_preferences": _provider_preferences(roles, crew.get("provider_preferences")),
        "rationale": (
            f"Reused existing crew '{crew['name']}' because its roles cover this plan "
            "without narrowing."
        ),
        "changes": [],
        "confirmation": "not_required",
        "closest_rejected": None,
    }


def _adapt_crew(assessment: dict, specialists: dict) -> dict:
    crew = assessment["crew"]
    changes = []
    for member in assessment["extra"]:
        label = member.get("name") or member.get("slug") or "unnamed"
        changes.append(f"Dropped role '{label}' (not required by this plan).")
    for spec in specialists.values():
        for change in spec.get("changes") or []:
            if change not in changes:
                changes.append(change)
    roles = [_crew_role_from_specialist(spec) for spec in specialists.values()]
    name = crew["name"]
    return {
        "action": "adapt",
        "name": name,
        "slug": crew.get("slug") or _slugify(name),
        "source": crew.get("source"),
        "source_id": crew.get("id"),
        "roles": roles,
        "tools": _union_tools(roles),
        "provider_preferences": _provider_preferences(roles, None),
        "rationale": f"Adapted crew '{name}' for this mission by narrowing it.",
        "changes": changes,
        "confirmation": "not_required",
        "closest_rejected": None,
    }


def _generate_crew(goal: str, needs: list[dict], specialists: dict, default_provider, closest) -> dict:
    roles = [_crew_role_from_specialist(spec) for spec in specialists.values()]
    name = _generated_crew_name(goal)
    required = ", ".join(_assess_required(needs))
    if closest is None:
        reused = [spec["name"] for spec in specialists.values() if spec.get("action") == "reuse"]
        if reused:
            rationale = (
                "Reuse and adapt fit poorly for a whole crew. No existing crew was "
                f"available. Reused {', '.join(reused)} inside this new crew. "
                f"Required roles: {required}."
            )
        else:
            rationale = (
                "Reuse and adapt fit poorly. No existing crew or role was close enough "
                f"to reject by name. Required roles: {required}."
            )
        rejected = None
    else:
        rationale = (
            "Reuse and adapt fit poorly. "
            f"Rejected closest {closest['kind']} '{closest['name']}' ({closest['id']}) "
            f"because it does not cover the required roles ({required})."
        )
        rejected = {"kind": closest["kind"], "name": closest["name"], "id": closest["id"]}
    return {
        "action": "generate",
        "name": name,
        "slug": _slugify(name),
        "source": "generated",
        "source_id": None,
        "roles": roles,
        "tools": _union_tools(roles),
        "provider_preferences": _provider_preferences(roles, {"default": default_provider or ""}),
        "rationale": rationale,
        "changes": [],
        "confirmation": "pending",
        "closest_rejected": rejected,
    }


def _closest_rejected(
    assessment: dict | None,
    roles: list[dict],
    needs: list[dict],
    specialists: dict | None = None,
) -> dict | None:
    if assessment is not None:
        crew = assessment["crew"]
        return {"kind": "crew", "name": crew["name"], "id": crew["id"]}
    reused = {
        spec.get("slug")
        for spec in (specialists or {}).values()
        if spec.get("action") == "reuse"
    }
    best = None
    best_score = -1.0
    for role in roles:
        if role.get("slug") in reused:
            continue
        for need in _unique_needs(needs):
            score, _narrow = _score_specialist(need, role)
            if score > best_score:
                best_score = score
                best = role
    if best is None:
        return None
    return {"kind": "role", "name": best.get("name") or best.get("slug"), "id": best.get("slug")}


def _crew_role_from_specialist(spec: dict) -> dict:
    action = spec.get("action")
    origin = {"reuse": "reused", "adapt": "adapted", "generate": "generated"}.get(action, action)
    return {
        "slug": spec.get("slug"),
        "name": spec.get("name"),
        "action": origin,
        "source": spec.get("source"),
        "system_prompt": spec.get("system_prompt") or "",
        "goal": spec.get("goal") or "",
        "tools": list(spec.get("tools") or []),
        "provider": spec.get("provider") or "",
    }


def _provider_preferences(roles: list[dict], existing: dict | None) -> dict:
    by_role = {}
    for role in roles:
        canonical = _canonical_roles(role.get("slug") or "", role.get("name") or "")
        key = canonical[0] if canonical else role.get("slug")
        if key and role.get("provider"):
            by_role[key] = role["provider"]
    default = ""
    if isinstance(existing, dict) and existing.get("default"):
        default = existing["default"]
    elif by_role:
        default = next(iter(by_role.values()))
    return {"default": default or "", "by_role": by_role}


def _assess_required(needs: list[dict]) -> list[str]:
    required = []
    for need in needs:
        if need["role"] not in required:
            required.append(need["role"])
    return required


def _generated_crew_name(goal: str) -> str:
    words = _significant_words(goal)[:4]
    head = " ".join(word.capitalize() for word in words) or "Mission"
    return f"{head} crew"


def _generated_specialist_name(need: dict) -> str:
    words = _significant_words(need.get("task") or "")[:3]
    if not words:
        words = [need["role"]]
    head = " ".join(word.capitalize() for word in words)
    return f"{head} {need['role'].capitalize()}"


def _specialist_prompt(name: str, need: dict) -> str:
    blurb = ROLE_BLURB.get(need["role"], "Complete the assigned step.")
    return (
        f"You are {name}, the {need['role']} specialist for this mission. {blurb} "
        "Stay on the step task."
    )


def _specialist_goal(need: dict) -> str:
    task = need.get("task") or need["role"]
    return task[:240]


def _role_from_mapping(row, source: str) -> dict:
    getter = row.get if isinstance(row, dict) else lambda key, default=None: row[key] if key in row.keys() else default
    tools = _parse_tools(getter("tools"))
    slug = str(getter("slug") or "").strip()
    return {
        "slug": slug,
        "name": str(getter("name") or slug),
        "system_prompt": str(getter("system_prompt") or ""),
        "goal": str(getter("goal") or ""),
        "backstory": str(getter("backstory") or ""),
        "tools": tools,
        "provider": str(getter("provider") or ""),
        "source": source,
    }


def _crew_from_provider(cfg: dict) -> dict:
    roles = []
    texts = [cfg.get("label") or cfg.get("id") or ""]
    for agent in cfg.get("agents") or []:
        slug = str(agent.get("role_slug") or agent.get("slug") or "").strip()
        tools = _parse_tools(agent.get("tools"))
        suffix = str(agent.get("task_suffix") or "")
        if suffix:
            texts.append(suffix)
        roles.append({
            "slug": slug,
            "name": str(agent.get("name") or slug),
            "provider": str(agent.get("provider") or ""),
            "tools": tools,
            "system_prompt": str(agent.get("system_prompt") or ""),
            "goal": str(agent.get("goal") or ""),
            "source": "provider_crew",
        })
    name = str(cfg.get("label") or cfg.get("id"))
    return {
        "id": cfg.get("id"),
        "name": name,
        "slug": _slugify(name),
        "source": "provider",
        "roles": roles,
        "text": " ".join(texts),
        "provider_preferences": _provider_preferences(roles, None),
    }


def _crew_from_memory(saved: dict) -> dict:
    roles = []
    for member in saved.get("roles") or []:
        if not isinstance(member, dict):
            continue
        item = _role_from_mapping(member, source="durable_role")
        roles.append(item)
    name = str(saved.get("name") or saved.get("slug") or "Saved crew")
    slug = str(saved.get("slug") or _slugify(name))
    return {
        "id": f"memory:{slug}",
        "name": name,
        "slug": slug,
        "source": "memory",
        "roles": roles,
        "text": " ".join([name, saved.get("rationale") or ""]),
        "provider_preferences": saved.get("provider_preferences") or _provider_preferences(roles, None),
    }


def _union_tools(roles: list[dict]) -> list[str]:
    found = []
    for role in roles:
        for tool in role.get("tools") or []:
            if tool not in found:
                found.append(tool)
    return found


def _parse_tools(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [part.strip() for part in text.split(",") if part.strip()]
    return []


def _canonical_roles(slug: str, name: str = "") -> list[str]:
    hay = re.sub(r"[^a-z0-9]+", "-", f"{slug} {name}".lower()).strip("-")
    parts = set(part for part in hay.split("-") if part)
    return [role for role in CANONICAL_ROLES if role in parts]


def _tokens(text: str) -> set[str]:
    return {
        word for word in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(word) > 2 and word not in _STOPWORDS
    }


def _significant_words(text: str) -> list[str]:
    words = []
    for word in re.findall(r"[A-Za-z0-9]+", text or ""):
        if len(word) > 2 and word.lower() not in _STOPWORDS:
            words.append(word.lower())
    return words


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "crew"


def _title_role(role: str) -> str:
    return role.capitalize()

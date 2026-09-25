"""
Tank — a self-hosted command center for running AI agents in parallel,
organized by workspace. Supports Claude Code and OpenAI-compatible APIs.

Run with:
    python app.py
"""
import logging
import os
import re
import threading

import yaml
from flask import Flask, redirect, render_template, request, url_for, make_response

_log = logging.getLogger(__name__)

import config
import git_sync
import log_format
import mad_scientist
import mad_scientist_graph
import mission
import models
import pattern_synthesis
import providers
import scheduler
import session_manager

app = Flask(
    __name__,
    template_folder=config.share_subdir("templates"),
    static_folder=config.share_subdir("static"),
)

_AI_NOTICES = {
    "saved": "Saved. Provider config reloaded.",
    "default": "Default provider saved and reloaded.",
    "added": "Provider added and reloaded.",
}
_PROVIDER_BOOL_BLOCKLIST = {"label", "model", "base_url", "api_key_env", "process", "type"}


@app.context_processor
def _provider_picker_context():
    """Grouped Model options for every template, including HTMX fragments."""
    return {
        "provider_groups_all": providers.grouped_for_ui(include_crews=True),
        "provider_groups_leaf": providers.grouped_for_ui(include_crews=False),
    }


def _configure_ai_context(error=None):
    notice_code = "" if error else request.args.get("notice", "")
    return {
        "catalog": providers.provider_catalog(),
        "default_info": providers.default_provider_info(),
        "notice": _AI_NOTICES.get(notice_code, ""),
        "error": error,
        "tank_version": config.TANK_VERSION,
    }


def _provider_changes_from_form() -> dict:
    bool_names = set(request.form.getlist("bool_shown")) - _PROVIDER_BOOL_BLOCKLIST
    changes = {}
    for name in request.form.getlist("shown"):
        if name in bool_names:
            changes[name] = request.form.get(name) == "1"
        else:
            changes[name] = request.form.get(name, "")
    if "type" in request.form:
        changes["type"] = request.form.get("type", "").strip()
    return changes

_stop_event = threading.Event()


def _start_background_workers():
    models.init_db()
    swept = session_manager.sweep_orphaned_runs()
    if swept:
        _log.warning(
            "Tank startup recovery: marked %d run(s) failed that were still "
            "'running' from before this restart",
            len(swept),
        )
    threading.Thread(
        target=session_manager.queue_worker_loop, args=(_stop_event,), daemon=True
    ).start()
    scheduler.start()


# ---------- dashboard ----------

def _slugify(text: str) -> str:
    """Convert a human-readable name into a YAML-safe lowercase slug."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower().strip())
    return slug.strip("_") or "crew"


def _save_crew_to_yaml(
    ws: dict,
    crew_slug: str,
    saved_crew_name: str,
    agents: list[dict],
) -> None:
    """
    Persist a GUI-built crew to providers.yaml and workspaces.yaml.

    Steps with custom persona fields (custom_goal / custom_backstory /
    custom_tools) become new saved roles in workspaces.yaml, whose slugs
    are then referenced in the providers.yaml crew entry.

    Both YAML files are round-tripped through PyYAML.  Any hand-written
    comments in those files will be lost on the first save via this path.
    """
    workspace_id: int  = ws["id"]
    workspace_slug: str = ws["slug"]

    # ── Build provider-agents list + collect new roles to persist ──────────
    provider_agents: list[dict] = []
    new_roles:       list[dict] = []

    for i, agent_cfg in enumerate(agents):
        pa: dict = {"provider": agent_cfg["provider"]}

        has_custom = bool(
            agent_cfg.get("custom_goal")
            or agent_cfg.get("custom_backstory")
            or agent_cfg.get("custom_tools")
        )

        if has_custom:
            step_slug = f"{crew_slug}_step_{i + 1}"

            # Start from the base saved role (if one was selected), then
            # apply the per-step custom overrides on top.
            new_role: dict = {
                "slug": step_slug,
                "name": f"{saved_crew_name} — Step {i + 1}",
            }
            base = None
            if agent_cfg.get("role_slug"):
                base = models.get_role_by_slug(workspace_id, agent_cfg["role_slug"])
            if base:
                for fld in ("system_prompt", "goal", "backstory", "tools"):
                    val = base[fld]
                    if val is not None:
                        new_role[fld] = val

            if agent_cfg.get("custom_goal"):
                new_role["goal"] = agent_cfg["custom_goal"]
            if agent_cfg.get("custom_backstory"):
                new_role["backstory"] = agent_cfg["custom_backstory"]
            if agent_cfg.get("custom_tools"):
                tool_list = [
                    t.strip()
                    for t in agent_cfg["custom_tools"].split(",")
                    if t.strip()
                ]
                if tool_list:
                    new_role["tools"] = tool_list

            new_roles.append(new_role)
            pa["role_slug"] = step_slug

        elif agent_cfg.get("role_slug"):
            pa["role_slug"] = agent_cfg["role_slug"]

        if agent_cfg.get("task_suffix"):
            pa["task_suffix"] = agent_cfg["task_suffix"]

        provider_agents.append(pa)

    # ── Write new roles to workspaces.yaml ─────────────────────────────────
    if new_roles:
        try:
            with open(config.WORKSPACES_FILE, "r", encoding="utf-8") as fh:
                ws_doc = yaml.safe_load(fh) or {}

            for ws_entry in ws_doc.get("workspaces", []):
                if ws_entry.get("slug") == workspace_slug:
                    existing = {r.get("slug") for r in ws_entry.get("roles", [])}
                    for nr in new_roles:
                        if nr["slug"] not in existing:
                            ws_entry.setdefault("roles", []).append(nr)
                    break

            with open(config.WORKSPACES_FILE, "w", encoding="utf-8") as fh:
                yaml.dump(
                    ws_doc, fh,
                    allow_unicode=True, default_flow_style=False, sort_keys=False,
                )
        except Exception as exc:
            _log.warning("_save_crew_to_yaml: failed to write workspaces.yaml — %s", exc)

    # ── Write crew provider entry to providers.yaml ─────────────────────────
    try:
        with open(config.PROVIDERS_FILE, "r", encoding="utf-8") as fh:
            prov_doc = yaml.safe_load(fh) or {}

        prov_doc.setdefault("providers", {})
        crew_provider_id = f"crew_{crew_slug}"
        prov_doc["providers"][crew_provider_id] = {
            "type":    "crew",
            "label":   saved_crew_name,
            "process": "sequential",
            "agents":  provider_agents,
        }

        with open(config.PROVIDERS_FILE, "w", encoding="utf-8") as fh:
            yaml.dump(
                prov_doc, fh,
                allow_unicode=True, default_flow_style=False, sort_keys=False,
            )
    except Exception as exc:
        _log.warning("_save_crew_to_yaml: failed to write providers.yaml — %s", exc)


def _crew_provider_ids() -> set:
    """
    Return the set of provider IDs that represent crew parent runs.

    Includes all registered 'crew'-type providers from providers.yaml PLUS
    the synthetic 'crew_builder' ID used by the GUI Crew Orchestrator so
    that dynamically-launched crew runs also receive the parent-tree UI.
    """
    registered = {
        pid
        for pid, _ in providers.list_providers()
        if providers.get_provider_type(pid) == "crew"
    }
    registered.add("crew_builder")
    registered.add("mad_scientist")
    return registered


def _dashboard_context(error=None, form=None):
    workspaces = []
    for ws in models.list_workspaces():
        runs = models.list_runs(workspace_id=ws["id"], limit=8)
        workspaces.append({
            "workspace": ws,
            "runs": runs,
            "repo_error": config.check_repo_path(ws["repo_path"]),
        })
    return {
        "workspaces": workspaces,
        "providers": providers.list_providers(),
        "default_provider": providers.get_default_provider(),
        "error": error,
        "form": form or {},
        "tank_version": config.TANK_VERSION,
    }


def _mission_list_context(ws):
    missions = models.list_missions(ws["id"], limit=10)
    return {
        "mission_entries": [
            {
                "mission": m,
                "runs": models.list_mission_runs(m["id"]),
                "graph": mad_scientist_graph.mission_view(m["id"]),
                "synthesis": pattern_synthesis.public_view(m["id"]),
            }
            for m in missions
        ],
        "workspace": ws,
        "providers": providers.list_providers(),
        "default_provider": providers.get_default_provider(),
    }


@app.route("/api/version")
def api_version():
    default = providers.get_default_provider()
    return {
        "version": config.TANK_VERSION,
        "template_folder": app.template_folder,
        "features": [
            "cancel_run",
            "delete_workspace",
            "resume_mission",
            "mad_scientist",
            "mad_scientist_graph",
            "restart_sweep",
            "configure_ai",
        ],
        "default_provider": default,
        "provider_keys": {
            pid: providers.provider_key_status(pid)
            for pid, _ in providers.list_providers()
        },
        "default_provider_probe": providers.probe_provider(default),
    }


@app.after_request
def _no_cache_html(response):
    if response.content_type and "text/html" in response.content_type:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/")
def dashboard():
    return render_template("dashboard.html", **_dashboard_context())


@app.route("/partials/workspace-cards")
def workspace_cards_partial():
    """Polled by the dashboard every few seconds to refresh run status."""
    return render_template(
        "partials/workspace_cards.html",
        workspaces=_dashboard_context()["workspaces"],
    )


@app.route("/workspaces", methods=["POST"])
def create_workspace():
    form = {
        "name": request.form.get("name", "").strip(),
        "slug": request.form.get("slug", "").strip(),
        "repo_path": request.form.get("repo_path", "").strip(),
        "git_remote": request.form.get("git_remote", "").strip(),
        "default_provider": request.form.get("default_provider", "").strip(),
    }
    try:
        models.create_workspace(
            name=form["name"],
            repo_path=form["repo_path"],
            slug=form["slug"] or None,
            git_remote=form["git_remote"] or None,
            default_provider=form["default_provider"] or None,
        )
    except ValueError as exc:
        return render_template(
            "dashboard.html", **_dashboard_context(error=str(exc), form=form)
        )
    return redirect(url_for("dashboard"))


@app.route("/workspaces/<slug>/delete", methods=["POST"])
def delete_workspace(slug):
    try:
        models.delete_workspace(slug)
    except ValueError as exc:
        ws = models.get_workspace(slug)
        if ws is None:
            return redirect(url_for("dashboard"))
        roles = models.list_roles(ws["id"])
        runs = models.list_runs(workspace_id=ws["id"], limit=30)
        chainable_runs = models.list_chainable_runs(ws["id"], limit=20)
        return render_template(
            "workspace.html",
            roles=roles,
            runs=runs,
            chainable_runs=chainable_runs,
            mission_templates=mission.list_templates(),
            repo_error=config.check_repo_path(ws["repo_path"]),
            delete_error=str(exc),
            tank_version=config.TANK_VERSION,
            crew_provider_ids=_crew_provider_ids(),
            **_mission_list_context(ws),
        )
    return redirect(url_for("dashboard"))


@app.route("/config/reload", methods=["POST"])
def reload_config():
    models.reload_config()
    scheduler.reload_jobs()
    return redirect(url_for("dashboard"))


@app.route("/config/ai")
def configure_ai():
    return render_template("configure_ai.html", **_configure_ai_context())


@app.route("/config/ai/default", methods=["POST"])
def set_default_ai_provider():
    try:
        providers.set_default_provider(request.form.get("provider_id", ""))
        models.reload_config()
    except providers.ProviderConfigError as exc:
        return render_template("configure_ai.html", **_configure_ai_context(error=str(exc))), 400
    return redirect(url_for("configure_ai", notice="default"))


@app.route("/config/ai/providers", methods=["POST"])
def add_ai_provider():
    try:
        providers.add_provider(
            provider_id=request.form.get("provider_id", ""),
            label=request.form.get("label", ""),
            provider_type=request.form.get("type", ""),
            model=request.form.get("model", ""),
            base_url=request.form.get("base_url", ""),
            api_key_env=request.form.get("api_key_env", ""),
        )
        models.reload_config()
    except providers.ProviderConfigError as exc:
        return render_template("configure_ai.html", **_configure_ai_context(error=str(exc))), 400
    return redirect(url_for("configure_ai", notice="added"))


@app.route("/config/ai/providers/<provider_id>", methods=["POST"])
def save_ai_provider(provider_id):
    try:
        providers.update_provider(provider_id, _provider_changes_from_form())
        models.reload_config()
    except providers.ProviderConfigError as exc:
        return render_template("configure_ai.html", **_configure_ai_context(error=str(exc))), 400
    return redirect(url_for("configure_ai", notice="saved"))


@app.route("/config/ai/providers/<provider_id>/probe", methods=["POST"])
def probe_ai_provider(provider_id):
    try:
        result = providers.probe_provider(provider_id)
    except providers.ProviderError as exc:
        result = {"ok": False, "detail": str(exc)}
    ok, message = providers.format_probe_result(result)
    return render_template("partials/provider_probe.html", ok=ok, message=message)


# ---------- workspace detail ----------

@app.route("/workspaces/<slug>")
def workspace_detail(slug):
    ws = models.get_workspace(slug)
    if ws is None:
        return "Workspace not found", 404
    roles = models.list_roles(ws["id"])
    runs = models.list_runs(workspace_id=ws["id"], limit=30)
    chainable_runs = models.list_chainable_runs(ws["id"], limit=20)
    return render_template(
        "workspace.html",
        roles=roles,
        runs=runs,
        chainable_runs=chainable_runs,
        mission_templates=mission.list_templates(),
        repo_error=config.check_repo_path(ws["repo_path"]),
        tank_version=config.TANK_VERSION,
        crew_provider_ids=_crew_provider_ids(),
        pattern_catalog=pattern_synthesis.catalog_preview(ws["id"]),
        **_mission_list_context(ws),
    )


@app.route("/workspaces/<slug>/runs", methods=["POST"])
def create_run(slug):
    ws = models.get_workspace(slug)
    if ws is None:
        return "Workspace not found", 404
    role_id = request.form.get("role_id") or None
    provider = request.form.get("provider") or None
    task = request.form.get("task", "").strip()
    parent_raw = request.form.get("parent_run_id", "").strip()
    parent_run_id = None
    chain_latest = False
    if parent_raw == "latest":
        chain_latest = True
    elif parent_raw.isdigit():
        parent_run_id = int(parent_raw)
    if task:
        try:
            models.create_run(
                ws["id"],
                role_id,
                task,
                provider=provider,
                parent_run_id=parent_run_id,
                chain_latest=chain_latest,
            )
        except ValueError as exc:
            resp = make_response(f"<p class='form-error'>{exc}</p>")
            resp.headers["HX-Retarget"] = "#run-form-error"
            resp.headers["HX-Reswap"] = "innerHTML"
            return resp
    runs = models.list_runs(workspace_id=ws["id"], limit=30)
    return render_template("partials/run_list.html", runs=runs, crew_provider_ids=_crew_provider_ids())


@app.route("/partials/runs/<slug>")
def run_list_partial(slug):
    """Polled to refresh the run list / live status on the workspace page."""
    ws = models.get_workspace(slug)
    if ws is None:
        return "Workspace not found", 404
    runs = models.list_runs(workspace_id=ws["id"], limit=30)
    return render_template("partials/run_list.html", runs=runs, crew_provider_ids=_crew_provider_ids())


def _render_run_output(run, payload=None):
    output = session_manager.tail_log(run["log_path"])
    if payload is None:
        payload = models.get_run_payload(run["id"])
    formatted = log_format.format_run_output(run, output)
    return render_template(
        "partials/run_output.html",
        run=run,
        output=output,
        formatted=formatted,
        payload=payload,
    )


@app.route("/runs/<int:run_id>/output")
def run_output(run_id):
    run = models.get_run(run_id)
    if run is None:
        return "Run not found", 404
    return _render_run_output(run)


@app.route("/partials/crew-step-row")
def crew_step_row():
    """
    Return a single crew-step row fragment for the GUI Crew Orchestrator.
    Appended via hx-swap='beforeend' into #crew-steps-list.
    Query param: workspace_id (int, required).
    """
    workspace_id = request.args.get("workspace_id", type=int)
    if workspace_id is None:
        return "workspace_id required", 400
    roles = models.list_roles(workspace_id)
    return render_template(
        "partials/crew_step_row.html",
        roles=roles,
    )


@app.route("/workspaces/<slug>/crews/launch", methods=["POST"])
def launch_crew(slug):
    """
    Parse a dynamically-built crew form, instantiate a synthetic crew config,
    create a crew_builder run, and hand it off to session_manager's
    launch_crew_builder which drives orchestrator.run_sequential_crew.
    """
    ws = models.get_workspace(slug)
    if ws is None:
        return "Workspace not found", 404

    crew_task        = request.form.get("crew_task", "").strip()
    role_ids         = request.form.getlist("agent_role_ids[]")
    provider_ids     = [p.strip() for p in request.form.getlist("agent_providers[]") if p.strip()]
    task_suffixes    = request.form.getlist("agent_task_suffixes[]")
    custom_goals     = request.form.getlist("custom_goals[]")
    custom_backstories = request.form.getlist("custom_backstories[]")
    custom_tools     = request.form.getlist("custom_tools[]")

    def _err(msg: str):
        """Return an error fragment targeted at #crew-launch-error."""
        resp = make_response(f"<p class='form-error'>{msg}</p>")
        resp.headers["HX-Retarget"] = "#crew-launch-error"
        resp.headers["HX-Reswap"]   = "innerHTML"
        return resp

    if not crew_task:
        return _err("Crew task description is required.")
    if not provider_ids:
        return _err("Add at least one agent step before launching.")

    # Build the ordered agents list for orchestrator.run_sequential_crew.
    agents: list[dict] = []
    for i, prov_id in enumerate(provider_ids):
        agent_cfg: dict = {"provider": prov_id}

        raw_role_id = (role_ids[i] if i < len(role_ids) else "").strip()
        if raw_role_id:
            try:
                role = models.get_role_by_id(int(raw_role_id))
            except (ValueError, TypeError):
                role = None
            if role is None or role["workspace_id"] != ws["id"]:
                return _err("Role does not belong to this workspace.")
            agent_cfg["role_slug"] = role["slug"]

        suffix = (task_suffixes[i] if i < len(task_suffixes) else "").strip()
        if suffix:
            agent_cfg["task_suffix"] = suffix

        # Custom persona fields — always submitted (even when section is collapsed),
        # so empty strings mean "no override for this step".
        goal      = (custom_goals[i]       if i < len(custom_goals)       else "").strip()
        backstory = (custom_backstories[i] if i < len(custom_backstories) else "").strip()
        tools     = (custom_tools[i]       if i < len(custom_tools)       else "").strip()
        if goal:
            agent_cfg["custom_goal"] = goal
        if backstory:
            agent_cfg["custom_backstory"] = backstory
        if tools:
            agent_cfg["custom_tools"] = tools

        agents.append(agent_cfg)

    # ── Optional: save this crew to YAML configs ───────────────────────────
    save_crew      = request.form.get("save_crew") == "1"
    saved_crew_name = request.form.get("saved_crew_name", "").strip()
    crew_label      = saved_crew_name if (save_crew and saved_crew_name) else "Dynamic Crew"

    crew_cfg: dict = {
        "type":    "crew_builder",
        "label":   crew_label,
        "process": "sequential",
        "agents":  agents,
    }

    if save_crew and saved_crew_name:
        crew_slug = _slugify(saved_crew_name)
        _save_crew_to_yaml(ws, crew_slug, saved_crew_name, agents)
        models.reload_config()
        # Full page reload so the new crew appears in all provider dropdowns.
        run_id = models.create_adhoc_crew_run(ws["id"], crew_task)
        session_manager.launch_crew_builder(run_id, crew_cfg)
        resp = make_response("", 200)
        resp.headers["HX-Refresh"] = "true"
        return resp

    run_id = models.create_adhoc_crew_run(ws["id"], crew_task)
    session_manager.launch_crew_builder(run_id, crew_cfg)

    runs = models.list_runs(workspace_id=ws["id"], limit=30)
    resp = make_response(
        render_template(
            "partials/run_list.html",
            runs=runs,
            crew_provider_ids=_crew_provider_ids(),
        )
    )
    resp.headers["HX-Trigger"] = "runRefresh"
    return resp


@app.route("/runs/<int:run_id>/children")
def run_children(run_id):
    """Return the crew-children partial; polled by crew parent run cards."""
    import local_agent as _la

    run = models.get_run(run_id)
    if run is None:
        return "Run not found", 404
    child_rows = models.list_child_runs(run_id)
    is_active = run["status"] in ("running", "pending", "awaiting_approval")

    children = []
    for child in child_rows:
        role = models.get_role_by_id(child["role_id"]) if child["role_id"] else None
        tools = _la._authorized_tools(dict(role) if role else None)
        children.append({"run": child, "tools": tools or []})

    return render_template(
        "partials/run_children.html",
        parent_run=run,
        children=children,
        is_active=is_active,
        run_id=run_id,
    )


@app.route("/runs/<int:run_id>/approve", methods=["POST"], strict_slashes=False)
def approve_run_patch(run_id):
    if not session_manager.approve_run(run_id):
        return "Run not awaiting approval", 400
    resp = make_response("", 204)
    resp.headers["HX-Trigger"] = "runRefresh"
    return resp


@app.route("/runs/<int:run_id>/cancel", methods=["POST"], strict_slashes=False)
def cancel_run_route(run_id):
    if not session_manager.cancel_run(run_id):
        return "Run cannot be cancelled", 400
    resp = make_response("", 204)
    resp.headers["HX-Trigger"] = "runRefresh"
    return resp


@app.route("/runs/<int:run_id>/reject", methods=["POST"], strict_slashes=False)
def reject_run_patch(run_id):
    if not session_manager.reject_run(run_id):
        return "Run not awaiting approval", 400
    resp = make_response("", 204)
    resp.headers["HX-Trigger"] = "runRefresh"
    return resp


@app.route("/workspaces/<slug>/missions", methods=["POST"])
def start_mission(slug):
    ws = models.get_workspace(slug)
    if ws is None:
        return "Workspace not found", 404
    template_id = request.form.get("template_id", "").strip()
    goal = request.form.get("goal", "").strip()
    provider = request.form.get("provider") or None
    error = None
    if not template_id or not goal:
        error = "Template and goal are both required"
    else:
        try:
            mission.start_mission(ws["id"], template_id, goal, provider=provider)
        except ValueError as exc:
            error = str(exc)

    if error:
        resp = make_response(f"<p class='form-error'>{error}</p>")
        resp.headers["HX-Retarget"] = "#mission-form-error"
        resp.headers["HX-Reswap"] = "innerHTML"
        return resp
    return render_template("partials/mission_list.html", **_mission_list_context(ws))


@app.route("/workspaces/<slug>/mad-scientist", methods=["POST"])
def start_mad_scientist(slug):
    ws = models.get_workspace(slug)
    if ws is None:
        return "Workspace not found", 404
    goal = request.form.get("mad_goal", "").strip()
    provider = request.form.get("mad_provider") or None
    max_steps = request.form.get("mad_max_steps", type=int)
    max_fix_loops = request.form.get("mad_max_fix_loops", type=int)
    spend_raw = (request.form.get("mad_spend_cap_usd") or "").strip()
    parallel_raw = (request.form.get("mad_max_parallel_steps") or "").strip()
    spend_cap = None
    max_parallel_steps = None
    if parallel_raw:
        try:
            max_parallel_steps = int(parallel_raw)
        except ValueError:
            resp = make_response("<p class='form-error'>Max parallel steps must be a whole number</p>")
            resp.headers["HX-Retarget"] = "#mad-scientist-error"
            resp.headers["HX-Reswap"] = "innerHTML"
            return resp
    if spend_raw:
        try:
            spend_cap = float(spend_raw)
        except ValueError:
            spend_cap = None
            resp = make_response("<p class='form-error'>Spend cap must be a number of USD</p>")
            resp.headers["HX-Retarget"] = "#mad-scientist-error"
            resp.headers["HX-Reswap"] = "innerHTML"
            return resp

    try:
        mad_scientist.start_mission(
            ws["id"],
            goal,
            provider=provider,
            max_steps=max_steps,
            max_fix_loops=max_fix_loops,
            spend_cap_usd=spend_cap,
            max_parallel_steps=max_parallel_steps,
        )
    except ValueError as exc:
        resp = make_response(f"<p class='form-error'>{exc}</p>")
        resp.headers["HX-Retarget"] = "#mad-scientist-error"
        resp.headers["HX-Reswap"] = "innerHTML"
        return resp

    resp = make_response(render_template("partials/mission_list.html", **_mission_list_context(ws)))
    resp.headers["HX-Trigger"] = "runRefresh"
    return resp


@app.route(
    "/workspaces/<slug>/mad-scientist/<int:mission_id>/crew-confirmation",
    methods=["POST"],
)
def confirm_generated_crew(slug, mission_id):
    """Promote a generated crew into workspace memory, or keep it on this mission."""
    ws = models.get_workspace(slug)
    if ws is None:
        return "Workspace not found", 404
    graph_mission = models.get_mad_scientist_mission_for_mission(mission_id)
    if graph_mission is None or graph_mission["workspace_id"] != ws["id"]:
        return "Mad Scientist mission not found for this workspace", 404
    decision = (request.form.get("decision") or "").strip()
    try:
        pattern_synthesis.set_crew_confirmation(mission_id, ws["id"], decision)
    except ValueError as exc:
        resp = make_response(f"<p class='form-error'>{exc}</p>")
        resp.headers["HX-Retarget"] = "#mad-scientist-error"
        resp.headers["HX-Reswap"] = "innerHTML"
        return resp
    resp = make_response(render_template("partials/mission_list.html", **_mission_list_context(ws)))
    resp.headers["HX-Trigger"] = "runRefresh"
    return resp


@app.route("/workspaces/<slug>/mad-scientist/<int:mission_id>/retry-scout", methods=["POST"])
def retry_mad_scientist_scout(slug, mission_id):
    ws = models.get_workspace(slug)
    if ws is None:
        return "Workspace not found", 404
    graph_mission = models.get_mad_scientist_mission_for_mission(mission_id)
    if graph_mission is None or graph_mission["workspace_id"] != ws["id"]:
        return "Mad Scientist mission not found for this workspace", 404
    try:
        mad_scientist.retry_scout(mission_id)
    except ValueError as exc:
        resp = make_response(f"<p class='form-error'>{exc}</p>")
        resp.headers["HX-Retarget"] = "#mad-scientist-error"
        resp.headers["HX-Reswap"] = "innerHTML"
        return resp
    resp = make_response(render_template("partials/mission_list.html", **_mission_list_context(ws)))
    resp.headers["HX-Trigger"] = "runRefresh"
    return resp


@app.route("/workspaces/<slug>/missions/<int:mission_id>/resume", methods=["POST"])
def resume_mission_route(slug, mission_id):
    ws = models.get_workspace(slug)
    if ws is None:
        return "Workspace not found", 404
    mission_row = models.get_mission(mission_id)
    if mission_row is None or mission_row["workspace_id"] != ws["id"]:
        return "Mission not found", 404
    from_run_raw = request.form.get("from_run_id", "").strip()
    from_run_id = int(from_run_raw) if from_run_raw.isdigit() else None
    provider = request.form.get("provider") or None
    try:
        mission.resume_mission(mission_id, from_run_id=from_run_id, provider=provider)
    except ValueError as exc:
        return render_template(
            "partials/mission_list.html",
            resume_error=str(exc),
            **_mission_list_context(ws),
        )
    resp = make_response(render_template("partials/mission_list.html", **_mission_list_context(ws)))
    resp.headers["HX-Trigger"] = "runRefresh"
    return resp


@app.route("/partials/missions/<slug>")
def mission_list_partial(slug):
    """Polled to refresh mission/stage progress on the workspace page."""
    ws = models.get_workspace(slug)
    if ws is None:
        return "Workspace not found", 404
    return render_template("partials/mission_list.html", **_mission_list_context(ws))


# ---------- git sync ----------

@app.route("/workspaces/<slug>/git/<action>", methods=["POST"])
def workspace_git_action(slug, action):
    ws = models.get_workspace(slug)
    if ws is None:
        return "Workspace not found", 404
    if action == "pull":
        result = git_sync.pull(ws["repo_path"])
    elif action == "push":
        result = git_sync.push(ws["repo_path"])
    elif action == "status":
        result = git_sync.status(ws["repo_path"])
    else:
        return "Unknown action", 400
    return render_template("partials/git_result.html", action=action, result=result)


# ---------- schedules ----------

@app.route("/schedules")
def schedules_page():
    schedules = models.list_schedules()
    workspaces = models.list_workspaces()
    roles_by_workspace = {ws["id"]: models.list_roles(ws["id"]) for ws in workspaces}
    return render_template(
        "schedules.html",
        schedules=schedules,
        workspaces=workspaces,
        roles_by_workspace=roles_by_workspace,
        providers=providers.list_providers(),
        default_provider=providers.get_default_provider(),
    )


@app.route("/schedules", methods=["POST"])
def create_schedule():
    workspace_id = request.form["workspace_id"]
    role_id = request.form.get("role_id") or None
    task = request.form["task"].strip()
    cron_expr = request.form["cron_expr"].strip()
    provider = request.form.get("provider") or None
    try:
        models.create_schedule(workspace_id, role_id, task, cron_expr, provider=provider)
    except ValueError as exc:
        return str(exc), 400
    scheduler.reload_jobs()
    return redirect(url_for("schedules_page"))


@app.route("/schedules/<int:schedule_id>/toggle", methods=["POST"])
def toggle_schedule(schedule_id):
    schedule_row = models.get_schedule(schedule_id)
    if schedule_row is not None:
        models.set_schedule_enabled(schedule_id, not schedule_row["enabled"])
        scheduler.reload_jobs()
    return redirect(url_for("schedules_page"))


@app.route("/schedules/<int:schedule_id>/delete", methods=["POST"])
def remove_schedule(schedule_id):
    models.delete_schedule(schedule_id)
    scheduler.reload_jobs()
    return redirect(url_for("schedules_page"))


def main():
    """Entry point for `python app.py`, `python -m app`, or the `tank` console script."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((config.HOST, config.PORT))
    except OSError:
        print(
            f"\n[tank] ERROR: port {config.PORT} is already in use.\n"
            f"[tank] Another Tank instance is probably still running with OLD code.\n"
            f"[tank] Stop it first (Ctrl+C in that terminal, or kill the python process),\n"
            f"[tank] then start again from: {os.path.dirname(os.path.abspath(__file__))}\n"
        )
        raise SystemExit(1) from None
    finally:
        sock.close()

    print(f"[tank] version {config.TANK_VERSION}")
    print(f"[tank] templates: {app.template_folder}")
    default = providers.get_default_provider()
    key_status = providers.provider_key_status(default)
    if key_status.get("env"):
        env = key_status["env"]
        if key_status["set"]:
            print(f"[tank] {env}: set ({key_status['length']} chars)")
        else:
            print(f"[tank] WARNING: {env} is not set — {default} will fail")
    probe = providers.probe_provider(default)
    if probe.get("ok"):
        print(f"[tank] {default}: API auth OK")
    else:
        print(f"[tank] WARNING: {default} auth check failed: {probe}")
    print(f"[tank] http://{config.HOST}:{config.PORT}")
    app.run(host=config.HOST, port=config.PORT, debug=False)


# Start workers when the app module is loaded (dev server, WSGI, or `tank` CLI).
_start_background_workers()

if __name__ == "__main__":
    main()

"""
Data layer for Tank. Plain sqlite3, no ORM — same pattern used in
Campaign Console. Workspaces and roles are defined in workspaces.yaml
(so they're git-trackable) and synced into the DB on every startup.
Runs and schedules are created at runtime and live only in the DB.
"""
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import json

import yaml

import config


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_db():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    git_remote TEXT,
    mcp_config_path TEXT,
    default_provider TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    system_prompt TEXT,
    default_task TEXT,
    goal TEXT,
    backstory TEXT,
    tools TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(workspace_id, slug)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    role_id INTEGER REFERENCES agent_roles(id) ON DELETE SET NULL,
    provider TEXT,
    task TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    pid INTEGER,
    log_path TEXT,
    error TEXT,
    agent_payload TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    role_id INTEGER REFERENCES agent_roles(id) ON DELETE SET NULL,
    provider TEXT,
    task TEXT NOT NULL,
    cron_expr TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS missions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    template TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    current_stage TEXT,
    fix_loop_count INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id, key)
);

CREATE TABLE IF NOT EXISTS mad_scientist_missions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER NOT NULL UNIQUE REFERENCES missions(id) ON DELETE CASCADE,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'running',
    goal TEXT NOT NULL,
    summary TEXT,
    spend_cap_usd REAL,
    spend_usd REAL NOT NULL DEFAULT 0,
    max_parallel_steps INTEGER,
    blocked_reason TEXT,
    token_cap INTEGER,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mad_scientist_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    graph_mission_id INTEGER NOT NULL REFERENCES mad_scientist_missions(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    provider TEXT,
    task TEXT NOT NULL,
    write_allowed INTEGER NOT NULL DEFAULT 0,
    success_criteria TEXT,
    status TEXT NOT NULL DEFAULT 'planned',
    position INTEGER NOT NULL DEFAULT 0,
    unresolved_dependencies TEXT,
    blocked_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(graph_mission_id, name)
);

CREATE TABLE IF NOT EXISTS mad_scientist_step_dependencies (
    step_id INTEGER NOT NULL REFERENCES mad_scientist_steps(id) ON DELETE CASCADE,
    depends_on_step_id INTEGER NOT NULL REFERENCES mad_scientist_steps(id) ON DELETE CASCADE,
    PRIMARY KEY (step_id, depends_on_step_id)
);

CREATE TABLE IF NOT EXISTS mad_scientist_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_id INTEGER NOT NULL REFERENCES mad_scientist_steps(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    attempt_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    cost_usd REAL,
    tokens INTEGER,
    patch_hash TEXT,
    error_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    mission_id INTEGER REFERENCES missions(id) ON DELETE SET NULL,
    subject_run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    evaluator_run_id INTEGER UNIQUE REFERENCES runs(id) ON DELETE SET NULL,
    step_id INTEGER REFERENCES mad_scientist_steps(id) ON DELETE SET NULL,
    evaluator_provider TEXT,
    verdict TEXT NOT NULL,
    critique TEXT,
    created_at TEXT NOT NULL
);
"""


def _migrate_db(conn):
    """Add columns introduced after initial release."""
    migrations = [
        ("workspaces", "default_provider", "TEXT"),
        ("workspaces", "test_command", "TEXT"),
        ("runs", "provider", "TEXT"),
        ("runs", "agent_payload", "TEXT"),
        ("runs", "parent_run_id", "INTEGER REFERENCES runs(id) ON DELETE SET NULL"),
        ("runs", "mission_id", "INTEGER REFERENCES missions(id) ON DELETE SET NULL"),
        ("runs", "stage_name", "TEXT"),
        ("schedules", "provider", "TEXT"),
        ("missions", "provider", "TEXT"),
        # CrewAI-style agent attributes on roles (added in v2)
        ("agent_roles", "goal", "TEXT"),
        ("agent_roles", "backstory", "TEXT"),
        ("agent_roles", "tools", "TEXT"),
        # Per-step persona overrides from the Crew Orchestrator GUI (added in v3)
        ("runs", "persona_override", "TEXT"),
        # Back-reference to the crew parent run so UI can group sub-runs (added in v4)
        # Distinct from parent_run_id, which chains prior output context.
        ("runs", "crew_run_id", "INTEGER REFERENCES runs(id) ON DELETE SET NULL"),
        ("missions", "parent_run_id", "INTEGER REFERENCES runs(id) ON DELETE SET NULL"),
        ("missions", "plan_json", "TEXT"),
        ("missions", "max_steps", "INTEGER"),
        ("missions", "max_fix_loops", "INTEGER"),
        ("mad_scientist_missions", "max_parallel_steps", "INTEGER"),
        ("mad_scientist_missions", "blocked_reason", "TEXT"),
        ("mad_scientist_missions", "token_cap", "INTEGER"),
        ("mad_scientist_missions", "tokens_used", "INTEGER NOT NULL DEFAULT 0"),
        ("mad_scientist_missions", "max_attempts", "INTEGER"),
        ("mad_scientist_steps", "blocked_reason", "TEXT"),
        ("mad_scientist_attempts", "tokens", "INTEGER"),
        ("mad_scientist_attempts", "patch_hash", "TEXT"),
        ("mad_scientist_attempts", "error_hash", "TEXT"),
    ]
    for table, column, col_type in migrations:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)
        _migrate_db(conn)
    sync_workspaces_from_yaml()


def reload_config():
    """Reload YAML config files into memory and the DB."""
    import mission
    import providers

    providers.load_providers()
    mission.load_templates()
    sync_workspaces_from_yaml()


def sync_workspaces_from_yaml():
    """Upsert workspaces/roles from workspaces.yaml. Safe to call repeatedly."""
    try:
        with open(config.WORKSPACES_FILE) as f:
            doc = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return

    with get_db() as conn:
        for ws in doc.get("workspaces", []):
            conn.execute(
                """
                INSERT INTO workspaces (slug, name, repo_path, git_remote, mcp_config_path, default_provider, test_command, created_at)
                VALUES (:slug, :name, :repo_path, :git_remote, :mcp_config_path, :default_provider, :test_command, :created_at)
                ON CONFLICT(slug) DO UPDATE SET
                    name=excluded.name,
                    repo_path=excluded.repo_path,
                    git_remote=excluded.git_remote,
                    mcp_config_path=excluded.mcp_config_path,
                    default_provider=excluded.default_provider,
                    test_command=excluded.test_command
                """,
                {
                    "slug": ws["slug"],
                    "name": ws["name"],
                    "repo_path": config.normalize_path(ws["repo_path"]),
                    "git_remote": ws.get("git_remote"),
                    "mcp_config_path": config.normalize_path(ws.get("mcp_config_path")),
                    "default_provider": ws.get("default_provider"),
                    "test_command": ws.get("test_command"),
                    "created_at": _now(),
                },
            )
            workspace_id = conn.execute(
                "SELECT id FROM workspaces WHERE slug = ?", (ws["slug"],)
            ).fetchone()["id"]

            for role in ws.get("roles", []):
                raw_tools = role.get("tools")
                tools_json = (
                    json.dumps(raw_tools)
                    if isinstance(raw_tools, list)
                    else raw_tools  # already a string or None
                )
                conn.execute(
                    """
                    INSERT INTO agent_roles
                        (workspace_id, slug, name, system_prompt, default_task,
                         goal, backstory, tools, created_at)
                    VALUES
                        (:workspace_id, :slug, :name, :system_prompt, :default_task,
                         :goal, :backstory, :tools, :created_at)
                    ON CONFLICT(workspace_id, slug) DO UPDATE SET
                        name=excluded.name,
                        system_prompt=excluded.system_prompt,
                        default_task=excluded.default_task,
                        goal=excluded.goal,
                        backstory=excluded.backstory,
                        tools=excluded.tools
                    """,
                    {
                        "workspace_id": workspace_id,
                        "slug": role["slug"],
                        "name": role["name"],
                        "system_prompt": role.get("system_prompt"),
                        "default_task": role.get("default_task"),
                        "goal": role.get("goal"),
                        "backstory": role.get("backstory"),
                        "tools": tools_json,
                        "created_at": _now(),
                    },
                )


# ---------- workspaces ----------

def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "workspace"


def _read_workspaces_doc() -> dict:
    try:
        with open(config.WORKSPACES_FILE) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _write_workspaces_doc(doc: dict):
    header = (
        "# Workspace and agent-role definitions for Tank.\n"
        "# Edited by hand or created from the dashboard.\n\n"
    )
    with open(config.WORKSPACES_FILE, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.dump(doc, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def create_workspace(
    name,
    repo_path,
    slug=None,
    git_remote=None,
    default_provider=None,
    mcp_config_path=None,
):
    """Append a workspace to workspaces.yaml and sync it into the DB."""
    name = (name or "").strip()
    repo_path = config.normalize_path((repo_path or "").strip())
    slug = (slug or "").strip() or _slugify(name)

    if not name:
        raise ValueError("Name is required")
    if not repo_path:
        raise ValueError("Repository path is required")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("Slug must use lowercase letters, numbers, and hyphens")
    if get_workspace(slug):
        raise ValueError(f"Workspace '{slug}' already exists")
    if not os.path.isdir(repo_path):
        raise ValueError(f"Repository path does not exist: {repo_path}")

    entry = {"slug": slug, "name": name, "repo_path": repo_path}
    if git_remote:
        entry["git_remote"] = git_remote.strip()
    if default_provider:
        entry["default_provider"] = default_provider.strip()
    if mcp_config_path:
        entry["mcp_config_path"] = config.normalize_path(mcp_config_path.strip())

    doc = _read_workspaces_doc()
    workspaces = doc.get("workspaces") or []
    workspaces.append(entry)
    doc["workspaces"] = workspaces
    _write_workspaces_doc(doc)
    sync_workspaces_from_yaml()
    return slug


def delete_workspace(slug: str) -> None:
    """Remove a workspace from workspaces.yaml and the database."""
    slug = (slug or "").strip()
    ws = get_workspace(slug)
    if ws is None:
        raise ValueError(f"Workspace '{slug}' not found")

    with get_db() as conn:
        active = conn.execute(
            """
            SELECT COUNT(*) AS n FROM runs
            WHERE workspace_id = ? AND status IN ('pending', 'running', 'awaiting_approval')
            """,
            (ws["id"],),
        ).fetchone()["n"]
        if active:
            raise ValueError(
                f"Cannot delete workspace while {active} run(s) are pending or active"
            )

    doc = _read_workspaces_doc()
    workspaces = doc.get("workspaces") or []
    filtered = [w for w in workspaces if w.get("slug") != slug]
    if len(filtered) != len(workspaces):
        doc["workspaces"] = filtered
        _write_workspaces_doc(doc)

    with get_db() as conn:
        conn.execute("DELETE FROM workspaces WHERE slug = ?", (slug,))


def list_workspaces():
    with get_db() as conn:
        return conn.execute("SELECT * FROM workspaces ORDER BY name").fetchall()


def get_workspace(slug):
    with get_db() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE slug = ?", (slug,)).fetchone()


def get_workspace_by_id(workspace_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()


# ---------- roles ----------

def list_roles(workspace_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM agent_roles WHERE workspace_id = ? ORDER BY name", (workspace_id,)
        ).fetchall()


def get_role_by_id(role_id):
    if role_id is None:
        return None
    with get_db() as conn:
        return conn.execute("SELECT * FROM agent_roles WHERE id = ?", (role_id,)).fetchone()


def get_role_by_slug(workspace_id, slug):
    if not slug:
        return None
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM agent_roles WHERE workspace_id = ? AND slug = ?",
            (workspace_id, slug),
        ).fetchone()


# ---------- runs ----------

CHAINABLE_PARENT_STATUSES = ("done", "failed", "rejected")

def resolve_run_provider(workspace, provider=None):
    import providers

    if provider:
        return providers.resolve_provider(provider)
    if workspace and workspace["default_provider"]:
        return providers.resolve_provider(workspace["default_provider"])
    return providers.get_default_provider()


def _assert_role_in_workspace(workspace_id, role_id):
    """Reject a role id that belongs to a different workspace."""
    if role_id is None or role_id == "":
        return None
    role = get_role_by_id(role_id)
    if role is None or int(role["workspace_id"]) != int(workspace_id):
        raise ValueError("Role does not belong to this workspace")
    return role["id"]


def create_run(
    workspace_id, role_id, task, provider=None, parent_run_id=None, chain_latest=False,
    mission_id=None, stage_name=None, persona_override=None, crew_run_id=None,
):
    workspace = get_workspace_by_id(workspace_id)
    role_id = _assert_role_in_workspace(workspace_id, role_id)
    provider = resolve_run_provider(workspace, provider)

    if chain_latest:
        parent_run_id = get_latest_chainable_run_id(workspace_id)
    elif parent_run_id is not None:
        parent_run_id = _validate_parent_run(workspace_id, parent_run_id)

    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO runs
                (workspace_id, role_id, provider, task,
                 parent_run_id, mission_id, stage_name,
                 persona_override, crew_run_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (workspace_id, role_id, provider, task,
             parent_run_id, mission_id, stage_name,
             persona_override, crew_run_id, _now()),
        )
        return cur.lastrowid


def _validate_parent_run(workspace_id, parent_run_id):
    parent = get_run(parent_run_id)
    if parent is None or parent["workspace_id"] != workspace_id:
        raise ValueError("Selected prior run is not in this workspace")
    if parent["status"] not in CHAINABLE_PARENT_STATUSES:
        raise ValueError("Selected prior run must be finished (done, failed, or rejected)")
    return parent_run_id


def get_latest_chainable_run_id(workspace_id, before_run_id=None):
    with get_db() as conn:
        query = """
            SELECT id FROM runs
            WHERE workspace_id = ? AND status IN ('done', 'failed', 'rejected')
        """
        params = [workspace_id]
        if before_run_id is not None:
            query += " AND id < ?"
            params.append(before_run_id)
        query += " ORDER BY id DESC LIMIT 1"
        row = conn.execute(query, params).fetchone()
        return row["id"] if row else None


def list_chainable_runs(workspace_id, limit=20):
    with get_db() as conn:
        return conn.execute(
            """
            SELECT * FROM runs
            WHERE workspace_id = ? AND status IN ('done', 'failed', 'rejected')
            ORDER BY id DESC LIMIT ?
            """,
            (workspace_id, limit),
        ).fetchall()


def get_run(run_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def list_runs(workspace_id=None, limit=50):
    with get_db() as conn:
        if workspace_id is not None:
            return conn.execute(
                "SELECT * FROM runs WHERE workspace_id = ? ORDER BY id DESC LIMIT ?",
                (workspace_id, limit),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def create_adhoc_crew_run(workspace_id: int, task: str) -> int:
    """
    Create a crew-builder run with the literal provider label 'crew_builder',
    bypassing resolve_run_provider so the synthetic ID is stored verbatim.
    The run starts as 'pending' and must be transitioned to 'running' by
    session_manager.launch_crew_builder immediately after creation.
    """
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO runs
                (workspace_id, role_id, provider, task, status, created_at)
            VALUES (?, NULL, 'crew_builder', ?, 'pending', ?)
            """,
            (workspace_id, task, _now()),
        )
        return cur.lastrowid


def create_synthetic_parent_run(workspace_id: int, task: str, provider: str) -> int:
    """Create a synthetic parent run used only for grouping/logging child runs."""
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO runs
                (workspace_id, role_id, provider, task, status, created_at)
            VALUES (?, NULL, ?, ?, 'pending', ?)
            """,
            (workspace_id, provider, task, _now()),
        )
        return cur.lastrowid


def list_child_runs(crew_run_id: int):
    """Return runs that are direct children of a crew run, in creation order."""
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM runs WHERE crew_run_id = ? ORDER BY id",
            (crew_run_id,),
        ).fetchall()


def list_pending_runs():
    with get_db() as conn:
        # Exclude crew_builder runs: they are managed directly by
        # session_manager.launch_crew_builder and must not be picked up by
        # the generic queue worker (which has no crew_cfg to pass).
        return conn.execute(
            "SELECT * FROM runs WHERE status = 'pending'"
            " AND (provider IS NULL OR provider NOT IN ('crew_builder', 'mad_scientist')) ORDER BY id"
        ).fetchall()


def count_running_runs():
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE status = 'running'"
            " AND (provider IS NULL OR provider NOT IN ('crew_builder', 'mad_scientist'))"
        ).fetchone()
        return row["n"]


def list_running_runs():
    """
    All runs currently marked 'running', with no provider filter --
    unlike list_pending_runs/count_running_runs, this intentionally
    includes crew_builder and mad_scientist parent runs too, since a
    startup sweep needs to catch every run a crashed process left
    stranded, not just ones the generic queue worker would pick up.
    """
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM runs WHERE status = 'running' ORDER BY id"
        ).fetchall()


def get_mad_scientist_mission(graph_mission_id):
    """Graph row by mad_scientist_missions.id. Includes workspace_id."""
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM mad_scientist_missions WHERE id = ?",
            (graph_mission_id,),
        ).fetchone()


def get_mad_scientist_mission_for_mission(mission_id):
    """Graph row for a Tank mission id. The retry-scout URL uses that id."""
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM mad_scientist_missions WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()


def get_run_payload(run_id):
    run = get_run(run_id)
    if run is None or not run["agent_payload"]:
        return None
    import json

    return json.loads(run["agent_payload"])


def update_run(run_id, **fields):
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [run_id]
    with get_db() as conn:
        conn.execute(f"UPDATE runs SET {columns} WHERE id = ?", values)


# ---------- schedules ----------

def create_schedule(workspace_id, role_id, task, cron_expr, provider=None):
    workspace = get_workspace_by_id(workspace_id)
    role_id = _assert_role_in_workspace(workspace_id, role_id)
    provider = resolve_run_provider(workspace, provider)
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO schedules (workspace_id, role_id, provider, task, cron_expr, enabled, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (workspace_id, role_id, provider, task, cron_expr, _now()),
        )
        return cur.lastrowid


def get_schedule(schedule_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()


def list_schedules(enabled_only=False):
    with get_db() as conn:
        if enabled_only:
            return conn.execute("SELECT * FROM schedules WHERE enabled = 1").fetchall()
        return conn.execute("SELECT * FROM schedules ORDER BY id DESC").fetchall()


def set_schedule_enabled(schedule_id, enabled):
    with get_db() as conn:
        conn.execute(
            "UPDATE schedules SET enabled = ? WHERE id = ?", (1 if enabled else 0, schedule_id)
        )


def touch_schedule(schedule_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE schedules SET last_run_at = ? WHERE id = ?", (_now(), schedule_id)
        )


def delete_schedule(schedule_id):
    with get_db() as conn:
        conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))


# ---------- missions ----------

def create_mission(
    workspace_id,
    template,
    goal,
    current_stage,
    provider=None,
    parent_run_id=None,
    max_steps=None,
    max_fix_loops=None,
):
    now = _now()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO missions
                (workspace_id, template, goal, status, current_stage, provider,
                 parent_run_id, max_steps, max_fix_loops, created_at, updated_at)
            VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                template,
                goal,
                current_stage,
                provider,
                parent_run_id,
                max_steps,
                max_fix_loops,
                now,
                now,
            ),
        )
        return cur.lastrowid


def get_mission(mission_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()


def list_missions(workspace_id, limit=20):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM missions WHERE workspace_id = ? ORDER BY id DESC LIMIT ?",
            (workspace_id, limit),
        ).fetchall()


def update_mission(mission_id, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    columns = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [mission_id]
    with get_db() as conn:
        conn.execute(f"UPDATE missions SET {columns} WHERE id = ?", values)


def list_mission_runs(mission_id, limit=50):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM runs WHERE mission_id = ? ORDER BY id DESC LIMIT ?",
            (mission_id, limit),
        ).fetchall()[::-1]


# ---------- workspace memory ----------

def get_workspace_memory(workspace_id, key="mad_scientist"):
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM workspace_memory WHERE workspace_id = ? AND key = ?",
            (workspace_id, key),
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return {"raw": row["value"]}


def insert_evaluation(
    workspace_id,
    verdict,
    mission_id=None,
    subject_run_id=None,
    evaluator_run_id=None,
    step_id=None,
    evaluator_provider=None,
    critique=None,
):
    """Record one cross-model critique. A second row for the same evaluator run is ignored."""
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO evaluations
                (workspace_id, mission_id, subject_run_id, evaluator_run_id, step_id,
                 evaluator_provider, verdict, critique, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                mission_id,
                subject_run_id,
                evaluator_run_id,
                step_id,
                evaluator_provider,
                verdict,
                critique,
                _now(),
            ),
        )
        return cur.lastrowid


def list_evaluations_for_mission(mission_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM evaluations WHERE mission_id = ? ORDER BY id",
            (mission_id,),
        ).fetchall()


def upsert_workspace_memory(workspace_id, key, value):
    now = _now()
    payload = json.dumps(value, indent=2, sort_keys=True)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO workspace_memory (workspace_id, key, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(workspace_id, key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (workspace_id, key, payload, now),
        )

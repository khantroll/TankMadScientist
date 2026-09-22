"""
Central configuration for Tank.

Everything is overridable via environment variables so the same code
runs on any Python install (local venv, system Python, production WSGI).
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def normalize_path(path):
    """Expand ~ and env vars, then normalize for the current OS."""
    if not path:
        return path
    return os.path.normpath(os.path.expandvars(os.path.expanduser(path)))


def read_env_secret(name: str) -> str:
    """Read and trim an API key from the environment."""
    val = os.environ.get(name)
    return val.strip() if val else ""


def check_repo_path(repo_path: str) -> str | None:
    """Return an error message when repo_path cannot be used as a working directory."""
    path = normalize_path(repo_path) if repo_path else ""
    if not path:
        return "Repository path is not configured"
    if not os.path.isdir(path):
        return f"Repository path does not exist or is not a directory: {path}"
    return None

DATA_DIR = os.environ.get("TANK_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "tank.db")
LOGS_DIR = os.path.join(DATA_DIR, "logs")

WORKSPACES_FILE = os.environ.get(
    "TANK_WORKSPACES_FILE", os.path.join(BASE_DIR, "workspaces.yaml")
)

PROVIDERS_FILE = os.environ.get(
    "TANK_PROVIDERS_FILE", os.path.join(BASE_DIR, "providers.yaml")
)

MISSION_TEMPLATES_FILE = os.environ.get(
    "TANK_MISSION_TEMPLATES_FILE", os.path.join(BASE_DIR, "mission_templates.yaml")
)

# Hard cap on automatic fix-loop iterations per mission, used when a
# template doesn't specify its own max_fix_loops.
DEFAULT_MAX_FIX_LOOPS = int(os.environ.get("TANK_DEFAULT_MAX_FIX_LOOPS", "3"))

# Path/name of the Claude Code CLI binary. Override if it's not on PATH.
CLAUDE_BIN = os.environ.get("TANK_CLAUDE_BIN", "claude")

# How often the queue worker checks for pending runs, in seconds.
POLL_INTERVAL_SECONDS = float(os.environ.get("TANK_POLL_INTERVAL", "2"))

# Hard cap on simultaneously running agents, across all workspaces.
MAX_PARALLEL_RUNS = int(os.environ.get("TANK_MAX_PARALLEL_RUNS", "3"))

HOST = os.environ.get("TANK_HOST", "127.0.0.1")
PORT = int(os.environ.get("TANK_PORT", "8742"))

# Bump when UI or API behavior changes — shown in the dashboard footer.
TANK_VERSION = "0.2.0"

# How many lines of a run's log to show in the live output panel.
LOG_TAIL_LINES = int(os.environ.get("TANK_LOG_TAIL_LINES", "200"))

# Max bytes of a prior run's output to inject when chaining runs.
CHAIN_RUN_MAX_BYTES = int(os.environ.get("TANK_CHAIN_RUN_MAX_BYTES", "48000"))

# Emit HTML comments showing approval-render decisions for local UI debugging.
DEBUG_APPROVAL_RENDER = os.environ.get("TANK_DEBUG_APPROVAL_RENDER", "").lower() in {
    "1", "true", "yes", "on"
}

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

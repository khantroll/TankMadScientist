"""
Central configuration for Tank.

Everything is overridable via environment variables so the same code
runs on any Python install (local venv, system Python, production WSGI).
"""
import os
import sys

# TODO: BASE_DIR is the live checkout for `pip install -e .`. A non-editable
# install still has to find templates/, static/, and the default YAML configs
# through share/tank (see share_subdir and _default_config_file). Turning the
# flat modules into a tank/ package with package_data is out of scope; the
# documented workflow remains an editable install.
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

def share_subdir(name: str) -> str:
    """Prefer files next to the source tree, then the installed share directory."""
    local = os.path.join(BASE_DIR, name)
    if os.path.isdir(local):
        return local
    installed = os.path.join(sys.prefix, "share", "tank", name)
    if os.path.isdir(installed):
        return installed
    return local


def _default_config_file(name: str) -> str:
    local = os.path.join(BASE_DIR, name)
    if os.path.exists(local):
        return local
    installed = os.path.join(sys.prefix, "share", "tank", name)
    if os.path.exists(installed):
        return installed
    return local


DATA_DIR = os.environ.get("TANK_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "tank.db")
LOGS_DIR = os.path.join(DATA_DIR, "logs")

WORKSPACES_FILE = os.environ.get("TANK_WORKSPACES_FILE", _default_config_file("workspaces.yaml"))

PROVIDERS_FILE = os.environ.get("TANK_PROVIDERS_FILE", _default_config_file("providers.yaml"))

MISSION_TEMPLATES_FILE = os.environ.get(
    "TANK_MISSION_TEMPLATES_FILE", _default_config_file("mission_templates.yaml")
)

# Hard cap on attempts for one Mad Scientist step (first run, fixer, and
# retry share this budget). Also the YAML fix-loop default.
DEFAULT_MAX_FIX_LOOPS = int(os.environ.get("TANK_DEFAULT_MAX_FIX_LOOPS", "3"))

# Mission circuit breakers. Blank Mad Scientist form fields use these.
# Token cap is enforced only after a provider reports a token count.
DEFAULT_SPEND_CAP_USD = float(os.environ.get("TANK_DEFAULT_SPEND_CAP_USD", "5"))
DEFAULT_TOKEN_CAP = int(os.environ.get("TANK_DEFAULT_TOKEN_CAP", "500000"))

# Path/name of the Claude Code CLI binary. Override if it's not on PATH.
CLAUDE_BIN = os.environ.get("TANK_CLAUDE_BIN", "claude")

# How often the queue worker checks for pending runs, in seconds.
POLL_INTERVAL_SECONDS = float(os.environ.get("TANK_POLL_INTERVAL", "2"))

# Hard cap on simultaneously running agents, across all workspaces.
MAX_PARALLEL_RUNS = int(os.environ.get("TANK_MAX_PARALLEL_RUNS", "3"))

# Optional cap on how many Mad Scientist attempts one mission may have in
# flight (pending, running, or awaiting approval). 0 means no extra cap,
# so a wide DAG still shares only the global cap above.
MAX_PARALLEL_RUNS_PER_MISSION = int(os.environ.get("TANK_MAX_PARALLEL_RUNS_PER_MISSION", "0"))

HOST = os.environ.get("TANK_HOST", "127.0.0.1")
PORT = int(os.environ.get("TANK_PORT", "8742"))

# Bump when UI or API behavior changes — shown in the dashboard footer.
TANK_VERSION = "0.4.1"

# How many lines of a run's log to show in the live output panel.
LOG_TAIL_LINES = int(os.environ.get("TANK_LOG_TAIL_LINES", "200"))

# Max bytes of a prior run's output to inject when chaining runs.
CHAIN_RUN_MAX_BYTES = int(os.environ.get("TANK_CHAIN_RUN_MAX_BYTES", "48000"))

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

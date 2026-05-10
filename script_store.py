"""
script_store.py
---------------
In-memory cache of all scripts fetched from GitLab.

Scripts are loaded at startup and on-demand when:
  - POST /api/scripts/reload is called (by GitLab CI after MR merge)

Structure:
  _store = {
    "db": [
      {
        "name": "rotate-secret",
        "display_name": "rotate-secret",
        "description": "Rotate Kubernetes secret",
        "language": "python",
        "namespace": "db",
        "team": "db",
        "path": "scripts/db/rotate-secret",
        "script_file": "scripts/db/rotate-secret/script.py",
        "has_logo": True,
        "approval_required": True,
        "dependencies": ["kubernetes", "hvac"],
        "args": [...],
        "resources": {"cpu": "200m", "memory": "256Mi"},
      },
      ...
    ],
    "backend": [...],
  }
"""

import logging
import threading
from typing import Optional

import yaml

import config
import gitlab_client

logger = logging.getLogger(__name__)

_store: dict[str, list] = {}
_lock = threading.Lock()
_loaded = False


def _parse_script_yaml(raw: str, team: str, script_name: str, script_path: str, has_logo: bool) -> Optional[dict]:
    """Parse a script.yaml string into a script metadata dict."""
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        logger.error("Failed to parse script.yaml for %s/%s: %s", team, script_name, e)
        return None

    if not isinstance(data, dict):
        logger.error("script.yaml for %s/%s is not a dict", team, script_name)
        return None

    language = data.get("language", "").lower()
    if language not in ("python", "bash", "powershell"):
        logger.warning("script %s/%s has unknown language '%s' — skipping", team, script_name, language)
        return None

    ext_map = {"python": "py", "bash": "sh", "powershell": "ps1"}
    script_file = f"{script_path}/script.{ext_map[language]}"

    # Parse args
    raw_args = data.get("args", []) or []
    args = []
    for arg in raw_args:
        if not isinstance(arg, dict):
            continue
        arg_type = arg.get("type", "string").lower()
        if arg_type not in ("string", "integer", "boolean", "select"):
            logger.warning("Unknown arg type '%s' for arg '%s' — defaulting to string",
                           arg_type, arg.get("name", ""))
            arg_type = "string"
        # options can be a list (simple select) or dict (dependent select)
        raw_options = arg.get("options", [])
        parsed_arg = {
            "name":        arg.get("name", ""),
            "type":        arg_type,
            "required":    bool(arg.get("required", False)),
            "unit":        arg.get("unit", ""),
            "min":         arg.get("min", None),
            "max":         arg.get("max", None),
            "default":     arg.get("default", ""),
            "description": arg.get("description", ""),
            "example":     str(arg.get("example", "")),
            "options":     raw_options,        # list or dict — preserved as-is
            "depends_on":  arg.get("depends_on", ""),  # parent arg name if dependent
        }
        args.append(parsed_arg)

    # Parse resources
    resources_raw = data.get("resources", {}) or {}
    resources = {
        "cpu":    str(resources_raw.get("cpu",    "200m")),
        "memory": str(resources_raw.get("memory", "256Mi")),
    }

    # Approval
    approval = data.get("approval", {})
    if isinstance(approval, dict):
        approval_required = bool(approval.get("required", True))
    else:
        approval_required = bool(approval)

    # Namespace
    namespace = data.get("namespace", team)

    # Dependencies
    deps = data.get("dependencies", []) or []
    if isinstance(deps, str):
        deps = [d.strip() for d in deps.split(",") if d.strip()]

    script = {
        "name":             data.get("name", script_name),
        "folder_name":      script_name,
        "description":      data.get("description", ""),
        "language":         language,
        "namespace":        namespace,
        "team":             team,
        "path":             script_path,
        "script_file":      script_file,
        "has_logo":         has_logo,
        "approval_required": approval_required,
        "dependencies":     deps,
        "args":             args,
        "resources":        resources,
    }

    logger.debug("Parsed script: %s/%s lang=%s args=%d approval=%s",
                 team, script_name, language, len(args), approval_required)
    return script


def load_all(teams: list[str] = None) -> None:
    """Fetch and parse all scripts from GitLab for all configured teams."""
    global _store, _loaded
    teams = teams or config.TEAMS
    logger.info("Loading scripts from GitLab for teams: %s", teams)

    new_store: dict[str, list] = {}

    for team in teams:
        logger.info("Fetching scripts for team: %s", team)
        team_scripts = []

        try:
            script_entries = gitlab_client.fetch_team_scripts(team)
        except Exception as e:
            logger.error("Failed to fetch scripts for team %s: %s", team, e)
            new_store[team] = []
            continue

        for entry in script_entries:
            script_name = entry["name"]
            script_path = entry["path"]
            has_logo    = entry["has_logo"]

            yaml_path = f"{script_path}/script.yaml"
            raw_yaml = gitlab_client.get_file_content(yaml_path)
            if not raw_yaml:
                logger.warning("Could not fetch script.yaml for %s/%s", team, script_name)
                continue

            parsed = _parse_script_yaml(raw_yaml, team, script_name, script_path, has_logo)
            if parsed:
                team_scripts.append(parsed)

        new_store[team] = team_scripts
        logger.info("Team %s: loaded %d scripts", team, len(team_scripts))

    with _lock:
        _store = new_store
        _loaded = True

    total = sum(len(v) for v in new_store.values())
    logger.info("Script store loaded: %d total scripts across %d teams", total, len(teams))


def get_all() -> dict[str, list]:
    """Return the full store {team: [script, ...]}."""
    with _lock:
        return dict(_store)


def get_team(team: str) -> list:
    """Return scripts for a single team."""
    with _lock:
        return list(_store.get(team, []))


def get_script(team: str, script_name: str) -> Optional[dict]:
    """Find a specific script by team and folder name."""
    with _lock:
        for s in _store.get(team, []):
            if s["folder_name"] == script_name:
                return dict(s)
    return None


def is_loaded() -> bool:
    with _lock:
        return _loaded


def reload_async(teams: list[str] = None) -> None:
    """Trigger a reload in a background thread (non-blocking)."""
    t = threading.Thread(target=load_all, args=(teams,), daemon=True)
    t.start()
    logger.info("Script store reload started in background")
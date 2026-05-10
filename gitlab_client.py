"""
gitlab_client.py
----------------
All GitLab API interactions:
  - Fetch script tree (list teams/scripts)
  - Fetch file contents (script.yaml, logo.png)
  - Create branch, push files, open MR for new script upload

Uses GitLab REST API v4.
All calls are authenticated with GITLAB_TOKEN from env (k8s Secret).
"""

import base64
import logging
import re
import time
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

# GitLab API base
# GITLAB_REPO_PATH can be either:
#   - "group/repo-name"  → encoded as group%2Frepo-name (GitLab standard)
#   - "12345"            → numeric project ID, no encoding needed
def _api(path: str) -> str:
    repo_path = config.GITLAB_REPO_PATH.strip()
    if repo_path.isdigit():
        # Numeric project ID — use as-is
        repo = repo_path
    else:
        # Namespace/project path — encode / as %2F per GitLab API spec
        repo = requests.utils.quote(repo_path, safe="")
    return f"{config.GITLAB_URL}/api/v4/projects/{repo}{path}"


def _headers() -> dict:
    return {
        "PRIVATE-TOKEN": config.GITLAB_TOKEN,
        "Content-Type": "application/json",
    }


def _get(url: str, **kwargs) -> requests.Response:
    logger.debug("GitLab GET %s", url)
    resp = requests.get(url, headers=_headers(), timeout=10, verify=False, **kwargs)
    logger.debug("GitLab GET %s → %d", url, resp.status_code)
    return resp


def _post(url: str, json: dict) -> requests.Response:
    logger.debug("GitLab POST %s payload_keys=%s", url, list(json.keys()))
    resp = requests.post(url, headers=_headers(), json=json, timeout=15, verify=False)
    logger.debug("GitLab POST %s → %d", url, resp.status_code)
    return resp


# ── Fetch helpers ─────────────────────────────────────────────────────────────

def list_tree(path: str = "", ref: str = None) -> list:
    """List files/dirs at a given path in the repo."""
    ref = ref or config.GITLAB_DEFAULT_BRANCH
    url = _api(f"/repository/tree")
    params = {"path": path, "ref": ref, "per_page": 100}
    resp = _get(url, params=params)
    if resp.status_code != 200:
        logger.error("GitLab list_tree failed: path=%s status=%d body=%s",
                     path, resp.status_code, resp.text[:300])
        return []
    items = resp.json()
    logger.debug("list_tree path=%s → %d items", path, len(items))
    return items


def get_file_content(file_path: str, ref: str = None) -> Optional[str]:
    """Fetch a text file from the repo. Returns decoded string or None."""
    ref = ref or config.GITLAB_DEFAULT_BRANCH
    encoded_path = requests.utils.quote(file_path, safe="")
    url = _api(f"/repository/files/{encoded_path}")
    resp = _get(url, params={"ref": ref})
    if resp.status_code == 404:
        logger.debug("File not found in GitLab: %s", file_path)
        return None
    if resp.status_code != 200:
        logger.error("GitLab get_file failed: path=%s status=%d body=%s",
                     file_path, resp.status_code, resp.text[:300])
        return None
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    logger.debug("Fetched file %s (%d bytes)", file_path, len(content))
    return content


def get_file_raw_bytes(file_path: str, ref: str = None) -> Optional[bytes]:
    """Fetch a binary file (e.g. logo.png) from the repo. Returns bytes or None."""
    ref = ref or config.GITLAB_DEFAULT_BRANCH
    encoded_path = requests.utils.quote(file_path, safe="")
    url = _api(f"/repository/files/{encoded_path}/raw")
    resp = _get(url, params={"ref": ref})
    if resp.status_code == 404:
        logger.debug("Binary file not found: %s", file_path)
        return None
    if resp.status_code != 200:
        logger.error("GitLab get_raw failed: path=%s status=%d", file_path, resp.status_code)
        return None
    logger.debug("Fetched binary file %s (%d bytes)", file_path, len(resp.content))
    return resp.content


# ── Script tree ───────────────────────────────────────────────────────────────

def fetch_team_scripts(team: str) -> list[dict]:
    """
    Fetch all scripts for a team.
    Returns list of dicts with keys: name, path, has_logo
    """
    # Build path — if SCRIPTS_BASE_PATH is empty, teams are at repo root
    base = f"{config.SCRIPTS_BASE_PATH}/{team}".lstrip("/") if config.SCRIPTS_BASE_PATH else team
    items = list_tree(base)
    scripts = []
    for item in items:
        if item.get("type") != "tree":
            continue
        script_name = item["name"]
        script_path = f"{base}/{script_name}"
        # Check what files exist in this script folder
        files = list_tree(script_path)
        file_names = [f["name"] for f in files]
        has_logo = any(f in file_names for f in ("logo.png", "logo.jpg", "logo.jpeg"))
        has_yaml = "script.yaml" in file_names
        if not has_yaml:
            logger.warning("Script %s/%s has no script.yaml — skipping", team, script_name)
            continue
        scripts.append({
            "name": script_name,
            "path": script_path,
            "has_logo": has_logo,
            "team": team,
        })
    logger.info("Team %s: found %d scripts", team, len(scripts))
    return scripts


# ── Upload new script ─────────────────────────────────────────────────────────

SCRIPT_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def validate_script_name(name: str) -> Optional[str]:
    """Returns error message or None if valid."""
    if not name:
        return "Script name is required"
    if not SCRIPT_NAME_RE.match(name):
        return "Script name must be lowercase letters, numbers and hyphens only (e.g. my-script)"
    if len(name) > 60:
        return "Script name must be 60 characters or fewer"
    return None


def create_script_mr(
    team: str,
    script_name: str,
    language: str,
    description: str,
    script_content: str,
    yaml_content: str,
    logo_bytes: Optional[bytes],
    approval_required: bool,
    logo_ext: str = "png",
) -> dict:
    """
    Creates a branch, pushes script files, opens an MR.
    Returns dict with 'mr_url' on success, 'error' on failure.
    """
    branch = f"add-{script_name}-{int(time.time())}"
    script_ext = {"python": "py", "bash": "sh", "powershell": "ps1"}[language]
    # Build path — if SCRIPTS_BASE_PATH is empty, teams are at repo root
    base_path = f"{config.SCRIPTS_BASE_PATH}/{team}/{script_name}".lstrip("/") if config.SCRIPTS_BASE_PATH else f"{team}/{script_name}"

    logger.info("Creating MR for script %s/%s branch=%s", team, script_name, branch)

    # 1. Create branch
    resp = _post(_api("/repository/branches"), {
        "branch": branch,
        "ref": config.GITLAB_DEFAULT_BRANCH,
    })
    if resp.status_code not in (200, 201):
        logger.error("Failed to create branch %s: %d %s", branch, resp.status_code, resp.text[:300])
        return {"error": f"Failed to create branch: {resp.json().get('message', resp.text[:100])}"}

    # 2. Build commit actions
    actions = [
        {
            "action": "create",
            "file_path": f"{base_path}/script.{script_ext}",
            "content": script_content,
        },
        {
            "action": "create",
            "file_path": f"{base_path}/script.yaml",
            "content": yaml_content,
        },
    ]
    if logo_bytes:
        actions.append({
            "action": "create",
            "file_path": f"{base_path}/logo.{logo_ext}",
            "content": base64.b64encode(logo_bytes).decode(),
            "encoding": "base64",
        })

    # 3. Commit all files
    resp = _post(_api("/repository/commits"), {
        "branch": branch,
        "commit_message": f"feat: add script {team}/{script_name}",
        "actions": actions,
    })
    if resp.status_code not in (200, 201):
        logger.error("Failed to commit files: %d %s", resp.status_code, resp.text[:300])
        return {"error": f"Failed to commit files: {resp.json().get('message', resp.text[:100])}"}

    logger.info("Committed %d files to branch %s", len(actions), branch)

    # 4. Open MR
    mr_desc = (
        f"## New Script: `{script_name}`\n\n"
        f"**Team:** {team}  \n"
        f"**Language:** {language}  \n"
        f"**Description:** {description}  \n"
        f"**Approval required:** {'Yes' if approval_required else 'No'}  \n\n"
        f"---\n*Opened automatically by Eden*"
    )
    resp = _post(_api("/merge_requests"), {
        "source_branch": branch,
        "target_branch": config.GITLAB_DEFAULT_BRANCH,
        "title": f"feat: add script {team}/{script_name}",
        "description": mr_desc,
        "remove_source_branch_on_merge": True,
    })
    if resp.status_code not in (200, 201):
        logger.error("Failed to open MR: %d %s", resp.status_code, resp.text[:300])
        return {"error": f"Failed to open MR: {resp.json().get('message', resp.text[:100])}"}

    mr_url = resp.json().get("web_url", "")
    logger.info("MR opened: %s", mr_url)
    return {"mr_url": mr_url, "branch": branch}
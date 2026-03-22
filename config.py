"""
config.py
---------
Single source of truth for all environment variables and configuration.
All values come from:
  - k8s ConfigMap (eden-config) mounted as env vars
  - k8s Secret (eden-secrets) mounted as env vars
  - Defaults where safe

Missing REQUIRED vars are logged as ERROR at startup.
Missing OPTIONAL vars are logged as WARNING.
"""

import os
import logging

logger = logging.getLogger(__name__)


def _required(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        logger.error("REQUIRED env var '%s' is not set — functionality will be broken", key)
    else:
        logger.debug("Config loaded: %s = [SET]", key)
    return val


def _optional(key: str, default: str = "") -> str:
    val = os.environ.get(key, default).strip()
    if not val:
        logger.warning("Optional env var '%s' is not set, using default: '%s'", key, default)
    else:
        logger.debug("Config loaded: %s = %s", key, val if "TOKEN" not in key and "PASSWORD" not in key else "[REDACTED]")
    return val


# ── Portal identity ───────────────────────────────────────────────────────────
PORTAL_TITLE  = _optional("PORTAL_TITLE",  "Eden")
TEAM_NAME     = _optional("TEAM_NAME",     "Platform Engineering")
LOG_LEVEL     = _optional("LOG_LEVEL",     "INFO")
PORT          = int(_optional("PORT",      "5000"))

# ── Sites (web app links) ─────────────────────────────────────────────────────
# Path to sites.json — should be a ConfigMap volume mount
SITES_FILE    = _optional("SITES_FILE",    "/config/sites.json")

# ── SMB / installs share ──────────────────────────────────────────────────────
SMB_SERVER    = _optional("SMB_SERVER",    "")
SMB_SHARE     = _optional("SMB_SHARE",     "installs")
SMB_BASE_PATH = _optional("SMB_BASE_PATH", "")
SMB_USER      = _optional("SMB_USER",      "")
SMB_PASSWORD  = _optional("SMB_PASSWORD",  "")   # from secret
SMB_DOMAIN    = _optional("SMB_DOMAIN",    "")
SMB_CACHE_TTL = int(_optional("SMB_CACHE_TTL", "60"))

# ── GitLab ────────────────────────────────────────────────────────────────────
GITLAB_URL        = _optional("GITLAB_URL",        "https://git")
GITLAB_REPO_PATH  = _optional("GITLAB_REPO_PATH",  "")   # e.g. my-group/eden-scripts
GITLAB_TOKEN      = _optional("GITLAB_TOKEN",       "")   # from secret
GITLAB_DEFAULT_BRANCH = _optional("GITLAB_DEFAULT_BRANCH", "main")

# ── Argo Workflows ────────────────────────────────────────────────────────────
ARGO_URL      = _optional("ARGO_URL",      "https://argoworkflow.example")
ARGO_TOKEN    = _optional("ARGO_TOKEN",    "")   # from secret

# ── Teams ─────────────────────────────────────────────────────────────────────
# Comma-separated list of team names, e.g. "db,backend,infra,frontend"
_teams_raw    = _optional("TEAMS", "")
TEAMS         = [t.strip() for t in _teams_raw.split(",") if t.strip()] if _teams_raw else []
if not TEAMS:
    logger.warning("TEAMS env var is empty — no script rows will be rendered")
else:
    logger.info("Teams configured: %s", TEAMS)

# ── Webhook security ──────────────────────────────────────────────────────────
RELOAD_TOKEN  = _optional("RELOAD_TOKEN", "")   # from secret
if not RELOAD_TOKEN:
    logger.warning("RELOAD_TOKEN not set — /api/scripts/reload endpoint is UNPROTECTED")

# ── Script store ──────────────────────────────────────────────────────────────
SCRIPTS_BASE_PATH = _optional("SCRIPTS_BASE_PATH", "scripts")   # path inside the repo
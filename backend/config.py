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
# DEPRECATED — sites now live in Postgres. Kept only as a one-time migration
# fallback if DB is unreachable at startup. Will be removed once DB is stable.
SITES_JSON    = _optional("SITES_JSON", "")

# ── Database (Postgres) ───────────────────────────────────────────────────────
DB_HOST     = _optional("DB_HOST",     "eden-postgres")
DB_PORT     = _optional("DB_PORT",     "5432")
DB_NAME     = _optional("DB_NAME",     "eden")
DB_USER     = _optional("DB_USER",     "eden")
DB_PASSWORD = _optional("DB_PASSWORD", "")   # from secret

# ── Authentication (oauth2-proxy) ─────────────────────────────────────────────
# Eden calls oauth2-proxy /oauth2/userinfo directly using the browser's
# session cookie — no nginx auth annotations needed.
AUTH_ENABLED     = _optional("AUTH_ENABLED", "false").lower() == "true"

# Internal cluster URL of the oauth2-proxy service.
# e.g. http://oauth2-proxy.oauth2-namespace.svc.cluster.local
OAUTH2_PROXY_URL = _optional("OAUTH2_PROXY_URL", "")

# Flask session secret
FLASK_SECRET_KEY = _optional("FLASK_SECRET_KEY", "dev-secret-change-me")

# Temporary stand-in username while AUTH_ENABLED=false, so star/submit
# features can be tested without ADFS wired up yet.
DEV_USERNAME = _optional("DEV_USERNAME", "dev-user")

# ── Admin users ───────────────────────────────────────────────────────────────
# Admin management is handled via the database (users table) once ADFS is
# enabled. While AUTH_ENABLED=false, everyone is treated as admin for testing.
# ADMIN_USERS config var is intentionally removed — do not add it to ConfigMap.

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
# SCRIPTS_BASE_PATH: subfolder inside the repo containing team folders.
# Leave empty if team folders are at the repo root.
SCRIPTS_BASE_PATH = os.environ.get("SCRIPTS_BASE_PATH", "").strip()
logger.debug("Config loaded: SCRIPTS_BASE_PATH = '%s'", SCRIPTS_BASE_PATH or "(root)")

# ── S3-compatible download storage ───────────────────────────────────────────
S3_ENDPOINT_URL      = _optional("S3_ENDPOINT_URL", "")
S3_REGION            = _optional("S3_REGION", "us-east-1")
S3_BUCKET            = _optional("S3_BUCKET", "")
S3_PREFIX            = _optional("S3_PREFIX", "downloads")
S3_ACCESS_KEY_ID     = _optional("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = _optional("S3_SECRET_ACCESS_KEY", "")
S3_ADDRESSING_STYLE  = _optional("S3_ADDRESSING_STYLE", "path")
S3_VERIFY_TLS        = _optional("S3_VERIFY_TLS", "true")
S3_CA_BUNDLE         = _optional("S3_CA_BUNDLE", "")
S3_PRESIGN_TTL       = int(_optional("S3_PRESIGN_TTL", "300"))

DOWNLOAD_DEFAULT_PAGE_SIZE = int(_optional("DOWNLOAD_DEFAULT_PAGE_SIZE", "24"))
DOWNLOAD_MAX_PAGE_SIZE     = int(_optional("DOWNLOAD_MAX_PAGE_SIZE", "100"))
DOWNLOAD_UPLOAD_MAX_BYTES  = int(_optional("DOWNLOAD_UPLOAD_MAX_BYTES", "10737418240"))
DOWNLOAD_UPLOAD_MULTIPART_OVERHEAD_BYTES = int(
    _optional("DOWNLOAD_UPLOAD_MULTIPART_OVERHEAD_BYTES", "16777216")
)

# ── Banner options for site cards ────────────────────────────────────────────
# Each entry: { "value": str, "label": str, "color": hex }
# "value" is stored in DB, "label" shown in UI, "color" is the banner color.
# Edit this list to add/remove/rename banner options.
BANNER_OPTIONS = [
    {"value": "",           "label": "None",       "color": None       },
    {"value": "deprecated", "label": "Deprecated", "color": "#E24B4A"  },
    {"value": "new",        "label": "New",        "color": "#22c55e"  },
    {"value": "np",         "label": "NP",         "color": "#3b82f6"  },
]

# ── Env colors for grouped-card environment rows ─────────────────────────────
# Each entry: { "value": str, "label": str, "color": hex }
# "value" is stored on the site row (env_color), "color" is used for the
# bullet + frame on that environment's row in a GroupedSiteCard.
ENV_COLOR_OPTIONS = [
    {"value": "",       "label": "None",   "color": None      },
    {"value": "green",  "label": "Green",  "color": "#22c55e" },
    {"value": "orange", "label": "Orange", "color": "#f59e0b" },        
    {"value": "purple", "label": "Purple", "color": "#a855f7" },
    {"value": "blue",   "label": "Blue",   "color": "#3b82f6" },
    {"value": "red",    "label": "Red",    "color": "#ef4444" },
]
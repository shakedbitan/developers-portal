"""
app.py
------
Eden -- Developer Portal
Flask application entry point.

Routes:
  GET  /                            -> main portal page
  GET  /api/apps                    -> JSON list of install apps (SMB)
  GET  /api/scripts                 -> JSON list of all scripts by team
  POST /api/scripts/reload          -> webhook: reload script cache from GitLab
  POST /api/scripts/submit          -> submit a workflow to Argo
  POST /api/scripts/upload          -> upload new script (creates GitLab MR)
  POST /api/scripts/upload-arg-file -> stateless: .js file in -> oneline-base64 value out
  GET  /api/scripts/<t>/<n>/logo    -> proxy script logo from GitLab
  GET  /download/<path>             -> stream installer from SMB
  GET  /favicon-proxy/<slug>        -> proxy favicon from internal site
"""

import base64
import concurrent.futures
import json
import logging
import os
import re
import sys
import threading

import requests
import yaml
from flask import (Flask, Response, jsonify, render_template,
                   request, stream_with_context, abort, send_from_directory)

# ── Logging setup ─────────────────────────────────────────────────────────────
_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)-20s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)
logger.info("Eden starting up -- log level: %s", _log_level)

import config
import smb_scanner
import icon_resolver
import gitlab_client
import argo_client
import script_store
import download_catalog
import db
import auth

import os as _os

# In production, Flask serves the React build from ../frontend/dist/
# app.py lives at /app/app.py in Docker (WORKDIR /app, COPY backend/ ./)
# React build is at /app/frontend/dist (COPY --from=frontend-builder /app/frontend/dist ./frontend/dist)
# So frontend/dist is a SIBLING of app.py, not a parent
_FRONTEND_DIST = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'frontend', 'dist')

# No static_folder — we serve the React build manually in the catch-all route
# so Flask's built-in static handler doesn't intercept our SPA routes
app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY

# Register download catalog routes
download_catalog.register_download_routes(app)


# ── TLS verification ──────────────────────────────────────────────────────────
# Use /etc/ssl/certs (internal CA directory) if available.
# requests/urllib3 supports a directory path natively.
_CA_CERT  = "/etc/ssl/certs/mycert.pem"
_CA_DIR   = "/etc/ssl/certs"
CA_VERIFY = _CA_CERT if os.path.isfile(_CA_CERT) else (_CA_DIR if os.path.isdir(_CA_DIR) else False)
if CA_VERIFY:
    logger.info("TLS: using CA bundle/dir %s", CA_VERIFY)
else:
    logger.warning("TLS: CA directory %s not found -- verification disabled", _CA_DIR)

# ── Static paths ──────────────────────────────────────────────────────────────
SITE_IMAGES_DIR   = os.path.join("static", "site-images")
FAVICON_CACHE_DIR = os.path.join("static", "site-images", "_favicon_cache")
os.makedirs(FAVICON_CACHE_DIR, exist_ok=True)


# ── Favicon helpers ───────────────────────────────────────────────────────────
# DISABLED — favicons looked pixelated/inconsistent. Sites now use ONLY
# manually-placed images in static/site-images/<slug>.<ext>, falling back
# to the generic placeholder. No network fetching, no caching needed.

# ── Startup ───────────────────────────────────────────────────────────────────

def _startup():
    # 1. Load scripts from GitLab
    # Always call load_all() even with TEAMS unset -- script_store handles
    # an empty team list fine (loads nothing), and falls back to the local
    # scripts.json fixture when GitLab returns zero scripts, which is how
    # local dev populates the Scripts page without a real GitLab/TEAMS setup.
    if config.TEAMS:
        logger.info("Startup: loading scripts for teams: %s", config.TEAMS)
    else:
        logger.warning("Startup: TEAMS not configured -- trying local scripts fallback")
    script_store.load_all()

    # 2. Initialize Postgres connection pool and schema
    logger.info("Startup: initializing database connection")
    db.init_pool()
    if db.is_available():
        db.init_schema()
        download_catalog.init_schema()
        logger.info("Startup: database ready")
    else:
        logger.error("Startup: database unavailable -- sites/stars features will not work")

threading.Thread(target=_startup, daemon=True).start()


# ── Site image helpers ────────────────────────────────────────────────────────

# Optional manual image overrides — place in frontend/public/site-images/<slug>.<ext>
# These are served by React's static file serving.
# Two locations are checked: "public" is the dev-time source (Vite's dev
# server serves it directly, live, with npm run dev -- no build needed) and
# "dist" is where it ends up after `npm run build` (Vite copies public/ into
# dist/, and that's what Flask serves in prod). Checking both means dropping
# a file in public/site-images/ works immediately in dev *and* survives a
# production build.
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_IMAGES_DIRS = [
    os.path.join(_BASE_DIR, "..", "frontend", "public", "site-images"),
    os.path.join(_BASE_DIR, "..", "frontend", "dist",   "site-images"),
]


def _slug(name: str) -> str:
    return name.lower().replace(" ", "_").replace("-", "_")


def _resolve_site_image(name: str, favicon_url: str = "") -> str:
    """
    Priority:
    1. Manual override image in frontend/public/site-images/<slug>.<ext> (or dist/, post-build)
    2. favicon_url stored in DB (data URL or http URL — both work in <img src>)
    3. Placeholder
    """
    slug = _slug(name)
    for ext in ("png", "ico", "jpg", "jpeg", "svg"):
        for images_dir in SITE_IMAGES_DIRS:
            fpath = os.path.join(images_dir, f"{slug}.{ext}")
            if os.path.exists(fpath):
                return f"/site-images/{slug}.{ext}"
    if favicon_url:
        return favicon_url
    return "/icons/placeholder.svg"


_LOCAL_SITES_FILE = os.path.join(_BASE_DIR, "..", "sites.json")

# DEV-ONLY in-memory overrides for editing local-fallback sites. Mirrors
# _local_stars below: without this, /api/sites/edit always fails with
# "couldn't save" when running against sites.json (no real Postgres) --
# correct for a real deployment (its DB is always up), but it means an
# admin-edit flow like changing a site's env_color can never actually be
# exercised locally. Resets on every backend restart.
_local_site_overrides_lock = threading.Lock()
_local_site_overrides: dict = {}  # site_id (int) -> dict of overridden fields


def _load_local_sites_fallback() -> list:
    """
    DEV-ONLY fallback: when Postgres has no sites (e.g. running locally
    without a seeded/available DB), serve the hardcoded list from
    sites.json at the repo root instead of an empty portal.
    Never kicks in in a real deployment -- a seeded DB won't be empty.
    """
    try:
        with open(_LOCAL_SITES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        sites = []
        for i, s in enumerate(raw):
            site_id = 900000 + i  # real int, not "local-N" -- see edit_local_site below
            site = {
                "id": site_id,
                "name": s.get("name", ""),
                "url": s.get("url", ""),
                "favicon_url": s.get("favicon_url", ""),
                "tags": s.get("tags") or [],
                "group_name": s.get("group_name"),
                "group_display_name": s.get("group_display_name"),
                "env_label": s.get("env_label"),
                "env_color": s.get("env_color"),
            }
            with _local_site_overrides_lock:
                site.update(_local_site_overrides.get(site_id, {}))
            sites.append(site)
        logger.warning("Database has no sites -- serving %d hardcoded sites from %s",
                        len(sites), _LOCAL_SITES_FILE)
        return sites
    except Exception as e:
        logger.error("Failed to load local sites fallback: %s", e)
        return []


def _edit_local_site(site_id: int, **fields) -> bool:
    if site_id not in {s["id"] for s in _load_local_sites_fallback()}:
        return False
    with _local_site_overrides_lock:
        existing = _local_site_overrides.setdefault(site_id, {})
        for key, value in fields.items():
            if value is not None:
                existing[key] = value
    return True


def _load_sites() -> list:
    """Load all approved sites from Postgres (falls back to sites.json if empty)."""
    try:
        sites = db.get_all_sites()
        if not sites:
            sites = _load_local_sites_fallback()
        for site in sites:
            site["image_url"] = _resolve_site_image(site["name"], site.get("favicon_url", "") or "")
            # Auto-compute group_display_name if not set
            if site.get("group_name") and not site.get("group_display_name"):
                site["group_display_name"] = site["group_name"].capitalize()
            # Attach banner color from config so the frontend doesn't need it
            banner_val = (site.get("tags") or [None])[0]
            site["banner_color"] = _BANNER_COLOR.get(banner_val) if banner_val else None
            # Resolved hex for the env-row bullet/frame in GroupedSiteCard.
            # env_color itself stays as the raw value (e.g. "green") so the
            # Edit modal's <select> can still preselect it correctly.
            env_val = site.get("env_color")
            site["env_color_hex"] = _ENV_COLOR.get(env_val) if env_val else None
        logger.debug("Loaded %d sites", len(sites))
        return sites
    except Exception as e:
        logger.error("Failed to load sites from database: %s", e)
        return []


# ── Arg validation ────────────────────────────────────────────────────────────

def _validate_args(user_args: dict, arg_defs: list) -> dict:
    errors = {}
    for arg_def in arg_defs:
        name     = arg_def["name"]
        required = arg_def.get("required", False)
        arg_type = arg_def.get("type", "string")
        val      = user_args.get(name, "")

        if required and (val is None or str(val).strip() == ""):
            errors[name] = "This field is required"
            continue
        if val is None or str(val).strip() == "":
            continue

        if arg_type == "integer":
            try:
                int_val = int(val)
            except (ValueError, TypeError):
                errors[name] = "Must be a whole number"
                continue
            mn   = arg_def.get("min")
            mx   = arg_def.get("max")
            unit = arg_def.get("unit", "")
            u    = f" {unit}" if unit else ""
            if mn is not None and int_val < int(mn):
                errors[name] = f"Must be at least {mn}{u}"
            elif mx is not None and int_val > int(mx):
                errors[name] = f"Must be at most {mx}{u}"

        elif arg_type == "boolean":
            if str(val).lower() not in ("true", "false", "1", "0", "yes", "no"):
                errors[name] = "Must be true or false"

        elif arg_type == "select":
            options = arg_def.get("options", [])
            if options:
                if isinstance(options, dict):
                    # Dependent select — validate against the parent's chosen options
                    depends_on = arg_def.get("depends_on", "")
                    parent_val = str(user_args.get(depends_on, "")) if depends_on else ""
                    if parent_val:
                        valid_opts = [str(o) for o in options.get(parent_val, [])]
                        if valid_opts and str(val) not in valid_opts:
                            errors[name] = f"Must be one of: {', '.join(valid_opts)}"
                    # If no parent value, skip — parent arg will catch missing value
                else:
                    # Simple flat select
                    if str(val) not in [str(o) for o in options]:
                        errors[name] = f"Must be one of: {', '.join(str(o) for o in options)}"

        elif arg_type == "js_file":
            # val here is already the file's oneline-base64 content, produced
            # by /api/scripts/upload-arg-file and passed straight through —
            # nothing is stored/referenced server-side. Just confirm it's
            # actually valid base64 before it goes anywhere near Argo.
            try:
                base64.b64decode(str(val), validate=True)
            except Exception:
                errors[name] = "Uploaded file content is invalid — please re-upload"

    return errors


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def index(path=""):
    """Serve static files from dist/ or fall back to index.html for SPA routing."""
    logger.debug("GET /%s", path)

    # Backend routes — never intercept these
    if path.startswith(("api/", "download/", "oauth2/", "debug/")):
        from flask import abort
        abort(404)

    # Known React SPA routes — always serve index.html regardless of file check
    # Known SPA routes — always serve index.html
    SPA_ROUTES = {"", "apps", "scripts", "downloads"}
    if path in SPA_ROUTES:
        auth.ensure_user_exists()
        index_path = _os.path.join(_FRONTEND_DIST, "index.html")
        if _os.path.exists(index_path):
            return send_from_directory(_FRONTEND_DIST, "index.html")
        logger.error("Frontend not built — not found at %s", index_path)
        return "Eden frontend not built. Run: cd frontend && npm run build", 503

    # Static assets (images, js chunks, css, icons) — serve from dist/
    if path:
        static_file = _os.path.join(_FRONTEND_DIST, path)
        if _os.path.isfile(static_file):
            return send_from_directory(_FRONTEND_DIST, path)

    # Unknown path — serve index.html and let React Router handle it
    auth.ensure_user_exists()
    return send_from_directory(_FRONTEND_DIST, "index.html")

def _index_data():
    """Return data needed by the React app (called by /api routes, not index)."""
    username = auth.get_current_username()
    sites           = _load_sites()
    starred_urls    = {s["url"] for s in db.get_user_stars(username)}
    starred_sites   = db.get_user_stars(username)
    for s in starred_sites:
        s["image_url"] = _resolve_site_image(s["name"], s.get("favicon_url", "") or "")
    # Remaining sites = all sites minus starred ones (starred shown separately)
    other_sites = [s for s in sites if s["url"] not in starred_urls]

    apps            = smb_scanner.scan()
    icon_resolver.resolve_all(apps, smb_stream_fn=smb_scanner.stream_file)
    scripts_by_team = script_store.get_all()
    logger.debug("Render: %d sites (%d starred), %d apps, teams=%s",
                 len(sites), len(starred_sites), len(apps),
                 {t: len(v) for t, v in scripts_by_team.items()})
    return render_template(
        "index.html",
        sites=other_sites,
        starred_sites=starred_sites,
        all_sites=sites,
        apps=apps,
        teams=config.TEAMS,
        scripts_by_team=scripts_by_team,
        title=config.PORTAL_TITLE,
        team=config.TEAM_NAME,
        username=username,
        is_admin=auth.is_admin(),
        config_auth_enabled=config.AUTH_ENABLED,
        smb_configured=bool(config.SMB_SERVER),
        gitlab_configured=bool(config.GITLAB_TOKEN and config.GITLAB_REPO_PATH),
    )


@app.route("/api/sites")
def api_sites():
    """Return all approved sites as a plain array."""
    return jsonify(_load_sites())


# Lookup: banner value → color, built once from config
_BANNER_COLOR = {o["value"]: o["color"] for o in config.BANNER_OPTIONS if o.get("color")}


@app.route("/api/banner-options")
def api_banner_options():
    """Return the list of available banner options for site cards."""
    return jsonify(config.BANNER_OPTIONS)


# Lookup: env_color value → hex, built once from config
_ENV_COLOR = {o["value"]: o["color"] for o in config.ENV_COLOR_OPTIONS if o.get("color")}


@app.route("/api/env-color-options")
def api_env_color_options():
    """Return the list of available env-row colors for the Submit/Edit modals."""
    return jsonify(config.ENV_COLOR_OPTIONS)


@app.route("/api/me")
def api_me():
    return jsonify({
        "username": auth.get_current_username(),
        "is_admin": auth.is_admin(),
    })


@app.route("/api/apps")
def api_apps():
    logger.debug("GET /api/apps")
    apps = smb_scanner.scan()
    return jsonify([
        {
            "name": a.name, "display_name": a.display_name, "icon_url": a.icon_url,
            "versions": [{"filename": v.filename, "label": v.label, "smb_path": v.smb_path}
                         for v in a.versions],
        }
        for a in apps
    ])


@app.route("/api/scripts")
def api_scripts():
    logger.debug("GET /api/scripts")
    return jsonify(script_store.get_all())


@app.route("/api/scripts/reload", methods=["POST"])
def api_scripts_reload():
    logger.info("POST /api/scripts/reload")
    if config.RELOAD_TOKEN:
        provided = request.headers.get("X-Reload-Token", "")
        if provided != config.RELOAD_TOKEN:
            logger.warning("Reload rejected -- invalid token")
            return jsonify({"error": "Unauthorized"}), 401
    script_store.reload_async()
    return jsonify({"status": "reload started"}), 202


# DEV-ONLY in-memory store for pending run approvals, mirroring
# _local_site_overrides above. In practice this rarely populates locally --
# Argo isn't reachable in dev (ARGO_URL defaults to a placeholder), so the
# submit call below fails before a record would ever be created -- but it
# keeps the shape consistent with db.py's real functions and lets the
# review/approve/reject UI be exercised against manually-seeded rows.
# Resets on every backend restart.
_local_run_approvals_lock = threading.Lock()
_local_run_approvals: list = []
_local_run_approval_next_id = 0


def _create_local_run_approval(team, script_name, args, workflow_name, namespace, submitted_by) -> dict:
    global _local_run_approval_next_id
    with _local_run_approvals_lock:
        _local_run_approval_next_id += 1
        _local_run_approvals.append({
            "id": _local_run_approval_next_id,
            "team": team,
            "script_name": script_name,
            "args": dict(args),
            "workflow_name": workflow_name,
            "namespace": namespace,
            "submitted_by": submitted_by,
            "status": "pending",
        })
        return {"id": _local_run_approval_next_id}


def _get_local_pending_run_approvals() -> list:
    with _local_run_approvals_lock:
        return [dict(r) for r in _local_run_approvals if r["status"] == "pending"]


def _update_local_run_approval_status(approval_id: int, status: str, reviewed_by: str,
                                       final_args: dict | None = None) -> bool:
    with _local_run_approvals_lock:
        for r in _local_run_approvals:
            if r["id"] == approval_id:
                r["status"] = status
                r["reviewed_by"] = reviewed_by
                if final_args is not None:
                    r["args"] = dict(final_args)
                return True
    return False


@app.route("/api/scripts/submit", methods=["POST"])
def api_scripts_submit():
    data = request.get_json(silent=True) or {}
    logger.info("POST /api/scripts/submit team=%s script=%s",
                data.get("team"), data.get("script_name"))

    # Only team/script_name identify WHICH script to run; everything else
    # (language, path, dependencies, resources, approval_required) is a
    # property of the script itself, already known server-side from
    # script_store (loaded from GitLab) — the frontend's RunModal never
    # sends them, it only sends {team, script_name, args}. Requiring them
    # here made every run attempt fail with "Missing fields: language,
    # script_path" before it ever reached the script-lookup/args below.
    required_fields = ["team", "script_name"]
    missing = [f for f in required_fields if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    team        = data["team"]
    script_name = data["script_name"]

    # Validate against whatever script_store actually has loaded (GitLab
    # teams in prod, or the local scripts.json fallback teams in dev) rather
    # than config.TEAMS directly -- the fallback only kicks in when TEAMS is
    # empty, so checking config.TEAMS here would reject every fallback team.
    if team not in script_store.get_all():
        return jsonify({"error": f"Unknown team: {team}"}), 400

    script_def = script_store.get_script(team, script_name)
    if not script_def:
        return jsonify({"error": f"Script not found: {team}/{script_name}"}), 404

    user_args         = data.get("args", {})
    validation_errors = _validate_args(user_args, script_def.get("args", []))
    if validation_errors:
        logger.warning("Submit validation errors: %s", validation_errors)
        return jsonify({"error": "Validation failed", "field_errors": validation_errors}), 422

    # js_file args need no special handling here — their value in user_args
    # is already the file's oneline-base64 content (produced up front by
    # /api/scripts/upload-arg-file), so they pass straight through to Argo
    # exactly like every other arg type. Nothing is stored/referenced
    # server-side at any point.

    approval_required = script_def.get("approval_required", True)
    result = argo_client.submit_workflow(
        team=team,
        script_name=script_name,
        language=script_def["language"],
        script_path=script_def["path"],
        user_args=user_args,
        dependencies=script_def.get("dependencies", []),
        approval_required=approval_required,
        resources=script_def.get("resources", {}),
    )

    # Runs that need approval are left suspended in Argo -- track them so
    # they can be reviewed (and their args edited) through Eden's own UI
    # instead of only through Argo's. Only recorded on a successful submit;
    # if Argo itself rejected the submission there's no workflow to approve.
    if approval_required and "error" not in result:
        submitted_by = auth.get_current_username()
        if db.is_available():
            db.create_run_approval(
                team=team, script_name=script_name, args=user_args,
                workflow_name=result["workflow_name"], namespace=result["namespace"],
                submitted_by=submitted_by,
            )
        else:
            _create_local_run_approval(
                team=team, script_name=script_name, args=user_args,
                workflow_name=result["workflow_name"], namespace=result["namespace"],
                submitted_by=submitted_by,
            )

    return jsonify(result), (200 if "error" not in result else 502)


@app.route("/api/scripts/upload", methods=["POST"])
def api_scripts_upload():
    logger.info("POST /api/scripts/upload")
    errors = {}

    script_name  = (request.form.get("script_name", "") or "").strip()
    team         = (request.form.get("team",         "") or "").strip()
    language     = (request.form.get("language",     "") or "").strip().lower()
    description  = (request.form.get("description",  "") or "").strip()
    dependencies = (request.form.get("dependencies", "") or "").strip()
    approval_req = request.form.get("approval_required", "true").lower() == "true"
    args_json    = (request.form.get("args",          "") or "").strip()
    namespace    = (request.form.get("namespace",     "") or team).strip()
    res_cpu      = (request.form.get("resources_cpu",    "200m")  or "200m").strip()
    res_mem      = (request.form.get("resources_memory", "256Mi") or "256Mi").strip()
    script_file  = request.files.get("script_file")
    logo_file    = request.files.get("logo")

    # Validate script name
    name_err = gitlab_client.validate_script_name(script_name)
    if name_err:
        errors["script_name"] = name_err

    # Validate team (see api_scripts_submit for why this checks script_store
    # instead of config.TEAMS directly)
    known_teams = list(script_store.get_all().keys())
    if not team:
        errors["team"] = "Team is required"
    elif team not in known_teams:
        errors["team"] = f"Unknown team. Valid: {', '.join(known_teams)}"

    # Validate language
    if language not in ("python", "bash", "powershell"):
        errors["language"] = "Must be python, bash or powershell"

    # Validate description
    if not description:
        errors["description"] = "Description is required"
    elif len(description) > 200:
        errors["description"] = f"Max 200 characters ({len(description)} used)"

    # Validate script file
    script_content = None
    if not script_file or not script_file.filename:
        errors["script_file"] = "Script file is required"
    else:
        ext_map  = {"python": ".py", "bash": ".sh", "powershell": ".ps1"}
        expected = ext_map.get(language, "")
        if expected and not script_file.filename.endswith(expected):
            errors["script_file"] = f"{language} scripts must end in {expected}"
        else:
            try:
                script_content = script_file.read().decode("utf-8")
            except Exception as e:
                errors["script_file"] = f"Cannot read file: {e}"

    # Validate logo
    logo_bytes = None
    logo_ext = "png"
    if logo_file and logo_file.filename:
        valid_logo_exts = (".png", ".jpg", ".jpeg")
        if not any(logo_file.filename.lower().endswith(e) for e in valid_logo_exts):
            errors["logo"] = "Logo must be a .png, .jpg or .jpeg file"
        else:
            logo_bytes = logo_file.read()
            logo_ext = logo_file.filename.rsplit(".", 1)[-1].lower()
            if len(logo_bytes) > 2 * 1024 * 1024:
                errors["logo"] = "Logo must be under 2MB"

    # Validate dependencies
    dep_list = []
    dep_re   = re.compile(r"^[a-zA-Z0-9_\-\.]+$")
    if dependencies:
        for dep in dependencies.split(","):
            dep = dep.strip()
            if not dep:
                continue
            if not dep_re.match(dep):
                errors["dependencies"] = f"Invalid name: '{dep}'"
                break
            dep_list.append(dep)

    # Validate args
    parsed_args = []
    if args_json:
        try:
            parsed_args = json.loads(args_json)
            if not isinstance(parsed_args, list):
                errors["args"] = "Args must be a JSON array"
            else:
                arg_name_re = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
                valid_types = ("string", "integer", "boolean", "select", "js_file")
                for i, arg in enumerate(parsed_args):
                    if not isinstance(arg, dict):
                        errors["args"] = f"Arg #{i+1} must be an object"; break
                    if not arg.get("name"):
                        errors["args"] = f"Arg #{i+1} missing name"; break
                    if not arg_name_re.match(arg["name"]):
                        errors["args"] = f"Arg name '{arg['name']}' must be kebab-case"; break
                    if arg.get("type") not in valid_types:
                        errors["args"] = f"Arg '{arg['name']}' type must be one of: {', '.join(valid_types)}"; break
                    if arg.get("type") == "select" and not arg.get("options"):
                        errors["args"] = f"Arg '{arg['name']}' (select) must have options"; break
                    if arg.get("type") != "integer" and (arg.get("min") is not None or arg.get("max") is not None):
                        errors["args"] = "min/max only valid for integer args"; break
        except json.JSONDecodeError as e:
            errors["args"] = f"Invalid JSON: {e}"

    if errors:
        logger.warning("Upload validation failed: %s", errors)
        return jsonify({"error": "Validation failed", "field_errors": errors}), 422

    # Build script.yaml
    yaml_data = {
        "name":        script_name,
        "namespace":   namespace,
        "language":    language,
        "description": description,
        "approval":    {"required": approval_req},
        "resources":   {"cpu": res_cpu, "memory": res_mem},
    }
    if dep_list:    yaml_data["dependencies"] = dep_list
    if parsed_args: yaml_data["args"]         = parsed_args
    yaml_content = yaml.dump(yaml_data, default_flow_style=False, sort_keys=False)

    result = gitlab_client.create_script_mr(
        team=team,
        script_name=script_name,
        language=language,
        description=description,
        script_content=script_content,
        yaml_content=yaml_content,
        logo_bytes=logo_bytes,
        approval_required=approval_req,
        logo_ext=logo_ext,
    )
    if "error" in result:
        return jsonify(result), 502

    # Store in DB for admin approval UI
    mr_url = result.get("web_url", "")
    mr_iid = result.get("iid") or 0
    db.create_script_submission(
        script_name=script_name, team=team, language=language,
        mr_url=mr_url, mr_iid=mr_iid,
        submitted_by=auth.get_current_username(),
    )
    logger.info("Script MR #%d created: %s/%s", mr_iid, team, script_name)
    return jsonify({"status": "mr_created", "mr_url": mr_url}), 201


@app.route("/api/scripts/upload-arg-file", methods=["POST"])
def api_scripts_upload_arg_file():
    """
    Convert a .js file into the oneline-base64 value a js_file run argument
    needs. Stateless — nothing is written to disk or kept in memory beyond
    this request; the response *is* the argument value, passed straight
    through by the RunModal into /api/scripts/submit's `args`, then straight
    through again into the Argo parameter string. Not admin-gated — same
    trust level as /api/scripts/submit itself (any user who can run a script
    can already send it arbitrary arg values; this just lets one of those
    values come from a file instead of being typed in).
    """
    logger.info("POST /api/scripts/upload-arg-file")

    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"error": "No file provided"}), 400

    if not upload.filename.lower().endswith(".js"):
        return jsonify({"error": "Only .js files are accepted"}), 422

    raw = upload.read()
    if not raw:
        return jsonify({"error": "File is empty"}), 422
    max_bytes = 1 * 1024 * 1024  # 1 MiB — a script argument, not a bulk artifact
    if len(raw) > max_bytes:
        return jsonify({"error": f"File too large (max {max_bytes // 1024} KB)"}), 422

    value = base64.b64encode(raw).decode("ascii")
    logger.info("Arg file converted: %s (%d bytes -> %d b64 chars)",
                upload.filename, len(raw), len(value))
    return jsonify({"value": value, "filename": upload.filename, "size": len(raw)}), 200


@app.route("/api/scripts/<team>/<script_name>/logo")
def script_logo(team: str, script_name: str):
    logger.debug("Logo request: %s/%s", team, script_name)
    # Try logo in all supported formats
    img_bytes = None
    for logo_ext in ("png", "jpg", "jpeg"):
        _lp = f"{config.SCRIPTS_BASE_PATH}/{team}/{script_name}/logo.{logo_ext}".lstrip("/") if config.SCRIPTS_BASE_PATH else f"{team}/{script_name}/logo.{logo_ext}"
        img_bytes = gitlab_client.get_file_raw_bytes(_lp)
        if img_bytes:
            break
    if img_bytes:
        return Response(img_bytes, content_type="image/png")
    for path, ct in [
        (os.path.join("static", "icons", "_logoplaceholder.png"), "image/png"),
        (os.path.join("static", "icons", "_placeholder.svg"),     "image/svg+xml"),
    ]:
        if os.path.exists(path):
            with open(path, "rb") as f:
                return Response(f.read(), content_type=ct)
    abort(404)


@app.route("/download/<path:smb_path>")
def download(smb_path):
    if not config.SMB_SERVER:
        abort(503)
    if ".." in smb_path:
        abort(400)
    filename = smb_path.split("/")[-1]
    logger.info("Download: %s", smb_path)
    return Response(
        stream_with_context(smb_scanner.stream_file(smb_path)),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type":        "application/octet-stream",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ── Auth (stub) ───────────────────────────────────────────────────────────────

@app.route("/debug/headers")
def debug_headers():
    """Temporary debug route — remove after auth is working."""
    headers = dict(request.headers)
    username_header = headers.get(config.AUTH_HEADER, "NOT FOUND")
    logger.info("DEBUG HEADERS: %s", headers)
    return jsonify({
        "all_headers": headers,
        "auth_header_name": config.AUTH_HEADER,
        "auth_header_value": username_header,
        "x_auth_request_user": headers.get("X-Auth-Request-User", "NOT FOUND"),
        "x_auth_request_email": headers.get("X-Auth-Request-Email", "NOT FOUND"),
        "x_forwarded_user": headers.get("X-Forwarded-User", "NOT FOUND"),
        "x_forwarded_email": headers.get("X-Forwarded-Email", "NOT FOUND"),
    })


# ── Site submissions ─────────────────────────────────────────────────────────

@app.route("/api/sites/submit", methods=["POST"])
def api_sites_submit():
    """Any logged-in user can submit a new web app for approval."""
    data = request.get_json(silent=True) or {}
    name         = (data.get("name", "") or "").strip()
    url          = (data.get("url", "") or "").strip()
    favicon_url  = (data.get("favicon_url", "") or "").strip()
    favicon_data = (data.get("favicon_data", "") or "").strip()  # base64 data URL from file upload
    tags         = data.get("tags", []) or []

    if not name or not url:
        return jsonify({"error": "Name and URL are required"}), 400
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "URL must start with http:// or https://"}), 400

    # Validate base64 image if provided
    if favicon_data:
        if not favicon_data.startswith("data:image/"):
            return jsonify({"error": "Invalid image format"}), 400
        if len(favicon_data) > 3 * 1024 * 1024:  # ~2MB in base64
            return jsonify({"error": "Image must be under 2MB"}), 400
        # Use the base64 data URL directly as favicon_url — browsers can render it
        # and we store it in the DB for display in the card
        favicon_url = favicon_data

    group_name         = (data.get("group_name")         or "").strip() or None
    group_display_name = (data.get("group_display_name") or "").strip() or None
    env_label          = (data.get("env_label")          or "").strip() or None
    env_color          = (data.get("env_color")          or "").strip() or None

    username = auth.get_current_username()
    result = db.create_submission(name, url, favicon_url, tags, username,
                                  group_name=group_name,
                                  group_display_name=group_display_name,
                                  env_label=env_label,
                                  env_color=env_color)
    if "error" in result:
        return jsonify(result), 502

    logger.info("Site submission created by %s: %s (%s)", username, name, url)
    return jsonify({"status": "submitted", "id": result["id"]}), 201


@app.route("/api/sites/pending")
def api_sites_pending():
    """List all pending submissions for the approval UI."""
    return jsonify(db.get_pending_submissions())


@app.route("/api/sites/review", methods=["POST"])
def api_sites_review():
    """Approve or reject a pending submission. Admins only."""
    if not auth.is_admin():
        return jsonify({"error": "Admin access required"}), 403
    data = request.get_json(silent=True) or {}
    submission_id = data.get("id")
    approve       = bool(data.get("approve", False))

    if not submission_id:
        return jsonify({"error": "Missing submission id"}), 400

    reviewer = auth.get_current_username()
    result = db.review_submission(int(submission_id), approve, reviewer)
    if "error" in result:
        return jsonify(result), 502

    return jsonify(result), 200


@app.route("/api/sites/delete", methods=["POST"])
def api_sites_delete():
    if not auth.is_admin():
        return jsonify({"error": "Admin access required"}), 403
    data    = request.get_json(silent=True) or {}
    site_id = data.get("site_id")
    if not site_id:
        return jsonify({"error": "Missing site_id"}), 400
    ok = db.delete_site(int(site_id))
    logger.info("Site %d deleted by %s", site_id, auth.get_current_username())
    return jsonify({"status": "deleted" if ok else "error"}), (200 if ok else 502)


@app.route("/api/sites/edit", methods=["POST"])
def api_sites_edit():
    if not auth.is_admin():
        return jsonify({"error": "Admin access required"}), 403
    data         = request.get_json(silent=True) or {}
    site_id      = data.get("site_id")
    name         = (data.get("name") or "").strip() or None
    url          = (data.get("url") or "").strip() or None
    tags         = data.get("tags")
    favicon_url  = data.get("favicon_url") or None
    favicon_data = (data.get("favicon_data") or "").strip()

    group_name         = (data.get("group_name")         or "").strip() or None
    group_display_name = (data.get("group_display_name") or "").strip() or None
    env_label          = (data.get("env_label")          or "").strip() or None
    env_color          = (data.get("env_color")          or "").strip() or None

    if not site_id:
        return jsonify({"error": "Missing site_id"}), 400
    if favicon_data and favicon_data.startswith("data:image/"):
        favicon_url = favicon_data

    if db.is_available():
        ok = db.edit_site(int(site_id), name=name, url=url, tags=tags, favicon_url=favicon_url,
                          group_name=group_name, group_display_name=group_display_name,
                          env_label=env_label, env_color=env_color)
    else:
        ok = _edit_local_site(int(site_id), name=name, url=url, tags=tags, favicon_url=favicon_url,
                              group_name=group_name, group_display_name=group_display_name,
                              env_label=env_label, env_color=env_color)
    logger.info("Site %d edited by %s", site_id, auth.get_current_username())
    return jsonify({"status": "updated" if ok else "error"}), (200 if ok else 502)


# ── Script submission approval ────────────────────────────────────────────────

@app.route("/api/scripts/pending")
def api_scripts_pending():
    """List pending script MRs for admin approval."""
    if not auth.is_admin():
        return jsonify({"error": "Admin access required"}), 403
    return jsonify(db.get_pending_script_submissions())


@app.route("/api/scripts/approve", methods=["POST"])
def api_scripts_approve():
    """Approve a script submission — merges the GitLab MR."""
    if not auth.is_admin():
        return jsonify({"error": "Admin access required"}), 403
    data = request.get_json(silent=True) or {}
    sub_id = data.get("id")
    if not sub_id:
        return jsonify({"error": "Missing id"}), 400

    subs = db.get_pending_script_submissions()
    sub  = next((s for s in subs if s["id"] == int(sub_id)), None)
    if not sub:
        return jsonify({"error": "Submission not found"}), 404

    try:
        result = gitlab_client.merge_mr(sub["mr_iid"])
        db.update_script_submission_status(int(sub_id), "approved")
        # Trigger script reload so new script appears immediately
        script_store.load_all()
        logger.info("Script MR %d merged by %s", sub["mr_iid"], auth.get_current_username())
        return jsonify({"status": "merged", "mr": result}), 200
    except Exception as e:
        logger.error("Failed to merge MR %d: %s", sub.get("mr_iid"), e)
        return jsonify({"error": str(e)}), 502


@app.route("/api/scripts/reject", methods=["POST"])
def api_scripts_reject():
    """Reject a script submission."""
    if not auth.is_admin():
        return jsonify({"error": "Admin access required"}), 403
    data = request.get_json(silent=True) or {}
    sub_id = data.get("id")
    if not sub_id:
        return jsonify({"error": "Missing id"}), 400
    db.update_script_submission_status(int(sub_id), "rejected")
    return jsonify({"status": "rejected"}), 200


# ── Script RUN approval (distinct from MR approval above -- this is a
#    submitted run's arguments awaiting review before Argo executes it,
#    not a script's code awaiting merge) ───────────────────────────────────────

def _get_pending_run_approvals_raw() -> list:
    return db.get_pending_run_approvals() if db.is_available() else _get_local_pending_run_approvals()


def _update_run_approval_status(approval_id: int, status: str, reviewed_by: str,
                                 final_args: dict | None = None) -> bool:
    if db.is_available():
        return db.update_run_approval_status(approval_id, status, reviewed_by, final_args)
    return _update_local_run_approval_status(approval_id, status, reviewed_by, final_args)


@app.route("/api/scripts/runs/pending")
def api_scripts_runs_pending():
    """
    List runs waiting on approval. Reconciles each against Argo's live
    status first -- a workflow can just as easily have been resumed
    directly through Argo's own UI (see the approval-feature discussion:
    admins have Argo access too), and a stale "pending" row here would be
    actively misleading rather than just outdated.
    """
    if not auth.is_admin():
        return jsonify({"error": "Admin access required"}), 403

    pending = _get_pending_run_approvals_raw()
    still_pending = []
    for item in pending:
        live = argo_client.get_workflow_status(item["namespace"], item["workflow_name"])
        if live is not None and live.get("approval_phase") not in (None, "Running", "Pending"):
            # Resolved outside Eden -- reflect the real outcome instead of
            # leaving a stale "pending" row for something already handled.
            resolved_approve = (live.get("approval_outputs") or {}).get("approve")
            resolved_status = (
                "approved" if resolved_approve == "YES"
                else "rejected" if resolved_approve == "NO"
                else item["status"]
            )
            _update_run_approval_status(item["id"], resolved_status, reviewed_by=None)
            continue

        # Pair the submitted values with the script's current arg
        # definitions (type, required, options, ...) so the review UI can
        # render proper type-aware editable fields instead of raw text.
        script_def = script_store.get_script(item["team"], item["script_name"])
        item["arg_defs"] = script_def.get("args", []) if script_def else []
        still_pending.append(item)

    return jsonify(still_pending)


@app.route("/api/scripts/runs/approve", methods=["POST"])
def api_scripts_runs_approve():
    """Approve a pending run -- resumes the Argo workflow with approve=YES,
    relaying whatever args the admin ends up submitting (edited or not)."""
    if not auth.is_admin():
        return jsonify({"error": "Admin access required"}), 403
    data = request.get_json(silent=True) or {}
    approval_id = data.get("id")
    edited_args = data.get("args")
    if not approval_id or edited_args is None:
        return jsonify({"error": "Missing id or args"}), 400
    approval_id = int(approval_id)

    item = next((p for p in _get_pending_run_approvals_raw() if p["id"] == approval_id), None)
    if not item:
        return jsonify({"error": "Approval request not found"}), 404

    result = argo_client.resume_workflow(item["namespace"], item["workflow_name"], "YES", edited_args)
    if "error" in result:
        return jsonify(result), 502

    reviewer = auth.get_current_username()
    _update_run_approval_status(approval_id, "approved", reviewer, final_args=edited_args)
    logger.info("Run approval %d (%s/%s) approved by %s",
                approval_id, item["team"], item["script_name"], reviewer)
    return jsonify({"status": "approved"}), 200


@app.route("/api/scripts/runs/reject", methods=["POST"])
def api_scripts_runs_reject():
    """Reject a pending run -- resumes the Argo workflow with approve=NO,
    which the template's own `when` condition already treats as "skip the
    run", so no other special-casing is needed here."""
    if not auth.is_admin():
        return jsonify({"error": "Admin access required"}), 403
    data = request.get_json(silent=True) or {}
    approval_id = data.get("id")
    if not approval_id:
        return jsonify({"error": "Missing id"}), 400
    approval_id = int(approval_id)

    item = next((p for p in _get_pending_run_approvals_raw() if p["id"] == approval_id), None)
    if not item:
        return jsonify({"error": "Approval request not found"}), 404

    result = argo_client.resume_workflow(item["namespace"], item["workflow_name"], "NO", item["args"])
    if "error" in result:
        return jsonify(result), 502

    reviewer = auth.get_current_username()
    _update_run_approval_status(approval_id, "rejected", reviewer)
    logger.info("Run approval %d (%s/%s) rejected by %s",
                approval_id, item["team"], item["script_name"], reviewer)
    return jsonify({"status": "rejected"}), 200


@app.route("/api/admin/status")
def api_admin_status():
    """Returns whether the current user is an admin. Used by the UI."""
    return jsonify({"is_admin": auth.is_admin(), "username": auth.get_current_username()})


@app.route("/api/admin/users")
def api_admin_users():
    """List all known users with their admin status. Admins only."""
    if not auth.is_admin():
        return jsonify({"error": "Admin access required"}), 403
    return jsonify(db.get_all_users())


@app.route("/api/admin/users/set", methods=["POST"])
def api_admin_users_set():
    """Promote or demote a user. Admins only."""
    if not auth.is_admin():
        return jsonify({"error": "Admin access required"}), 403
    data = request.get_json(silent=True) or {}
    username = (data.get("username", "") or "").strip()
    make_admin = bool(data.get("is_admin", False))
    if not username:
        return jsonify({"error": "Missing username"}), 400

    # Prevent the only admin from demoting themselves accidentally
    if username == auth.get_current_username() and not make_admin:
        all_users = db.get_all_users()
        admin_count = sum(1 for u in all_users if u["is_admin"])
        if admin_count <= 1:
            return jsonify({"error": "Cannot remove the last admin"}), 400

    ok = db.set_user_admin(username, make_admin)
    logger.info("Admin status changed by %s: %s -> is_admin=%s",
                auth.get_current_username(), username, make_admin)
    return jsonify({"status": "updated" if ok else "error"}), (200 if ok else 502)


# ── Starred apps ──────────────────────────────────────────────────────────────

# DEV-ONLY in-memory stars fallback. Mirrors _load_local_sites_fallback:
# when Postgres is unavailable, star state is kept in-process (per username)
# instead of hitting the DB, so starring works locally without a real DB.
# Resets on every backend restart. Never used against a real deployment --
# its DB is always up, so db.is_available() is True there and this is skipped.
_local_stars_lock = threading.Lock()
_local_stars: dict = {}   # username -> ordered list of local site_id strings


def _local_sites_by_id() -> dict:
    return {s["id"]: s for s in _load_local_sites_fallback()}


def _get_local_stars(username: str) -> list:
    by_id = _local_sites_by_id()
    with _local_stars_lock:
        ids = list(_local_stars.get(username, []))
    return [dict(by_id[i]) for i in ids if i in by_id]


def _add_local_star(username: str, site_id: int) -> bool:
    if site_id not in _local_sites_by_id():
        return False
    with _local_stars_lock:
        ids = _local_stars.setdefault(username, [])
        if site_id not in ids:
            ids.append(site_id)
    return True


def _remove_local_star(username: str, site_id: int) -> bool:
    with _local_stars_lock:
        ids = _local_stars.get(username, [])
        if site_id in ids:
            ids.remove(site_id)
    return True


def _reorder_local_stars(username: str, ordered_ids: "list[int]") -> bool:
    with _local_stars_lock:
        _local_stars[username] = list(ordered_ids)
    return True


@app.route("/api/stars")
def api_stars_get():
    username = auth.get_current_username()
    stars = db.get_user_stars(username) if db.is_available() else _get_local_stars(username)
    for s in stars:
        s["image_url"] = _resolve_site_image(s["name"], s.get("favicon_url") or "")
        # Auto-compute group_display_name if not set
        if s.get("group_name") and not s.get("group_display_name"):
            s["group_display_name"] = s["group_name"].capitalize()
        banner_val = (s.get("tags") or [None])[0]
        s["banner_color"] = _BANNER_COLOR.get(banner_val) if banner_val else None
        env_val = s.get("env_color")
        s["env_color_hex"] = _ENV_COLOR.get(env_val) if env_val else None
    return jsonify(stars)


@app.route("/api/stars/add", methods=["POST"])
def api_stars_add():
    data    = request.get_json(silent=True) or {}
    site_id = data.get("site_id")
    if not site_id:
        return jsonify({"error": "Missing site_id"}), 400
    username = auth.get_current_username()
    if db.is_available():
        ok = db.star_site(username, int(site_id))
    else:
        # Fallback site ids are real ints now (900000+i, see
        # _load_local_sites_fallback) -- this used to coerce to str(),
        # which silently failed every star-add once the ids stopped being
        # "local-N" strings, since the lookup dict is keyed by int.
        ok = _add_local_star(username, int(site_id))
    return jsonify({"status": "starred" if ok else "error"}), (200 if ok else 502)


@app.route("/api/stars/remove", methods=["POST"])
def api_stars_remove():
    data    = request.get_json(silent=True) or {}
    site_id = data.get("site_id")
    if not site_id:
        return jsonify({"error": "Missing site_id"}), 400
    username = auth.get_current_username()
    if db.is_available():
        ok = db.unstar_site(username, int(site_id))
    else:
        ok = _remove_local_star(username, int(site_id))
    return jsonify({"status": "unstarred" if ok else "error"}), (200 if ok else 502)


@app.route("/api/stars/reorder", methods=["POST"])
def api_stars_reorder():
    data = request.get_json(silent=True) or {}
    ids  = data.get("site_ids", [])
    if not isinstance(ids, list):
        return jsonify({"error": "site_ids must be a list"}), 400
    username = auth.get_current_username()
    if db.is_available():
        ok = db.reorder_stars(username, [int(i) for i in ids])
    else:
        ok = _reorder_local_stars(username, [int(i) for i in ids])
    return jsonify({"status": "reordered" if ok else "error"}), (200 if ok else 502)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT,
            debug=os.environ.get("DEBUG", "false").lower() == "true")
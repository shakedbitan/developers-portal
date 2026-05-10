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
  GET  /api/scripts/<t>/<n>/logo    -> proxy script logo from GitLab
  GET  /download/<path>             -> stream installer from SMB
  GET  /favicon-proxy/<slug>        -> proxy favicon from internal site
"""

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
                   request, stream_with_context, abort)

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

app = Flask(__name__)

# ── TLS verification ──────────────────────────────────────────────────────────
# Use /etc/ssl/certs (internal CA directory) if available.
# requests/urllib3 supports a directory path natively.
_CA_DIR   = "/etc/ssl/certs"
CA_VERIFY = _CA_DIR if os.path.isdir(_CA_DIR) else False
if CA_VERIFY:
    logger.info("TLS: using CA directory %s", CA_VERIFY)
else:
    logger.warning("TLS: CA directory %s not found -- verification disabled", _CA_DIR)

# ── Static paths ──────────────────────────────────────────────────────────────
SITE_IMAGES_DIR   = os.path.join("static", "site-images")
FAVICON_CACHE_DIR = os.path.join("static", "site-images", "_favicon_cache")
os.makedirs(FAVICON_CACHE_DIR, exist_ok=True)


# ── Favicon helpers ───────────────────────────────────────────────────────────

def _fetch_favicon(site_url: str, slug: str, favicon_url: str = "") -> bytes | None:
    """
    Fetch a favicon from an internal site.
    If favicon_url is provided (from sites JSON), try that first.
    Otherwise tries /favicon.ico, /favicon.png, /apple-touch-icon.png.
    Does NOT follow redirects (redirect = login page interception).
    Returns raw bytes on success, None if nothing usable found.
    """
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(site_url)
    base   = (parsed.scheme, parsed.netloc)

    candidates = []
    # Explicit favicon URL from sites config always goes first
    if favicon_url:
        candidates.append(favicon_url)
    # Generic fallbacks
    candidates += [
        urlunparse((*base, "/favicon.ico",          "", "", "")),
        urlunparse((*base, "/favicon.png",          "", "", "")),
        urlunparse((*base, "/apple-touch-icon.png", "", "", "")),
    ]

    for url in candidates:
        try:
            r    = requests.get(url, timeout=3, verify=CA_VERIFY,
                                allow_redirects=False,
                                headers={"User-Agent": "Eden-Portal/1.0"})
            data = r.content or b""
            ct   = r.headers.get("Content-Type", "").lower()

            if r.status_code != 200 or len(data) < 10:
                logger.debug("Favicon skip %s status=%d size=%d", url, r.status_code, len(data))
                continue

            # Reject HTML (login page interceptions)
            snippet = data[:300].lower()
            if b"<html" in snippet or b"<!doctype" in snippet:
                logger.debug("Favicon skip %s -- HTML login page", url)
                continue

            logger.debug("Favicon found for %s at %s (%d bytes ct=%s)", slug, url, len(data), ct)
            return data

        except requests.exceptions.Timeout:
            logger.debug("Favicon timeout: %s", url)
        except Exception as e:
            logger.debug("Favicon error %s: %s", url, e)

    return None


def _prefetch_favicons(sites: list) -> None:
    """Fetch and cache all site favicons concurrently at startup."""
    def fetch_one(site):
        slug       = _slug(site["name"])
        cache_path = os.path.join(FAVICON_CACHE_DIR, f"{slug}.ico")
        if os.path.exists(cache_path):
            logger.debug("Favicon already cached: %s", slug)
            return
        data = _fetch_favicon(site["url"], slug, site.get("favicon", ""))
        if data:
            try:
                with open(cache_path, "wb") as f:
                    f.write(data)
                logger.info("Favicon prefetched: %s", slug)
            except Exception as e:
                logger.warning("Could not cache favicon for %s: %s", slug, e)
        else:
            logger.debug("No favicon found at startup for %s", slug)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        executor.map(fetch_one, sites)
    logger.info("Favicon prefetch complete for %d sites", len(sites))


# ── Startup ───────────────────────────────────────────────────────────────────

def _startup():
    # 1. Load scripts from GitLab
    if config.TEAMS:
        logger.info("Startup: loading scripts for teams: %s", config.TEAMS)
        script_store.load_all()
    else:
        logger.warning("Startup: TEAMS not configured -- scripts section will be empty")

    # 2. Prefetch all favicons concurrently (so page loads are instant)
    sites = _load_sites()
    if sites:
        logger.info("Startup: prefetching favicons for %d sites", len(sites))
        _prefetch_favicons(sites)

threading.Thread(target=_startup, daemon=True).start()


# ── Site image helpers ────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    return name.lower().replace(" ", "_").replace("-", "_")


def _resolve_site_image(name: str, url: str, favicon_url: str = "") -> str:
    """
    Priority:
    1. Manual image in static/site-images/<slug>.<ext>  (drop file in repo)
    2. Cached favicon from prefetch or earlier proxy call
    3. Proxy URL -- fetches favicon_url first (if set), then generic paths
    """
    slug = _slug(name)
    # 1. Manual static image
    for ext in ("png", "ico", "jpg", "jpeg", "svg"):
        if os.path.exists(os.path.join(SITE_IMAGES_DIR, f"{slug}.{ext}")):
            return f"/static/site-images/{slug}.{ext}"
    # 2. Already cached
    if os.path.exists(os.path.join(FAVICON_CACHE_DIR, f"{slug}.ico")):
        return f"/static/site-images/_favicon_cache/{slug}.ico"
    # 3. Proxy -- passes explicit favicon_url if provided
    proxy = f"/favicon-proxy/{slug}?url={requests.utils.quote(url, safe='')}"
    if favicon_url:
        proxy += f"&favicon={requests.utils.quote(favicon_url, safe='')}"
    return proxy


def _load_sites() -> list:
    """Load web app links from SITES_JSON env var (set in eden-config ConfigMap)."""
    raw = (config.SITES_JSON or "").strip()
    if raw:
        try:
            sites = json.loads(raw)
            logger.debug("Loaded %d sites from SITES_JSON", len(sites))
            for site in sites:
                site["image_url"] = _resolve_site_image(
                    site["name"], site["url"], site.get("favicon", ""))
            return sites
        except Exception as e:
            logger.error("Failed to parse SITES_JSON: %s", e)

    # Fallback: local sites.json (useful for dev/testing)
    if os.path.exists("sites.json"):
        try:
            with open("sites.json") as f:
                sites = json.load(f)
            logger.debug("Loaded %d sites from local sites.json (fallback)", len(sites))
            for site in sites:
                site["image_url"] = _resolve_site_image(
                    site["name"], site["url"], site.get("favicon", ""))
            return sites
        except Exception as e:
            logger.error("Failed to load local sites.json: %s", e)

    logger.warning("No sites configured -- set SITES_JSON in eden-config ConfigMap")
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
            if options and str(val) not in [str(o) for o in options]:
                errors[name] = f"Must be one of: {', '.join(str(o) for o in options)}"

    return errors


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    logger.debug("GET /")
    sites           = _load_sites()
    apps            = smb_scanner.scan()
    icon_resolver.resolve_all(apps, smb_stream_fn=smb_scanner.stream_file)
    scripts_by_team = script_store.get_all()
    logger.debug("Render: %d sites, %d apps, teams=%s",
                 len(sites), len(apps), {t: len(v) for t, v in scripts_by_team.items()})
    return render_template(
        "index.html",
        sites=sites,
        apps=apps,
        teams=config.TEAMS,
        scripts_by_team=scripts_by_team,
        title=config.PORTAL_TITLE,
        team=config.TEAM_NAME,
        smb_configured=bool(config.SMB_SERVER),
        gitlab_configured=bool(config.GITLAB_TOKEN and config.GITLAB_REPO_PATH),
    )


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


@app.route("/api/scripts/submit", methods=["POST"])
def api_scripts_submit():
    data = request.get_json(silent=True) or {}
    logger.info("POST /api/scripts/submit team=%s script=%s",
                data.get("team"), data.get("script_name"))

    required_fields = ["team", "script_name", "language", "script_path"]
    missing = [f for f in required_fields if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    team        = data["team"]
    script_name = data["script_name"]

    if team not in config.TEAMS:
        return jsonify({"error": f"Unknown team: {team}"}), 400

    script_def = script_store.get_script(team, script_name)
    if not script_def:
        return jsonify({"error": f"Script not found: {team}/{script_name}"}), 404

    user_args         = data.get("args", {})
    validation_errors = _validate_args(user_args, script_def.get("args", []))
    if validation_errors:
        logger.warning("Submit validation errors: %s", validation_errors)
        return jsonify({"error": "Validation failed", "field_errors": validation_errors}), 422

    result = argo_client.submit_workflow(
        team=team,
        script_name=script_name,
        language=data["language"],
        script_path=data["script_path"],
        user_args=user_args,
        dependencies=data.get("dependencies", []),
        approval_required=data.get("approval_required", True),
        resources=data.get("resources", {}),
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

    # Validate team
    if not team:
        errors["team"] = "Team is required"
    elif team not in config.TEAMS:
        errors["team"] = f"Unknown team. Valid: {', '.join(config.TEAMS)}"

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
                valid_types = ("string", "integer", "boolean", "select")
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
    return jsonify(result), 201


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


@app.route("/favicon-proxy/<slug>")
def favicon_proxy(slug: str):
    site_url    = requests.utils.unquote(request.args.get("url",     ""))
    favicon_url = requests.utils.unquote(request.args.get("favicon", ""))
    logger.debug("Favicon proxy: %s -> %s (explicit favicon: %s)", slug, site_url, favicon_url or "none")

    data = _fetch_favicon(site_url, slug, favicon_url)
    if data:
        cache_path = os.path.join(FAVICON_CACHE_DIR, f"{slug}.ico")
        try:
            with open(cache_path, "wb") as f:
                f.write(data)
        except Exception as e:
            logger.warning("Could not cache favicon for %s: %s", slug, e)
        return Response(data, content_type="image/x-icon")

    logger.debug("No favicon found for %s -- returning placeholder", slug)
    with open(os.path.join("static", "icons", "_placeholder.svg"), "rb") as f:
        return Response(f.read(), content_type="image/svg+xml")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT,
            debug=os.environ.get("DEBUG", "false").lower() == "true")
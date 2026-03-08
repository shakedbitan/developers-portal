"""
app.py
------
Dev Portal — Flask backend.

Routes:
  GET  /                    → main portal page
  GET  /api/apps            → JSON list of apps + versions (for JS refresh)
  GET  /download/<smb_path> → streams installer file from SMB share

All assets (icons, site images) are served from static/ — no external network calls.
"""

import json
import os
import logging

import requests
from flask import Flask, render_template, Response, stream_with_context, jsonify, abort, request

import smb_scanner
import icon_resolver

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SITES_FILE   = os.environ.get("SITES_FILE",   "sites.json")
PORTAL_TITLE = os.environ.get("PORTAL_TITLE", "Eden")
MINI_TITLE    = os.environ.get("MINI_TITLE", "Welcome to")


SITE_IMAGES_DIR  = os.path.join("static", "site-images")
FAVICON_CACHE_DIR = os.path.join("static", "site-images", "_favicon_cache")
os.makedirs(FAVICON_CACHE_DIR, exist_ok=True)


def load_sites():
    with open(SITES_FILE, "r") as f:
        sites = json.load(f)
    for site in sites:
        site["image_url"] = _resolve_site_image(site["name"], site["url"])
    return sites


def _slug(name: str) -> str:
    return name.lower().replace(" ", "_").replace("-", "_")


def _resolve_site_image(name: str, url: str) -> str:
    """
    Priority:
      1. static/site-images/<slug>.png/ico/jpg/svg  — manual file in repo
      2. Favicon fetched from the internal site and cached locally
      3. static/icons/_placeholder.svg
    """
    slug = _slug(name)

    # 1. Manual image
    for ext in ("png", "ico", "jpg", "jpeg", "svg"):
        if os.path.exists(os.path.join(SITE_IMAGES_DIR, f"{slug}.{ext}")):
            return f"/static/site-images/{slug}.{ext}"

    # 2. Cached favicon (fetched previously)
    cached = os.path.join(FAVICON_CACHE_DIR, f"{slug}.ico")
    if os.path.exists(cached):
        return f"/static/site-images/_favicon_cache/{slug}.ico"

    # 3. Trigger a background fetch — return proxy URL; browser will call it once
    #    and the result is also cached to disk for next startup
    return f"/favicon-proxy/{slug}?url={requests.utils.quote(url, safe='')}"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/favicon-proxy/<slug>")
def favicon_proxy(slug):
    """
    Fetches /favicon.ico from the internal site URL (passed as ?url=).
    Caches the result to static/site-images/_favicon_cache/<slug>.ico.
    Falls back to placeholder on any error.
    All traffic stays inside the cluster — no internet calls.
    """
    from urllib.parse import urlparse, urlunparse
    site_url = requests.utils.unquote(request.args.get("url", ""))

    # Build favicon URL: strip path, append /favicon.ico
    try:
        parsed = urlparse(site_url)
        favicon_url = urlunparse((parsed.scheme, parsed.netloc, "/favicon.ico", "", "", ""))
        r = requests.get(favicon_url, timeout=3, verify=False)
        if r.status_code == 200 and len(r.content) > 0:
            # Cache to disk
            cache_path = os.path.join(FAVICON_CACHE_DIR, f"{slug}.ico")
            with open(cache_path, "wb") as f:
                f.write(r.content)
            return Response(r.content, content_type=r.headers.get("Content-Type", "image/x-icon"))
    except Exception as e:
        logger.debug("Favicon fetch failed for %s: %s", slug, e)

    # Fallback: placeholder
    with open(os.path.join("static", "icons", "_placeholder.svg"), "rb") as f:
        return Response(f.read(), content_type="image/svg+xml")

@app.route("/")
def index():
    sites = load_sites()
    apps  = smb_scanner.scan()
    icon_resolver.resolve_all(apps, smb_stream_fn=smb_scanner.stream_file)

    return render_template(
        "index.html",
        sites=sites,
        apps=apps,
        title=PORTAL_TITLE,
        team=MINI_TITLE,
        smb_configured=bool(os.environ.get("SMB_SERVER")),
    )


@app.route("/api/apps")
def api_apps():
    """JSON endpoint — lets the frontend poll for live updates without full reload."""
    apps = smb_scanner.scan()
    return jsonify([
        {
            "name": a.name,
            "display_name": a.display_name,
            "icon_url": a.icon_url,
            "versions": [
                {"filename": v.filename, "label": v.label, "smb_path": v.smb_path}
                for v in a.versions
            ],
        }
        for a in apps
    ])


@app.route("/download/<path:smb_path>")
def download(smb_path):
    """Streams a file from the SMB share directly to the browser as a download."""
    if not os.environ.get("SMB_SERVER"):
        abort(503, description="SMB not configured.")

    filename = smb_path.split("/")[-1]
    # Basic path sanity — no traversal
    if ".." in smb_path:
        abort(400)

    def generate():
        yield from smb_scanner.stream_file(smb_path)

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "application/octet-stream",
        "X-Content-Type-Options": "nosniff",
    }
    return Response(
        stream_with_context(generate()),
        headers=headers,
    )



# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
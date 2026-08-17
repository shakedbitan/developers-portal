"""
icon_resolver.py
----------------
Resolves icons for installer apps using this priority:

  1. Manual PNG in  static/icons/<appname>.png  (repo-provided, best quality)
  2. Auto-extracted from the first .exe/.msi file found for the app
     (uses icoextract for .exe, falls back to Pillow for simple .ico)
  3. Generic placeholder SVG (data URI)

Icons are cached on disk at  static/icons/_cache/<appname>.png
so extraction only runs once per app per container lifetime.
"""

import io
import os
import logging

logger = logging.getLogger(__name__)

ICONS_DIR       = os.path.join("static", "icons")
ICONS_CACHE_DIR = os.path.join(ICONS_DIR, "_cache")
PLACEHOLDER     = "/static/icons/_placeholder.svg"

os.makedirs(ICONS_CACHE_DIR, exist_ok=True)


def _manual_path(app_name: str) -> str:
    return os.path.join(ICONS_DIR, f"{app_name}.png")


def _cache_path(app_name: str) -> str:
    return os.path.join(ICONS_CACHE_DIR, f"{app_name}.png")


def _try_extract_from_exe(exe_bytes: bytes, app_name: str) -> bool:
    """Try icoextract → Pillow → save PNG to cache. Returns True on success."""
    try:
        import icoextract
        extractor = icoextract.IconExtractor(data=exe_bytes)
        ico_data = extractor.get_icon()          # returns bytes of .ico
        _save_ico_as_png(ico_data, app_name)
        return True
    except Exception as e:
        logger.debug("icoextract failed for %s: %s", app_name, e)

    # Fallback: maybe the file itself IS an .ico embedded somewhere
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(exe_bytes))
        img.save(_cache_path(app_name), "PNG")
        return True
    except Exception as e:
        logger.debug("Pillow fallback failed for %s: %s", app_name, e)

    return False


def _save_ico_as_png(ico_bytes: bytes, app_name: str):
    from PIL import Image
    ico = Image.open(io.BytesIO(ico_bytes))
    # Pick largest size
    sizes = ico.ico.sizes() if hasattr(ico, "ico") else []
    if sizes:
        ico.size = max(sizes)
    ico.save(_cache_path(app_name), "PNG")


def resolve(app, smb_stream_fn=None) -> str:
    """
    Returns a URL path string for the app's icon.
    app: smb_scanner.App instance
    smb_stream_fn: callable(smb_path) → bytes, used to fetch installer for extraction
    """
    # 1. Manual PNG wins
    if os.path.exists(_manual_path(app.name)):
        return f"/static/icons/{app.name}.png"

    # 2. Cached extraction
    if os.path.exists(_cache_path(app.name)):
        return f"/static/icons/_cache/{app.name}.png"

    # 3. Try to extract from first installer
    if smb_stream_fn and app.versions:
        try:
            first = app.versions[0]
            _, ext = os.path.splitext(first.filename)
            if ext.lower() in (".exe", ".msi"):
                raw = b"".join(smb_stream_fn(first.smb_path))
                if _try_extract_from_exe(raw, app.name):
                    logger.info("Extracted icon for %s", app.name)
                    return f"/static/icons/_cache/{app.name}.png"
        except Exception as e:
            logger.warning("Icon extraction failed for %s: %s", app.name, e)

    # 4. Placeholder
    return PLACEHOLDER


def resolve_all(apps, smb_stream_fn=None):
    """Attach icon_url to each App in-place."""
    for app in apps:
        app.icon_url = resolve(app, smb_stream_fn)
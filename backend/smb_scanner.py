"""
smb_scanner.py
--------------
Connects to a CIFS/SMB share and scans the installs directory structure.

Expected share layout:
  \\SERVER\share\
    vscode\
      VSCodeSetup-1.85.0.exe
      VSCodeSetup-1.86.0.exe
    git\
      Git-2.43-64-bit.exe
    nodejs\
      node-v20.11.0-x64.msi

Returns a list of App objects, each with a name and list of Version objects.
Results are cached for CACHE_TTL seconds to avoid hammering the share on every request.
"""

import os
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

import smbclient  # smbprotocol

logger = logging.getLogger(__name__)

# ── Config (read from env) ────────────────────────────────────────────────────
SMB_SERVER   = os.environ.get("SMB_SERVER", "")          # e.g. "fileserver.corp"
SMB_SHARE    = os.environ.get("SMB_SHARE", "installs")   # share name (no backslashes)
SMB_BASE_PATH = os.environ.get("SMB_BASE_PATH", "")      # sub-folder inside share, optional
SMB_USER     = os.environ.get("SMB_USER", "")
SMB_PASSWORD = os.environ.get("SMB_PASSWORD", "")
SMB_DOMAIN   = os.environ.get("SMB_DOMAIN", "")
CACHE_TTL    = int(os.environ.get("SMB_CACHE_TTL", "60"))  # seconds

# Recognised installer extensions
INSTALLER_EXTS = {".exe", ".msi", ".msix", ".appx", ".zip", ".7z"}

# ── Data classes ──────────────────────────────────────────────────────────────
@dataclass
class Version:
    filename: str          # e.g. "VSCodeSetup-1.86.0.exe"
    label: str             # cleaned-up display label
    smb_path: str          # full SMB path for download route


@dataclass
class App:
    name: str              # folder name, e.g. "vscode"
    display_name: str      # title-cased for UI
    versions: list = field(default_factory=list)
    icon_url: str = ""     # set by icon_resolver


# ── Cache ─────────────────────────────────────────────────────────────────────
_cache: Optional[list] = None
_cache_ts: float = 0.0


def _unc(path: str = "") -> str:
    """Build a UNC path: \\SERVER\SHARE\path"""
    base = f"\\\\{SMB_SERVER}\\{SMB_SHARE}"
    if SMB_BASE_PATH:
        base += f"\\{SMB_BASE_PATH.strip('\\')}"
    if path:
        base += f"\\{path.strip('\\')}"
    return base


def _register():
    """Register SMB credentials (idempotent)."""
    if SMB_USER:
        smbclient.register_session(
            SMB_SERVER,
            username=SMB_USER,
            password=SMB_PASSWORD,
            domain=SMB_DOMAIN or None,
        )


def _clean_label(filename: str) -> str:
    """Turn a filename into a readable version label.
    e.g. 'VSCodeSetup-1.86.0.exe' → '1.86.0'
         'node-v20.11.0-x64.msi'  → 'v20.11.0 x64'
    """
    import re
    stem = os.path.splitext(filename)[0]
    # try to extract a version-like token
    match = re.search(r"v?(\d+[\d.]+[\w.-]*)", stem)
    if match:
        return match.group(0).replace("-", " ").strip()
    return stem


def scan() -> list:
    """
    Return list[App] from the SMB share.
    Uses a TTL cache to avoid re-scanning on every request.
    """
    global _cache, _cache_ts

    if not SMB_SERVER:
        logger.warning("SMB_SERVER not configured — returning empty installs list.")
        return []

    now = time.time()
    if _cache is not None and (now - _cache_ts) < CACHE_TTL:
        return _cache

    try:
        _register()
        apps = []
        root = _unc()

        for entry in smbclient.scandir(root):
            if not entry.is_dir():
                continue
            app_name = entry.name
            app_dir  = _unc(app_name)
            versions = []

            for fentry in smbclient.scandir(app_dir):
                if fentry.is_file():
                    _, ext = os.path.splitext(fentry.name)
                    if ext.lower() in INSTALLER_EXTS:
                        versions.append(Version(
                            filename=fentry.name,
                            label=_clean_label(fentry.name),
                            smb_path=f"{app_name}/{fentry.name}",
                        ))

            if versions:
                # Sort versions: newest-looking first (simple lexicographic desc)
                versions.sort(key=lambda v: v.filename, reverse=True)
                apps.append(App(
                    name=app_name,
                    display_name=app_name.replace("-", " ").replace("_", " ").title(),
                    versions=versions,
                ))

        apps.sort(key=lambda a: a.display_name)
        _cache = apps
        _cache_ts = now
        logger.info("SMB scan complete: %d apps found.", len(apps))
        return apps

    except Exception as exc:
        logger.error("SMB scan failed: %s", exc)
        # Return stale cache if available, else empty
        return _cache or []


def stream_file(smb_path: str):
    """
    Generator: yields chunks of a file from the SMB share.
    smb_path is relative, e.g. 'vscode/VSCodeSetup-1.86.0.exe'
    """
    _register()
    full_path = _unc(smb_path)
    with smbclient.open_file(full_path, mode="rb") as f:
        while True:
            chunk = f.read(1024 * 1024)  # 1 MB chunks
            if not chunk:
                break
            yield chunk
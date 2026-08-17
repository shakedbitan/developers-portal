"""
auth.py
-------
Authentication via oauth2-proxy.

Instead of relying on nginx header injection (which is proving unreliable),
Eden calls oauth2-proxy's /oauth2/userinfo endpoint directly on each request,
forwarding the user's session cookie. This is the most robust approach.

oauth2-proxy validates the cookie and returns the user's identity.
Eden caches the result in Flask's session to avoid calling oauth2-proxy
on every single request.

While AUTH_ENABLED=false, returns DEV_USERNAME for local testing.
"""

import logging

import requests as http_requests
from flask import request, session

import config
import db

logger = logging.getLogger(__name__)

# Internal URL to oauth2-proxy userinfo endpoint
# Called server-side — no TLS needed since it's cluster-internal
def _get_userinfo_url() -> str:
    return config.OAUTH2_PROXY_URL.rstrip("/") + "/oauth2/userinfo"


def _fetch_username_from_proxy() -> str:
    """
    Call oauth2-proxy /oauth2/userinfo with the user's session cookie.
    Returns the username or empty string if not authenticated.
    """
    cookie_header = request.headers.get("Cookie", "")
    if not cookie_header:
        logger.debug("No cookie in request — not authenticated")
        return ""

    try:
        resp = http_requests.get(
            _get_userinfo_url(),
            headers={"Cookie": cookie_header},
            timeout=3,
            verify=False,
        )
        if resp.status_code != 200:
            logger.debug("oauth2-proxy userinfo returned %d", resp.status_code)
            return ""

        data = resp.json()
        logger.debug("oauth2-proxy userinfo: %s", data)

        # Try common claim names in order of preference
        username = (
            data.get("user")
            or data.get("email")
            or data.get("upn")
            or data.get("preferred_username")
            or ""
        )

        # Strip domain — handle all formats:
        # DOMAIN\\username -> username
        # domain/username  -> username
        # user@domain.com  -> user
        if "\\" in username:
            username = username.split("\\")[-1]
        elif "/" in username and not username.startswith("http"):
            username = username.split("/")[-1]
        elif "@" in username:
            username = username.split("@")[0]

        return username.strip()

    except Exception as e:
        logger.warning("Failed to call oauth2-proxy userinfo: %s", e)
        return ""


def get_current_username() -> str:
    """
    Returns the authenticated username.

    Checks Flask session cache first (avoids calling oauth2-proxy on every
    request). If not cached, calls oauth2-proxy /oauth2/userinfo directly.

    While AUTH_ENABLED=false: returns DEV_USERNAME for testing.
    """
    if not config.AUTH_ENABLED:
        return config.DEV_USERNAME

    # Check session cache first
    cached = session.get("_eden_username")
    if cached:
        return cached

    # Call oauth2-proxy directly
    username = _fetch_username_from_proxy()
    if username:
        session["_eden_username"] = username
        logger.info("Authenticated user: %s", username)

    return username


def is_authenticated() -> bool:
    """Whether the current request has a valid authenticated user."""
    if not config.AUTH_ENABLED:
        return True
    return bool(get_current_username())


def is_admin() -> bool:
    """
    Whether the current user has admin privileges.
    While AUTH_ENABLED=false — always True (dev/test mode).
    When AUTH_ENABLED=true — checks the users table in Postgres.
    """
    if not config.AUTH_ENABLED:
        return True
    username = get_current_username()
    if not username:
        return False
    return db.is_user_admin(username)


def ensure_user_exists():
    """
    Ensure the current user has a DB record.
    Bootstraps the first-ever user as admin.
    No-op while AUTH_ENABLED=false.
    """
    if not config.AUTH_ENABLED:
        return
    username = get_current_username()
    if username:
        db.get_or_create_user(username)
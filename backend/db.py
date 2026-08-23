"""
db.py
-----
Postgres connection pool and all database queries for Eden.

Tables:
  sites              - approved web app links (replaces old SITES_JSON ConfigMap)
  site_submissions   - pending user-submitted web apps awaiting approval
  user_stars         - per-user starred apps with drag-to-reorder position
"""

import logging
import threading
from contextlib import contextmanager
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import SimpleConnectionPool

import config

logger = logging.getLogger(__name__)

_pool: Optional[SimpleConnectionPool] = None
_pool_lock = threading.Lock()


def init_pool():
    """Initialize the connection pool. Call once at startup."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            return
        try:
            _pool = SimpleConnectionPool(
                minconn=1,
                maxconn=10,
                host=config.DB_HOST,
                port=config.DB_PORT,
                dbname=config.DB_NAME,
                user=config.DB_USER,
                password=config.DB_PASSWORD,
                connect_timeout=5,
            )
            logger.info("Postgres connection pool initialized: %s:%s/%s",
                        config.DB_HOST, config.DB_PORT, config.DB_NAME)
        except Exception as e:
            logger.error("Failed to initialize Postgres pool: %s", e)
            _pool = None


@contextmanager
def get_conn():
    """Context manager that yields a connection from the pool."""
    if _pool is None:
        init_pool()
    if _pool is None:
        raise RuntimeError("Postgres pool not available")
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def is_available() -> bool:
    """Check if DB is reachable. Used for health checks / fallback logic."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception as e:
        logger.warning("Postgres health check failed: %s", e)
        return False


def _add_group_columns():
    """Add group_name, group_display_name, env_label, env_color to sites and site_submissions if missing."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                for tbl in ("sites", "site_submissions"):
                    cur.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS group_name TEXT")
                    cur.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS group_display_name TEXT")
                    cur.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS env_label TEXT")
                    cur.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS env_color TEXT")
        logger.info("group columns ensured on sites and site_submissions tables")
    except Exception as e:
        logger.warning("group column migration skipped: %s", e)


def _fix_user_stars_constraint():
    """
    Migrate user_stars from old PRIMARY KEY (username, site_url)
    to new UNIQUE (username, site_id). Safe to call repeatedly.
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Check if the new constraint already exists
                cur.execute("""
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'user_stars_username_site_id_key'
                """)
                if cur.fetchone():
                    return  # already migrated

                # Drop old primary key constraint if it exists
                cur.execute("""
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = 'user_stars'::regclass
                      AND contype = 'p'
                """)
                old_pk = cur.fetchone()
                if old_pk:
                    cur.execute(f"ALTER TABLE user_stars DROP CONSTRAINT {old_pk[0]}")
                    logger.info("Dropped old user_stars primary key: %s", old_pk[0])

                # Drop NOT NULL on site_url so we can insert with site_id only
                cur.execute("""
                    ALTER TABLE user_stars
                    ALTER COLUMN site_url DROP NOT NULL
                """)
                logger.info("Dropped NOT NULL on user_stars.site_url")

                # Add new unique constraint on site_id
                cur.execute("""
                    ALTER TABLE user_stars
                    ADD CONSTRAINT user_stars_username_site_id_key
                    UNIQUE (username, site_id)
                """)
                logger.info("Added user_stars UNIQUE (username, site_id) constraint")
    except Exception as e:
        logger.warning("user_stars constraint migration skipped: %s", e)


def init_schema():
    """Create tables if they don't exist. Safe to call on every startup."""
    schema_sql = """
    CREATE TABLE IF NOT EXISTS sites (
        id                 SERIAL PRIMARY KEY,
        name               TEXT NOT NULL,
        url                TEXT NOT NULL UNIQUE,
        favicon_url        TEXT,
        tags               TEXT[] DEFAULT '{}',
        created_at         TIMESTAMP DEFAULT NOW(),
        created_by         TEXT,
        group_name         TEXT,
        group_display_name TEXT,
        env_label          TEXT,
        env_color          TEXT
    );

    CREATE TABLE IF NOT EXISTS site_submissions (
        id                 SERIAL PRIMARY KEY,
        name               TEXT NOT NULL,
        url                TEXT NOT NULL,
        favicon_url        TEXT,
        tags               TEXT[] DEFAULT '{}',
        submitted_by       TEXT,
        submitted_at       TIMESTAMP DEFAULT NOW(),
        status             TEXT DEFAULT 'pending',
        reviewed_by        TEXT,
        reviewed_at        TIMESTAMP,
        group_name         TEXT,
        group_display_name TEXT,
        env_label          TEXT,
        env_color          TEXT
    );

    CREATE TABLE IF NOT EXISTS user_stars (
        username    TEXT NOT NULL,
        site_url    TEXT,
        site_id     INTEGER,
        star_order  INTEGER NOT NULL DEFAULT 0,
        starred_at  TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS users (
        username    TEXT PRIMARY KEY,
        is_admin    BOOLEAN NOT NULL DEFAULT FALSE,
        first_seen  TIMESTAMP DEFAULT NOW(),
        last_seen   TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS script_submissions (
        id           SERIAL PRIMARY KEY,
        script_name  TEXT NOT NULL,
        team         TEXT NOT NULL,
        language     TEXT NOT NULL,
        mr_url       TEXT,
        mr_iid       INTEGER,
        submitted_by TEXT,
        submitted_at TIMESTAMP DEFAULT NOW(),
        status       TEXT DEFAULT 'pending'
    );

    -- Script *runs* that require approval before Argo actually executes
    -- them (distinct from script_submissions above, which is code/MR
    -- review -- this is runtime-argument review for an already-approved
    -- script). One row per Argo workflow left suspended at its `approval`
    -- node; args is the submitter's values, editable by the approver
    -- before it's relayed back to Argo's resume call.
    CREATE TABLE IF NOT EXISTS script_run_approvals (
        id            SERIAL PRIMARY KEY,
        team          TEXT NOT NULL,
        script_name   TEXT NOT NULL,
        args          JSONB NOT NULL DEFAULT '{}',
        workflow_name TEXT NOT NULL,
        namespace     TEXT NOT NULL,
        submitted_by  TEXT,
        submitted_at  TIMESTAMP DEFAULT NOW(),
        status        TEXT DEFAULT 'pending',
        reviewed_by   TEXT,
        reviewed_at   TIMESTAMP
    );

    ALTER TABLE user_stars ADD COLUMN IF NOT EXISTS site_id INTEGER;
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
        logger.info("Database schema initialized")
    except Exception as e:
        logger.error("Failed to initialize schema: %s", e)

    # Fix user_stars constraint separately — drop old PK, add new UNIQUE on site_id
    _add_group_columns()
    _fix_user_stars_constraint()


# ── Sites ─────────────────────────────────────────────────────────────────────

def get_all_sites() -> list[dict]:
    """Return all approved sites."""
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, name, url, favicon_url, tags,
                           group_name, group_display_name, env_label, env_color
                    FROM sites ORDER BY name ASC
                """)
                rows = cur.fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        logger.error("get_all_sites failed: %s", e)
        return []


def create_site(name: str, url: str, favicon_url: str, tags: list, created_by: str, **kwargs) -> dict:
    """Insert a new approved site directly (used when admin approves a submission)."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO sites (name, url, favicon_url, tags, created_by,
                                         group_name, group_display_name, env_label, env_color)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (url) DO UPDATE SET name = EXCLUDED.name
                       RETURNING id""",
                    (name, url, favicon_url, tags, created_by,
                     kwargs.get('group_name'), kwargs.get('group_display_name'),
                     kwargs.get('env_label'), kwargs.get('env_color')),
                )
                site_id = cur.fetchone()[0]
        logger.info("Site created: %s (%s) by %s", name, url, created_by)
        return {"id": site_id, "name": name, "url": url}
    except Exception as e:
        logger.error("create_site failed: %s", e)
        return {"error": str(e)}


def delete_site(site_id: int) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sites WHERE id = %s", (site_id,))
                # Same silent-no-op risk as edit_site: a DELETE matching zero
                # rows doesn't raise either.
                if cur.rowcount == 0:
                    logger.error("delete_site: no row matched id=%s -- nothing was deleted", site_id)
                    return False
        return True
    except Exception as e:
        logger.error("delete_site failed: %s", e)
        return False


def edit_site(site_id: int, name: str = None, url: str = None,
              tags: list = None, favicon_url: str = None,
              group_name: str = None, group_display_name: str = None,
              env_label: str = None, env_color: str = None) -> bool:
    """Update site fields including group columns."""
    if all(v is None for v in [name, url, tags, favicon_url,
                                group_name, group_display_name, env_label, env_color]):
        return True
    try:
        sets, vals = [], []
        if name               is not None: sets.append("name = %s");               vals.append(name)
        if url                is not None: sets.append("url = %s");                vals.append(url)
        if tags               is not None: sets.append("tags = %s");               vals.append(tags)
        if favicon_url        is not None: sets.append("favicon_url = %s");        vals.append(favicon_url)
        if group_name         is not None: sets.append("group_name = %s");         vals.append(group_name or None)
        if group_display_name is not None: sets.append("group_display_name = %s"); vals.append(group_display_name or None)
        if env_label          is not None: sets.append("env_label = %s");          vals.append(env_label or None)
        if env_color          is not None: sets.append("env_color = %s");          vals.append(env_color or None)
        vals.append(site_id)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE sites SET {', '.join(sets)} WHERE id = %s", vals)
                # Postgres doesn't error on an UPDATE that matches zero rows --
                # it just succeeds with rowcount 0. Without this check, a
                # stale/wrong site_id (or a row that no longer exists) made
                # this return True unconditionally: no exception, no log,
                # nothing written, and the API reported "updated" anyway.
                if cur.rowcount == 0:
                    logger.error("edit_site: no row matched id=%s -- nothing was written", site_id)
                    return False
        return True
    except Exception as e:
        logger.error("edit_site failed: %s", e)
        return False


# ── Site submissions (pending approval) ─────────────────────────────────────────

def create_submission(name: str, url: str, favicon_url: str, tags: list, submitted_by: str,
                      group_name: str = None, group_display_name: str = None,
                      env_label: str = None, env_color: str = None) -> dict:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO site_submissions
                       (name, url, favicon_url, tags, submitted_by, group_name, group_display_name, env_label, env_color)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (name, url, favicon_url, tags, submitted_by, group_name, group_display_name, env_label, env_color),
                )
                sub_id = cur.fetchone()[0]
        logger.info("Submission created: %s (%s) by %s", name, url, submitted_by)
        return {"id": sub_id}
    except Exception as e:
        logger.error("create_submission failed: %s", e)
        return {"error": str(e)}


def get_pending_submissions() -> list[dict]:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """SELECT id, name, url, favicon_url, tags, submitted_by, submitted_at
                       FROM site_submissions WHERE status = 'pending'
                       ORDER BY submitted_at ASC"""
                )
                rows = cur.fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        logger.error("get_pending_submissions failed: %s", e)
        return []


def review_submission(submission_id: int, approve: bool, reviewer: str) -> dict:
    """Approve or reject a pending submission. If approved, also creates the live site."""
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM site_submissions WHERE id = %s", (submission_id,))
                sub = cur.fetchone()
                if not sub:
                    return {"error": "Submission not found"}

                new_status = "approved" if approve else "rejected"
                cur.execute(
                    """UPDATE site_submissions
                       SET status = %s, reviewed_by = %s, reviewed_at = NOW()
                       WHERE id = %s""",
                    (new_status, reviewer, submission_id),
                )

        if approve:
            result = create_site(
                sub["name"], sub["url"], sub["favicon_url"],
                sub["tags"] or [], sub["submitted_by"],
                group_name=sub.get("group_name"),
                group_display_name=sub.get("group_display_name"),
                env_label=sub.get("env_label"),
                env_color=sub.get("env_color"),
            )
            if "error" in result:
                return result

        logger.info("Submission %d %s by %s", submission_id, new_status, reviewer)
        return {"status": new_status}
    except Exception as e:
        logger.error("review_submission failed: %s", e)
        return {"error": str(e)}


# ── User stars ───────────────────────────────────────────────────────────────

def get_user_stars(username: str) -> list[dict]:
    """Return starred sites for a user, in their chosen order."""
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """SELECT s.id, s.name, s.url, s.favicon_url, s.tags,
                              s.group_name, s.group_display_name, s.env_label, s.env_color,
                              us.star_order
                       FROM user_stars us
                       JOIN sites s ON s.id = us.site_id
                       WHERE us.username = %s
                       ORDER BY us.star_order ASC""",
                    (username,),
                )
                rows = cur.fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        logger.error("get_user_stars failed: %s", e)
        return []


def star_site(username: str, site_id: int) -> bool:
    """Add a star by site_id — appended to end of user's current order."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(MAX(star_order), -1) + 1 FROM user_stars WHERE username = %s",
                    (username,),
                )
                next_order = cur.fetchone()[0]
                cur.execute(
                    """INSERT INTO user_stars (username, site_id, star_order)
                       VALUES (%s, %s, %s)
                       ON CONFLICT ON CONSTRAINT user_stars_username_site_id_key DO NOTHING""",
                    (username, site_id, next_order),
                )
        return True
    except Exception as e:
        logger.error("star_site failed: %s", e)
        return False


def unstar_site(username: str, site_id: int) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM user_stars WHERE username = %s AND site_id = %s",
                    (username, site_id),
                )
        return True
    except Exception as e:
        logger.error("unstar_site failed: %s", e)
        return False


def reorder_stars(username: str, ordered_ids: list[int]) -> bool:
    """Persist new drag-and-drop order by site_id."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                for idx, site_id in enumerate(ordered_ids):
                    cur.execute(
                        """UPDATE user_stars SET star_order = %s
                           WHERE username = %s AND site_id = %s""",
                        (idx, username, site_id),
                    )
        return True
    except Exception as e:
        logger.error("reorder_stars failed: %s", e)
        return False


# ── Script submissions ─────────────────────────────────────────────────────────

def create_script_submission(script_name: str, team: str, language: str,
                              mr_url: str, mr_iid: int, submitted_by: str) -> dict:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO script_submissions
                       (script_name, team, language, mr_url, mr_iid, submitted_by)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                    (script_name, team, language, mr_url, mr_iid, submitted_by),
                )
                return {"id": cur.fetchone()[0]}
    except Exception as e:
        logger.error("create_script_submission failed: %s", e)
        return {"error": str(e)}


def get_pending_script_submissions() -> list[dict]:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """SELECT id, script_name, team, language, mr_url, mr_iid,
                              submitted_by, submitted_at
                       FROM script_submissions WHERE status = 'pending'
                       ORDER BY submitted_at ASC"""
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error("get_pending_script_submissions failed: %s", e)
        return []


def update_script_submission_status(submission_id: int, status: str) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE script_submissions SET status = %s WHERE id = %s",
                    (status, submission_id),
                )
                if cur.rowcount == 0:
                    logger.error("update_script_submission_status: no row matched id=%s", submission_id)
                    return False
        return True
    except Exception as e:
        logger.error("update_script_submission_status failed: %s", e)
        return False


# ── Script run approvals ────────────────────────────────────────────────────

def create_run_approval(team: str, script_name: str, args: dict,
                         workflow_name: str, namespace: str, submitted_by: str) -> dict:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO script_run_approvals
                       (team, script_name, args, workflow_name, namespace, submitted_by)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                    (team, script_name, psycopg2.extras.Json(args),
                     workflow_name, namespace, submitted_by),
                )
                return {"id": cur.fetchone()[0]}
    except Exception as e:
        logger.error("create_run_approval failed: %s", e)
        return {"error": str(e)}


def get_pending_run_approvals() -> list[dict]:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """SELECT id, team, script_name, args, workflow_name, namespace,
                              submitted_by, submitted_at
                       FROM script_run_approvals WHERE status = 'pending'
                       ORDER BY submitted_at ASC"""
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error("get_pending_run_approvals failed: %s", e)
        return []


def update_run_approval_status(approval_id: int, status: str, reviewed_by: str,
                                final_args: dict | None = None) -> bool:
    """final_args, when given, records what was actually sent to Argo on
    approval -- may differ from the submitter's original args if an admin
    edited a value before approving."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if final_args is not None:
                    cur.execute(
                        """UPDATE script_run_approvals
                           SET status = %s, reviewed_by = %s, reviewed_at = NOW(), args = %s
                           WHERE id = %s""",
                        (status, reviewed_by, psycopg2.extras.Json(final_args), approval_id),
                    )
                else:
                    cur.execute(
                        """UPDATE script_run_approvals
                           SET status = %s, reviewed_by = %s, reviewed_at = NOW()
                           WHERE id = %s""",
                        (status, reviewed_by, approval_id),
                    )
                if cur.rowcount == 0:
                    logger.error("update_run_approval_status: no row matched id=%s", approval_id)
                    return False
        return True
    except Exception as e:
        logger.error("update_run_approval_status failed: %s", e)
        return False


# ── Users / admin management ────────────────────────────────────────────────

def get_or_create_user(username: str) -> dict:
    """
    Look up a user, creating it if this is their first login.
    The very first user ever created becomes admin automatically (bootstrap).
    Updates last_seen on every call.
    """
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM users WHERE username = %s", (username,))
                user = cur.fetchone()

                if user:
                    cur.execute(
                        "UPDATE users SET last_seen = NOW() WHERE username = %s",
                        (username,),
                    )
                    return dict(user)

                # New user — check if this is the very first user in the table
                cur.execute("SELECT COUNT(*) AS cnt FROM users")
                is_first_user = cur.fetchone()["cnt"] == 0

                cur.execute(
                    """INSERT INTO users (username, is_admin)
                       VALUES (%s, %s) RETURNING *""",
                    (username, is_first_user),
                )
                new_user = dict(cur.fetchone())
                if is_first_user:
                    logger.info("First user bootstrap: %s is now admin", username)
                return new_user
    except Exception as e:
        logger.error("get_or_create_user failed: %s", e)
        return {"username": username, "is_admin": False}


def is_user_admin(username: str) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT is_admin FROM users WHERE username = %s", (username,))
                row = cur.fetchone()
                return bool(row[0]) if row else False
    except Exception as e:
        logger.error("is_user_admin failed: %s", e)
        return False


def set_user_admin(username: str, is_admin: bool) -> bool:
    """Promote or demote a user. Used by the admin management UI."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET is_admin = %s WHERE username = %s",
                    (is_admin, username),
                )
                if cur.rowcount == 0:
                    logger.error("set_user_admin: no user matched username=%s", username)
                    return False
        return True
    except Exception as e:
        logger.error("set_user_admin failed: %s", e)
        return False


def get_all_users() -> list[dict]:
    """List all known users — for the admin management UI."""
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT username, is_admin, first_seen, last_seen FROM users ORDER BY first_seen ASC"
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error("get_all_users failed: %s", e)
        return []

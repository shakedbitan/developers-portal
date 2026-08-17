"""
PostgreSQL-backed download catalog and S3-compatible download API.

This module is intentionally self-contained so it can be added to the existing
application without expanding ``db.py``.  It stores one row per logical product
(``download_items``) and one row per downloadable architecture/version/OS
combination (``download_variants``).

Application integration::

    import download_catalog

    # Run after db.init_pool()/db.init_schema() during application startup.
    download_catalog.init_schema()

    # Run once, after the Flask app has been created.
    download_catalog.register_download_routes(app)

The uploader can use ``CatalogRepository.upsert_item`` and
``CatalogRepository.upsert_variant`` so it shares the same validation and
upsert semantics as the website.

Registered API contract:

* ``GET /api/downloads?q=&category=&cursor=&limit=`` returns
  ``{items, next_cursor, total}``; a category or a search of 2+ characters is
  required, so opening Eden never loads the full catalog.
* ``GET /api/downloads/categories`` returns the five category labels/counts.
* ``GET /api/downloads/<id>`` returns one logical item and all active variants.
* ``POST /api/downloads/variants/<id>/url`` returns a short-lived S3 URL.
* ``POST /api/downloads/upload`` lets an admin stream a new artifact into S3
  and atomically create either a catalog item plus its first variant or one
  additional variant on an existing item.

Required runtime configuration is read from environment variables (or matching
attributes added to config.py):

    S3_BUCKET, S3_ENDPOINT_URL, S3_REGION, S3_ACCESS_KEY_ID,
    S3_SECRET_ACCESS_KEY, S3_ADDRESSING_STYLE, S3_VERIFY_TLS,
    S3_CA_BUNDLE, S3_PRESIGN_TTL, S3_PREFIX, DOWNLOAD_UPLOAD_MAX_BYTES,
    DOWNLOAD_UPLOAD_MULTIPART_OVERHEAD_BYTES

Only ``S3_BUCKET`` is mandatory.  Omitting ``S3_ENDPOINT_URL`` deliberately
allows AWS S3; an air-gapped S3-compatible installation should set it.
Credentials may be omitted when boto3's normal credential chain is available.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import mimetypes
import os
import re
import threading
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any, BinaryIO, ContextManager
from urllib.parse import quote, urlparse

import psycopg2.extras
from flask import Blueprint, Flask, current_app, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

# Match the existing backend convention: app.py and sibling modules are loaded
# from the backend directory as top-level modules.
import auth as eden_auth
import config as eden_config
import db as eden_db


logger = logging.getLogger(__name__)


def _integer_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = getattr(eden_config, name, os.environ.get(name, str(default)))
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using %d", name, raw, default)
        return default
    if parsed < minimum or parsed > maximum:
        logger.warning(
            "%s=%d is outside %d..%d; using %d",
            name,
            parsed,
            minimum,
            maximum,
            default,
        )
        return default
    return parsed


CATEGORIES: tuple[dict[str, str], ...] = (
    {"id": "development", "label": "Development"},
    {"id": "infrastructure", "label": "IT & Infrastructure"},
    {"id": "security", "label": "Security"},
    {"id": "data", "label": "Data & Databases"},
    {"id": "productivity", "label": "Productivity & Design"},
    {"id": "utilities", "label": "Utilities"},
    {"id": "drivers", "label": "Drivers & Firmware"},
    {"id": "other", "label": "Other"},
)
CATEGORY_IDS = frozenset(category["id"] for category in CATEGORIES)
CATEGORY_LABELS = {category["id"]: category["label"] for category in CATEGORIES}

MAX_PAGE_SIZE = _integer_setting("DOWNLOAD_MAX_PAGE_SIZE", 100, 1, 500)
DEFAULT_PAGE_SIZE = _integer_setting(
    "DOWNLOAD_DEFAULT_PAGE_SIZE", 24, 1, MAX_PAGE_SIZE
)
MAX_PAGE = 1_000_000
MIN_SEARCH_LENGTH = 2
MAX_SEARCH_LENGTH = 128
DEFAULT_PRESIGN_TTL = 300
MIN_PRESIGN_TTL = 60
MAX_PRESIGN_TTL = 3600
DEFAULT_UPLOAD_MAX_BYTES = 10 * 1024 * 1024 * 1024
DOWNLOAD_UPLOAD_MAX_BYTES = _integer_setting(
    "DOWNLOAD_UPLOAD_MAX_BYTES",
    DEFAULT_UPLOAD_MAX_BYTES,
    1,
    5 * 1024 * 1024 * 1024 * 1024,
)
DEFAULT_UPLOAD_MULTIPART_OVERHEAD_BYTES = 16 * 1024 * 1024
DOWNLOAD_UPLOAD_MULTIPART_OVERHEAD_BYTES = _integer_setting(
    "DOWNLOAD_UPLOAD_MULTIPART_OVERHEAD_BYTES",
    DEFAULT_UPLOAD_MULTIPART_OVERHEAD_BYTES,
    64 * 1024,
    1024 * 1024 * 1024,
)
UPLOAD_REQUEST_MAX_BYTES = (
    DOWNLOAD_UPLOAD_MAX_BYTES + DOWNLOAD_UPLOAD_MULTIPART_OVERHEAD_BYTES
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_BUCKET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_TOKEN_RE = re.compile(r"^[\w .+()-]{0,128}$", re.UNICODE)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

_ARCHITECTURE_ALIASES = {
    "": "unknown",
    "none": "unknown",
    "n/a": "unknown",
    "noarch": "universal",
    "any": "universal",
    "all": "universal",
    "amd64": "x64",
    "x86_64": "x64",
    "x86-64": "x64",
    "win64": "x64",
    "64-bit": "x64",
    "64bit": "x64",
    "i386": "x86",
    "i686": "x86",
    "win32": "x86",
    "32-bit": "x86",
    "32bit": "x86",
    "aarch64": "arm64",
}


class CatalogError(RuntimeError):
    """Base class for expected catalog failures."""


class CatalogValidationError(CatalogError):
    """Raised when untrusted catalog or request data is invalid."""

    def __init__(self, message: str, *, field: str | None = None):
        super().__init__(message)
        self.field = field


class CatalogNotFoundError(CatalogError):
    """Raised when a catalog item or variant does not exist/is inactive."""


class CatalogConflictError(CatalogError):
    """Raised when a new logical catalog item would replace an existing one."""


class StorageConfigurationError(CatalogError):
    """Raised when the S3 signer cannot be configured."""


class ArtifactTooLargeError(CatalogValidationError):
    """Raised while streaming when an artifact exceeds the configured limit."""


class EmptyArtifactError(CatalogValidationError):
    """Raised after upload when the selected artifact contains no bytes."""


class StorageUploadError(CatalogError):
    """Raised when object storage rejects or interrupts an artifact upload."""


class CatalogPersistenceError(CatalogError):
    """Raised when validated upload metadata cannot be saved to PostgreSQL."""


# Migration numbers are monotonic and recorded transactionally.  Future schema
# changes should be appended instead of modifying a migration already deployed.
MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (
        1,
        "create download catalog",
        r"""
        CREATE TABLE IF NOT EXISTS download_items (
            id              BIGSERIAL PRIMARY KEY,
            slug            TEXT NOT NULL UNIQUE,
            name            TEXT NOT NULL,
            description     TEXT NOT NULL DEFAULT '',
            category        TEXT NOT NULL,
            publisher       TEXT NOT NULL DEFAULT '',
            icon_url        TEXT,
            tags            TEXT[] NOT NULL DEFAULT '{}',
            aliases         TEXT[] NOT NULL DEFAULT '{}',
            metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            search_vector   TSVECTOR,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT download_items_category_check
                CHECK (category IN ('development', 'infrastructure', 'data', 'productivity'))
        );

        CREATE TABLE IF NOT EXISTS download_variants (
            id                BIGSERIAL PRIMARY KEY,
            item_id           BIGINT NOT NULL REFERENCES download_items(id)
                                ON DELETE CASCADE,
            storage_bucket    TEXT NOT NULL DEFAULT '',
            object_key        TEXT NOT NULL,
            file_name         TEXT NOT NULL,
            version           TEXT NOT NULL DEFAULT '',
            architecture      TEXT NOT NULL DEFAULT 'unknown',
            operating_system  TEXT NOT NULL DEFAULT '',
            file_type         TEXT NOT NULL DEFAULT '',
            size_bytes        BIGINT,
            sha256            TEXT,
            etag              TEXT,
            content_type      TEXT,
            last_modified     TIMESTAMPTZ,
            metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
            is_active         BOOLEAN NOT NULL DEFAULT TRUE,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT download_variants_object_unique
                UNIQUE (storage_bucket, object_key),
            CONSTRAINT download_variants_size_check
                CHECK (size_bytes IS NULL OR size_bytes >= 0),
            CONSTRAINT download_variants_sha256_check
                CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-fA-F]{64}$')
        );

        CREATE OR REPLACE FUNCTION eden_download_item_before_write()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at := NOW();
            NEW.search_vector :=
                setweight(to_tsvector('simple', COALESCE(NEW.name, '')), 'A') ||
                setweight(to_tsvector('simple', COALESCE(NEW.publisher, '')), 'B') ||
                setweight(to_tsvector('simple', COALESCE(array_to_string(NEW.aliases, ' '), '')), 'A') ||
                setweight(to_tsvector('simple', COALESCE(array_to_string(NEW.tags, ' '), '')), 'B') ||
                setweight(to_tsvector('simple', COALESCE(NEW.description, '')), 'C');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS eden_download_item_before_write_trigger
            ON download_items;
        CREATE TRIGGER eden_download_item_before_write_trigger
            BEFORE INSERT OR UPDATE ON download_items
            FOR EACH ROW EXECUTE FUNCTION eden_download_item_before_write();

        CREATE OR REPLACE FUNCTION eden_download_variant_before_write()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at := NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS eden_download_variant_before_write_trigger
            ON download_variants;
        CREATE TRIGGER eden_download_variant_before_write_trigger
            BEFORE UPDATE ON download_variants
            FOR EACH ROW EXECUTE FUNCTION eden_download_variant_before_write();

        CREATE INDEX IF NOT EXISTS download_items_search_idx
            ON download_items USING GIN (search_vector);
        CREATE INDEX IF NOT EXISTS download_items_category_name_idx
            ON download_items (category, lower(name), id) WHERE is_active;
        CREATE INDEX IF NOT EXISTS download_items_updated_idx
            ON download_items (updated_at DESC, id) WHERE is_active;
        CREATE INDEX IF NOT EXISTS download_variants_item_idx
            ON download_variants (item_id, architecture, version) WHERE is_active;

        -- Backfill the search vector if rows were inserted before the trigger.
        UPDATE download_items SET name = name WHERE search_vector IS NULL;
        """,
    ),
    (
        2,
        "use canonical download category taxonomy",
        r"""
        -- This also upgrades databases initialized by early development builds.
        ALTER TABLE download_items
            DROP CONSTRAINT IF EXISTS download_items_category_check;
        UPDATE download_items
        SET category = CASE category
            WHEN 'code' THEN 'development'
            WHEN 'tools' THEN 'infrastructure'
            WHEN 'creative' THEN 'productivity'
            ELSE category
        END
        WHERE category IN ('code', 'tools', 'creative');
        ALTER TABLE download_items
            ADD CONSTRAINT download_items_category_check
            CHECK (category IN ('development', 'infrastructure', 'data', 'productivity'));
        """,
    ),
    (
        3,
        "allow visible other download category",
        r"""
        ALTER TABLE download_items
            DROP CONSTRAINT IF EXISTS download_items_category_check;
        ALTER TABLE download_items
            ADD CONSTRAINT download_items_category_check
            CHECK (category IN ('development', 'infrastructure', 'data', 'productivity', 'other'));
        """,
    ),
    (
        4,
        "expand download category taxonomy",
        r"""
        -- Additive: existing rows (including 'other') stay valid. This only
        -- widens the allowed set with security/utilities/drivers so more
        -- specific categories are available going forward.
        ALTER TABLE download_items
            DROP CONSTRAINT IF EXISTS download_items_category_check;
        ALTER TABLE download_items
            ADD CONSTRAINT download_items_category_check
            CHECK (category IN (
                'development', 'infrastructure', 'security', 'data',
                'productivity', 'utilities', 'drivers', 'other'
            ));
        """,
    ),
)

_MIGRATION_LOCK_ID = 2_794_336_241  # Stable advisory lock unique to this module.


def init_schema(
    connection_provider: Callable[[], ContextManager[Any]] | None = None,
) -> list[int]:
    """Apply pending catalog migrations and return the versions applied now.

    PostgreSQL's transaction-scoped advisory lock prevents two Gunicorn workers
    from applying the same migration concurrently.  A failed migration rolls
    back both its DDL and version marker.
    """

    connections = connection_provider or eden_db.get_conn
    applied_now: list[int] = []
    with connections() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_ID,))
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS download_catalog_schema_migrations (
                    version       INTEGER PRIMARY KEY,
                    description   TEXT NOT NULL,
                    applied_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute("SELECT version FROM download_catalog_schema_migrations")
            already_applied = {int(row[0]) for row in cur.fetchall()}

            for version, description, sql in MIGRATIONS:
                if version in already_applied:
                    continue
                logger.info("Applying download catalog migration %d: %s", version, description)
                cur.execute(sql)
                cur.execute(
                    """
                    INSERT INTO download_catalog_schema_migrations (version, description)
                    VALUES (%s, %s)
                    """,
                    (version, description),
                )
                applied_now.append(version)

    if applied_now:
        logger.info("Download catalog schema ready; applied migrations %s", applied_now)
    else:
        logger.debug("Download catalog schema already current")
    return applied_now


def _clean_text(
    value: Any,
    *,
    field: str,
    required: bool = False,
    max_length: int = 512,
) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise CatalogValidationError(f"{field} must be a string", field=field)
    if required and not text:
        raise CatalogValidationError(f"{field} is required", field=field)
    if len(text) > max_length:
        raise CatalogValidationError(
            f"{field} must be at most {max_length} characters", field=field
        )
    if _CONTROL_CHAR_RE.search(text):
        raise CatalogValidationError(f"{field} contains control characters", field=field)
    return text


def _clean_string_list(
    value: Any,
    *,
    field: str,
    max_items: int = 64,
    max_item_length: int = 128,
) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CatalogValidationError(f"{field} must be an array of strings", field=field)
    if len(value) > max_items:
        raise CatalogValidationError(
            f"{field} may contain at most {max_items} values", field=field
        )
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        item = _clean_text(raw, field=field, max_length=max_item_length)
        folded = item.casefold()
        if item and folded not in seen:
            seen.add(folded)
            result.append(item)
    return result


def _clean_metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CatalogValidationError("metadata must be an object", field="metadata")
    result = dict(value)
    try:
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise CatalogValidationError(
            "metadata must contain valid JSON values", field="metadata"
        ) from exc
    if len(encoded.encode("utf-8")) > 256 * 1024:
        raise CatalogValidationError(
            "metadata must be no larger than 256 KiB", field="metadata"
        )
    return result


def _clean_bool(value: Any, *, field: str, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise CatalogValidationError(f"{field} must be true or false", field=field)


def _clean_datetime(value: Any, *, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise CatalogValidationError(
                f"{field} must be an ISO-8601 timestamp", field=field
            ) from exc
    else:
        raise CatalogValidationError(
            f"{field} must be an ISO-8601 timestamp", field=field
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def normalize_architecture(value: Any) -> str:
    """Normalize common architecture aliases while retaining safe custom ones."""

    architecture = _clean_text(
        value, field="architecture", max_length=128
    ).casefold()
    architecture = _ARCHITECTURE_ALIASES.get(architecture, architecture)
    if not _SAFE_TOKEN_RE.fullmatch(architecture):
        raise CatalogValidationError(
            "architecture contains unsupported characters", field="architecture"
        )
    return architecture or "unknown"


def validate_item_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a logical catalog item for an importer."""

    if not isinstance(payload, Mapping):
        raise CatalogValidationError("item must be an object")
    slug = _clean_text(payload.get("slug"), field="slug", required=True, max_length=128).lower()
    if not _SLUG_RE.fullmatch(slug):
        raise CatalogValidationError(
            "slug must use lowercase letters, numbers, dots, dashes, or underscores",
            field="slug",
        )
    category = _clean_text(
        payload.get("category"), field="category", required=True, max_length=32
    ).lower()
    if category not in CATEGORY_IDS:
        raise CatalogValidationError(
            f"category must be one of: {', '.join(sorted(CATEGORY_IDS))}",
            field="category",
        )
    icon_url = _clean_text(payload.get("icon_url"), field="icon_url", max_length=2048)
    if icon_url and not (
        icon_url.startswith(("/", "https://", "http://", "data:image/"))
    ):
        raise CatalogValidationError(
            "icon_url must be a relative, http(s), or image data URL", field="icon_url"
        )
    return {
        "slug": slug,
        "name": _clean_text(payload.get("name"), field="name", required=True, max_length=256),
        "description": _clean_text(
            payload.get("description"), field="description", max_length=4000
        ),
        "category": category,
        "publisher": _clean_text(payload.get("publisher"), field="publisher", max_length=256),
        "icon_url": icon_url or None,
        "tags": _clean_string_list(payload.get("tags"), field="tags"),
        "aliases": _clean_string_list(payload.get("aliases"), field="aliases"),
        "metadata": _clean_metadata(payload.get("metadata")),
        "is_active": _clean_bool(payload.get("is_active"), field="is_active"),
    }


def validate_variant_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize an S3 object variant for an importer."""

    if not isinstance(payload, Mapping):
        raise CatalogValidationError("variant must be an object")
    object_key = _clean_text(
        payload.get("object_key"), field="object_key", required=True, max_length=1024
    )
    bucket = _clean_text(payload.get("storage_bucket"), field="storage_bucket", max_length=255)
    if bucket and not _BUCKET_RE.fullmatch(bucket):
        raise CatalogValidationError("storage_bucket is invalid", field="storage_bucket")

    sha256 = _clean_text(payload.get("sha256"), field="sha256", max_length=64)
    if sha256 and not _SHA256_RE.fullmatch(sha256):
        raise CatalogValidationError(
            "sha256 must contain exactly 64 hexadecimal characters", field="sha256"
        )

    raw_size = payload.get("size_bytes")
    size_bytes: int | None
    if raw_size in (None, ""):
        size_bytes = None
    else:
        if isinstance(raw_size, bool):
            raise CatalogValidationError("size_bytes must be an integer", field="size_bytes")
        try:
            size_bytes = int(raw_size)
        except (TypeError, ValueError) as exc:
            raise CatalogValidationError(
                "size_bytes must be an integer", field="size_bytes"
            ) from exc
        if size_bytes < 0:
            raise CatalogValidationError(
                "size_bytes cannot be negative", field="size_bytes"
            )

    version = _clean_text(payload.get("version"), field="version", max_length=128)
    if version.casefold() in {"n/a", "none", "unknown"}:
        version = ""
    operating_system = _clean_text(
        payload.get("operating_system"), field="operating_system", max_length=128
    ).casefold()
    if operating_system in {"n/a", "none", "unknown"}:
        operating_system = ""

    return {
        "storage_bucket": bucket,
        "object_key": object_key,
        "file_name": _clean_text(
            payload.get("file_name"), field="file_name", required=True, max_length=512
        ),
        "version": version,
        "architecture": normalize_architecture(payload.get("architecture")),
        "operating_system": operating_system,
        "file_type": _clean_text(
            payload.get("file_type"), field="file_type", max_length=64
        ).lower().lstrip("."),
        "size_bytes": size_bytes,
        "sha256": sha256.lower() or None,
        "etag": _clean_text(payload.get("etag"), field="etag", max_length=256) or None,
        "content_type": _clean_text(
            payload.get("content_type"), field="content_type", max_length=256
        ) or None,
        "last_modified": _clean_datetime(
            payload.get("last_modified"), field="last_modified"
        ),
        "metadata": _clean_metadata(payload.get("metadata")),
        "is_active": _clean_bool(payload.get("is_active"), field="is_active"),
    }


def _form_string_list(form: Mapping[str, Any], field: str) -> list[str]:
    """Accept repeated, JSON-array, or comma-separated multipart list fields."""

    getter = getattr(form, "getlist", None)
    raw_values = list(getter(field)) if callable(getter) else [form.get(field)]
    if callable(getter):
        raw_values.extend(getter(f"{field}[]"))
    values: list[str] = []
    for raw in raw_values:
        if raw in (None, ""):
            continue
        if not isinstance(raw, str):
            raise CatalogValidationError(f"{field} must contain strings", field=field)
        candidate = raw.strip()
        if candidate.startswith("["):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError as exc:
                raise CatalogValidationError(
                    f"{field} must be a JSON array or comma-separated list",
                    field=field,
                ) from exc
            if not isinstance(parsed, list):
                raise CatalogValidationError(f"{field} must be an array", field=field)
            values.extend(parsed)
        else:
            values.extend(part.strip() for part in candidate.split(","))
    return _clean_string_list(values, field=field)


def _slug_for_uploaded_item(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9._-]+", "-", ascii_name.casefold()).strip("-._")
    base = base or "download"
    canonical_name = unicodedata.normalize("NFC", name).casefold().strip()
    suffix = hashlib.sha256(canonical_name.encode("utf-8")).hexdigest()[:12]
    max_base_length = 128 - len(suffix) - 1
    base = base[:max_base_length].rstrip("-._") or "download"
    return f"{base}-{suffix}"


def _display_filename(value: Any) -> str:
    """Keep a Unicode basename for users while discarding supplied paths."""

    if not isinstance(value, str):
        raise CatalogValidationError(
            "artifact must have a valid filename", field="artifact"
        )
    normalized = unicodedata.normalize("NFC", value)
    basename = re.split(r"[\\/]", normalized)[-1]
    basename = _clean_text(
        basename, field="artifact", required=True, max_length=512
    )
    if basename in {".", ".."}:
        raise CatalogValidationError(
            "artifact must have a valid filename", field="artifact"
        )
    return basename


def _ascii_storage_filename(filename: str) -> str:
    """Create an ASCII-only key component without changing the display name."""

    stem, extension = os.path.splitext(filename)
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    ascii_extension = (
        unicodedata.normalize("NFKD", extension.lstrip("."))
        .encode("ascii", "ignore")
        .decode()
    )
    safe_stem = secure_filename(ascii_stem)[:200] or "artifact"
    safe_extension = secure_filename(ascii_extension)[:32]
    return f"{safe_stem}.{safe_extension}" if safe_extension else safe_stem


def _safe_object_key_segment(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return secure_filename(ascii_text)[:128] or fallback


def build_upload_object_key(
    *,
    prefix: str,
    slug: str,
    version: str,
    architecture: str,
    filename: str,
) -> str:
    """Build a collision-resistant server-owned key; clients never choose it."""

    prefix = str(prefix or "").strip().strip("/")
    if _CONTROL_CHAR_RE.search(prefix) or any(
        part in {".", ".."} for part in prefix.split("/")
    ):
        raise StorageConfigurationError("S3_PREFIX contains an invalid path segment")
    safe_filename = _ascii_storage_filename(filename)
    parts = [
        part
        for part in (
            prefix,
            _safe_object_key_segment(slug, "download"),
            _safe_object_key_segment(version, "unversioned"),
            _safe_object_key_segment(architecture, "unknown"),
            f"{uuid.uuid4().hex}-{safe_filename}",
        )
        if part
    ]
    key = "/".join(parts)
    if len(key.encode("utf-8")) > 1024:
        raise StorageConfigurationError("The generated S3 object key is too long")
    return key


def parse_upload_form(
    form: Mapping[str, Any], artifact: Any, *, uploaded_by: str
) -> dict[str, Any]:
    """Validate admin multipart fields before any storage or database write."""

    mode = _clean_text(form.get("mode"), field="mode", required=True, max_length=16).lower()
    if mode not in {"new", "version"}:
        raise CatalogValidationError("mode must be new or version", field="mode")
    if artifact is None:
        raise CatalogValidationError("artifact is required", field="artifact")

    filename = _display_filename(getattr(artifact, "filename", "") or "")
    supplied_file_type = form.get("file_type") or form.get("file_format")
    file_type = supplied_file_type or os.path.splitext(filename)[1].lstrip(".")
    operating_system = form.get("operating_system") or form.get("platform")
    guessed_content_type = mimetypes.guess_type(filename)[0]
    content_type = guessed_content_type or getattr(artifact, "mimetype", None)
    audit_metadata = {
        "source": "eden-admin-upload",
        "uploaded_by": _clean_text(
            uploaded_by, field="uploaded_by", required=True, max_length=256
        ),
    }
    # member_name groups related products/components under the same catalog
    # item (e.g. "Ultimate" vs "Community" edition) — optional, additive.
    member_name = _clean_text(
        form.get("member_name"), field="member_name", max_length=256
    )
    variant_metadata = dict(audit_metadata)
    if member_name:
        variant_metadata["member_name"] = member_name
    # A harmless placeholder key allows the existing variant validator to
    # validate all client-owned fields before S3 receives any bytes.
    variant = validate_variant_payload(
        {
            "storage_bucket": "",
            "object_key": "pending",
            "file_name": filename,
            "version": form.get("version"),
            "architecture": form.get("architecture"),
            "operating_system": operating_system,
            "file_type": file_type,
            "content_type": content_type,
            "metadata": variant_metadata,
            "is_active": True,
        }
    )

    if mode == "new":
        name = _clean_text(
            form.get("name"), field="name", required=True, max_length=256
        )
        item = validate_item_payload(
            {
                "slug": _slug_for_uploaded_item(name),
                "name": name,
                "description": form.get("description"),
                "category": form.get("category"),
                "publisher": form.get("publisher"),
                "icon_url": None,
                "tags": _form_string_list(form, "tags"),
                "aliases": _form_string_list(form, "aliases"),
                "metadata": audit_metadata,
                "is_active": True,
            }
        )
        return {
            "mode": mode,
            "created_catalog": True,
            "item_id": None,
            "item": item,
            "variant": variant,
        }

    raw_item_id = form.get("catalog_id")
    if isinstance(raw_item_id, bool):
        raise CatalogValidationError(
            "catalog_id must be a positive integer", field="catalog_id"
        )
    try:
        item_id = int(raw_item_id)
    except (TypeError, ValueError) as exc:
        raise CatalogValidationError(
            "catalog_id must be a positive integer", field="catalog_id"
        ) from exc
    if item_id < 1:
        raise CatalogValidationError(
            "catalog_id must be a positive integer", field="catalog_id"
        )
    return {
        "mode": mode,
        "created_catalog": False,
        "item_id": item_id,
        "item": None,
        "variant": variant,
    }


class _HashingLimitedReader:
    """File-like proxy that counts and hashes bytes as boto3 consumes them."""

    def __init__(self, stream: BinaryIO, max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._hash = hashlib.sha256()
        self.size_bytes = 0

    @property
    def sha256(self) -> str:
        return self._hash.hexdigest()

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        if chunk:
            next_size = self.size_bytes + len(chunk)
            if next_size > self._max_bytes:
                raise ArtifactTooLargeError(
                    f"artifact exceeds the {self._max_bytes}-byte upload limit",
                    field="artifact",
                )
            self.size_bytes = next_size
            self._hash.update(chunk)
        return chunk

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        # boto3 may seek before reading to determine content length. Rewinding
        # after bytes were hashed would corrupt the digest, so fail explicitly.
        if self.size_bytes:
            raise OSError("artifact upload stream cannot be rewound after reading")
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        return self._stream.tell()

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        method = getattr(self._stream, "seekable", None)
        return bool(method()) if callable(method) else False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _serialize(value: Any) -> Any:
    """Convert common database values into a deterministic JSON-safe form."""

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value


def _present_item(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe item with compatibility-friendly display labels."""

    item = _serialize(dict(row))
    item.pop("search_rank", None)
    item.pop("sort_name", None)
    item["display_name"] = item.get("name", "")
    item["category_label"] = CATEGORY_LABELS.get(
        item.get("category"), item.get("category", "")
    )
    versions = item.get("versions")
    if isinstance(versions, list):
        versions.sort(key=_natural_version_key, reverse=True)
        item["latest_version"] = versions[0] if versions else None
    variants = item.get("variants")
    if isinstance(variants, list):
        # Stable two-pass ordering keeps the newest natural version first in
        # each architecture, while favoring the architectures users most often
        # need as the modal's initial choice.
        variants.sort(
            key=lambda variant: _natural_version_key(variant.get("version", "")),
            reverse=True,
        )
        architecture_order = {
            "x64": 0,
            "arm64": 1,
            "x86": 2,
            "universal": 3,
            "unknown": 4,
        }
        variants.sort(
            key=lambda variant: (
                str(variant.get("operating_system", "")),
                architecture_order.get(str(variant.get("architecture", "")), 5),
                str(variant.get("architecture", "")),
            )
        )
    return item


def _natural_version_key(value: Any) -> tuple[tuple[int, Any], ...]:
    normalized = re.sub(r"^v(?=\d)", "", str(value).casefold())
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", normalized)
    )


def _parse_positive_int(
    value: Any,
    *,
    field: str,
    default: int,
    maximum: int,
) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CatalogValidationError(f"{field} must be an integer", field=field) from exc
    if parsed < 1 or parsed > maximum:
        raise CatalogValidationError(
            f"{field} must be between 1 and {maximum}", field=field
        )
    return parsed


def _cursor_context(*, q: str, category: str, sort: str, order: str) -> str:
    """Bind a cursor to the query that created it."""

    canonical = json.dumps(
        {"q": q, "category": category, "sort": sort, "order": order},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:20]


def _encode_cursor(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        {"v": 1, **dict(payload)}, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str, *, expected_context: str) -> dict[str, Any]:
    if not value or len(value) > 2048:
        raise CatalogValidationError("cursor is invalid", field="cursor")
    try:
        padded = value + ("=" * (-len(value) % 4))
        raw = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        )
        if len(raw) > 1536:
            raise ValueError("cursor payload is too large")
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise CatalogValidationError("cursor is invalid", field="cursor") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or payload.get("c") != expected_context
    ):
        raise CatalogValidationError(
            "cursor does not belong to this query", field="cursor"
        )
    return payload


def validate_list_query(args: Mapping[str, Any]) -> dict[str, Any]:
    """Validate list/search parameters without interpolating user SQL."""

    query = _clean_text(args.get("q"), field="q", max_length=MAX_SEARCH_LENGTH)
    category = _clean_text(args.get("category"), field="category", max_length=32).lower()
    if category and category not in CATEGORY_IDS:
        raise CatalogValidationError(
            f"category must be one of: {', '.join(sorted(CATEGORY_IDS))}",
            field="category",
        )
    if query and len(query) < MIN_SEARCH_LENGTH:
        raise CatalogValidationError(
            f"q must contain at least {MIN_SEARCH_LENGTH} characters", field="q"
        )
    if not query and not category:
        raise CatalogValidationError(
            "Choose a category or enter at least 2 search characters"
        )

    requested_sort = _clean_text(args.get("sort"), field="sort", max_length=16).lower()
    sort = requested_sort or ("relevance" if query else "name")
    if sort not in {"relevance", "name", "updated"}:
        raise CatalogValidationError(
            "sort must be relevance, name, or updated", field="sort"
        )
    if sort == "relevance" and not query:
        sort = "name"

    requested_order = _clean_text(args.get("order"), field="order", max_length=4).lower()
    order = requested_order or ("desc" if sort in {"relevance", "updated"} else "asc")
    if order not in {"asc", "desc"}:
        raise CatalogValidationError("order must be asc or desc", field="order")

    cursor = _clean_text(args.get("cursor"), field="cursor", max_length=2048)
    page_value = args.get("page")
    page = _parse_positive_int(
        page_value, field="page", default=1, maximum=MAX_PAGE
    )
    if cursor and page_value not in (None, "", 1, "1"):
        raise CatalogValidationError(
            "cursor cannot be combined with page", field="cursor"
        )

    # ``limit`` is canonical. ``page_size`` remains accepted for callers built
    # during the first UI prototype.
    limit_value = args.get("limit")
    if limit_value in (None, ""):
        limit_value = args.get("page_size")
    limit = _parse_positive_int(
        limit_value,
        field="limit",
        default=DEFAULT_PAGE_SIZE,
        maximum=MAX_PAGE_SIZE,
    )

    return {
        "q": query,
        "category": category,
        "cursor": cursor,
        "limit": limit,
        "page": page,
        "sort": sort,
        "order": order,
    }


class CatalogRepository:
    """Catalog queries and importer upserts using Eden's connection pool."""

    def __init__(
        self,
        connection_provider: Callable[[], ContextManager[Any]] | None = None,
    ) -> None:
        self._connections = connection_provider or eden_db.get_conn

    def categories(self) -> list[dict[str, Any]]:
        sql = """
            SELECT i.category,
                   COUNT(DISTINCT i.id)::INTEGER AS item_count,
                   COUNT(v.id)::INTEGER AS variant_count
            FROM download_items i
            JOIN download_variants v
              ON v.item_id = i.id AND v.is_active
            WHERE i.is_active
            GROUP BY i.category
        """
        with self._connections() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                counts = {row["category"]: dict(row) for row in cur.fetchall()}
        # Only categories that actually have something in them — an empty
        # tile is just dead weight in the UI. (The full CATEGORIES list is
        # still used elsewhere, e.g. to validate an item's category on write.)
        populated: list[dict[str, Any]] = []
        for definition in CATEGORIES:
            count = counts.get(definition["id"], {})
            item_count = int(count.get("item_count", 0))
            if item_count < 1:
                continue
            populated.append(
                {
                    **definition,
                    "item_count": item_count,
                    "variant_count": int(count.get("variant_count", 0)),
                }
            )
        return populated

    def search(
        self,
        *,
        q: str = "",
        category: str = "",
        cursor: str = "",
        limit: int | None = None,
        page: int = 1,
        page_size: int | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> dict[str, Any]:
        params = validate_list_query(
            {
                "q": q,
                "category": category,
                "cursor": cursor,
                "limit": limit,
                "page": page,
                "page_size": page_size,
                "sort": sort,
                "order": order,
            }
        )
        q = params["q"]
        category = params["category"]
        cursor = params["cursor"]
        limit = params["limit"]
        page = params["page"]
        sort = params["sort"]
        order = params["order"]

        filter_parts = [
            "i.is_active",
            "EXISTS (SELECT 1 FROM download_variants available "
            "WHERE available.item_id = i.id AND available.is_active)",
        ]
        filter_values: list[Any] = []
        if category:
            filter_parts.append("i.category = %s")
            filter_values.append(category)

        if q:
            search_cte = (
                "WITH search_input AS ("
                "SELECT websearch_to_tsquery('simple', %s) AS tsq, lower(%s) AS needle"
                ")"
            )
            from_suffix = " CROSS JOIN search_input s"
            search_values: list[Any] = [q, q]
            filter_parts.append(
                "(i.search_vector @@ s.tsq "
                "OR strpos(lower(i.name), s.needle) > 0 "
                "OR strpos(lower(i.publisher), s.needle) > 0 "
                "OR strpos(lower(i.description), s.needle) > 0 "
                "OR strpos(lower(array_to_string(i.tags, ' ')), s.needle) > 0 "
                "OR strpos(lower(array_to_string(i.aliases, ' ')), s.needle) > 0 "
                "OR EXISTS ("
                "SELECT 1 FROM download_variants matched_variant "
                "WHERE matched_variant.item_id = i.id "
                "AND matched_variant.is_active "
                "AND (strpos(lower(matched_variant.file_name), s.needle) > 0 "
                "OR strpos(lower(matched_variant.version), s.needle) > 0 "
                "OR strpos(lower(matched_variant.architecture), s.needle) > 0 "
                "OR strpos(lower(matched_variant.operating_system), s.needle) > 0 "
                "OR strpos(lower(COALESCE(matched_variant.metadata->>'member_name', '')), "
                "s.needle) > 0)"
                "))"
            )
            rank_expression = (
                "((CASE WHEN lower(i.name) = s.needle THEN 100.0 "
                "WHEN strpos(lower(i.name), s.needle) = 1 THEN 50.0 "
                "WHEN strpos(lower(i.name), s.needle) > 0 THEN 25.0 ELSE 0.0 END "
                "+ ts_rank_cd(i.search_vector, s.tsq))::double precision)"
            )
        else:
            search_cte = ""
            from_suffix = ""
            search_values = []
            rank_expression = "0.0"

        filter_sql = " AND ".join(filter_parts)
        context = _cursor_context(q=q, category=category, sort=sort, order=order)
        cursor_payload = (
            _decode_cursor(cursor, expected_context=context) if cursor else None
        )

        direction = "ASC" if order == "asc" else "DESC"
        if sort == "relevance":
            order_sql = f"search_rank {direction}, lower(i.name) ASC, i.id ASC"
        elif sort == "updated":
            order_sql = f"i.updated_at {direction}, i.id {direction}"
        else:
            order_sql = f"lower(i.name) {direction}, i.id {direction}"

        page_parts = list(filter_parts)
        page_values = list(filter_values)
        if cursor_payload is not None:
            try:
                last_id = int(cursor_payload["i"])
                if (
                    last_id < 1
                    or last_id > 9_223_372_036_854_775_807
                    or isinstance(cursor_payload["i"], bool)
                ):
                    raise ValueError("invalid id")
                if sort == "relevance":
                    last_rank = float(cursor_payload["r"])
                    last_name = str(cursor_payload["n"])
                    if not math.isfinite(last_rank) or len(last_name) > 256:
                        raise ValueError("invalid relevance key")
                    rank_operator = ">" if order == "asc" else "<"
                    page_parts.append(
                        f"({rank_expression} {rank_operator} %s OR "
                        f"({rank_expression} = %s AND "
                        "(lower(i.name) > %s OR "
                        "(lower(i.name) = %s AND i.id > %s))))"
                    )
                    page_values.extend(
                        [last_rank, last_rank, last_name, last_name, last_id]
                    )
                elif sort == "updated":
                    last_updated = str(cursor_payload["u"])
                    if len(last_updated) > 64:
                        raise ValueError("invalid timestamp key")
                    # Parsing here rejects arbitrary values before PostgreSQL sees them.
                    datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
                    operator = ">" if order == "asc" else "<"
                    page_parts.append(
                        f"(i.updated_at, i.id) {operator} (%s::timestamptz, %s)"
                    )
                    page_values.extend([last_updated, last_id])
                else:
                    last_name = str(cursor_payload["n"])
                    if len(last_name) > 256 or _CONTROL_CHAR_RE.search(last_name):
                        raise ValueError("invalid name key")
                    operator = ">" if order == "asc" else "<"
                    page_parts.append(
                        f"(lower(i.name), i.id) {operator} (%s, %s)"
                    )
                    page_values.extend([last_name, last_id])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise CatalogValidationError("cursor is invalid", field="cursor") from exc

        page_where_sql = " AND ".join(page_parts)
        count_sql = f"""
            {search_cte}
            SELECT COUNT(*)
            FROM download_items i{from_suffix}
            WHERE {filter_sql}
        """
        page_sql = f"""
            {search_cte}
            SELECT i.id, i.slug, i.name, lower(i.name) AS sort_name,
                   i.description, i.category,
                   i.publisher, i.icon_url, i.tags, i.aliases, i.metadata,
                   i.created_at, i.updated_at,
                   COUNT(v.id)::INTEGER AS variant_count,
                   COALESCE(
                       array_agg(DISTINCT v.architecture ORDER BY v.architecture)
                           FILTER (WHERE v.id IS NOT NULL),
                       '{{}}'
                   ) AS architectures,
                   COALESCE(
                       array_agg(DISTINCT v.version ORDER BY v.version DESC)
                           FILTER (WHERE v.id IS NOT NULL AND v.version <> ''),
                       '{{}}'
                   ) AS versions,
                   COALESCE(
                       array_agg(DISTINCT v.file_type ORDER BY v.file_type)
                           FILTER (WHERE v.id IS NOT NULL AND v.file_type <> ''),
                       '{{}}'
                   ) AS file_types,
                   {rank_expression} AS search_rank
            FROM download_items i{from_suffix}
            LEFT JOIN download_variants v
              ON v.item_id = i.id AND v.is_active
            WHERE {page_where_sql}
            GROUP BY i.id{', s.needle, s.tsq' if q else ''}
            ORDER BY {order_sql}
            LIMIT %s OFFSET %s
        """
        offset = 0 if cursor else (page - 1) * limit
        count_values = search_values + filter_values
        result_values = search_values + page_values

        with self._connections() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(count_sql, count_values)
                total_items = int(cur.fetchone()[0])
                cur.execute(page_sql, result_values + [limit + 1, offset])
                rows = [dict(row) for row in cur.fetchall()]
                has_more = len(rows) > limit
                if has_more:
                    rows.pop()

        next_cursor: str | None = None
        if has_more and rows:
            last = rows[-1]
            cursor_data: dict[str, Any] = {"c": context, "i": int(last["id"])}
            if sort == "relevance":
                cursor_data.update(
                    r=float(last["search_rank"]), n=str(last["sort_name"])
                )
            elif sort == "updated":
                updated_at = last["updated_at"]
                cursor_data["u"] = (
                    updated_at.isoformat()
                    if isinstance(updated_at, datetime)
                    else str(updated_at)
                )
            else:
                cursor_data["n"] = str(last["sort_name"])
            next_cursor = _encode_cursor(cursor_data)

        items = [_present_item(row) for row in rows]
        total_pages = math.ceil(total_items / limit) if total_items else 0
        return {
            "items": items,
            "next_cursor": next_cursor,
            "total": total_items,
            # Kept for the initial page/page_size client; new clients only need
            # ``next_cursor`` and ``total``.
            "pagination": {
                "page": page,
                "page_size": limit,
                "total_items": total_items,
                "total_pages": total_pages,
                "has_more": has_more,
            },
            "query": {
                "q": q,
                "category": category or None,
                "limit": limit,
                "sort": sort,
                "order": order,
            },
        }

    def get_item(self, item_id: int) -> dict[str, Any]:
        if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id < 1:
            raise CatalogValidationError("item_id must be a positive integer", field="item_id")
        sql = """
            SELECT i.id, i.slug, i.name, i.description, i.category,
                   i.publisher, i.icon_url, i.tags, i.aliases, i.metadata,
                   i.created_at, i.updated_at,
                   COUNT(v.id)::INTEGER AS variant_count,
                   COALESCE(
                       array_agg(DISTINCT v.architecture ORDER BY v.architecture)
                           FILTER (WHERE v.id IS NOT NULL), '{}'
                   ) AS architectures,
                   COALESCE(
                       array_agg(DISTINCT v.version ORDER BY v.version DESC)
                           FILTER (WHERE v.id IS NOT NULL AND v.version <> ''), '{}'
                   ) AS versions,
                   COALESCE(
                       array_agg(DISTINCT v.file_type ORDER BY v.file_type)
                           FILTER (WHERE v.id IS NOT NULL AND v.file_type <> ''), '{}'
                   ) AS file_types
            FROM download_items i
            LEFT JOIN download_variants v
              ON v.item_id = i.id AND v.is_active
            WHERE i.id = %s AND i.is_active
            GROUP BY i.id
            HAVING COUNT(v.id) > 0
        """
        with self._connections() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (item_id,))
                row = cur.fetchone()
                if not row:
                    raise CatalogNotFoundError("Download item not found")
                rows = [dict(row)]
                self._attach_variants(cur, rows)
        return _present_item(rows[0])

    def get_variant_for_download(self, variant_id: int) -> dict[str, Any]:
        if isinstance(variant_id, bool) or not isinstance(variant_id, int) or variant_id < 1:
            raise CatalogValidationError(
                "variant_id must be a positive integer", field="variant_id"
            )
        sql = """
            SELECT v.id, v.item_id, v.storage_bucket, v.object_key,
                   v.file_name, v.version, v.architecture,
                   v.operating_system, v.file_type, v.size_bytes,
                   v.sha256, v.content_type, i.name AS item_name
            FROM download_variants v
            JOIN download_items i ON i.id = v.item_id
            WHERE v.id = %s AND v.is_active AND i.is_active
        """
        with self._connections() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (variant_id,))
                row = cur.fetchone()
        if not row:
            raise CatalogNotFoundError("Download variant not found")
        return _serialize(dict(row))

    @staticmethod
    def _attach_variants(cur: Any, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        item_ids = [item["id"] for item in items]
        cur.execute(
            """
            SELECT id, item_id, file_name, version, architecture,
                   operating_system, file_type, size_bytes, sha256,
                   last_modified, metadata
            FROM download_variants
            WHERE item_id = ANY(%s) AND is_active
            ORDER BY item_id, operating_system, architecture,
                     version DESC, file_name
            """,
            (item_ids,),
        )
        grouped: dict[int, list[dict[str, Any]]] = {item_id: [] for item_id in item_ids}
        for row in cur.fetchall():
            variant = dict(row)
            item_id = int(variant.pop("item_id"))
            grouped.setdefault(item_id, []).append(variant)
        for item in items:
            item["variants"] = grouped.get(item["id"], [])

    def get_item_identity(self, item_id: int) -> dict[str, Any]:
        """Return the server-owned identity used to place a version upload."""

        if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id < 1:
            raise CatalogValidationError("item_id must be a positive integer", field="item_id")
        with self._connections() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, slug, name, description, category, publisher,
                           tags, aliases, metadata, created_at, updated_at
                    FROM download_items
                    WHERE id = %s AND is_active
                    """,
                    (item_id,),
                )
                row = cur.fetchone()
        if not row:
            raise CatalogNotFoundError("Download item not found")
        return _present_item(dict(row))

    @staticmethod
    def _insert_uploaded_variant(
        cur: Any, item_id: int, variant: Mapping[str, Any]
    ) -> dict[str, Any]:
        cur.execute(
            """
            INSERT INTO download_variants
                (item_id, storage_bucket, object_key, file_name, version,
                 architecture, operating_system, file_type, size_bytes,
                 sha256, etag, content_type, last_modified, metadata, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, item_id, storage_bucket, object_key, file_name,
                      version, architecture, operating_system, file_type,
                      size_bytes, sha256, etag, content_type, last_modified,
                      metadata, is_active, created_at, updated_at
            """,
            (
                item_id,
                variant["storage_bucket"],
                variant["object_key"],
                variant["file_name"],
                variant["version"],
                variant["architecture"],
                variant["operating_system"],
                variant["file_type"],
                variant["size_bytes"],
                variant["sha256"],
                variant["etag"],
                variant["content_type"],
                variant["last_modified"],
                psycopg2.extras.Json(variant["metadata"]),
                variant["is_active"],
            ),
        )
        return _serialize(dict(cur.fetchone()))

    def create_uploaded_item(
        self, item_payload: Mapping[str, Any], variant_payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Atomically create a new logical app and its first artifact variant."""

        item = validate_item_payload(item_payload)
        variant = validate_variant_payload(variant_payload)
        try:
            with self._connections() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # A transaction-scoped name lock makes the check-and-insert
                    # atomic even though imported catalogs may legitimately
                    # contain historical duplicate names and cannot safely gain
                    # a global unique index during an in-place upgrade.
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(lower(%s)))",
                        (item["name"],),
                    )
                    cur.execute(
                        """
                        SELECT id
                        FROM download_items
                        WHERE is_active AND lower(name) = lower(%s)
                        LIMIT 1
                        """,
                        (item["name"],),
                    )
                    if cur.fetchone():
                        raise CatalogConflictError(
                            "An app with this name already exists; use Add version instead"
                        )
                    cur.execute(
                        """
                        INSERT INTO download_items
                            (slug, name, description, category, publisher, icon_url,
                             tags, aliases, metadata, is_active)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id, slug, name, description, category, publisher,
                                  icon_url, tags, aliases, metadata, is_active,
                                  created_at, updated_at
                        """,
                        (
                            item["slug"],
                            item["name"],
                            item["description"],
                            item["category"],
                            item["publisher"],
                            item["icon_url"],
                            item["tags"],
                            item["aliases"],
                            psycopg2.extras.Json(item["metadata"]),
                            item["is_active"],
                        ),
                    )
                    item_row = dict(cur.fetchone())
                    variant_row = self._insert_uploaded_variant(
                        cur, int(item_row["id"]), variant
                    )
        except psycopg2.errors.UniqueViolation as exc:
            constraint = getattr(getattr(exc, "diag", None), "constraint_name", "")
            if constraint == "download_items_slug_key":
                raise CatalogConflictError(
                    "A different app already uses this catalog identifier; "
                    "choose a more distinct app name"
                ) from exc
            raise

        item_row["variants"] = [variant_row]
        item_row["variant_count"] = 1
        item_row["versions"] = [variant_row["version"]] if variant_row["version"] else []
        item_row["architectures"] = [variant_row["architecture"]]
        item_row["file_types"] = [variant_row["file_type"]] if variant_row["file_type"] else []
        return _present_item(item_row), variant_row

    def add_uploaded_variant(
        self, item_id: int, variant_payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Attach an uploaded artifact to an active item in one DB transaction."""

        if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id < 1:
            raise CatalogValidationError("item_id must be a positive integer", field="item_id")
        variant = validate_variant_payload(variant_payload)
        with self._connections() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, slug, name, description, category, publisher,
                           tags, aliases, metadata, created_at, updated_at
                    FROM download_items
                    WHERE id = %s AND is_active
                    FOR SHARE
                    """,
                    (item_id,),
                )
                item_row = cur.fetchone()
                if not item_row:
                    raise CatalogNotFoundError("Download item not found")
                variant_row = self._insert_uploaded_variant(cur, item_id, variant)
        return _present_item(dict(item_row)), variant_row

    def upsert_item(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        item = validate_item_payload(payload)
        sql = """
            INSERT INTO download_items
                (slug, name, description, category, publisher, icon_url,
                 tags, aliases, metadata, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (slug) DO UPDATE SET
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                category = EXCLUDED.category,
                publisher = EXCLUDED.publisher,
                icon_url = EXCLUDED.icon_url,
                tags = EXCLUDED.tags,
                aliases = EXCLUDED.aliases,
                metadata = EXCLUDED.metadata,
                is_active = EXCLUDED.is_active
            RETURNING id, slug, name, category, is_active, created_at, updated_at
        """
        values = (
            item["slug"],
            item["name"],
            item["description"],
            item["category"],
            item["publisher"],
            item["icon_url"],
            item["tags"],
            item["aliases"],
            psycopg2.extras.Json(item["metadata"]),
            item["is_active"],
        )
        with self._connections() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, values)
                row = cur.fetchone()
        return _serialize(dict(row))

    def upsert_variant(
        self, item_id: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id < 1:
            raise CatalogValidationError("item_id must be a positive integer", field="item_id")
        variant = validate_variant_payload(payload)
        sql = """
            INSERT INTO download_variants
                (item_id, storage_bucket, object_key, file_name, version,
                 architecture, operating_system, file_type, size_bytes,
                 sha256, etag, content_type, last_modified, metadata, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (storage_bucket, object_key) DO UPDATE SET
                item_id = EXCLUDED.item_id,
                file_name = EXCLUDED.file_name,
                version = EXCLUDED.version,
                architecture = EXCLUDED.architecture,
                operating_system = EXCLUDED.operating_system,
                file_type = EXCLUDED.file_type,
                size_bytes = EXCLUDED.size_bytes,
                sha256 = EXCLUDED.sha256,
                etag = EXCLUDED.etag,
                content_type = EXCLUDED.content_type,
                last_modified = EXCLUDED.last_modified,
                metadata = EXCLUDED.metadata,
                is_active = EXCLUDED.is_active
            RETURNING id, item_id, storage_bucket, object_key, file_name,
                      version, architecture, operating_system, file_type,
                      size_bytes, sha256, is_active, created_at, updated_at
        """
        values = (
            item_id,
            variant["storage_bucket"],
            variant["object_key"],
            variant["file_name"],
            variant["version"],
            variant["architecture"],
            variant["operating_system"],
            variant["file_type"],
            variant["size_bytes"],
            variant["sha256"],
            variant["etag"],
            variant["content_type"],
            variant["last_modified"],
            psycopg2.extras.Json(variant["metadata"]),
            variant["is_active"],
        )
        with self._connections() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, values)
                row = cur.fetchone()
        return _serialize(dict(row))


def _config_value(name: str, default: str = "") -> str:
    configured = getattr(eden_config, name, None)
    if configured is None:
        configured = os.environ.get(name, default)
    return str(configured).strip() if configured is not None else default


def _parse_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise StorageConfigurationError(f"{name} must be true or false")


class S3DownloadSigner:
    """Lazily constructs a boto3 client and signs server-selected object keys."""

    def __init__(
        self,
        *,
        bucket: str | None = None,
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        addressing_style: str | None = None,
        verify: bool | str | None = None,
        expires_in: int | None = None,
        client: Any | None = None,
    ) -> None:
        self.bucket = (bucket if bucket is not None else _config_value("S3_BUCKET")).strip()
        self.endpoint_url = (
            endpoint_url
            if endpoint_url is not None
            else _config_value("S3_ENDPOINT_URL")
        ).strip() or None
        if self.endpoint_url:
            parsed_endpoint = urlparse(self.endpoint_url)
            if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.netloc:
                raise StorageConfigurationError(
                    "S3_ENDPOINT_URL must be a complete http(s) URL"
                )
        self.region = (
            region if region is not None else _config_value("S3_REGION", "us-east-1")
        ).strip() or "us-east-1"
        self.access_key_id = (
            access_key_id
            if access_key_id is not None
            else _config_value("S3_ACCESS_KEY_ID")
        ).strip() or None
        self.secret_access_key = (
            secret_access_key
            if secret_access_key is not None
            else _config_value("S3_SECRET_ACCESS_KEY")
        ).strip() or None
        if bool(self.access_key_id) != bool(self.secret_access_key):
            raise StorageConfigurationError(
                "S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must be set together"
            )
        self.addressing_style = (
            addressing_style
            if addressing_style is not None
            else _config_value("S3_ADDRESSING_STYLE", "path")
        ).strip().lower()
        if self.addressing_style not in {"auto", "path", "virtual"}:
            raise StorageConfigurationError(
                "S3_ADDRESSING_STYLE must be auto, path, or virtual"
            )

        if verify is None:
            ca_bundle = _config_value("S3_CA_BUNDLE")
            verify = ca_bundle or _parse_bool(
                _config_value("S3_VERIFY_TLS", "true"), name="S3_VERIFY_TLS"
            )
        self.verify = verify

        if expires_in is None:
            raw_ttl = _config_value("S3_PRESIGN_TTL", str(DEFAULT_PRESIGN_TTL))
            try:
                expires_in = int(raw_ttl)
            except ValueError as exc:
                raise StorageConfigurationError("S3_PRESIGN_TTL must be an integer") from exc
        if not MIN_PRESIGN_TTL <= expires_in <= MAX_PRESIGN_TTL:
            raise StorageConfigurationError(
                f"S3_PRESIGN_TTL must be between {MIN_PRESIGN_TTL} and {MAX_PRESIGN_TTL}"
            )
        self.expires_in = expires_in
        self._client = client
        self._client_lock = threading.Lock()

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            try:
                import boto3
                from botocore.config import Config as BotoConfig
            except ImportError as exc:
                raise StorageConfigurationError(
                    "boto3 is required for S3 downloads; add it to requirements.txt"
                ) from exc

            kwargs: dict[str, Any] = {
                "service_name": "s3",
                "region_name": self.region,
                "verify": self.verify,
                "config": BotoConfig(
                    signature_version="s3v4",
                    s3={"addressing_style": self.addressing_style},
                    retries={"max_attempts": 3, "mode": "standard"},
                    connect_timeout=3,
                    read_timeout=10,
                ),
            }
            if self.endpoint_url:
                kwargs["endpoint_url"] = self.endpoint_url
            if self.access_key_id:
                kwargs["aws_access_key_id"] = self.access_key_id
            if self.secret_access_key:
                kwargs["aws_secret_access_key"] = self.secret_access_key
            self._client = boto3.client(**kwargs)
            return self._client

    def presign(self, variant: Mapping[str, Any]) -> dict[str, Any]:
        object_key = _clean_text(
            variant.get("object_key"),
            field="object_key",
            required=True,
            max_length=1024,
        )
        variant_bucket = _clean_text(
            variant.get("storage_bucket"), field="storage_bucket", max_length=255
        )
        bucket = variant_bucket or self.bucket
        if not bucket:
            raise StorageConfigurationError("S3_BUCKET is not configured")
        if not _BUCKET_RE.fullmatch(bucket):
            raise StorageConfigurationError("The configured S3 bucket name is invalid")

        original_name = _clean_text(
            variant.get("file_name"), field="file_name", required=True, max_length=512
        )
        ascii_name = secure_filename(
            unicodedata.normalize("NFKD", original_name).encode("ascii", "ignore").decode()
        ) or "download"
        disposition = (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(original_name, safe='')}"
        )
        object_params: dict[str, Any] = {
            "Bucket": bucket,
            "Key": object_key,
            "ResponseContentDisposition": disposition,
        }
        content_type = variant.get("content_type")
        if content_type:
            object_params["ResponseContentType"] = _clean_text(
                content_type, field="content_type", max_length=256
            )

        url = self._get_client().generate_presigned_url(
            ClientMethod="get_object",
            Params=object_params,
            ExpiresIn=self.expires_in,
            HttpMethod="GET",
        )
        return {
            "url": url,
            "filename": original_name,
            "expires_in": self.expires_in,
        }


class S3ArtifactStore(S3DownloadSigner):
    """Streams admin uploads to the same private S3 storage used for downloads."""

    def __init__(self, *, max_bytes: int | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.max_bytes = max_bytes if max_bytes is not None else DOWNLOAD_UPLOAD_MAX_BYTES
        if isinstance(self.max_bytes, bool) or not isinstance(self.max_bytes, int):
            raise StorageConfigurationError("DOWNLOAD_UPLOAD_MAX_BYTES must be an integer")
        if self.max_bytes < 1:
            raise StorageConfigurationError("DOWNLOAD_UPLOAD_MAX_BYTES must be positive")

    def _validated_bucket(self) -> str:
        if not self.bucket:
            raise StorageConfigurationError("S3_BUCKET is not configured")
        if not _BUCKET_RE.fullmatch(self.bucket):
            raise StorageConfigurationError("The configured S3 bucket name is invalid")
        return self.bucket

    def delete(self, bucket: str, object_key: str) -> None:
        bucket = _clean_text(
            bucket, field="storage_bucket", required=True, max_length=255
        )
        if not _BUCKET_RE.fullmatch(bucket):
            raise StorageConfigurationError("The S3 bucket name is invalid")
        object_key = _clean_text(
            object_key, field="object_key", required=True, max_length=1024
        )
        self._get_client().delete_object(Bucket=bucket, Key=object_key)

    def upload(
        self,
        stream: BinaryIO,
        *,
        slug: str,
        filename: str,
        version: str,
        architecture: str,
        content_type: str | None,
    ) -> dict[str, Any]:
        bucket = self._validated_bucket()
        object_key = build_upload_object_key(
            prefix=_config_value("S3_PREFIX", "downloads"),
            slug=slug,
            version=version,
            architecture=architecture,
            filename=filename,
        )
        reader = _HashingLimitedReader(stream, self.max_bytes)
        extra_args: dict[str, Any] = {"Metadata": {"uploaded-via": "eden"}}
        if content_type:
            extra_args["ContentType"] = _clean_text(
                content_type, field="content_type", max_length=256
            )

        try:
            from boto3.s3.transfer import TransferConfig
        except ImportError as exc:
            raise StorageConfigurationError(
                "boto3 is required for S3 uploads; add it to requirements.txt"
            ) from exc

        try:
            self._get_client().upload_fileobj(
                reader,
                bucket,
                object_key,
                ExtraArgs=extra_args,
                Config=TransferConfig(use_threads=False),
            )
            if reader.size_bytes == 0:
                raise EmptyArtifactError("artifact cannot be empty", field="artifact")
        except Exception:
            # Keys contain a random UUID and are never supplied by the client,
            # so best-effort cleanup cannot remove a pre-existing catalog file.
            try:
                self.delete(bucket, object_key)
            except Exception:
                logger.exception(
                    "Could not clean up failed S3 upload: s3://%s/%s",
                    bucket,
                    object_key,
                )
            raise

        etag: str | None = None
        last_modified: datetime | None = datetime.now(timezone.utc)
        try:
            head = self._get_client().head_object(Bucket=bucket, Key=object_key)
            etag = str(head.get("ETag") or "").strip('"') or None
            last_modified = head.get("LastModified") or last_modified
        except Exception:
            # The upload is already durable and the database has the stronger
            # SHA-256 digest and exact streamed byte count; ETag is optional.
            logger.warning(
                "Uploaded s3://%s/%s but could not read optional object headers",
                bucket,
                object_key,
                exc_info=True,
            )

        return {
            "storage_bucket": bucket,
            "object_key": object_key,
            "size_bytes": reader.size_bytes,
            "sha256": reader.sha256,
            "etag": etag,
            "last_modified": last_modified,
        }


def upload_catalog_artifact(
    *,
    repository: CatalogRepository,
    storage: S3ArtifactStore,
    form: Mapping[str, Any],
    artifact: Any,
    uploaded_by: str,
) -> dict[str, Any]:
    """Upload one artifact and compensate S3 if its atomic DB write fails."""

    parsed = parse_upload_form(form, artifact, uploaded_by=uploaded_by)
    if parsed["mode"] == "version":
        try:
            existing_item = repository.get_item_identity(parsed["item_id"])
        except (CatalogNotFoundError, CatalogValidationError):
            raise
        except Exception as exc:
            raise CatalogPersistenceError(
                "Could not verify the selected catalog app"
            ) from exc
        slug = existing_item["slug"]
    else:
        existing_item = None
        slug = parsed["item"]["slug"]

    try:
        stored = storage.upload(
            artifact.stream,
            slug=slug,
            filename=parsed["variant"]["file_name"],
            version=parsed["variant"]["version"],
            architecture=parsed["variant"]["architecture"],
            content_type=parsed["variant"]["content_type"],
        )
    except (
        ArtifactTooLargeError,
        EmptyArtifactError,
        CatalogValidationError,
        StorageConfigurationError,
    ):
        raise
    except Exception as exc:
        raise StorageUploadError("Artifact upload failed") from exc

    variant = {**parsed["variant"], **stored}
    try:
        if parsed["mode"] == "new":
            item, saved_variant = repository.create_uploaded_item(
                parsed["item"], variant
            )
        else:
            item, saved_variant = repository.add_uploaded_variant(
                parsed["item_id"], variant
            )
    except (CatalogConflictError, CatalogNotFoundError, CatalogValidationError):
        try:
            storage.delete(stored["storage_bucket"], stored["object_key"])
        except Exception:
            logger.exception(
                "Database rejected upload and S3 compensation failed: s3://%s/%s",
                stored["storage_bucket"],
                stored["object_key"],
            )
        raise
    except Exception as exc:
        try:
            storage.delete(stored["storage_bucket"], stored["object_key"])
        except Exception:
            logger.exception(
                "Database failed and S3 compensation failed: s3://%s/%s",
                stored["storage_bucket"],
                stored["object_key"],
            )
        raise CatalogPersistenceError("Could not save artifact metadata") from exc

    return {
        "item": item,
        "variant": saved_variant,
        "created_catalog": parsed["created_catalog"],
    }


def _error_response(message: str, status: int, code: str, field: str | None = None):
    body: dict[str, Any] = {"error": message, "code": code}
    if field:
        body["field"] = field
    return jsonify(body), status


def create_download_blueprint(
    *,
    repository: CatalogRepository | None = None,
    signer: S3DownloadSigner | None = None,
    uploader: S3ArtifactStore | None = None,
    auth_module: Any | None = None,
    url_prefix: str = "/api/downloads",
) -> Blueprint:
    """Create the API blueprint; injectable dependencies make routes testable."""

    repo = repository or CatalogRepository()
    download_signer = signer or S3DownloadSigner()
    artifact_store = uploader or S3ArtifactStore()
    auth_service = auth_module or eden_auth
    blueprint = Blueprint("download_catalog", __name__, url_prefix=url_prefix)

    @blueprint.record_once
    def configure_upload_request_limit(state: Any) -> None:
        # Flask enforces this while consuming request streams, including when a
        # proxy forwards a chunked body without Content-Length. Respect an
        # operator-supplied stricter application-wide limit.
        if state.app.config.get("MAX_CONTENT_LENGTH") is None:
            state.app.config["MAX_CONTENT_LENGTH"] = UPLOAD_REQUEST_MAX_BYTES

    @blueprint.errorhandler(RequestEntityTooLarge)
    def upload_request_too_large(_exc: RequestEntityTooLarge):
        return _error_response(
            "artifact and multipart fields exceed the configured upload limit",
            413,
            "artifact_too_large",
            "artifact",
        )

    @blueprint.before_request
    def require_authenticated_user():
        if not auth_service.is_authenticated():
            return _error_response(
                "Authentication required", 401, "authentication_required"
            )
        auth_service.ensure_user_exists()
        if (
            request.endpoint == f"{blueprint.name}.upload_artifact"
            and request.content_length is not None
            and request.content_length > UPLOAD_REQUEST_MAX_BYTES
        ):
            return _error_response(
                "artifact and multipart fields exceed the configured upload limit",
                413,
                "artifact_too_large",
                "artifact",
            )
        return None

    @blueprint.get("")
    @blueprint.get("/")
    def list_downloads():
        try:
            query = validate_list_query(request.args)
            result = repo.search(**query)
            response = jsonify(result)
            response.headers["Cache-Control"] = "private, max-age=30"
            return response
        except CatalogValidationError as exc:
            return _error_response(str(exc), 400, "invalid_request", exc.field)
        except Exception:
            current_app.logger.exception("Download catalog query failed")
            return _error_response(
                "Download catalog is temporarily unavailable",
                503,
                "catalog_unavailable",
            )

    @blueprint.get("/categories")
    def list_categories():
        try:
            response = jsonify({"categories": repo.categories()})
            response.headers["Cache-Control"] = "private, max-age=60"
            return response
        except Exception:
            current_app.logger.exception("Download category query failed")
            return _error_response(
                "Download catalog is temporarily unavailable",
                503,
                "catalog_unavailable",
            )

    @blueprint.post("/upload")
    def upload_artifact():
        # Check the role before accessing request.files so an unauthorized
        # request is rejected before Flask parses or spools a large body.
        if not auth_service.is_admin():
            return _error_response("Admin access required", 403, "admin_required")
        try:
            result = upload_catalog_artifact(
                repository=repo,
                storage=artifact_store,
                form=request.form,
                artifact=request.files.get("artifact"),
                uploaded_by=auth_service.get_current_username(),
            )
            current_app.logger.info(
                "Download artifact uploaded: user=%s item_id=%s variant_id=%s new_item=%s",
                auth_service.get_current_username(),
                result["item"]["id"],
                result["variant"]["id"],
                result["created_catalog"],
            )
            response = jsonify(result)
            response.headers["Cache-Control"] = "no-store"
            return response, 201
        except ArtifactTooLargeError as exc:
            return _error_response(str(exc), 413, "artifact_too_large", exc.field)
        except EmptyArtifactError as exc:
            return _error_response(str(exc), 400, "empty_artifact", exc.field)
        except CatalogConflictError as exc:
            return _error_response(str(exc), 409, "catalog_conflict")
        except CatalogNotFoundError as exc:
            return _error_response(str(exc), 404, "not_found")
        except CatalogValidationError as exc:
            return _error_response(str(exc), 400, "invalid_request", exc.field)
        except StorageConfigurationError as exc:
            current_app.logger.error("S3 upload storage is not configured: %s", exc)
            return _error_response(
                "Download storage is not configured", 503, "storage_unavailable"
            )
        except StorageUploadError:
            current_app.logger.exception("S3 artifact upload failed")
            return _error_response(
                "Could not upload this artifact", 502, "upload_failed"
            )
        except CatalogPersistenceError:
            current_app.logger.exception("Download upload metadata write failed")
            return _error_response(
                "Could not save artifact metadata", 503, "catalog_unavailable"
            )
        except Exception:
            current_app.logger.exception("Unexpected download artifact upload failure")
            return _error_response(
                "Could not upload this artifact", 500, "upload_failed"
            )

    @blueprint.get("/<int:item_id>")
    def download_details(item_id: int):
        try:
            response = jsonify({"item": repo.get_item(item_id)})
            response.headers["Cache-Control"] = "private, max-age=30"
            return response
        except CatalogNotFoundError as exc:
            return _error_response(str(exc), 404, "not_found")
        except CatalogValidationError as exc:
            return _error_response(str(exc), 400, "invalid_request", exc.field)
        except Exception:
            current_app.logger.exception("Download item query failed: id=%s", item_id)
            return _error_response(
                "Download catalog is temporarily unavailable",
                503,
                "catalog_unavailable",
            )

    @blueprint.post("/variants/<int:variant_id>/url")
    @blueprint.post("/variants/<int:variant_id>/download")
    def create_download_url(variant_id: int):
        try:
            variant = repo.get_variant_for_download(variant_id)
            signed = download_signer.presign(variant)
            current_app.logger.info(
                "Presigned download created: user=%s item_id=%s variant_id=%s",
                auth_service.get_current_username(),
                variant["item_id"],
                variant_id,
            )
            response = jsonify(
                {
                    "url": signed["url"],
                    "expires_in": signed["expires_in"],
                }
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        except CatalogNotFoundError as exc:
            return _error_response(str(exc), 404, "not_found")
        except CatalogValidationError as exc:
            return _error_response(str(exc), 400, "invalid_request", exc.field)
        except StorageConfigurationError as exc:
            current_app.logger.error("S3 signer is not configured: %s", exc)
            return _error_response(
                "Download storage is not configured", 503, "storage_unavailable"
            )
        except Exception:
            current_app.logger.exception(
                "Failed to create presigned download URL: variant_id=%s", variant_id
            )
            return _error_response(
                "Could not prepare this download", 502, "download_unavailable"
            )

    return blueprint


def register_download_routes(
    app: Flask,
    *,
    repository: CatalogRepository | None = None,
    signer: S3DownloadSigner | None = None,
    uploader: S3ArtifactStore | None = None,
    auth_module: Any | None = None,
    url_prefix: str = "/api/downloads",
) -> Blueprint:
    """Register all download routes on an existing Flask application."""

    blueprint = create_download_blueprint(
        repository=repository,
        signer=signer,
        uploader=uploader,
        auth_module=auth_module,
        url_prefix=url_prefix,
    )
    app.register_blueprint(blueprint)
    return blueprint


__all__ = [
    "CATEGORIES",
    "ArtifactTooLargeError",
    "CatalogConflictError",
    "CatalogError",
    "CatalogNotFoundError",
    "CatalogRepository",
    "CatalogValidationError",
    "CatalogPersistenceError",
    "EmptyArtifactError",
    "S3ArtifactStore",
    "S3DownloadSigner",
    "StorageConfigurationError",
    "StorageUploadError",
    "create_download_blueprint",
    "build_upload_object_key",
    "init_schema",
    "normalize_architecture",
    "parse_upload_form",
    "register_download_routes",
    "validate_item_payload",
    "validate_list_query",
    "validate_variant_payload",
    "upload_catalog_artifact",
]
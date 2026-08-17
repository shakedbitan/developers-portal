#!/usr/bin/env python3
"""Safely reconcile Eden's existing download catalog with organized metadata.

The script never uploads, deletes, renames, or copies an object in S3.  It only
changes catalog rows in PostgreSQL. Recognizable variants are moved beneath
canonical product rows; entries listed in ``suppressed_variants`` are
soft-hidden with ``is_active=false``. In particular, ``download_variants.id``,
``storage_bucket`` and ``object_key`` are never written, and every change can
be restored through the guarded rollback command.

Examples::

    # Plan only (the default).  No database rows are changed.
    python tools/reconcile_download_catalog.py \
        --metadata data/metadata-organized.json \
        --report reconcile-plan.json

    # Apply one transaction after creating an external JSON backup.
    python tools/reconcile_download_catalog.py \
        --metadata data/metadata-organized.json \
        --apply --backup catalog-before.json --report reconcile-result.json

    # Preview and then apply a rollback by audit run id.
    python tools/reconcile_download_catalog.py --rollback RUN_ID
    python tools/reconcile_download_catalog.py --rollback RUN_ID --apply \
        --backup catalog-before-rollback.json

Database settings are read from DB_HOST, DB_PORT, DB_NAME, DB_USER and
DB_PASSWORD.  DB_SCHEMA defaults to ``public``.  ``--apply`` is deliberately
required for every database-changing operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_METADATA = PROJECT_ROOT / "data" / "metadata-organized.json"
ALLOWED_CATEGORIES = {
    "development",
    "infrastructure",
    "security",
    "data",
    "productivity",
    "utilities",
    "drivers",
    "other",
}
SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
SCHEMA_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
ADVISORY_LOCK_KEYS = (1162103111, 328419)
AUDIT_TABLE = "download_catalog_reconciliation_audit"

ITEM_COLUMNS = (
    "id",
    "slug",
    "name",
    "description",
    "category",
    "publisher",
    "icon_url",
    "tags",
    "aliases",
    "metadata",
    "is_active",
    "created_at",
    "updated_at",
)
VARIANT_COLUMNS = (
    "id",
    "item_id",
    "storage_bucket",
    "object_key",
    "file_name",
    "version",
    "architecture",
    "operating_system",
    "file_type",
    "size_bytes",
    "sha256",
    "etag",
    "content_type",
    "last_modified",
    "metadata",
    "is_active",
    "created_at",
    "updated_at",
)
ITEM_MUTABLE_COLUMNS = (
    "slug",
    "name",
    "description",
    "category",
    "publisher",
    "icon_url",
    "tags",
    "aliases",
    "metadata",
    "is_active",
)
VARIANT_MUTABLE_COLUMNS = (
    "item_id",
    "version",
    "architecture",
    "operating_system",
    "metadata",
    "is_active",
)
PROVENANCE_FIELDS = (
    "source_catalog_key",
    "source_name",
    "member_name",
    "member_key",
)
OPTIONAL_MEMBER_FIELDS = (
    "locale",
    "release",
    "edition",
    "raw_version",
    "raw_architecture",
)


class ReconciliationError(Exception):
    """A safe-to-report validation, matching, or conflict error."""

    def __init__(self, message: str, *, errors: Optional[List[dict]] = None) -> None:
        super().__init__(message)
        self.errors = errors or [{"code": "reconciliation_error", "message": message}]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_safe(value: Any) -> Any:
    """Convert DB values to deterministic JSON-compatible values."""
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def stable_json(value: Any) -> str:
    return json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _string(value: Any, field: str, errors: List[dict], *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        errors.append({"code": "invalid_field", "field": field, "message": "must be a string"})
        return ""
    result = value.strip()
    if not result and not allow_empty:
        errors.append({"code": "missing_field", "field": field, "message": "must not be empty"})
    return result


def _string_list(value: Any, field: str, errors: List[dict]) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append({"code": "invalid_field", "field": field, "message": "must be an array"})
        return []
    result: List[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(
                {"code": "invalid_field", "field": f"{field}[{index}]", "message": "must be a string"}
            )
            continue
        cleaned = item.strip()
        if cleaned:
            result.append(cleaned)
    return merge_strings(result)


def merge_strings(*groups: Iterable[str]) -> List[str]:
    """Case-insensitive union with stable, deterministic spelling and order."""
    chosen: Dict[str, str] = {}
    for group in groups:
        for raw in group or []:
            if not isinstance(raw, str):
                continue
            value = raw.strip()
            if not value:
                continue
            key = value.casefold()
            previous = chosen.get(key)
            if previous is None or (value.casefold(), value) < (previous.casefold(), previous):
                chosen[key] = value
    return sorted(chosen.values(), key=lambda item: (item.casefold(), item))


def read_and_normalize_catalog(path: Path) -> Tuple[dict, str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconciliationError(f"Could not parse metadata JSON: {exc}") from exc
    return normalize_catalog(payload), digest


def normalize_catalog(payload: Any) -> dict:
    """Validate organized schema-v1 metadata and return a compact normalized form."""
    errors: List[dict] = []
    if not isinstance(payload, dict):
        raise ReconciliationError("Metadata root must be an object")
    try:
        schema_version = int(payload.get("schema_version", 0))
    except (TypeError, ValueError):
        schema_version = 0
    if schema_version != 1:
        errors.append(
            {
                "code": "unsupported_schema",
                "field": "schema_version",
                "message": "organized metadata must use schema_version 1",
            }
        )
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        errors.append({"code": "invalid_field", "field": "items", "message": "must be an array"})
        raw_items = []

    items: List[dict] = []
    seen_slugs: Set[str] = set()
    for item_index, raw_item in enumerate(raw_items):
        prefix = f"items[{item_index}]"
        if not isinstance(raw_item, dict):
            errors.append({"code": "invalid_field", "field": prefix, "message": "must be an object"})
            continue
        slug = _string(raw_item.get("catalog_key"), f"{prefix}.catalog_key", errors).lower()
        if slug and not SLUG_PATTERN.fullmatch(slug):
            errors.append(
                {
                    "code": "invalid_slug",
                    "field": f"{prefix}.catalog_key",
                    "message": "must contain only lowercase letters, numbers, and internal hyphens",
                    "value": slug,
                }
            )
        if slug in seen_slugs:
            errors.append(
                {"code": "duplicate_catalog_key", "field": f"{prefix}.catalog_key", "value": slug}
            )
        seen_slugs.add(slug)
        display_name = _string(raw_item.get("display_name"), f"{prefix}.display_name", errors)
        category = _string(raw_item.get("category"), f"{prefix}.category", errors).lower()
        if category and category not in ALLOWED_CATEGORIES:
            errors.append(
                {
                    "code": "invalid_category",
                    "field": f"{prefix}.category",
                    "value": category,
                    "allowed": sorted(ALLOWED_CATEGORIES),
                }
            )
        raw_variants = raw_item.get("variants")
        if not isinstance(raw_variants, list) or not raw_variants:
            errors.append(
                {"code": "invalid_field", "field": f"{prefix}.variants", "message": "must be a non-empty array"}
            )
            raw_variants = []

        variants: List[dict] = []
        for variant_index, raw_variant in enumerate(raw_variants):
            variant_prefix = f"{prefix}.variants[{variant_index}]"
            if not isinstance(raw_variant, dict):
                errors.append(
                    {"code": "invalid_field", "field": variant_prefix, "message": "must be an object"}
                )
                continue
            variant = {
                "target_slug": slug,
                "file_name": _string(raw_variant.get("file_name"), f"{variant_prefix}.file_name", errors),
                "source_catalog_key": _string(
                    raw_variant.get("source_catalog_key"),
                    f"{variant_prefix}.source_catalog_key",
                    errors,
                ).lower(),
                "source_name": _string(
                    raw_variant.get("source_name"), f"{variant_prefix}.source_name", errors
                ),
                "member_name": _string(
                    raw_variant.get("member_name", ""),
                    f"{variant_prefix}.member_name",
                    errors,
                    allow_empty=True,
                ),
                "member_key": _string(
                    raw_variant.get("member_key", ""),
                    f"{variant_prefix}.member_key",
                    errors,
                    allow_empty=True,
                ),
                "version": _string(
                    raw_variant.get("version", ""), f"{variant_prefix}.version", errors, allow_empty=True
                ),
                "architecture": _string(
                    raw_variant.get("architecture", "unknown"),
                    f"{variant_prefix}.architecture",
                    errors,
                    allow_empty=True,
                )
                or "unknown",
                "operating_system": _string(
                    raw_variant.get("operating_system", ""),
                    f"{variant_prefix}.operating_system",
                    errors,
                    allow_empty=True,
                ),
            }
            for field in OPTIONAL_MEMBER_FIELDS:
                if field in raw_variant:
                    variant[field] = _string(
                        raw_variant.get(field, ""), f"{variant_prefix}.{field}", errors, allow_empty=True
                    )
            # These values are only used by an explicitly enabled unique fallback.
            for field in ("source_relative_path", "source_path", "sha256"):
                if field in raw_variant and raw_variant.get(field) is not None:
                    variant[field] = _string(
                        raw_variant.get(field), f"{variant_prefix}.{field}", errors, allow_empty=True
                    )
            variants.append(variant)

        metadata = raw_item.get("metadata") or {}
        if not isinstance(metadata, dict):
            errors.append({"code": "invalid_field", "field": f"{prefix}.metadata", "message": "must be an object"})
            metadata = {}
        items.append(
            {
                "catalog_key": slug,
                "display_name": display_name,
                "description": _string(
                    raw_item.get("description", ""), f"{prefix}.description", errors, allow_empty=True
                ),
                "category": category,
                "publisher": _string(
                    raw_item.get("publisher", ""), f"{prefix}.publisher", errors, allow_empty=True
                ),
                "icon_url": raw_item.get("icon_url") if isinstance(raw_item.get("icon_url"), str) else None,
                "tags": _string_list(raw_item.get("tags", []), f"{prefix}.tags", errors),
                "source_names": _string_list(
                    raw_item.get("source_names", []), f"{prefix}.source_names", errors
                ),
                "metadata": json_safe(metadata),
                "variants": variants,
            }
        )

    visible_identities = {
        (variant["source_catalog_key"], variant["file_name"])
        for item in items
        for variant in item["variants"]
    }
    raw_suppressed = payload.get("suppressed_variants", [])
    if not isinstance(raw_suppressed, list):
        errors.append(
            {
                "code": "invalid_field",
                "field": "suppressed_variants",
                "message": "must be an array",
            }
        )
        raw_suppressed = []
    suppressed_variants: List[dict] = []
    seen_suppressed: Set[Tuple[str, str]] = set()
    for suppressed_index, raw_suppressed_variant in enumerate(raw_suppressed):
        prefix = f"suppressed_variants[{suppressed_index}]"
        if not isinstance(raw_suppressed_variant, dict):
            errors.append(
                {"code": "invalid_field", "field": prefix, "message": "must be an object"}
            )
            continue
        suppressed = {
            "source_catalog_key": _string(
                raw_suppressed_variant.get("source_catalog_key"),
                f"{prefix}.source_catalog_key",
                errors,
            ).lower(),
            "file_name": _string(
                raw_suppressed_variant.get("file_name"),
                f"{prefix}.file_name",
                errors,
            ),
            "source_name": _string(
                raw_suppressed_variant.get("source_name"),
                f"{prefix}.source_name",
                errors,
            ),
            "reason": _string(
                raw_suppressed_variant.get("reason", "unclassified package"),
                f"{prefix}.reason",
                errors,
            ),
        }
        identity = (suppressed["source_catalog_key"], suppressed["file_name"])
        if identity in visible_identities:
            errors.append(
                {
                    "code": "visible_suppressed_overlap",
                    "field": prefix,
                    "source_catalog_key": identity[0],
                    "file_name": identity[1],
                }
            )
        if identity in seen_suppressed:
            errors.append(
                {
                    "code": "duplicate_suppressed_identity",
                    "field": prefix,
                    "source_catalog_key": identity[0],
                    "file_name": identity[1],
                }
            )
        seen_suppressed.add(identity)
        suppressed_variants.append(suppressed)

    if errors:
        raise ReconciliationError(
            f"Metadata validation failed with {len(errors)} error(s)", errors=errors
        )
    return {
        "schema_version": 1,
        "items": items,
        "suppressed_variants": suppressed_variants,
    }


def _row_dict(row: Mapping[str, Any]) -> dict:
    result = dict(row)
    result["metadata"] = dict(result.get("metadata") or {})
    result["tags"] = list(result.get("tags") or []) if "tags" in result else []
    result["aliases"] = list(result.get("aliases") or []) if "aliases" in result else []
    return result


def load_catalog_rows(cur: Any) -> Tuple[List[dict], List[dict]]:
    cur.execute(
        """
        SELECT id, slug, name, description, category, publisher, icon_url,
               tags, aliases, metadata, is_active, created_at, updated_at
        FROM download_items
        ORDER BY id
        """
    )
    items = [_row_dict(row) for row in cur.fetchall()]
    cur.execute(
        """
        SELECT v.id, v.item_id, v.storage_bucket, v.object_key, v.file_name,
               v.version, v.architecture, v.operating_system, v.file_type,
               v.size_bytes, v.sha256, v.etag, v.content_type, v.last_modified,
               v.metadata, v.is_active, v.created_at, v.updated_at,
               i.slug AS current_item_slug
        FROM download_variants v
        JOIN download_items i ON i.id = v.item_id
        ORDER BY v.id
        """
    )
    variants = [_row_dict(row) for row in cur.fetchall()]
    return items, variants


def desired_variant_signature(variant: Mapping[str, Any]) -> str:
    fields = (
        "target_slug",
        "source_catalog_key",
        "source_name",
        "member_name",
        "member_key",
        "version",
        "architecture",
        "operating_system",
        *OPTIONAL_MEMBER_FIELDS,
    )
    return stable_json({field: variant.get(field, "") for field in fields})


def variant_metadata_after(live: Mapping[str, Any], desired: Mapping[str, Any]) -> dict:
    result = dict(live.get("metadata") or {})
    for field in PROVENANCE_FIELDS:
        result[field] = desired[field]
    for field in OPTIONAL_MEMBER_FIELDS:
        if field in desired:
            result[field] = desired[field]
    result["canonical_catalog_key"] = desired["target_slug"]
    result["catalog_organization_schema_version"] = 1
    result["catalog_visibility"] = "published"
    result.pop("catalog_suppression_reason", None)
    return result


def _changed_fields(live: Mapping[str, Any], target_slug: str, desired: Mapping[str, Any], metadata: dict) -> List[str]:
    changed: List[str] = []
    if live.get("current_item_slug") != target_slug:
        changed.append("item_id")
    for field in ("version", "architecture", "operating_system"):
        if (live.get(field) or "") != (desired.get(field) or ""):
            changed.append(field)
    if stable_json(live.get("metadata") or {}) != stable_json(metadata):
        changed.append("metadata")
    if not bool(live.get("is_active")):
        changed.append("is_active")
    return changed


def suppressed_variant_metadata_after(
    live: Mapping[str, Any], suppressed: Mapping[str, Any]
) -> dict:
    result = dict(live.get("metadata") or {})
    result["source_catalog_key"] = suppressed["source_catalog_key"]
    result["source_name"] = suppressed["source_name"]
    result["catalog_visibility"] = "suppressed"
    result["catalog_suppression_reason"] = suppressed["reason"]
    result["catalog_organization_schema_version"] = 1
    return result


def build_plan(
    catalog: Mapping[str, Any],
    live_items: Sequence[Mapping[str, Any]],
    live_variants: Sequence[Mapping[str, Any]],
    *,
    allow_unique_fallbacks: bool = False,
) -> dict:
    """Build a guarded deterministic plan without mutating its inputs."""
    items = [_row_dict(row) for row in live_items]
    variants = [_row_dict(row) for row in live_variants]
    items_by_id = {int(row["id"]): row for row in items}
    items_by_slug = {str(row["slug"]): row for row in items}
    errors: List[dict] = []
    warnings: List[dict] = []

    primary: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    paths: Dict[str, List[dict]] = defaultdict(list)
    hashes: Dict[str, List[dict]] = defaultdict(list)
    for live in variants:
        metadata = live.get("metadata") or {}
        source_key = str(metadata.get("source_catalog_key") or live.get("current_item_slug") or "").strip().lower()
        primary[(source_key, str(live.get("file_name") or ""))].append(live)
        source_path = str(metadata.get("source_relative_path") or "").strip()
        if source_path:
            paths[source_path].append(live)
        digest = str(live.get("sha256") or "").strip().lower()
        if SHA256_PATTERN.fullmatch(digest):
            hashes[digest].append(live)

    desired_groups: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    canonical_specs: Dict[str, dict] = {}
    for item in catalog["items"]:
        canonical_specs[item["catalog_key"]] = dict(item)
        for desired in item["variants"]:
            desired_groups[(desired["source_catalog_key"], desired["file_name"])].append(desired)

    assigned_ids: Dict[int, Tuple[str, str]] = {}
    assignments: List[dict] = []
    source_item_ids_by_target: Dict[str, Set[int]] = defaultdict(set)
    for key in sorted(desired_groups, key=lambda value: (value[0], value[1].casefold(), value[1])):
        desired_rows = desired_groups[key]
        signatures = {desired_variant_signature(row) for row in desired_rows}
        if len(signatures) != 1:
            errors.append(
                {
                    "code": "conflicting_desired_identity",
                    "source_catalog_key": key[0],
                    "file_name": key[1],
                    "message": "the same source identity has different canonical targets or patches",
                }
            )
            continue
        desired = desired_rows[0]
        candidates = list(primary.get(key, []))
        match_method = "source_catalog_key+file_name"

        if not candidates and allow_unique_fallbacks:
            source_path = str(desired.get("source_relative_path") or desired.get("source_path") or "").strip()
            if source_path:
                path_candidates = paths.get(source_path, [])
                if len(path_candidates) == 1:
                    candidates = list(path_candidates)
                    match_method = "unique_source_relative_path"
                elif len(path_candidates) > 1:
                    errors.append(
                        {
                            "code": "ambiguous_source_path",
                            "source_catalog_key": key[0],
                            "file_name": key[1],
                            "source_relative_path": source_path,
                            "candidate_variant_ids": sorted(int(row["id"]) for row in path_candidates),
                        }
                    )
                    continue
            if not candidates:
                digest = str(desired.get("sha256") or "").strip().lower()
                if digest:
                    if not SHA256_PATTERN.fullmatch(digest):
                        errors.append(
                            {"code": "invalid_sha256", "source_catalog_key": key[0], "file_name": key[1]}
                        )
                        continue
                    hash_candidates = hashes.get(digest, [])
                    if len(hash_candidates) == 1:
                        candidates = list(hash_candidates)
                        match_method = "unique_sha256"
                    elif len(hash_candidates) > 1:
                        errors.append(
                            {
                                "code": "ambiguous_sha256",
                                "source_catalog_key": key[0],
                                "file_name": key[1],
                                "sha256": digest,
                                "candidate_variant_ids": sorted(int(row["id"]) for row in hash_candidates),
                            }
                        )
                        continue

        if not candidates:
            errors.append(
                {
                    "code": "unmatched_variant",
                    "source_catalog_key": key[0],
                    "file_name": key[1],
                    "message": "no live variant has this exact source identity",
                }
            )
            continue
        if len(candidates) > 1:
            warnings.append(
                {
                    "code": "duplicate_live_source_identity",
                    "source_catalog_key": key[0],
                    "file_name": key[1],
                    "candidate_variant_ids": sorted(int(row["id"]) for row in candidates),
                    "message": "all rows will receive the same target and patch; no row is discarded",
                }
            )

        for live in sorted(candidates, key=lambda row: int(row["id"])):
            variant_id = int(live["id"])
            previous_key = assigned_ids.get(variant_id)
            if previous_key is not None and previous_key != key:
                errors.append(
                    {
                        "code": "variant_assigned_twice",
                        "variant_id": variant_id,
                        "first_source_identity": list(previous_key),
                        "second_source_identity": list(key),
                    }
                )
                continue
            assigned_ids[variant_id] = key
            metadata_after = variant_metadata_after(live, desired)
            changes = _changed_fields(live, desired["target_slug"], desired, metadata_after)
            source_item_ids_by_target[desired["target_slug"]].add(int(live["item_id"]))
            assignments.append(
                {
                    "action": "publish",
                    "variant_id": variant_id,
                    "source_item_id": int(live["item_id"]),
                    "source_slug": str(live.get("current_item_slug") or ""),
                    "target_slug": desired["target_slug"],
                    "file_name": live["file_name"],
                    "version": desired["version"],
                    "architecture": desired["architecture"],
                    "operating_system": desired["operating_system"],
                    "metadata": metadata_after,
                    "is_active": True,
                    "changed_fields": changes,
                    "match_method": match_method,
                    # Guards proving the UPDATE cannot alter S3 identity.
                    "storage_bucket": live["storage_bucket"],
                    "object_key": live["object_key"],
                }
            )

    for suppressed in sorted(
        catalog.get("suppressed_variants", []),
        key=lambda value: (
            value["source_catalog_key"],
            value["file_name"].casefold(),
            value["file_name"],
        ),
    ):
        key = (suppressed["source_catalog_key"], suppressed["file_name"])
        candidates = list(primary.get(key, []))
        if not candidates:
            warnings.append(
                {
                    "code": "suppressed_variant_not_present",
                    "source_catalog_key": key[0],
                    "file_name": key[1],
                    "message": "the source is already absent from this database; no action is needed",
                }
            )
            continue
        if len(candidates) > 1:
            warnings.append(
                {
                    "code": "duplicate_live_suppressed_identity",
                    "source_catalog_key": key[0],
                    "file_name": key[1],
                    "candidate_variant_ids": sorted(int(row["id"]) for row in candidates),
                    "message": "all matching rows will be soft-hidden",
                }
            )
        for live in sorted(candidates, key=lambda row: int(row["id"])):
            variant_id = int(live["id"])
            previous_key = assigned_ids.get(variant_id)
            if previous_key is not None:
                errors.append(
                    {
                        "code": "variant_assigned_twice",
                        "variant_id": variant_id,
                        "first_source_identity": list(previous_key),
                        "second_source_identity": list(key),
                    }
                )
                continue
            assigned_ids[variant_id] = key
            metadata_after = suppressed_variant_metadata_after(live, suppressed)
            changes: List[str] = []
            if stable_json(live.get("metadata") or {}) != stable_json(metadata_after):
                changes.append("metadata")
            if bool(live.get("is_active")):
                changes.append("is_active")
            assignments.append(
                {
                    "action": "suppress",
                    "variant_id": variant_id,
                    "source_item_id": int(live["item_id"]),
                    "source_slug": str(live.get("current_item_slug") or ""),
                    "target_slug": None,
                    "file_name": live["file_name"],
                    "metadata": metadata_after,
                    "is_active": False,
                    "changed_fields": changes,
                    "match_method": "source_catalog_key+file_name",
                    "storage_bucket": live["storage_bucket"],
                    "object_key": live["object_key"],
                }
            )

    canonical_items: List[dict] = []
    desired_variants_by_target: Dict[str, List[dict]] = defaultdict(list)
    for desired_rows in desired_groups.values():
        for desired in desired_rows:
            desired_variants_by_target[desired["target_slug"]].append(desired)

    for slug in sorted(canonical_specs):
        spec = canonical_specs[slug]
        existing = items_by_slug.get(slug)
        matched_source_ids = source_item_ids_by_target.get(slug, set())
        if existing is not None and int(existing["id"]) not in matched_source_ids:
            organization = (existing.get("metadata") or {}).get("catalog_organization") or {}
            already_canonical = (
                isinstance(organization, dict)
                and organization.get("canonical_catalog_key") == slug
                and organization.get("schema_version") == 1
            )
            if not already_canonical:
                errors.append(
                    {
                        "code": "target_slug_conflict",
                        "catalog_key": slug,
                        "existing_item_id": int(existing["id"]),
                        "message": (
                            "the canonical slug belongs to an unrelated existing item; "
                            "rename it or explicitly organize it before reconciliation"
                        ),
                    }
                )
        source_items = [
            items_by_id[item_id]
            for item_id in sorted(source_item_ids_by_target.get(slug, set()))
            if item_id in items_by_id
        ]
        desired_variants = desired_variants_by_target.get(slug, [])
        aliases = merge_strings(
            existing.get("aliases", []) if existing else [],
            [spec["display_name"]],
            spec.get("source_names", []),
            (row["source_name"] for row in desired_variants),
            (row["member_name"] for row in desired_variants),
            (row.get("name", "") for row in source_items),
            *(row.get("aliases", []) for row in source_items),
        )
        tags = merge_strings(
            existing.get("tags", []) if existing else [],
            spec.get("tags", []),
            *(row.get("tags", []) for row in source_items),
        )
        metadata = dict(existing.get("metadata") or {}) if existing else {}
        metadata.update(spec.get("metadata") or {})
        metadata["catalog_organization"] = {
            "schema_version": 1,
            "canonical_catalog_key": slug,
            "source_catalog_keys": sorted({row["source_catalog_key"] for row in desired_variants}),
            "member_keys": sorted({row["member_key"] for row in desired_variants if row["member_key"]}),
        }
        desired_item = {
            "slug": slug,
            "name": spec["display_name"],
            "description": spec.get("description") or (existing.get("description", "") if existing else ""),
            "category": spec["category"],
            "publisher": spec.get("publisher") or (existing.get("publisher", "") if existing else ""),
            "icon_url": spec.get("icon_url") if spec.get("icon_url") is not None else (
                existing.get("icon_url") if existing else None
            ),
            "tags": tags,
            "aliases": aliases,
            "metadata": metadata,
            "is_active": True,
        }
        item_changes = [
            field
            for field in ITEM_MUTABLE_COLUMNS
            if field != "slug"
            and (
                existing is None
                or stable_json(existing.get(field)) != stable_json(desired_item.get(field))
            )
        ]
        canonical_items.append(
            {
                "existing_id": int(existing["id"]) if existing else None,
                "desired": desired_item,
                "changed_fields": item_changes if existing else list(ITEM_MUTABLE_COLUMNS),
                "source_item_ids": sorted(source_item_ids_by_target.get(slug, set())),
            }
        )

    if errors:
        raise ReconciliationError(
            f"Reconciliation plan is blocked by {len(errors)} conflict(s)", errors=errors
        )

    touched_item_ids = sorted({row["source_item_id"] for row in assignments})
    return {
        "canonical_items": canonical_items,
        "assignments": sorted(assignments, key=lambda row: row["variant_id"]),
        "touched_item_ids": touched_item_ids,
        "warnings": warnings,
        "metadata_variant_records": sum(len(item["variants"]) for item in catalog["items"]),
        "metadata_suppressed_records": len(catalog.get("suppressed_variants", [])),
    }


def plan_report(plan: Mapping[str, Any], *, run_id: str, metadata_digest: str, mode: str) -> dict:
    assignments = plan["assignments"]
    canonical = plan["canonical_items"]
    return {
        "run_id": run_id,
        "mode": mode,
        "status": "planned" if mode == "dry-run" else "applying",
        "generated_at": utc_now(),
        "metadata_sha256": metadata_digest,
        "counts": {
            "canonical_items": len(canonical),
            "canonical_items_to_insert": sum(row["existing_id"] is None for row in canonical),
            "canonical_items_to_update": sum(bool(row["changed_fields"]) and row["existing_id"] is not None for row in canonical),
            "metadata_variant_records": plan["metadata_variant_records"],
            "metadata_suppressed_records": plan["metadata_suppressed_records"],
            "live_variant_rows_matched": len(assignments),
            "variant_rows_to_move": sum("item_id" in row["changed_fields"] for row in assignments),
            "variant_rows_to_patch": sum(bool(row["changed_fields"]) for row in assignments),
            "variant_rows_to_suppress": sum(
                row["action"] == "suppress" and "is_active" in row["changed_fields"]
                for row in assignments
            ),
            "touched_source_items": len(plan["touched_item_ids"]),
        },
        "warnings": json_safe(plan["warnings"]),
        "variant_changes": [
            {
                "variant_id": row["variant_id"],
                "action": row["action"],
                "source_slug": row["source_slug"],
                "target_slug": row["target_slug"],
                "file_name": row["file_name"],
                "changed_fields": row["changed_fields"],
                "match_method": row["match_method"],
            }
            for row in assignments
            if row["changed_fields"]
        ],
        "item_changes": [
            {
                "slug": row["desired"]["slug"],
                "existing_id": row["existing_id"],
                "changed_fields": row["changed_fields"],
            }
            for row in canonical
            if row["changed_fields"]
        ],
    }


def connect_from_environment() -> Any:
    try:
        import psycopg2
    except ImportError as exc:
        raise ReconciliationError("psycopg2 is required; install requirements.txt") from exc
    kwargs: Dict[str, Any] = {
        "host": os.environ.get("DB_HOST", "eden-postgres").strip(),
        "port": os.environ.get("DB_PORT", "5432").strip(),
        "dbname": os.environ.get("DB_NAME", "eden").strip(),
        "user": os.environ.get("DB_USER", "eden").strip(),
        "connect_timeout": int(os.environ.get("DB_CONNECT_TIMEOUT", "10")),
        "application_name": "eden-download-catalog-reconciler",
    }
    password = os.environ.get("DB_PASSWORD", "")
    if password:
        kwargs["password"] = password
    sslmode = os.environ.get("DB_SSLMODE", "").strip()
    if sslmode:
        kwargs["sslmode"] = sslmode
    return psycopg2.connect(**kwargs)


def begin_locked_transaction(conn: Any, schema: str) -> Any:
    if not SCHEMA_PATTERN.fullmatch(schema):
        raise ReconciliationError(f"Invalid DB_SCHEMA: {schema!r}")
    try:
        import psycopg2.extras
    except ImportError as exc:
        raise ReconciliationError("psycopg2 is required; install requirements.txt") from exc
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT set_config('search_path', %s, true)", (schema,))
    cur.execute("SELECT to_regclass('download_items') AS items, to_regclass('download_variants') AS variants")
    tables = cur.fetchone()
    if not tables or not tables["items"] or not tables["variants"]:
        raise ReconciliationError(
            f"download_items/download_variants were not found in PostgreSQL schema {schema!r}"
        )
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", ADVISORY_LOCK_KEYS)
    cur.execute("LOCK TABLE download_items, download_variants IN SHARE ROW EXCLUSIVE MODE")
    return cur


def default_backup_path(run_id: str, *, rollback: bool = False) -> Path:
    label = "before-rollback" if rollback else "before-reconcile"
    return Path.cwd() / f"download-catalog-{label}-{run_id}.json"


def write_json_exclusive(path: Path, payload: Any) -> str:
    """Create, never overwrite, a JSON file and return its SHA-256."""
    rendered = json.dumps(json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(rendered)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def write_report(path: Optional[Path], report: Mapping[str, Any]) -> None:
    rendered = json.dumps(json_safe(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path is None:
        sys.stdout.write(rendered)
        return
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(rendered)
    os.replace(str(temporary), str(path))


def backup_payload(
    *, run_id: str, metadata_digest: str, items: Sequence[Mapping[str, Any]], variants: Sequence[Mapping[str, Any]], action: str
) -> dict:
    return {
        "format": "eden-download-catalog-backup-v1",
        "run_id": run_id,
        "action": action,
        "created_at": utc_now(),
        "metadata_sha256": metadata_digest,
        "download_items": json_safe(items),
        "download_variants": json_safe(variants),
    }


def ensure_audit_table(cur: Any) -> None:
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (
            run_id UUID PRIMARY KEY,
            action TEXT NOT NULL DEFAULT 'reconcile',
            metadata_sha256 TEXT NOT NULL,
            backup_path TEXT NOT NULL,
            backup_sha256 TEXT NOT NULL,
            before_state JSONB NOT NULL,
            after_state JSONB NOT NULL,
            report JSONB NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            rolled_back_at TIMESTAMPTZ,
            rollback_backup_path TEXT,
            rollback_backup_sha256 TEXT,
            rollback_report JSONB
        )
        """
    )


def _snapshot_for_audit(
    items: Sequence[Mapping[str, Any]],
    variants: Sequence[Mapping[str, Any]],
    item_ids: Set[int],
    variant_ids: Set[int],
) -> dict:
    return json_safe(
        {
            "items": [dict(row) for row in items if int(row["id"]) in item_ids],
            "variants": [dict(row) for row in variants if int(row["id"]) in variant_ids],
        }
    )


def apply_plan(
    cur: Any,
    plan: dict,
    before_items: Sequence[Mapping[str, Any]],
    before_variants: Sequence[Mapping[str, Any]],
    report: dict,
    *,
    run_id: str,
    metadata_digest: str,
    backup_path: Path,
    backup_digest: str,
) -> dict:
    from psycopg2.extras import Json

    ensure_audit_table(cur)
    target_ids: Dict[str, int] = {}
    inserted_item_ids: List[int] = []
    preexisting_affected_ids: Set[int] = set(plan["touched_item_ids"])
    for canonical in plan["canonical_items"]:
        desired = canonical["desired"]
        existing_id = canonical["existing_id"]
        if existing_id is None:
            cur.execute(
                """
                INSERT INTO download_items
                    (slug, name, description, category, publisher, icon_url,
                     tags, aliases, metadata, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                RETURNING id
                """,
                (
                    desired["slug"], desired["name"], desired["description"], desired["category"],
                    desired["publisher"], desired["icon_url"], desired["tags"], desired["aliases"],
                    Json(desired["metadata"]),
                ),
            )
            item_id = int(cur.fetchone()["id"])
            inserted_item_ids.append(item_id)
        else:
            item_id = int(existing_id)
            preexisting_affected_ids.add(item_id)
            if canonical["changed_fields"]:
                cur.execute(
                    """
                    UPDATE download_items
                    SET name = %s, description = %s, category = %s, publisher = %s,
                        icon_url = %s, tags = %s, aliases = %s, metadata = %s,
                        is_active = TRUE
                    WHERE id = %s
                    """,
                    (
                        desired["name"], desired["description"], desired["category"], desired["publisher"],
                        desired["icon_url"], desired["tags"], desired["aliases"], Json(desired["metadata"]), item_id,
                    ),
                )
                if cur.rowcount != 1:
                    raise ReconciliationError(f"Canonical item {desired['slug']!r} disappeared during apply")
        target_ids[desired["slug"]] = item_id

    changed_variant_ids: Set[int] = set()
    suppressed_variant_ids: Set[int] = set()
    for assignment in plan["assignments"]:
        if not assignment["changed_fields"]:
            continue
        variant_id = int(assignment["variant_id"])
        if assignment["action"] == "suppress":
            cur.execute(
                """
                UPDATE download_variants
                SET metadata = %s, is_active = FALSE
                WHERE id = %s AND storage_bucket = %s AND object_key = %s
                """,
                (
                    Json(assignment["metadata"]),
                    variant_id,
                    assignment["storage_bucket"],
                    assignment["object_key"],
                ),
            )
            suppressed_variant_ids.add(variant_id)
        else:
            cur.execute(
                """
                UPDATE download_variants
                SET item_id = %s, version = %s, architecture = %s,
                    operating_system = %s, metadata = %s, is_active = TRUE
                WHERE id = %s AND storage_bucket = %s AND object_key = %s
                """,
                (
                    target_ids[assignment["target_slug"]],
                    assignment["version"],
                    assignment["architecture"],
                    assignment["operating_system"],
                    Json(assignment["metadata"]),
                    variant_id,
                    assignment["storage_bucket"],
                    assignment["object_key"],
                ),
            )
        if cur.rowcount != 1:
            raise ReconciliationError(
                f"Variant {variant_id} changed identity or disappeared during apply; transaction was rolled back"
            )
        changed_variant_ids.add(variant_id)

    target_id_set = set(target_ids.values())
    deactivated_ids: List[int] = []
    for item_id in plan["touched_item_ids"]:
        if item_id in target_id_set:
            continue
        cur.execute(
            """
            UPDATE download_items i
            SET is_active = FALSE
            WHERE i.id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM download_variants v
                  WHERE v.item_id = i.id AND v.is_active
              )
              AND i.is_active
            RETURNING i.id
            """,
            (item_id,),
        )
        row = cur.fetchone()
        if row:
            deactivated_ids.append(int(row["id"]))

    affected_item_ids = preexisting_affected_ids | target_id_set
    affected_variant_ids = {int(row["variant_id"]) for row in plan["assignments"]}
    before_state = _snapshot_for_audit(
        before_items, before_variants, preexisting_affected_ids, affected_variant_ids
    )
    before_state["inserted_item_ids"] = inserted_item_ids

    after_items, after_variants = load_catalog_rows(cur)
    after_state = _snapshot_for_audit(
        after_items, after_variants, affected_item_ids, affected_variant_ids
    )
    report["status"] = "applied"
    report["applied_at"] = utc_now()
    report["backup"] = {"path": str(backup_path.resolve()), "sha256": backup_digest}
    report["counts"]["canonical_items_inserted"] = len(inserted_item_ids)
    report["counts"]["variant_rows_changed"] = len(changed_variant_ids)
    report["counts"]["variant_rows_suppressed"] = len(suppressed_variant_ids)
    report["counts"]["empty_source_items_deactivated"] = len(deactivated_ids)
    report["inserted_item_ids"] = inserted_item_ids
    report["deactivated_item_ids"] = deactivated_ids

    cur.execute(
        f"""
        INSERT INTO {AUDIT_TABLE}
            (run_id, action, metadata_sha256, backup_path, backup_sha256,
             before_state, after_state, report)
        VALUES (%s, 'reconcile', %s, %s, %s, %s, %s, %s)
        """,
        (
            run_id, metadata_digest, str(backup_path.resolve()), backup_digest,
            Json(before_state), Json(after_state), Json(json_safe(report)),
        ),
    )
    return report


def _rows_by_id(rows: Sequence[Mapping[str, Any]]) -> Dict[int, dict]:
    return {int(row["id"]): _row_dict(row) for row in rows}


def rollback_conflicts(audit: Mapping[str, Any], live_items: Sequence[Mapping[str, Any]], live_variants: Sequence[Mapping[str, Any]]) -> List[dict]:
    after = dict(audit.get("after_state") or {})
    expected_items = _rows_by_id(after.get("items") or [])
    expected_variants = _rows_by_id(after.get("variants") or [])
    current_items = _rows_by_id(live_items)
    current_variants = _rows_by_id(live_variants)
    conflicts: List[dict] = []
    for item_id, expected in expected_items.items():
        current = current_items.get(item_id)
        if current is None:
            conflicts.append({"code": "rollback_item_missing", "item_id": item_id})
            continue
        changed = [
            field for field in ITEM_MUTABLE_COLUMNS
            if stable_json(current.get(field)) != stable_json(expected.get(field))
        ]
        if changed:
            conflicts.append({"code": "rollback_item_drift", "item_id": item_id, "fields": changed})
    for variant_id, expected in expected_variants.items():
        current = current_variants.get(variant_id)
        if current is None:
            conflicts.append({"code": "rollback_variant_missing", "variant_id": variant_id})
            continue
        fields = (*VARIANT_MUTABLE_COLUMNS, "storage_bucket", "object_key")
        changed = [
            field for field in fields
            if stable_json(current.get(field)) != stable_json(expected.get(field))
        ]
        if changed:
            conflicts.append({"code": "rollback_variant_drift", "variant_id": variant_id, "fields": changed})
    inserted_ids = {int(value) for value in (audit.get("before_state") or {}).get("inserted_item_ids", [])}
    expected_variant_ids = set(expected_variants)
    for variant in live_variants:
        if int(variant["item_id"]) in inserted_ids and int(variant["id"]) not in expected_variant_ids:
            conflicts.append(
                {
                    "code": "rollback_new_target_has_unexpected_variant",
                    "item_id": int(variant["item_id"]),
                    "variant_id": int(variant["id"]),
                }
            )
    return conflicts


def load_audit(cur: Any, run_id: str) -> dict:
    cur.execute("SELECT to_regclass(%s) AS audit_table", (AUDIT_TABLE,))
    table = cur.fetchone()
    if not table or not table["audit_table"]:
        raise ReconciliationError("No reconciliation audit table exists; there is nothing this script can roll back")
    cur.execute(f"SELECT * FROM {AUDIT_TABLE} WHERE run_id = %s FOR UPDATE", (run_id,))
    row = cur.fetchone()
    if not row:
        raise ReconciliationError(f"No reconciliation audit run exists with id {run_id}")
    return dict(row)


def apply_rollback(cur: Any, audit: dict, report: dict, *, backup_path: Path, backup_digest: str) -> dict:
    from psycopg2.extras import Json

    before = dict(audit.get("before_state") or {})
    before_items = _rows_by_id(before.get("items") or [])
    before_variants = _rows_by_id(before.get("variants") or [])

    # Old/source items still exist (they were only soft-deactivated), so restore
    # their catalog fields before moving variants back to them.
    for item_id, item in sorted(before_items.items()):
        cur.execute(
            """
            UPDATE download_items
            SET slug = %s, name = %s, description = %s, category = %s,
                publisher = %s, icon_url = %s, tags = %s, aliases = %s,
                metadata = %s, is_active = %s
            WHERE id = %s
            """,
            (
                item["slug"], item["name"], item["description"], item["category"], item["publisher"],
                item.get("icon_url"), item.get("tags") or [], item.get("aliases") or [],
                Json(item.get("metadata") or {}), bool(item["is_active"]), item_id,
            ),
        )
        if cur.rowcount != 1:
            raise ReconciliationError(f"Cannot restore missing download item {item_id}")

    for variant_id, variant in sorted(before_variants.items()):
        cur.execute(
            """
            UPDATE download_variants
            SET item_id = %s, version = %s, architecture = %s,
                operating_system = %s, metadata = %s, is_active = %s
            WHERE id = %s AND storage_bucket = %s AND object_key = %s
            """,
            (
                variant["item_id"], variant["version"], variant["architecture"],
                variant["operating_system"], Json(variant.get("metadata") or {}),
                bool(variant["is_active"]), variant_id,
                variant["storage_bucket"], variant["object_key"],
            ),
        )
        if cur.rowcount != 1:
            raise ReconciliationError(f"Cannot safely restore download variant {variant_id}")

    deleted_ids: List[int] = []
    for item_id in sorted(int(value) for value in before.get("inserted_item_ids", [])):
        cur.execute(
            """
            DELETE FROM download_items i
            WHERE i.id = %s
              AND NOT EXISTS (SELECT 1 FROM download_variants v WHERE v.item_id = i.id)
            RETURNING i.id
            """,
            (item_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ReconciliationError(
                f"New canonical item {item_id} is not empty; rollback transaction was cancelled"
            )
        deleted_ids.append(int(row["id"]))

    report["status"] = "rolled_back"
    report["rolled_back_at"] = utc_now()
    report["backup"] = {"path": str(backup_path.resolve()), "sha256": backup_digest}
    report["counts"]["inserted_items_deleted"] = len(deleted_ids)
    cur.execute(
        f"""
        UPDATE {AUDIT_TABLE}
        SET rolled_back_at = NOW(), rollback_backup_path = %s,
            rollback_backup_sha256 = %s, rollback_report = %s
        WHERE run_id = %s AND rolled_back_at IS NULL
        """,
        (str(backup_path.resolve()), backup_digest, Json(json_safe(report)), str(audit["run_id"])),
    )
    if cur.rowcount != 1:
        raise ReconciliationError("Audit row was already rolled back or changed concurrently")
    return report


def run_reconciliation(args: argparse.Namespace) -> Tuple[int, dict]:
    run_id = str(uuid.uuid4())
    try:
        catalog, metadata_digest = read_and_normalize_catalog(args.metadata.resolve())
    except (OSError, ReconciliationError) as exc:
        errors = exc.errors if isinstance(exc, ReconciliationError) else [{"code": "metadata_io", "message": str(exc)}]
        return 2, {
            "run_id": run_id,
            "mode": "apply" if args.apply else "dry-run",
            "status": "blocked",
            "generated_at": utc_now(),
            "errors": errors,
        }

    conn = connect_from_environment()
    try:
        cur = begin_locked_transaction(conn, args.schema)
        items, variants = load_catalog_rows(cur)
        try:
            plan = build_plan(
                catalog, items, variants, allow_unique_fallbacks=args.allow_unique_fallbacks
            )
        except ReconciliationError as exc:
            conn.rollback()
            return 2, {
                "run_id": run_id,
                "mode": "apply" if args.apply else "dry-run",
                "status": "blocked",
                "generated_at": utc_now(),
                "metadata_sha256": metadata_digest,
                "errors": exc.errors,
            }
        report = plan_report(
            plan,
            run_id=run_id,
            metadata_digest=metadata_digest,
            mode="apply" if args.apply else "dry-run",
        )
        if not args.apply:
            conn.rollback()
            return 0, report

        backup_path = args.backup or default_backup_path(run_id)
        backup_digest = write_json_exclusive(
            backup_path,
            backup_payload(
                run_id=run_id,
                metadata_digest=metadata_digest,
                items=items,
                variants=variants,
                action="reconcile",
            ),
        )
        report = apply_plan(
            cur,
            plan,
            items,
            variants,
            report,
            run_id=run_id,
            metadata_digest=metadata_digest,
            backup_path=backup_path,
            backup_digest=backup_digest,
        )
        conn.commit()
        return 0, report
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_rollback(args: argparse.Namespace) -> Tuple[int, dict]:
    try:
        parsed_run_id = str(uuid.UUID(args.rollback))
    except (ValueError, AttributeError) as exc:
        raise ReconciliationError("--rollback must be a valid reconciliation run UUID") from exc
    operation_id = str(uuid.uuid4())
    conn = connect_from_environment()
    try:
        cur = begin_locked_transaction(conn, args.schema)
        audit = load_audit(cur, parsed_run_id)
        if audit.get("rolled_back_at") is not None:
            conn.rollback()
            return 2, {
                "run_id": operation_id,
                "rollback_of": parsed_run_id,
                "mode": "rollback-apply" if args.apply else "rollback-dry-run",
                "status": "blocked",
                "errors": [{"code": "already_rolled_back", "message": "this audit run was already rolled back"}],
            }
        items, variants = load_catalog_rows(cur)
        conflicts = rollback_conflicts(audit, items, variants)
        report = {
            "run_id": operation_id,
            "rollback_of": parsed_run_id,
            "mode": "rollback-apply" if args.apply else "rollback-dry-run",
            "status": "planned" if not conflicts else "blocked",
            "generated_at": utc_now(),
            "counts": {
                "items_to_restore": len((audit.get("before_state") or {}).get("items", [])),
                "variants_to_restore": len((audit.get("before_state") or {}).get("variants", [])),
                "inserted_items_to_delete": len((audit.get("before_state") or {}).get("inserted_item_ids", [])),
            },
            "errors": conflicts,
        }
        if conflicts or not args.apply:
            conn.rollback()
            return (2 if conflicts else 0), report

        backup_path = args.backup or default_backup_path(operation_id, rollback=True)
        backup_digest = write_json_exclusive(
            backup_path,
            backup_payload(
                run_id=operation_id,
                metadata_digest=str(audit.get("metadata_sha256") or ""),
                items=items,
                variants=variants,
                action=f"rollback:{parsed_run_id}",
            ),
        )
        report = apply_rollback(
            cur, audit, report, backup_path=backup_path, backup_digest=backup_digest
        )
        conn.commit()
        return 0, report
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reorganize Eden download catalog rows without touching S3 objects. Default: dry-run."
    )
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--apply", action="store_true", help="Commit the planned reconciliation or rollback")
    parser.add_argument("--rollback", metavar="RUN_ID", help="Preview/rollback a prior applied audit run")
    parser.add_argument("--backup", type=Path, help="New external JSON backup path (must not already exist)")
    parser.add_argument("--report", type=Path, help="Write the JSON report here instead of stdout")
    parser.add_argument(
        "--allow-unique-fallbacks",
        action="store_true",
        help="If the exact source key + filename is absent, allow a globally unique source path or SHA-256 match",
    )
    parser.add_argument("--schema", default=os.environ.get("DB_SCHEMA", "public").strip() or "public")
    args = parser.parse_args(argv)
    if args.rollback and args.allow_unique_fallbacks:
        parser.error("--allow-unique-fallbacks is not used with --rollback")
    if args.backup is not None and args.report is not None:
        if args.backup.resolve() == args.report.resolve():
            parser.error("--backup and --report must be different files")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if args.rollback:
            code, report = run_rollback(args)
        else:
            code, report = run_reconciliation(args)
    except ReconciliationError as exc:
        code = 2
        report = {
            "mode": "rollback" if args.rollback else ("apply" if args.apply else "dry-run"),
            "status": "blocked",
            "generated_at": utc_now(),
            "errors": exc.errors,
        }
    except Exception as exc:
        code = 1
        report = {
            "mode": "rollback" if args.rollback else ("apply" if args.apply else "dry-run"),
            "status": "failed",
            "generated_at": utc_now(),
            "errors": [{"code": "unexpected_error", "message": str(exc)}],
        }
    write_report(args.report, report)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

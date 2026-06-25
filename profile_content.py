"""Profile-specific approved content configuration and media resolution."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence


CONTENT_KEYS = (
    "approved_replies",
    "approved_captions",
    "approved_media",
    "search_topics",
)

_LEGACY_KEYS = {
    "approved_replies": "comments",
    "approved_captions": "post_captions",
}


class ContentConfigurationError(ValueError):
    """Raised when pools.json contains an invalid profile-content structure."""


def _mapping(value, label: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContentConfigurationError(f"'{label}' must be a JSON object")
    return dict(value)


def _configured_list(container: Mapping, key: str, label: str) -> list[str] | None:
    configured_key = key
    if key not in container:
        legacy_key = _LEGACY_KEYS.get(key)
        if not legacy_key or legacy_key not in container:
            return None
        configured_key = legacy_key

    value = container[configured_key]
    if not isinstance(value, list):
        raise ContentConfigurationError(
            f"'{label}.{configured_key}' must be a JSON array"
        )
    if any(not isinstance(item, str) for item in value):
        raise ContentConfigurationError(
            f"'{label}.{configured_key}' may contain only strings"
        )
    return list(value)


def get_profile_content(data: Mapping, profile_id: str) -> dict:
    """Resolve exact approved content for one profile without automatic sharding.

    A key present in ``profiles[profile_id]`` wins, including an empty list.
    Missing profile keys fall back to ``defaults``, then to legacy top-level
    pools. Unknown profiles receive only configured defaults/top-level values.
    """
    root = _mapping(data, "root")
    defaults = _mapping(root.get("defaults"), "defaults")
    profiles = _mapping(root.get("profiles"), "profiles")

    for configured_profile_id, value in profiles.items():
        if not isinstance(configured_profile_id, str) or not configured_profile_id:
            raise ContentConfigurationError("profile keys must be non-empty strings")
        _mapping(value, f"profiles.{configured_profile_id}")

    profile = None
    if profile_id and profile_id in profiles:
        profile = _mapping(profiles[profile_id], f"profiles.{profile_id}")

    resolved: dict[str, object] = {
        "profile_id": profile_id,
        "source": "profile" if profile is not None else "defaults",
    }
    for key in CONTENT_KEYS:
        value = None
        if profile is not None:
            value = _configured_list(profile, key, f"profiles.{profile_id}")
        if value is None:
            value = _configured_list(defaults, key, "defaults")
        if value is None:
            value = _configured_list(root, key, "root")
        resolved[key] = value if value is not None else []

    return resolved


def validate_profile_content(data: Mapping) -> None:
    """Validate every configured profile and all fallback content lists."""
    root = _mapping(data, "root")
    profiles = _mapping(root.get("profiles"), "profiles")
    get_profile_content(root, "")
    for profile_id in profiles:
        get_profile_content(root, profile_id)


def resolve_approved_media(
    media_root: str,
    configured_paths: Sequence[str],
    allowed_extensions: Sequence[str],
) -> tuple[list[str], list[str]]:
    """Resolve existing approved files while rejecting unsafe media paths."""
    approved: list[str] = []
    errors: list[str] = []
    if not media_root:
        return approved, ["media root is not configured"] if configured_paths else []

    root = os.path.realpath(os.path.abspath(media_root))
    allowed = {extension.casefold() for extension in allowed_extensions}
    seen: set[str] = set()

    for configured_path in configured_paths:
        display_path = str(configured_path)
        if not display_path.strip():
            errors.append("approved media entry is empty")
            continue
        if os.path.isabs(display_path):
            errors.append(f"approved media path must be relative: {display_path}")
            continue

        candidate = os.path.realpath(os.path.abspath(os.path.join(root, display_path)))
        try:
            inside_root = os.path.commonpath([root, candidate]) == root
        except ValueError:
            inside_root = False
        if not inside_root:
            errors.append(f"approved media path leaves media directory: {display_path}")
            continue
        if os.path.splitext(candidate)[1].casefold() not in allowed:
            errors.append(f"approved media has unsupported extension: {display_path}")
            continue
        if not os.path.isfile(candidate):
            errors.append(f"approved media file not found: {display_path}")
            continue

        identity = os.path.normcase(candidate)
        if identity not in seen:
            approved.append(candidate)
            seen.add(identity)

    return approved, errors


def media_history_key(media_root: str, media_path: str) -> str:
    """Return a stable path key for per-profile media history."""
    relative = os.path.relpath(os.path.realpath(media_path), os.path.realpath(media_root))
    return relative.replace("\\", "/")

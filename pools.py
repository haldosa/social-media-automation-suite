import json

from config import _POOLS_PATH, BRAND_VOICE
from profile_content import (
    ContentConfigurationError,
    get_profile_content as _resolve_profile_content,
    validate_profile_content,
)


try:
    with open(_POOLS_PATH, "r", encoding="utf-8") as _f:
        data = json.load(_f)
except json.JSONDecodeError as exc:
    raise SystemExit(f"Invalid JSON in content pools file: {exc}") from exc

try:
    validate_profile_content(data)
except ContentConfigurationError as exc:
    raise SystemExit(f"Invalid content pools configuration: {exc}") from exc


def get_profile_content(profile_id: str) -> dict:
    """Return the exact approved content configured for ``profile_id``."""
    return _resolve_profile_content(data, profile_id)


_DEFAULT_CONTENT = get_profile_content("")
APPROVED_REPLIES = _DEFAULT_CONTENT["approved_replies"]
APPROVED_CAPTIONS = _DEFAULT_CONTENT["approved_captions"]
APPROVED_MEDIA = _DEFAULT_CONTENT["approved_media"]
SEARCH_TOPIC_POOL = _DEFAULT_CONTENT["search_topics"]

PREFLIGHT_SITES_POOL = data["PREFLIGHT_SITES_POOL"]

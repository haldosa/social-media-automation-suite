"""Deterministic publishing preparation and business-safe content validation."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping


DEFAULT_BRAND_VOICE = {
    "tone": "professional",
    "max_emojis": 2,
    "max_hashtags": 5,
    "banned_terms": [],
    "allowed_abbreviations": [],
}

_CASUAL_ABBREVIATIONS = frozenset({
    "afaik", "btw", "fomo", "fr", "idc", "idk", "ikr", "imo", "imho",
    "lmao", "lol", "ngl", "omg", "pls", "rn", "rofl", "smh", "tbh",
    "thx", "tl;dr", "u", "ur", "wanna", "gonna",
})

_SPAM_PATTERNS = (
    re.compile(r"\bfollow\s*(?:4|for)\s*follow\b", re.IGNORECASE),
    re.compile(r"\blike\s*(?:4|for)\s*like\b", re.IGNORECASE),
    re.compile(r"\b(?:click|tap)\s+(?:the\s+)?link\b", re.IGNORECASE),
    re.compile(r"\bcheck\s+(?:out\s+)?my\s+(?:bio|profile|page)\b", re.IGNORECASE),
    re.compile(r"\b(?:dm|message)\s+me\b", re.IGNORECASE),
    re.compile(r"\bguaranteed\s+(?:income|profit|results?)\b", re.IGNORECASE),
    re.compile(r"\bget\s+rich\s+quick\b", re.IGNORECASE),
)

_HASHTAG_RE = re.compile(r"(?<!\w)#[^\W#]+", re.UNICODE)
_MENTION_RE = re.compile(r"(?<!\w)@[^\W@]+", re.UNICODE)
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_WORD_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)


class ContentPolicyError(ValueError):
    """Raised when approved text does not satisfy the publishing policy."""


def normalize_brand_voice(brand_voice: Mapping | None = None) -> dict:
    """Return a complete, validated brand-voice policy without mutating input."""
    raw = dict(brand_voice or {}) if isinstance(brand_voice, Mapping) else {}
    policy = dict(DEFAULT_BRAND_VOICE)

    tone = str(raw.get("tone") or policy["tone"]).strip()
    policy["tone"] = tone or DEFAULT_BRAND_VOICE["tone"]

    for key in ("max_emojis", "max_hashtags"):
        try:
            value = int(raw.get(key, policy[key]))
        except (TypeError, ValueError):
            value = policy[key]
        policy[key] = max(0, value)

    for key in ("banned_terms", "allowed_abbreviations"):
        value = raw.get(key, policy[key])
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",")]
        if not isinstance(value, (list, tuple, set, frozenset)):
            value = []
        policy[key] = [str(item).strip() for item in value if str(item).strip()]

    return policy


def _prepare_text(text: str) -> str:
    if not isinstance(text, str):
        return ""
    prepared = unicodedata.normalize("NFKC", text)
    prepared = prepared.replace("\r\n", "\n").replace("\r", "\n")
    prepared = "".join(
        character
        for character in prepared
        if character in "\n\t\u200d"
        or unicodedata.category(character) not in {"Cc", "Cf"}
    )
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in prepared.split("\n")]
    prepared = "\n".join(lines)
    prepared = re.sub(r"\n{3,}", "\n\n", prepared)
    return prepared.strip()


def _is_emoji(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x1F1E6 <= codepoint <= 0x1F1FF
        or 0x1F300 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or 0x2300 <= codepoint <= 0x23FF
    )


def _emoji_count(text: str) -> int:
    count = 0
    joins_previous = False
    regional_indicator_open = False
    for character in text:
        codepoint = ord(character)
        if character == "\u200d":
            joins_previous = True
            continue
        if 0x1F3FB <= codepoint <= 0x1F3FF or codepoint in {0xFE0E, 0xFE0F, 0x20E3}:
            continue
        if not _is_emoji(character):
            joins_previous = False
            regional_indicator_open = False
            continue
        if 0x1F1E6 <= codepoint <= 0x1F1FF:
            if not regional_indicator_open:
                count += 1
            regional_indicator_open = not regional_indicator_open
        elif joins_previous:
            joins_previous = False
            regional_indicator_open = False
        else:
            count += 1
            regional_indicator_open = False
    return count


def _contains_term(text: str, term: str) -> bool:
    escaped = re.escape(term).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<!\w){escaped}(?!\w)", text, re.IGNORECASE))


def _common_validation_errors(
    text: str,
    brand_voice: Mapping | None,
    *,
    max_length: int,
    reply: bool,
) -> list[str]:
    policy = normalize_brand_voice(brand_voice)
    errors: list[str] = []

    if not isinstance(text, str) or not text.strip():
        return ["text is empty"]
    if text != _prepare_text(text):
        errors.append("text contains unprepared whitespace or control characters")
    if len(text) > max_length:
        errors.append(f"text exceeds {max_length} characters")
    if not any(character.isalnum() for character in text):
        errors.append("text must contain meaningful words")

    emoji_count = _emoji_count(text)
    if emoji_count > policy["max_emojis"]:
        errors.append(f"text exceeds the {policy['max_emojis']} emoji limit")
    hashtag_count = len(_HASHTAG_RE.findall(text))
    if hashtag_count > policy["max_hashtags"]:
        errors.append(f"text exceeds the {policy['max_hashtags']} hashtag limit")

    for term in policy["banned_terms"]:
        if _contains_term(text, term):
            errors.append(f"text contains banned term: {term}")

    allowed = {item.casefold() for item in policy["allowed_abbreviations"]}
    words = [word.casefold() for word in _WORD_RE.findall(text)]
    disallowed = sorted({word for word in words if word in _CASUAL_ABBREVIATIONS and word not in allowed})
    if disallowed:
        errors.append("text contains unapproved abbreviation: " + ", ".join(disallowed))

    if any(pattern.search(text) for pattern in _SPAM_PATTERNS):
        errors.append("text contains a spam or engagement-bait phrase")
    if len(_URL_RE.findall(text)) > (0 if reply else 1):
        errors.append("text contains too many links")
    if len(_MENTION_RE.findall(text)) > (1 if reply else 3):
        errors.append("text contains too many mentions")
    if re.search(r"([^\w\s])\1{3,}", text, re.UNICODE):
        errors.append("text contains repetitive punctuation")
    if re.search(r"\b([^\W_]+)(?:\s+\1){2,}\b", text, re.IGNORECASE | re.UNICODE):
        errors.append("text contains repeated words")

    comparable_words = [word for word in words if len(word) > 1]
    if len(comparable_words) >= 8:
        uniqueness = len(set(comparable_words)) / len(comparable_words)
        if uniqueness < 0.45:
            errors.append("text is excessively repetitive")

    alpha = [character for character in text if character.isalpha()]
    if len(alpha) >= 12 and sum(character.isupper() for character in alpha) / len(alpha) > 0.8:
        errors.append("text uses excessive capitalization")

    if re.search(r"(?<=[a-z0-9.!?])(?=[A-Z][a-z]{2,})", text):
        errors.append("text appears to contain concatenated fragments")
    lines = [line for line in text.splitlines() if line.strip()]
    if (
        len(lines) > 1
        and all(len(_WORD_RE.findall(line)) >= 3 for line in lines)
        and all(re.search(r"[.!?][\"'’)]?$", line) for line in lines)
    ):
        errors.append("text appears to contain independently concatenated captions")

    units = [
        re.sub(r"\W+", " ", unit.casefold()).strip()
        for unit in re.split(r"[.!?]+|\n+", text)
    ]
    units = [unit for unit in units if len(unit) >= 8]
    if len(units) != len(set(units)):
        errors.append("text contains duplicate sentences or lines")

    return errors


def caption_validation_errors(text: str, brand_voice: Mapping | None = None) -> list[str]:
    """Return policy violations for a prospective caption."""
    return _common_validation_errors(text, brand_voice, max_length=500, reply=False)


def reply_validation_errors(text: str, brand_voice: Mapping | None = None) -> list[str]:
    """Return policy violations for a prospective business reply."""
    errors = _common_validation_errors(text, brand_voice, max_length=300, reply=True)
    if isinstance(text, str) and len(_WORD_RE.findall(text)) < 3:
        errors.append("reply is too short to be meaningful")
    return errors


def validate_caption(text: str, brand_voice: Mapping | None = None) -> bool:
    """Return ``True`` only when a caption satisfies the publishing policy."""
    return not caption_validation_errors(text, brand_voice)


def validate_reply(text: str, brand_voice: Mapping | None = None) -> bool:
    """Return ``True`` only when a reply is professional and business-safe."""
    return not reply_validation_errors(text, brand_voice)


def prepare_caption_for_publishing(
    text: str,
    brand_voice: Mapping | None = None,
) -> str:
    """Normalize approved caption formatting and reject policy violations."""
    prepared = _prepare_text(text)
    errors = caption_validation_errors(prepared, brand_voice)
    if errors:
        raise ContentPolicyError("; ".join(errors))
    return prepared


def prepare_reply_for_publishing(
    text: str,
    brand_voice: Mapping | None = None,
) -> str:
    """Normalize an approved reply and reject it unless it remains business-safe."""
    prepared = _prepare_text(text)
    errors = reply_validation_errors(prepared, brand_voice)
    if errors:
        raise ContentPolicyError("; ".join(errors))
    return prepared

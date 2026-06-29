import json
import os
import tempfile
from dotenv import load_dotenv
from content_policy import normalize_brand_voice

load_dotenv()

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UI_CONFIG_FILE = os.path.join(_SCRIPT_DIR, "operations_ui_config.json")
_LEGACY_UI_CONFIG_FILE = os.path.join(_SCRIPT_DIR, "warmer_ui_config.json")


def _load_ui_config() -> dict:
    config_path = UI_CONFIG_FILE
    if not os.path.isfile(config_path) and os.path.isfile(_LEGACY_UI_CONFIG_FILE):
        config_path = _LEGACY_UI_CONFIG_FILE
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{config_path} must contain a JSON object.")
    return data


_UI_CONFIG = _load_ui_config()


def _ui_value(key: str, default=None):
    value = _UI_CONFIG.get(key)
    if value in (None, ""):
        return default
    return value


def _parse_profile_ids(value) -> list[str]:
    if isinstance(value, list):
        return [str(p).strip() for p in value if str(p).strip()]
    if isinstance(value, str):
        return [p.strip() for p in value.replace("\n", ",").split(",") if p.strip()]
    return []


def _parse_active_hours(value, default=(8, 23)) -> tuple[int, int]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            start, end = int(value[0]), int(value[1])
            if 0 <= start <= end <= 23:
                return (start, end)
        except (TypeError, ValueError):
            pass
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
        if len(parts) == 2:
            try:
                start, end = int(parts[0]), int(parts[1])
                if 0 <= start <= end <= 23:
                    return (start, end)
            except ValueError:
                pass
    return default


def _parse_int(value, default: int, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def _parse_float(value, default: float, minimum: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def _parse_probability(value, default: float) -> float:
    return max(0.0, min(1.0, _parse_float(value, default, minimum=0.0)))


def _parse_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if value in (None, ""):
        return default
    return bool(value)

#  Approved content pools and search topics
# POOLS_JSON_PATH can be:
#   1. Set via env var POOLS_JSON_PATH (absolute or relative to CWD)
#   2. Default: pools.json next to this script

_POOLS_PATH = os.getenv(
    "POOLS_JSON_PATH",
    os.path.join(_SCRIPT_DIR, "pools.json"),
)
if not os.path.isfile(_POOLS_PATH):
    raise SystemExit(
        f"Content pools file not found: {_POOLS_PATH}\n"
        "Set POOLS_JSON_PATH in your .env or place pools.json next to the script."
    )

NSTBROWSER_BASE_URL = "http://localhost:8848/api/v2"  # official v2 base endpoint

NSTBROWSER_API_KEY = _ui_value("nstbrowser_api_key", os.getenv("NSTBROWSER_API_KEY"))
if not NSTBROWSER_API_KEY:
    raise SystemExit(
        "NSTBROWSER_API_KEY is not set. Add it in operations_ui_config.json or .env."
    )

PROFILE_IDS = _parse_profile_ids(_ui_value("profile_ids", os.getenv("PROFILE_IDS")))
if not PROFILE_IDS:
    raise SystemExit(
        "PROFILE_IDS is not set. Add profile IDs in operations_ui_config.json or .env."
    )

TARGET_SOCIAL_URL   = _ui_value("target_social_url", os.getenv("TARGET_SOCIAL_URL", "https://www.threads.net"))
PREFLIGHT_SITES_MIN  = 2    # minimum number of pre-flight sites to visit
PREFLIGHT_SITES_MAX  = 4    # maximum number of pre-flight sites to visit
PREFLIGHT_DWELL_MIN  = 18   # minimum seconds on each pre-flight site
PREFLIGHT_DWELL_MAX  = 55   # maximum seconds on each pre-flight site

# Session duration ,  smooth log-normal distribution.
# Real social-media session lengths follow a right-skewed continuous
# distribution (many short sessions, occasional long ones) ,  NOT the
# bimodal uniform draw that creates an artificial 32-40 min gap.
#   mu=2.95, sigma=0.55  â†’  median â‰ˆ 19 min, mean â‰ˆ 22 min
#   Clamped to [5, 80] min so outliers stay reasonable.
SESSION_LOGNORMAL_MU    = 2.95   # ln(minutes) centre
SESSION_LOGNORMAL_SIGMA = 0.55   # ln(minutes) spread
SESSION_CLAMP_MIN       = 5      # hard floor (minutes)
SESSION_CLAMP_MAX       = 80     # hard ceiling (minutes)

BUFFER_MIN_MIN      = 8     # minimum buffer between profiles (minutes)
BUFFER_MAX_MIN      = 25    # maximum buffer between profiles (minutes)
# 15 % chance of an extended mid-run break (20-60 min) after a profile
BUFFER_LONG_PROB    = 0.15
BUFFER_LONG_MIN     = 20    # extended break minimum (minutes)
BUFFER_LONG_MAX     = 60    # extended break maximum (minutes)

# Time-of-day scheduling; profile operations do not run outside these hours.
# (24-hour local time).  Set ACTIVE_HOURS_RANGE = (0, 23) to disable.
ACTIVE_HOURS_RANGE  = _parse_active_hours(
    _ui_value("active_hours", os.getenv("ACTIVE_HOURS_RANGE")),
    default=(8, 23),
)   # only run between the configured local hours
# Simulated inactive day ,  skip the entire run with this probability.
# Models the natural days when a real user simply doesn't open Threads.
INACTIVE_DAY_PROB   = 0     # replaced by daemon scheduler's per-profile day-off logic

# Optional publishing policy. Text is never embellished or rewritten; these
# controls only determine whether approved captions and replies may be used.
BRAND_VOICE = normalize_brand_voice(_ui_value("brand_voice", {}))

# Daily approved-publishing targets per profile.
# The daemon draws one daily target inside these ranges and stores it in
# post_state.json so the plan survives restarts and remains auditable.
DAILY_MEDIA_POSTS_MIN = _parse_int(
    _ui_value("daily_media_posts_min", os.getenv("DAILY_MEDIA_POSTS_MIN")),
    1,
)
DAILY_MEDIA_POSTS_MAX = _parse_int(
    _ui_value("daily_media_posts_max", os.getenv("DAILY_MEDIA_POSTS_MAX")),
    1,
)
if DAILY_MEDIA_POSTS_MAX < DAILY_MEDIA_POSTS_MIN:
    DAILY_MEDIA_POSTS_MAX = DAILY_MEDIA_POSTS_MIN

DAILY_TEXT_POSTS_MIN = _parse_int(
    _ui_value("daily_text_posts_min", os.getenv("DAILY_TEXT_POSTS_MIN")),
    3,
)
DAILY_TEXT_POSTS_MAX = _parse_int(
    _ui_value("daily_text_posts_max", os.getenv("DAILY_TEXT_POSTS_MAX")),
    5,
)
if DAILY_TEXT_POSTS_MAX < DAILY_TEXT_POSTS_MIN:
    DAILY_TEXT_POSTS_MAX = DAILY_TEXT_POSTS_MIN

# Daemon publishing planner. Keep day-off probability at 0.0 for "no sleep days".
DAEMON_DAY_OFF_PROB = _parse_probability(
    _ui_value("daemon_day_off_prob", os.getenv("DAEMON_DAY_OFF_PROB")),
    0.0,
)
DAEMON_ENFORCE_DAILY_PUBLISHING_TARGETS = _parse_bool(
    _ui_value("daemon_enforce_daily_publishing_targets", True),
    True,
)

# Minimum/maximum gap between posts and between same-day daemon publishing
# sessions. These are policy guardrails, not content randomization.
POST_MIN_INTERVAL_MINUTES = _parse_float(
    _ui_value("post_min_interval_minutes", os.getenv("POST_MIN_INTERVAL_MINUTES")),
    90.0,
    minimum=1.0,
)
POST_MAX_INTERVAL_MINUTES = _parse_float(
    _ui_value("post_max_interval_minutes", os.getenv("POST_MAX_INTERVAL_MINUTES")),
    180.0,
    minimum=POST_MIN_INTERVAL_MINUTES,
)
if POST_MAX_INTERVAL_MINUTES < POST_MIN_INTERVAL_MINUTES:
    POST_MAX_INTERVAL_MINUTES = POST_MIN_INTERVAL_MINUTES

DAEMON_PUBLISHING_SESSION_GAP_MIN_MINUTES = _parse_float(
    _ui_value(
        "daemon_publishing_session_gap_min_minutes",
        os.getenv("DAEMON_PUBLISHING_SESSION_GAP_MIN_MINUTES"),
    ),
    90.0,
    minimum=1.0,
)
DAEMON_PUBLISHING_SESSION_GAP_MAX_MINUTES = _parse_float(
    _ui_value(
        "daemon_publishing_session_gap_max_minutes",
        os.getenv("DAEMON_PUBLISHING_SESSION_GAP_MAX_MINUTES"),
    ),
    180.0,
    minimum=DAEMON_PUBLISHING_SESSION_GAP_MIN_MINUTES,
)
if DAEMON_PUBLISHING_SESSION_GAP_MAX_MINUTES < DAEMON_PUBLISHING_SESSION_GAP_MIN_MINUTES:
    DAEMON_PUBLISHING_SESSION_GAP_MAX_MINUTES = DAEMON_PUBLISHING_SESSION_GAP_MIN_MINUTES

#  Content posting  #
# Root folder for profile-approved media paths declared in pools.json.
# Profiles with no approved_media entries post text-only captions.
# Relative paths are resolved against the directory that contains this script
# so the suite works regardless of the working directory it is launched from.
MEDIA_POOL_DIR        = os.path.join(_SCRIPT_DIR, "media")   # e.g. "media_pool"
POST_MEDIA_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

# Temp directory used by _prepare_image_for_profile() to store sanitized
# per-profile image copies.  Cleaned up by the OS between reboots.
_POST_TEMP_DIR = os.path.join(tempfile.gettempdir(), "nstbrowser_post_scratch")


SCREENSHOT_DIR      = os.path.join(_SCRIPT_DIR, "screenshots")
LOG_FILE            = os.path.join(_SCRIPT_DIR, "profile_operations.log")
MOUSE_LOG_FILE      = os.path.join(_SCRIPT_DIR, "mouse_moves.log")  # dedicated cursor movement log
MOUSE_TRACE         = False             # True = log every Bezier step (verbose)
DEBUG_CURSOR_OVERLAY= False             # True = inject red dot overlay to visualise cursor movement

POST_STATE_FILE       = os.path.join(_SCRIPT_DIR, "post_state.json")

ELEMENT_CONFIDENCE_THRESHOLD = 0.45

# Specific URL path segments that indicate a challenge/verification screen.
# Matched against the lowercased URL; these are path prefixes, not substrings
# of page content, so they cannot be accidentally triggered by feed posts.
CHALLENGE_URL_PATHS = [
    "/challenge",
    "/checkpoint",
    "/accounts/suspended",
    "/accounts/disabled",
    "instagram.com/challenge",
    "instagram.com/checkpoint",
]

# Structural DOM selectors that only appear on challenge/verification screens.
# Using form actions and specific input names rather than body text so that
# user-generated content on the feed can never cause a false positive.
CHALLENGE_DOM_SELECTORS = [
    'form[action*="/challenge"]',
    'form[action*="/checkpoint"]',
    'input[name="security_code"]',
    'input[name="verification_code"]',
    'button[name="Choice"][value="0"]',   # "Send security code" button
]

HEARTBEAT_FILE = "heartbeat.json"
_HEARTBEAT_INTERVAL_SEC = 300                    # 5 minutes

#NICHE_ENGAGEMENT_PROB = 0.85   # probability of engaging with on-topic post
#OFFTOPIC_ENGAGEMENT_PROB = 0.08  # probability of engaging with off-topic post

import os
import tempfile
from dotenv import load_dotenv

load_dotenv()

# ── Content pools (comments, captions, search topics) ──────────────────────
# POOLS_JSON_PATH can be:
#   1. Set via env var POOLS_JSON_PATH (absolute or relative to CWD)
#   2. Default: pools.json next to this script

_POOLS_PATH = os.getenv(
    "POOLS_JSON_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "pools.json"),
)
if not os.path.isfile(_POOLS_PATH):
    raise SystemExit(
        f"Content pools file not found: {_POOLS_PATH}\n"
        "Set POOLS_JSON_PATH in your .env or place pools.json next to the script."
    )

# ── Chrome profiles (primary runtime flow) ─────────────────────────────────────
CHROME_PROFILES = [
    {"id": "demo1", "port": 9222, "dir": r"C:\Users\User\AppData\Local\Google\Chrome\User Data\Profile 4"},
    {"id": "demo2", "port": 9223, "dir": r"C:\Users\User\AppData\Local\Google\Chrome\User Data\Profile 5"},
]
_CHROME_PROFILE_IDS = {p["id"] for p in CHROME_PROFILES}

_raw_profiles = os.getenv("PROFILE_IDS", "").strip()
if _raw_profiles:
    _parsed_profiles = [p.strip() for p in _raw_profiles.split(",") if p.strip()]
    PROFILE_IDS = [pid for pid in _parsed_profiles if pid in _CHROME_PROFILE_IDS]
    if not PROFILE_IDS:
        PROFILE_IDS = [p["id"] for p in CHROME_PROFILES]
else:
    PROFILE_IDS = [p["id"] for p in CHROME_PROFILES]

_raw_cookie_profiles = os.getenv("COOKIE_PROFILE_IDS", "").strip()
if _raw_cookie_profiles:
    _parsed_cookie_profiles = [p.strip() for p in _raw_cookie_profiles.split(",") if p.strip()]
    COOKIE_PROFILE_IDS = [pid for pid in _parsed_cookie_profiles if pid in _CHROME_PROFILE_IDS]
    if not COOKIE_PROFILE_IDS:
        COOKIE_PROFILE_IDS = PROFILE_IDS.copy()
else:
    COOKIE_PROFILE_IDS = PROFILE_IDS.copy()

TARGET_SOCIAL_URL   = "https://www.threads.net"       # change to your target
PREFLIGHT_SITES_MIN  = 2    # minimum number of pre-flight sites to visit
PREFLIGHT_SITES_MAX  = 4    # maximum number of pre-flight sites to visit
PREFLIGHT_DWELL_MIN  = 18   # minimum seconds on each pre-flight site
PREFLIGHT_DWELL_MAX  = 55   # maximum seconds on each pre-flight site

# Session duration ,  smooth log-normal distribution.
# Real social-media session lengths follow a right-skewed continuous
# distribution (many short sessions, occasional long ones) ,  NOT the
# bimodal uniform draw that creates a detectable 32–40 min gap.
#   mu=2.95, sigma=0.55  →  median ≈ 19 min, mean ≈ 22 min
#   Clamped to [5, 80] min so outliers stay realistic.
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

# Time-of-day scheduling ,  the warmer will refuse to run outside these hours
# (24-hour local time).  Set ACTIVE_HOURS_RANGE = (0, 23) to disable.
ACTIVE_HOURS_RANGE  = (8, 23)   # only run between 08:00 and 23:00 local time
# Simulated inactive day ,  skip the entire run with this probability.
# Models the natural days when a real user simply doesn't open Threads.
INACTIVE_DAY_PROB   = 0     # replaced by daemon scheduler's per-profile day-off logic

# ── Content posting ────────────────────────────────────────────────────────── #
# Set MEDIA_POOL_DIR to a local folder of images to attach to new posts.
# Leave as None to post text-only captions.
# Relative paths are resolved against the directory that contains this script
# so the bot works regardless of the working directory it is launched from.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_POOL_DIR        = os.path.join(_SCRIPT_DIR, "media")   # e.g. "media_pool"
POST_MEDIA_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

# Temp directory used by _prepare_image_for_profile() to store uniquified
# per-profile image copies.  Cleaned up by the OS between reboots.
_POST_TEMP_DIR = os.path.join(tempfile.gettempdir(), "nstbrowser_post_scratch")


SCREENSHOT_DIR      = os.path.join(_SCRIPT_DIR, "screenshots")
LOG_FILE            = os.path.join(_SCRIPT_DIR, "nstbrowser_warmer.log")
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

# Backward compatibility alias; Chrome profiles are now the primary flow.
DEMO_PROFILES = CHROME_PROFILES

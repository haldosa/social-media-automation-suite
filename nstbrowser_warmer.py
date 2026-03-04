"""
NstBrowser Social Media Account Warmer  (API v2)
=================================================
Target platform : threads.net
API reference   : https://apidocs.nstbrowser.io/

Requirements:
    pip install selenium requests webdriver-manager pyautogui pyperclip Pillow piexif

Setup:
    1. Install & launch the NstBrowser desktop app.
    2. Settings -> API Key -> copy your key.
    3. Fill in NSTBROWSER_API_KEY and PROFILE_IDS below.
    4. Run:  python nstbrowser_warmer.py
"""

import random
import time
import logging
import math
import os
import sys
import ctypes
import glob as _glob
import re
import textwrap
import argparse
import tempfile
import hashlib
import requests
import json
from datetime import datetime, date
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput
from selenium.webdriver.common.actions import interaction
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    WebDriverException,
)

# ------------------------------------------------------------------ #
#  CONFIGURATION
# ------------------------------------------------------------------ #
NSTBROWSER_API_KEY  = "e1862a26-1045-4bbf-b52f-ba0319204dd4"      # Settings -> API Key
NSTBROWSER_BASE_URL = "http://localhost:8848/api/v2"  # official v2 base endpoint

PROFILE_IDS = [
    "251894f1-0abc-4e5b-831c-1d3d594de9aa", "47dc0595-fbf8-4006-9b4e-1b3b055fb57d",
]

TARGET_SOCIAL_URL   = "https://www.threads.net"       # change to your target

# Pre-flight site pool — 2-4 sites are sampled at random each run to vary
# the browsing history seeded before navigating to Threads.
PREFLIGHT_SITES_POOL = [
    "https://www.wikipedia.org",
    "https://www.bbc.com",
    "https://www.reddit.com",
    "https://www.youtube.com",
    "https://www.nytimes.com",
    "https://www.theguardian.com",
    "https://www.espn.com",
    "https://news.ycombinator.com",
]
PREFLIGHT_SITES_MIN  = 2    # minimum number of pre-flight sites to visit
PREFLIGHT_SITES_MAX  = 4    # maximum number of pre-flight sites to visit
PREFLIGHT_DWELL_MIN  = 18   # minimum seconds on each pre-flight site
PREFLIGHT_DWELL_MAX  = 55   # maximum seconds on each pre-flight site

SESSION_MIN_MIN     = 6     # minimum session length (minutes)
SESSION_MAX_MIN     = 32    # maximum session length (minutes)
# 15 % chance of a long session (40-70 min) — models binge days
SESSION_LONG_PROB   = 0.15
SESSION_LONG_MIN    = 40    # long-session minimum (minutes)
SESSION_LONG_MAX    = 70    # long-session maximum (minutes)

BUFFER_MIN_MIN      = 8     # minimum buffer between profiles (minutes)
BUFFER_MAX_MIN      = 25    # maximum buffer between profiles (minutes)
# 15 % chance of an extended mid-run break (20-60 min) after a profile
BUFFER_LONG_PROB    = 0.15
BUFFER_LONG_MIN     = 20    # extended break minimum (minutes)
BUFFER_LONG_MAX     = 60    # extended break maximum (minutes)

# Time-of-day scheduling — the warmer will refuse to run outside these hours
# (24-hour local time).  Set ACTIVE_HOURS_RANGE = (0, 23) to disable.
ACTIVE_HOURS_RANGE  = (8, 23)   # only run between 08:00 and 23:00 local time
# Simulated inactive day — skip the entire run with this probability.
# Models the natural days when a real user simply doesn't open Threads.
INACTIVE_DAY_PROB   = 0.18
# Comment pool — short, natural-sounding replies that fit a wide range of posts.
# Add / remove entries to tune the vocabulary used by the bot.
COMMENT_POOL = [
    "Love this",
    "So true",
    "This is great!",
    "Exactly how I feel",
    "Needed to see this today",
    "Facts",
    "Well said",
    "This made my day",
    "Couldn't agree more",
    "Really interesting perspective",
    "This is so good",
    "Haha yes exactly",
    "Okay this is actually really good",
    "lol same",
    "Absolutely",
    "More people need to see this",
    "Love the energy here",
    "This is everything",
    "Amazing",
    "Yes!!",
]

# ── Content posting ────────────────────────────────────────────────────────── #
# Set MEDIA_POOL_DIR to a local folder of images to attach to new posts.
# Leave as None to post text-only captions.
MEDIA_POOL_DIR        = "media"                               # e.g. "media_pool"
POST_MEDIA_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

# Captions for original posts.  Add / remove entries freely.
POST_CAPTION_POOL = [
    "Little moments, big feelings.",
    "Day well spent.",
    "Grateful for today.",
    "Just sharing what\u2019s on my mind.",
    "Sometimes less really is more.",
    "Finding beauty in the ordinary.",
    "Small wins count too.",
    "This made me smile \u2014 sharing it.",
    "Quiet day, loud thoughts.",
    "Not everything needs a caption, but here we are.",
]

# Path to the persistent posting-state JSON (per-profile daily counts + age).
POST_STATE_FILE       = "post_state.json"

# Minimum elapsed session time (seconds) before a post action is allowed.
# Ensures the bot scrolls/reads for a meaningful passive phase first.
POST_PASSIVE_PHASE_SEC = 0 #random.uniform(5 * 60, 10 * 60)   # 5–10 min, re-drawn each run

# Absolute floor between any two posts per profile (seconds).
# Poisson sampling can occasionally produce very short gaps; this guards them.
POST_MIN_GAP_SEC = 4 * 3600   # 4 hours hard floor

# Caption variation helpers — used by _humanize_caption().
# Emoji-only tier: one of these is posted as the entire caption (~5 % of posts).
POST_CAPTION_EMOJIS = [
    "✨", "🌿", "🌅", "☀️", "🍃", "💫", "🌙", "🫶", "🤍", "🌊", "🌸", "🎵", "🫧", "🍂",
]
# Short-fragment tier: casual 1-3 word phrases (~10 % of posts).
POST_CAPTION_SHORTS = [
    "honestly", "same tho", "no notes", "vibes only", "felt that",
    "we outside", "period", "big mood", "ok yeah", "real ones know",
    "not me crying", "still thinking about this",
]

# Temp directory used by _prepare_image_for_profile() to store uniquified
# per-profile image copies.  Cleaned up by the OS between reboots.
_POST_TEMP_DIR = os.path.join(tempfile.gettempdir(), "nstbrowser_post_scratch")

SCREENSHOT_DIR      = "screenshots"
LOG_FILE            = "nstbrowser_warmer.log"
MOUSE_LOG_FILE      = "mouse_moves.log"  # dedicated cursor movement log
MOUSE_TRACE         = False             # True = log every Bezier step (verbose)
DEBUG_CURSOR_OVERLAY= True             # True = inject red dot overlay to visualise cursor movement

# ── Selector constants ─────────────────────────────────────────────────────── #
# Profile link in post header — href="/@username"
FEED_PROFILE_LINK  = 'a[href^="/@"][role="link"]'
# Small + icon follow button (SVG aria-label="Follow")
QUICK_FOLLOW_BTN   = 'div[role="button"]:has(svg[aria-label="Follow"])'
# XPath for text-based Follow button (feed inline, profile page, cards)
FOLLOW_BTN_XPATH   = '//div[@role="button" and .//div[normalize-space(text())="Follow"]]'
# X button on suggested cards
DISMISS_CARD_BTN   = 'div[role="button"]:has(svg[aria-label="Close"])'
# Reply (comment) button in each post’s action bar
REPLY_BTN_CSS      = 'div[role="button"]:has(svg[aria-label="Reply"])'
# Contenteditable reply box that appears after clicking Reply
COMMENT_BOX_CSS    = 'div[contenteditable="true"][role="textbox"]'
# Post button that submits the comment (XPath — scoped to role=button wrapping text “Post”)
COMMENT_POST_XPATH = '//div[@role="button" and .//div[normalize-space(text())="Post"]]'
# Hidden file-upload input inside the compose modal
COMPOSE_FILE_INPUT_CSS = 'input[type="file"][accept]'
# Compose / New-post button in the nav sidebar (aria-label="Create")
COMPOSE_BTN_SELECTORS = [
    ("css", 'div[role="button"]:has(svg[aria-label="Create"])'),
    ("css", 'a[role="link"]:has(svg[aria-label="Create"])'),
    ("css", 'div[role="button"][aria-label="Create"]'),
    ("xpath", '//div[@role="button" and .//*[local-name()="svg"][@aria-label="Create"]]'),
]
# Compose modal textbox (new-post box, not the comment/reply box)
COMPOSE_TEXTBOX_CSS = 'div[data-lexical-editor="true"][contenteditable="true"]'
# "Attach media" button inside the compose modal
COMPOSE_ATTACH_BTN_CSS = 'div[role="button"]:has(svg[aria-label="Attach media"])'
# ─────────────────────────────────────────────────────────────────────────────#

# ------------------------------------------------------------------ #

os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# UTF-8 safe logging — prevents cp1252 crash on Windows terminals
_fmt      = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")
_file_h   = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_h.setFormatter(_fmt)
_stream_h = logging.StreamHandler()
_stream_h.setFormatter(_fmt)
if hasattr(_stream_h.stream, "reconfigure"):
    try:
        _stream_h.stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
logging.basicConfig(level=logging.INFO, handlers=[_file_h, _stream_h])
log = logging.getLogger(__name__)

# Dedicated mouse-movement logger — writes to its own file at DEBUG level.
# Arc summaries are always written; per-step positions only when MOUSE_TRACE=True.
_mouse_fh = logging.FileHandler(MOUSE_LOG_FILE, encoding="utf-8")
_mouse_fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
_mlog = logging.getLogger("mouse")
_mlog.setLevel(logging.DEBUG)
_mlog.addHandler(_mouse_fh)
_mlog.propagate = False  # keep mouse events out of the main log


# ================================================================== #
#  HIGH-PRECISION TIMER  (Windows 15.625 ms fix)
# ================================================================== #
# Windows default timer resolution is ~15.6 ms.  All short sleeps
# (keystrokes, mouse steps, scroll ticks) snap to this grid, creating
# a detectable 64 Hz spike in inter-event timestamps.  We raise the
# resolution to 1 ms at startup and use a busy-wait tail for sub-ms
# precision.

if sys.platform == "win32":
    try:
        _winmm = ctypes.windll.winmm
        _winmm.timeBeginPeriod(1)
        import atexit as _atexit
        _atexit.register(_winmm.timeEndPeriod, 1)
        log.info("Windows timer resolution raised to 1 ms (timeBeginPeriod)")
    except Exception:
        log.debug("Could not raise Windows timer resolution")


def precise_sleep(seconds: float) -> None:
    """High-precision sleep: kernel sleep for bulk, busy-wait for the tail.

    On Windows with timeBeginPeriod(1), kernel sleep resolution is ~1 ms.
    The last 2 ms are consumed by a busy-wait loop on perf_counter to
    eliminate jitter from the kernel scheduler.
    """
    if seconds <= 0:
        return
    if seconds <= 0.002:
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            pass
        return
    end = time.perf_counter() + seconds
    kernel_sleep = seconds - 0.002
    if kernel_sleep > 0:
        time.sleep(kernel_sleep)
    while time.perf_counter() < end:
        pass


# ================================================================== #
#  CHROMEDRIVER $cdc_ VARIABLE DEFENCE
# ================================================================== #
# ChromeDriver injects $cdc_asdjflasutopfhvcZLmcfl_ (or similarly named)
# properties into every document context.  Meta's JS can enumerate
# document properties and detect these.  Multi-layered defence:
#   Layer 1 — Pre-page JS mask (Page.addScriptToEvaluateOnNewDocument)
#   Layer 2 — Binary-patch ChromeDriver (build-time, cached)
#   Layer 3 — Runtime verification

_CDC_MASK_JS = """
(function() {
    const re = /\\$[a-z]dc_/;
    const names = Object.getOwnPropertyNames(document);
    for (const p of names) {
        if (re.test(p)) {
            delete document[p];
            Object.defineProperty(document, p, {
                get: function() { return undefined; },
                configurable: false,
            });
        }
    }
})();
"""


def _patch_chromedriver_binary(path: str) -> str:
    """Binary-patch ChromeDriver to replace $cdc_ variable name.

    Replaces all occurrences of the $cdc_ diagnostic property
    with a benign string of equal length.  The patch is idempotent —
    a .patched marker file prevents re-patching on subsequent runs.
    """
    patched_marker = path + ".patched"
    if os.path.exists(patched_marker):
        return path
    try:
        with open(path, "rb") as f:
            data = f.read()
        pattern = re.compile(rb'\$cdc_[a-zA-Z0-9]{22}_')
        matches = list(pattern.finditer(data))
        if not matches:
            log.info("ChromeDriver binary: no $cdc_ pattern found (already clean or new version)")
            open(patched_marker, "w").close()
            return path
        for m in reversed(matches):
            replacement = b'$xxx_' + b'a' * (len(m.group()) - 5)
            data = data[:m.start()] + replacement + data[m.end():]
        with open(path, "wb") as f:
            f.write(data)
        open(patched_marker, "w").close()
        log.info("ChromeDriver binary patched: %d $cdc_ occurrence(s) replaced", len(matches))
    except PermissionError:
        log.warning("ChromeDriver binary patch failed: permission denied (file in use?)")
    except Exception as exc:
        log.warning("ChromeDriver binary patch failed: %s", exc)
    return path


# ================================================================== #
#  NSTBROWSER API v2
# ================================================================== #

def _headers() -> dict:
    return {"x-api-key": NSTBROWSER_API_KEY}


def start_profile(profile_id: str) -> dict:
    """
    POST /api/v2/browsers/{profileId}
    No body, no Content-Type — exactly as the official curl example shows.
    Returns data dict containing webSocketDebuggerUrl and port.
    """
    url  = f"{NSTBROWSER_BASE_URL}/browsers/{profile_id}"
    resp = requests.post(url, headers=_headers(), timeout=60)
    if not resp.ok:
        log.error("start_profile HTTP %s — %s", resp.status_code, resp.text[:300])
    resp.raise_for_status()
    body = resp.json()
    if body.get("err") is True or body.get("code") not in (0, 200):
        raise RuntimeError(f"NstBrowser start error for {profile_id}: {body}")
    data = body["data"]
    log.info("Profile %s launched  |  port=%s  |  ws=%s",
             profile_id, data.get("port"), data.get("webSocketDebuggerUrl"))
    return data


def stop_profile(profile_id: str) -> None:
    """DELETE /api/v2/browsers/{profileId}"""
    url = f"{NSTBROWSER_BASE_URL}/browsers/{profile_id}"
    try:
        resp = requests.delete(url, headers=_headers(), timeout=15)
        body = resp.json()
        if body.get("err") is False or body.get("code") in (0, 200):
            log.info("Profile %s closed cleanly.", profile_id)
        else:
            log.warning("Unexpected close response for %s: %s", profile_id, body)
    except Exception as exc:
        log.error("Error closing profile %s: %s", profile_id, exc)


def get_running_browsers() -> list:
    """GET /api/v2/browsers — lists all running browser instances."""
    url = f"{NSTBROWSER_BASE_URL}/browsers"
    try:
        resp = requests.get(url, headers=_headers(), timeout=10)
        resp.raise_for_status()
        return resp.json().get("data") or []
    except Exception as exc:
        log.debug("Could not fetch running browsers: %s", exc)
        return []


# ================================================================== #
#  CHROMEDRIVER RESOLUTION
# ================================================================== #

def _get_browser_major_version(ws_url: str) -> int:
    """Query /json/version on the running browser to get the Chrome major version."""
    host_port = ws_url.replace("ws://", "").split("/")[0]
    try:
        resp = requests.get(f"http://{host_port}/json/version", timeout=5)
        m = re.search(r"/(\d+)", resp.json().get("Browser", ""))
        if m:
            return int(m.group(1))
    except Exception as exc:
        log.debug("Could not read browser version: %s", exc)
    return 0


def _get_chromedriver_path(major: int) -> str:
    """
    Resolve the correct chromedriver in priority order:
      1. NstBrowser's own bundled chromedriver (guaranteed version match)
      2. webdriver-manager auto-download
      3. System PATH fallback
    """
    # 1. Bundled
    search_roots = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\NstBrowser\resources\app\browser"),
        os.path.expandvars(r"%PROGRAMFILES%\NstBrowser\resources\app\browser"),
        os.path.expanduser("~/Library/Application Support/NstBrowser/browser"),
        "/Applications/NstBrowser.app/Contents/Resources/app/browser",
        os.path.expanduser("~/.config/NstBrowser/browser"),
        "/opt/NstBrowser/resources/app/browser",
    ]
    for root in search_roots:
        for pat in (os.path.join(root, "**", "chromedriver.exe"),
                    os.path.join(root, "**", "chromedriver")):
            hits = _glob.glob(pat, recursive=True)
            if hits:
                log.info("Using bundled chromedriver: %s", hits[0])
                return _patch_chromedriver_binary(hits[0])

    # 2. webdriver-manager
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        path = ChromeDriverManager(driver_version=str(major)).install()
        log.info("webdriver-manager chromedriver: %s", path)
        return _patch_chromedriver_binary(path)
    except ImportError:
        log.warning("webdriver-manager not installed — run: pip install webdriver-manager")
    except Exception as e:
        log.warning("webdriver-manager failed (%s) — falling back to PATH", e)

    # 3. PATH
    log.warning("Using system chromedriver from PATH (may fail if version mismatches).")
    return "chromedriver"


def connect_selenium(ws_debugger_url: str) -> webdriver.Chrome:
    """
    Attach Selenium to the already-running NstBrowser Orbita browser via CDP.

    When attaching via debuggerAddress the ONLY valid ChromeOption is
    debuggerAddress — all launch-time flags are rejected by ChromeDriver
    because the browser process is already running.
    """
    address = ws_debugger_url.replace("ws://", "").split("/")[0]
    log.info("Connecting Selenium -> debuggerAddress: %s", address)

    major = _get_browser_major_version(ws_debugger_url)
    log.info("Detected Orbita major version: %s", major or "unknown")

    options = webdriver.ChromeOptions()
    options.add_experimental_option("debuggerAddress", address)

    driver = webdriver.Chrome(
        service=Service(executable_path=_get_chromedriver_path(major)),
        options=options,
    )

    # NstBrowser's Orbita engine handles navigator.webdriver and fingerprint
    # spoofing at a lower level than CDP scripts.  Injecting our own
    # Page.addScriptToEvaluateOnNewDocument patch creates a detectable
    # inconsistency (the characteristic getOwnPropertyDescriptor side-effects
    # that Meta's bot detection explicitly checks for), so we omit it entirely
    # and trust the profile's built-in fingerprint configuration.
    driver.execute_cdp_cmd("Network.enable", {})

    # Layer 1 — Pre-page JS mask for $cdc_ ChromeDriver variable.
    # This runs before every page load to intercept the variable injection.
    # Unlike navigator.webdriver patching (handled by Orbita), this targets
    # ChromeDriver's diagnostic property which Orbita does not mask.
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": _CDC_MASK_JS,
        })
    except WebDriverException as exc:
        log.debug("$cdc_ pre-page mask injection failed: %s", exc)

    # Layer 3 — Runtime verification on the current page context.
    try:
        _cdc_found = driver.execute_script(
            "return Object.getOwnPropertyNames(document)"
            ".filter(function(p){return /\\$[a-z]dc_/.test(p)});"
        )
        if _cdc_found:
            log.warning("$cdc_ variables still present — force-removing: %s", _cdc_found)
            driver.execute_script(_CDC_MASK_JS)
        else:
            log.info("$cdc_ mask verified: no ChromeDriver variables detected")
    except WebDriverException:
        pass

    log.info("Selenium attached successfully.")
    return driver


# ================================================================== #
#  HUMAN-LIKE INTERACTION PRIMITIVES
# ================================================================== #

# Bigram pairs that are naturally slow for most touch-typists — awkward
# hand transitions that produce longer inter-key intervals in corpus data.
_SLOW_BIGRAMS = {
    'qu', 'wr', 'xc', 'zx', 'bv', 'vb', 'pq', 'yw', 'wq', 'xz',
}

def human_type(element, text: str, driver=None) -> None:
    """
    Type text with a realistic keystroke timing model.

    Timing model:
    - Base delay: log-normal centred ~80 ms (matches corpus inter-key data).
      Most keystrokes land in 40-120 ms; occasional slow ones up to ~600 ms.
    - Slow bigrams (wr, qu, xc …): 1.4–2× longer due to awkward hand transitions.
    - Word boundaries (space): +50–180 ms micro-pause.
    - Post-sentence punctuation (.!?): +200–600 ms re-reading pause.
    - Rare mid-word hesitation (thinking of next word): +300–800 ms, ~4 % chance.
    - Burst pattern: 3–7 characters are typed in rapid succession, then a
      brief burst-gap (60–200 ms extra) before the next burst begins — matching
      the way humans type in phrases rather than character-by-character.

    When driver is supplied the focus click is dispatched via CDP at the
    current _cursor_pos (where bezier_move left the cursor), so the click
    lands at the natural offset rather than snapping to the element centre.
    """
    if driver is not None:
        # CDP click at wherever the bezier arc landed — no centre-snap.
        _cdp_click(driver)
    else:
        element.click()   # fallback when driver is unavailable
    precise_sleep(random.uniform(0.08, 0.25))   # focus-settle after click
    prev      = ''
    word_len  = 0
    burst_rem = random.randint(3, 7)          # characters left in current burst

    for char in text:
        # Base: log-normal centred around 80 ms, clamped 40–600 ms
        base = random.lognormvariate(math.log(0.08), 0.4)
        base = max(0.04, min(base, 0.60))

        # Slow bigram penalty
        if (prev + char).lower() in _SLOW_BIGRAMS:
            base *= random.uniform(1.4, 2.0)

        # Word boundary
        if char == ' ':
            base += random.uniform(0.05, 0.18)
            word_len = 0
        else:
            word_len += 1

        # Post-sentence punctuation re-reading pause
        if prev in '.!?':
            base += random.uniform(0.20, 0.60)

        # Rare mid-word hesitation
        if word_len > 4 and random.random() < 0.04:
            base += random.uniform(0.30, 0.80)

        # Burst gap: extra pause at end of each burst
        burst_rem -= 1
        if burst_rem <= 0:
            base += random.uniform(0.06, 0.20)
            burst_rem = random.randint(3, 7)

        element.send_keys(char)
        precise_sleep(base)
        prev = char


def _bezier_point(p0, p1, p2, t):
    """Quadratic Bezier interpolation between three 2-D control points."""
    x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
    y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
    return int(x), int(y)


def _ease_in_out_sine(t: float) -> float:
    """Ease-in-out sine: maps a linear 0→1 parameter to an S-curve position.
    Produces slow acceleration at the start of the arc, peak speed at the
    midpoint, and deceleration back to near-zero as the cursor arrives at the
    target — matching Fitts's Law and real human mouse-movement profiles."""
    return -(math.cos(math.pi * t) - 1) / 2


# Persistent cursor state — updated after every bezier_move and on each fresh
# page load (via init_cursor_pos).  Using a tracked position means no movement
# ever starts from a hard-coded corner; every arc begins from wherever the
# cursor realistically last rested.
_cursor_pos: list = [0, 0]


def _set_cursor(x: int, y: int, tag: str = "") -> None:
    """
    Update _cursor_pos and emit a compact one-line position log to BOTH the
    dedicated mouse-movement file (_mlog) AND the main console (log.info),
    so every cursor coordinate change is visible in the live run output.

    Low-level arc detail (ARC / STEP lines) continues to go only to _mlog.
    This function covers the final settled position after each move.
    """
    global _cursor_pos
    _cursor_pos[0], _cursor_pos[1] = x, y
    label = f"  [{tag}]" if tag else ""
    _mlog.debug("CURSOR  (%d, %d)%s", x, y, label)
    log.info("cursor  (%d, %d)%s", x, y, label)


# Profiles interacted with (followed or visited) during this session.
# Cleared at the start of each run_social_session call so the same person
# is never followed / visited twice in one session.
_session_followed: set = set()


# ------------------------------------------------------------------ #
#  DEBUG CURSOR OVERLAY
# ------------------------------------------------------------------ #
# Injects a visible red dot + coordinate label into the live browser page.
# Responds to real DOM mousemove events fired by Selenium’s ActionChains,
# so it follows every bezier step in real time.
# Injected via execute_script after each page load — safe, no fingerprint risk.

_CURSOR_OVERLAY_JS = """
(function () {
    var ID  = '__cursor_debug_dot';
    var LID = '__cursor_debug_lbl';
    // Remove stale instances from previous injection on same page
    var old = document.getElementById(ID);  if (old) old.remove();
    var olL = document.getElementById(LID); if (olL) olL.remove();

    var dot = document.createElement('div');
    dot.id  = ID;
    dot.style.cssText = [
        'position:fixed', 'top:0', 'left:0',
        'width:14px', 'height:14px',
        'background:rgba(255,40,40,0.88)',
        'border:2px solid #fff',
        'border-radius:50%',
        'pointer-events:none',
        'z-index:2147483647',
        'transform:translate(-50%,-50%)',
        'box-shadow:0 0 5px rgba(0,0,0,0.6)',
    ].join(';');

    var lbl = document.createElement('div');
    lbl.id  = LID;
    lbl.style.cssText = [
        'position:fixed', 'top:0', 'left:0',
        'background:rgba(0,0,0,0.72)',
        'color:#0ff',
        'font:bold 10px/1 monospace',
        'padding:2px 5px',
        'border-radius:3px',
        'pointer-events:none',
        'z-index:2147483647',
        'white-space:nowrap',
    ].join(';');

    // Append to <html> not <body> so React reconciler cannot wipe the nodes.
    document.documentElement.appendChild(dot);
    document.documentElement.appendChild(lbl);

    document.addEventListener('mousemove', function (e) {
        var x = e.clientX, y = e.clientY;
        dot.style.left = x + 'px';
        dot.style.top  = y + 'px';
        lbl.style.left = (x + 14) + 'px';
        lbl.style.top  = (y -  8) + 'px';
        lbl.textContent = x + ', ' + y;
    }, true);

    var found = document.getElementById(ID);
    return {
        appended: found !== null,
        bodyChildCount: document.body ? document.body.children.length : -1,
        cspMeta: (document.querySelector('meta[http-equiv="Content-Security-Policy"]') || {}).content || 'none'
    };
})();
"""

def inject_cursor_overlay(driver) -> None:
    """Inject the visual cursor overlay into the current page.
    Cursor placement is now managed by navigate_to / navigate_history;
    this function only handles the visual debug dot."""
    if not DEBUG_CURSOR_OVERLAY:
        return
    try:
        # Brief settle so SPA hydration finishes before we inject.
        time.sleep(0.4)
        result = driver.execute_script(_CURSOR_OVERLAY_JS)
        if result:
            log.info("Overlay inject: appended=%s  bodyChildren=%s  csp=%s",
                     result.get('appended'), result.get('bodyChildCount'),
                     (result.get('cspMeta') or 'none')[:120])
    except WebDriverException as exc:
        log.debug("Cursor overlay injection failed: %s", exc)

# ------------------------------------------------------------------ #
#  SHARED BÉZIER PATH ENGINE
# ------------------------------------------------------------------ #
# JS that replays a pre-computed path as DOM mousemove events at the
# per-step delay computed in Python.  One execute_script call fires the
# entire arc; Python sleeps while JS runs — no per-step round-trips.
_BEZIER_DISPATCH_JS = """
(function(pts, delays) {
    var i = 0;
    function tick() {
        if (i >= pts.length) return;
        var p = pts[i]; var d = delays[i]; i++;
        document.dispatchEvent(new MouseEvent('mousemove', {
            clientX: p[0], clientY: p[1],
            bubbles: true, cancelable: true, view: window
        }));
        setTimeout(tick, d);
    }
    tick();
})(arguments[0], arguments[1]);
"""

def debug_cursor_state(driver, label: str = "") -> None:
    """Log both the Python-tracked position and the overlay's actual DOM position.

    Compares _cursor_pos (Python state) with the visual debug dot's style.left/top
    to detect drift between the two cursor systems.
    """
    try:
        dom_pos = driver.execute_script("""
            var dot = document.getElementById('__cursor_debug_dot');
            if (!dot) return {x: -1, y: -1, exists: false};
            return {
                x: parseInt(dot.style.left) || 0,
                y: parseInt(dot.style.top)  || 0,
                exists: true
            };
        """)
        log.info("CURSOR SYNC CHECK [%s]  python=(%d,%d)  dom=(%d,%d)  overlay_exists=%s",
                 label,
                 _cursor_pos[0], _cursor_pos[1],
                 dom_pos['x'], dom_pos['y'],
                 dom_pos['exists'])
        if dom_pos['exists']:
            drift = math.hypot(dom_pos['x'] - _cursor_pos[0],
                               dom_pos['y'] - _cursor_pos[1])
            if drift > 15:
                log.warning("CURSOR DRIFT  %.1fpx between Python state and overlay DOM", drift)
    except Exception as e:
        log.debug("debug_cursor_state failed: %s", e)

def _fire_bezier_arc(
    driver,
    x0: int, y0: int, x1: int, y1: int,
    vw: int, vh: int,
    *,
    exact_end: bool = False,
) -> tuple:
    """
    Build a randomised quadratic Bézier path from (x0,y0) to (x1,y1),
    dispatch it as JS mousemove events at ~60 fps, then sleep for the full
    arc duration.  Returns (points, delays) for any post-arc work by the
    caller (e.g. snap-gap logging in bezier_move).

    Control-point strategy
    ----------------------
    The cp is always offset perpendicular to the travel direction so the
    curve has genuine curvature even on vertical or horizontal arcs.
    • 25 % of arcs use a large lateral deviation (more visible sweep).
    • 35 % let the cp bulge outside the viewport bbox — real paths often
      arc beyond the straight-line trajectory on diagonal moves.
    • The sign is flipped when the chosen direction would be eaten by the
      viewport edge, guaranteeing a real perpendicular offset.

    Tremor model
    ------------
    Two-factor: velocity × distance.
    • velocity bell (sin π·t): tremor is low at mid-arc (fast movement)
      and rises at endpoints.  The approach phase (t > 0.80) adds extra
      corrective wobble, matching Fitts's Law biomechanics.
    • dist_scale: short arcs get proportionally less absolute tremor.
    The last step is not tremored:
      exact_end=True  → forced to exactly (x1, y1) (coord-based arcs).
      exact_end=False → bare Bézier point used (bezier_move, where the
                        Phase 2 ActionChains snap corrects the position).
    """
    _arc_dist = math.hypot(x1 - x0, y1 - y0)
    _mid_x    = (x0 + x1) / 2.0
    _mid_y    = (y0 + y1) / 2.0
    _perp_x   = -(y1 - y0) / _arc_dist
    _perp_y   =  (x1 - x0) / _arc_dist
    min_cp_offset = max(20, int(_arc_dist * 0.15))
    if random.random() < 0.25:
        # 25 % excursion: large lateral deviation for a visible curve
        lateral = random.randint(30, 80) * random.choice([-1, 1])
    else:
        lateral = random.randint(
            min_cp_offset,
            max(min_cp_offset + 10, int(_arc_dist * 0.25)),
        ) * random.choice([-1, 1])
    # Flip sign if chosen lateral direction gets eaten by viewport clamp.
    _cp_x_trial   = int(_mid_x + _perp_x * lateral)
    _cp_y_trial   = int(_mid_y + _perp_y * lateral)
    _cp_x_clamped = max(0, min(_cp_x_trial, int(vw)))
    _cp_y_clamped = max(0, min(_cp_y_trial, int(vh)))
    if abs(_cp_x_clamped - int(_mid_x)) + abs(_cp_y_clamped - int(_mid_y)) < min_cp_offset:
        lateral = -lateral
    # 35 % of arcs: let the cp bulge outside the viewport bounding box.
    if random.random() < 0.35:
        extra = random.uniform(0.20, 0.40) * _arc_dist * random.choice([-1, 1])
        cp = (int(_mid_x + _perp_x * (lateral + extra)),
              int(_mid_y + _perp_y * (lateral + extra)))
    else:
        cp = (
            max(0, min(int(_mid_x + _perp_x * lateral), int(vw))),
            max(0, min(int(_mid_y + _perp_y * lateral), int(vh))),
        )
    steps      = max(20, min(90, int(_arc_dist / 3.5)))  # ~3.5 px/step; clamp 20-90
    step_ms    = random.uniform(12.0, 22.0)
    points     = []
    delays     = []
    prev       = (x0, y0)
    dist_scale = max(0.30, min(_arc_dist / 500.0, 1.0))
    drift_x    = 0.0
    drift_y    = 0.0
    for i in range(1, steps + 1):
        t_raw  = i / steps
        t      = _ease_in_out_sine(t_raw)
        nx, ny = _bezier_point((x0, y0), cp, (x1, y1), t)
        if i < steps:
            # Two-factor tremor: velocity bell × distance scale.
            # Approach phase (t > 0.80) ramps up corrective wobble.
            velocity  = math.sin(math.pi * t_raw)
            approach  = max(0.0, (t_raw - 0.80) / 0.20) if t_raw > 0.80 else 0.0
            # Bleed-out: linearly reduce tremor and drift over the last few
            # steps so the arc quiets gracefully before the final snap,
            # avoiding a hard velocity discontinuity at landing.
            bleed_steps    = max(1, min(3, steps // 4))
            steps_from_end = steps - i
            bleed_factor   = (
                steps_from_end / (bleed_steps + 1)
                if steps_from_end <= bleed_steps else 1.0
            )
            tremor_sd = (1.2 * (1.0 - velocity * 0.55) + approach * 1.2) * dist_scale * bleed_factor
            nx = int(nx + random.gauss(0, tremor_sd))
            ny = int(ny + random.gauss(0, tremor_sd * 0.75))
            # Low-frequency drift: correlated wrist/arm oscillation.
            drift_x = drift_x * 0.88 + random.gauss(0, 0.55 * dist_scale * bleed_factor)
            drift_y = drift_y * 0.88 + random.gauss(0, 0.40 * dist_scale * bleed_factor)
            drift_cap = max(1.0, 4.0 * dist_scale)
            drift_x = max(-drift_cap, min(drift_x, drift_cap))
            drift_y = max(-drift_cap, min(drift_y, drift_cap))
            nx = max(0, min(int(nx + drift_x), int(vw) - 1))
            ny = max(0, min(int(ny + drift_y), int(vh) - 1))
        elif exact_end:
            nx, ny = x1, y1   # force exact landing for coord-based arcs
        dx, dy = nx - prev[0], ny - prev[1]
        points.append([nx, ny, dx, dy])
        prev = (nx, ny)
        vel  = math.sin(math.pi * t_raw)
        d_ms = step_ms * (1.5 - vel * 0.7) + random.gauss(0, 2.5)
        delays.append(max(8.0, d_ms))
    # Cumulative step-fire times for STEP log annotation.
    cum_ms     = 0.0
    step_times = []
    for d in delays:
        step_times.append(cum_ms)
        cum_ms += d
    _mlog.debug(
        "ARC  from=(%d,%d)  cp=(%d,%d)  to=(%d,%d)  steps=%d  ms/step=%.1f  dur=%.0fms",
        x0, y0, cp[0], cp[1], x1, y1, steps, step_ms, cum_ms,
    )
    if MOUSE_TRACE:
        for i, ((nx, ny, dx, dy), t_ms) in enumerate(zip(points, step_times), 1):
            _mlog.debug("STEP  i=%02d  t=+%.0fms  pos=(%d,%d)  delta=(%+d,%+d)",
                        i, t_ms, nx, ny, dx, dy)
    # Dispatch via CDP Input.dispatchMouseEvent — produces isTrusted:true
    # events with the full pointermove → mousemove chain that real input
    # produces.  Per-step round-trips are ~1 ms over localhost, well within
    # the 8–22 ms inter-step budget.
    for pt, d_ms in zip(points, delays):
        try:
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": pt[0],
                "y": pt[1],
            })
        except WebDriverException:
            pass
        precise_sleep(d_ms / 1000.0)
    return points, delays


def _cdp_click(driver, x: int = None, y: int = None) -> None:
    """Dispatch a trusted click via CDP Input.dispatchMouseEvent.

    If x, y are omitted, clicks at the current _cursor_pos.
    Produces mousePressed + mouseReleased with a realistic inter-event
    gap drawn from a human-like distribution.
    """
    cx = x if x is not None else _cursor_pos[0]
    cy = y if y is not None else _cursor_pos[1]
    driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
        "type": "mousePressed",
        "x": cx, "y": cy,
        "button": "left",
        "clickCount": 1,
    })
    precise_sleep(random.uniform(0.04, 0.11))
    driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
        "type": "mouseReleased",
        "x": cx, "y": cy,
        "button": "left",
        "clickCount": 1,
    })

def init_cursor_pos(driver) -> None:
    """
    Silently set _cursor_pos to a random position within the current viewport.

    No DOM event is dispatched — a single-step jump from (0,0) to a random
    coordinate is a detectable bot signal.  The first real cursor event the
    page sees will be the drift arc from _navigate_and_settle or the first
    bezier_move call, both of which start from this seeded position.
    """
    global _cursor_pos
    try:
        vw = driver.execute_script("return window.innerWidth")
        vh = driver.execute_script("return window.innerHeight")
        x = random.randint(int(vw * 0.10), int(vw * 0.90))
        y = random.randint(int(vh * 0.15), int(vh * 0.85))
        _mlog.debug("INIT  vp=(%dx%d)", vw, vh)
        _set_cursor(x, y, "init")
    except WebDriverException as exc:
        log.debug("init_cursor_pos failed: %s", exc)

def bezier_move(driver, target_element) -> None:
    """
    Move the mouse to target_element along a randomised quadratic Bezier curve
    at ~60 fps using CDP Input.dispatchMouseEvent for trusted events.

    All Bezier points are pre-computed in Python and dispatched via CDP,
    producing isTrusted:true mouseMoved events with the full
    pointermove → mousemove chain.

    Cursor continuity: _cursor_pos is used as the start point and updated
    after each call so every arc begins from where the cursor last rested.
    """
    global _cursor_pos
    try:
        vw   = driver.execute_script("return window.innerWidth")
        vh   = driver.execute_script("return window.innerHeight")
        rect = driver.execute_script(
            "var r=arguments[0].getBoundingClientRect();"
            "return {x:r.left+r.width/2, y:r.top+r.height/2,"
            "        w:r.width, h:r.height};",
            target_element,
        )
        x1 = int(rect["x"])
        y1 = int(rect["y"])
        # Aim offset: humans don't land on the geometric centre.
        # Sigma scales with element size; clamped to ±35 % of dimension.
        _ew = max(1, int(rect["w"]))
        _eh = max(1, int(rect["h"]))
        off_dx = int(max(-_ew * 0.35, min(random.gauss(0, max(2.0, _ew * 0.12)), _ew * 0.35)))
        off_dy = int(max(-_eh * 0.35, min(random.gauss(0, max(2.0, _eh * 0.12)), _eh * 0.35)))
        # Start from last known position, clamped to current viewport.
        x0 = max(0, min(_cursor_pos[0], int(vw)))
        y0 = max(0, min(_cursor_pos[1], int(vh)))
        # Proximity guard: cursor already within 25 px — treat as hovering.
        if math.hypot(x1 - x0, y1 - y0) < 25:
            _cdp_x = int(rect["x"]) + off_dx
            _cdp_y = int(rect["y"]) + off_dy
            try:
                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mouseMoved",
                    "x": _cdp_x,
                    "y": _cdp_y,
                })
            except WebDriverException:
                pass
            _set_cursor(_cdp_x, _cdp_y, "hover-dwell")
            _mlog.debug("DWELL  cursor within 25px of target  dist=%.1fpx",
                        math.hypot(x1 - x0, y1 - y0))
            debug_cursor_state(driver, "bezier-dwell")
            return
        # Off-viewport correction: scroll element into view then re-query.
        if x1 < 0 or y1 < 0 or x1 > int(vw) or y1 > int(vh):
            driver.execute_script(
                "arguments[0].scrollIntoView({behavior:'instant', block:'center'});",
                target_element,
            )
            precise_sleep(random.uniform(0.3, 0.6))
            rect = driver.execute_script(
                "var r=arguments[0].getBoundingClientRect();"
                "return {x:r.left+r.width/2, y:r.top+r.height/2, w:r.width, h:r.height};",
                target_element,
            )
            x1 = int(rect["x"])
            y1 = int(rect["y"])
            if x1 < 0 or y1 < 0 or x1 > int(vw) or y1 > int(vh):
                _mlog.debug("SKIP  target off-screen after scroll  pos=(%d,%d)", x1, y1)
                return
        # Arc destination incorporates aim offset so JS animation and the
        # Phase 2 ActionChains snap land at the same position.
        arc_x, arc_y = x1 + off_dx, y1 + off_dy
        points, _ = _fire_bezier_arc(driver, x0, y0, arc_x, arc_y, vw, vh)
        # Diagnostic: warn if last synthetic point is far from the snap target.
        snap_x = int(rect["x"]) + off_dx
        snap_y = int(rect["y"]) + off_dy
        if points:
            last_syn_x, last_syn_y = points[-1][0], points[-1][1]
            snap_gap = math.hypot(snap_x - last_syn_x, snap_y - last_syn_y)
            if snap_gap > 10:
                _mlog.warning(
                    "SNAP GAP  last_synthetic=(%d,%d)  snap_target=(%d,%d)  gap=%.1fpx",
                    last_syn_x, last_syn_y, snap_x, snap_y, snap_gap,
                )
        # CDP dispatch already produced trusted events at the exact
        # endpoint — no Phase 2 ActionChains snap needed.
        _set_cursor(snap_x, snap_y, "elem-hover")
        debug_cursor_state(driver, "bezier-snap")

    except WebDriverException:
        pass

def bezier_move_to_coords(driver, x1: int, y1: int, tag: str = "arc-end") -> None:
    """
    Animate the cursor from _cursor_pos to explicit viewport coordinates
    (x1, y1) along a randomised quadratic Bezier arc at ~60 fps.

    Unlike bezier_move(), no DOM element is required.  Used for:
      • parking the cursor at y=0 before page navigation  ("nav-park")
      • idle cursor drift onto content after page load     ("idle-settle")
      • cursor wanders during reading pauses               ("reading-wander")
      • hand-shift nudges between scroll chunks            ("scroll-drift")
      • pre-aim drifts toward a UI region                  ("nav-hover")

    CDP mouseMoved dispatch via _fire_bezier_arc() with exact_end=True
    so the arc lands exactly on the target coordinate.  CDP produces
    trusted events — no Phase 2 ActionBuilder snap needed.
    """
    global _cursor_pos
    try:
        vw = driver.execute_script("return window.innerWidth")
        vh = driver.execute_script("return window.innerHeight")
        x0 = max(0, min(_cursor_pos[0], int(vw) - 1))
        y0 = max(0, min(_cursor_pos[1], int(vh) - 1))
        x1 = max(0, min(x1, int(vw) - 1))
        y1 = max(0, min(y1, int(vh) - 1))
        if x0 == x1 and y0 == y1:
            return
        _fire_bezier_arc(driver, x0, y0, x1, y1, vw, vh, exact_end=True)
        _set_cursor(x1, y1, tag)
        debug_cursor_state(driver, f"bezier-coords/{tag}")
    except WebDriverException:
        pass


def _navigate_and_settle(driver, action) -> None:
    """
    Shared navigation kernel used by navigate_to() and navigate_history().

    Steps:
      1. Park the cursor at the browser address-bar row (y=0, random x) via a
         smooth Bezier arc — simulating the user reaching for the URL bar.
      2. Execute the navigation action (driver.get / back / forward).
      3. Wait for DOMContentLoaded.
      4. Inject the visual debug overlay.
      5. Silent position set — cursor was at (park_x, 0) before navigation and
         is conceptually still there; no dispatch needed on the fresh page.
      6. Brief settle pause (user's eye scans the freshly rendered page).
      7. Drift the cursor into the feed — the first synthetic event the new
         page sees, arcing naturally from the address-bar area down into content.
    """
    global _cursor_pos
    # 1. Park at address-bar row
    try:
        vw = driver.execute_script("return window.innerWidth")
    except Exception:
        vw = 1280
    park_x = random.randint(int(vw * 0.25), int(vw * 0.75))
    bezier_move_to_coords(driver, park_x, 0, tag="nav-park")

    # 2. Navigate
    action()
    # Phase 1 — wait for the browser's resource-load signal.
    try:
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        pass
    # Phase 2 — SPA content check: wait for a feed article or pressable
    # container to appear.  readyState fires before React has rendered any
    # feed cards, so without this the settle pause and idle-settle drift
    # happen against a blank loading screen.
    try:
        WebDriverWait(driver, 15).until(
            lambda d: d.find_elements(
                By.CSS_SELECTOR,
                "article, div[data-pressable-container='true']",
            )
        )
    except TimeoutException:
        pass  # fall through — page may still be partially usable

    # 3. Overlay — inject after readyState complete, then verify it survives
    #    React's next reconcile pass (1 s later).
    inject_cursor_overlay(driver)
    if DEBUG_CURSOR_OVERLAY:
        exists_now = driver.execute_script(
            "return document.getElementById('__cursor_debug_dot') !== null;"
        )
        log.info("Overlay present immediately after inject: %s", exists_now)
        time.sleep(1.0)
        exists_1s = driver.execute_script(
            "return document.getElementById('__cursor_debug_dot') !== null;"
        )
        log.info("Overlay still present 1 s later (React wipe check): %s", exists_1s)
        if exists_now and not exists_1s:
            log.warning("React wiped the overlay — re-injecting into documentElement")
            inject_cursor_overlay(driver)

    # 4. Silent position set — fresh page has no cursor history.
    #    Cursor was at (park_x, 0) before navigation; it's still conceptually
    #    there.  No dispatch needed — the drift arc below is the first event
    #    the new page sees, which avoids a detectable in-place jump on load.
    _set_cursor(park_x, 0, "fresh-page")

    # 5. Settle — 1.5–3.5 s to mimic a real user visually orienting
    #    after a full page navigation before moving the mouse.
    precise_sleep(random.uniform(1.5, 3.5))

    # 6. Drift into content — first synthetic event on the new page,
    #    starting from (park_x, 0) and moving naturally into the feed area.
    try:
        vw2 = driver.execute_script("return window.innerWidth")
        vh2 = driver.execute_script("return window.innerHeight")
        rx = random.randint(int(vw2 * 0.15), int(vw2 * 0.85))
        ry = random.randint(int(vh2 * 0.25), int(vh2 * 0.75))
        bezier_move_to_coords(driver, rx, ry, tag="idle-settle")
    except Exception:
        pass


def navigate_to(driver, url: str) -> None:
    """Navigate to url with human-like cursor park → restore → drift."""
    log.info("[ NAV ]  → %s", url)
    _navigate_and_settle(driver, lambda: driver.get(url))


def navigate_history(driver, direction: str = "back") -> None:
    """Go back or forward in history with human-like cursor park → restore → drift."""
    log.info("[ NAV ]  %s", direction)
    fn = driver.back if direction == "back" else driver.forward
    _navigate_and_settle(driver, fn)


# ------------------------------------------------------------------ #
#  SMOOTH SCROLLING
#
#  Root cause of the previous script timeout:
#  execute_async_script with a JS Promise relied on the browser calling
#  the Selenium "done" callback, which Orbita sometimes never triggers —
#  causing a script timeout after 30 s.
#
#  Fix: use plain synchronous execute_script in a Python loop.
#  Each call moves `step_px` pixels and returns immediately.
#  We sleep `tick_ms` ms between calls in Python — same visual effect,
#  no async plumbing, zero risk of timeout.
# ------------------------------------------------------------------ #

def smooth_scroll_chunk(driver, distance_px: int,
                        step_px: int = 6, tick_ms: int = 16) -> None:
    """
    Scroll distance_px using a sine ease-in/ease-out velocity curve to mimic
    real scroll-wheel physics (fast start, peak speed, deceleration).

    distance_px  positive = down, negative = up
    step_px      pixels per step at peak velocity
    tick_ms      base milliseconds between steps
    """
    total     = abs(distance_px)
    direction = 1 if distance_px >= 0 else -1
    steps     = int(max(1, total // max(1, step_px)))
    scrolled  = 0

    for i in range(steps):
        # Sine-based ease-in/ease-out: slow at start and end, fast in middle
        t        = (i + 0.5) / steps                        # normalised 0..1
        velocity = 0.5 - 0.5 * math.cos(math.pi * t)       # bell curve 0..1
        # Step size scales with velocity: 1 px minimum, up to 2× step_px peak
        move_f   = 1 + velocity * (step_px * 2 - 1)
        move     = max(1, min(int(move_f), total - scrolled))
        if move <= 0:
            break
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseWheel",
            "x": _cursor_pos[0],
            "y": _cursor_pos[1],
            "deltaX": 0,
            "deltaY": direction * move,
        })
        scrolled += move
        # Tick duration varies inversely with velocity (slow ends, fast middle)
        delay = (tick_ms / 1000.0) * (0.5 + (1.0 - velocity) * 1.0)
        precise_sleep(delay + random.uniform(-0.003, 0.003))

    # Flush any sub-step remainder
    remainder = total - scrolled
    if remainder > 0:
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseWheel",
            "x": _cursor_pos[0],
            "y": _cursor_pos[1],
            "deltaX": 0,
            "deltaY": direction * remainder,
        })


def stochastic_scroll(driver, total_seconds: float) -> None:
    """
    Scroll the page for total_seconds with natural human variance.

    Reading pause tiers (per chunk):
      3%  distraction  8–15 s  (phone buzz, looking away)
     15%  long read    4.5–9 s  (interesting post)
     17%  quick skim   0.3–1.2 s (nothing to see, keep scrolling)
     65%  normal read  1.5–4 s
    """
    def _reading_pause(seconds: float) -> None:
        """
        Sleep for `seconds` while continuously drifting the cursor — mimicking
        a user's eyes and hand moving across content they're reading.

        Rather than a flat sleep followed by a single wander, the pause is
        broken into micro-segments of 0.6–2.0 s each.  After each segment there
        is a 72 % chance of a small cursor nudge.  Nudges are *local* — biased
        toward the current cursor position + Gaussian scatter — so the cursor
        drifts organically across the content area rather than teleporting.

        Short pauses (< 0.8 s) are served as a plain sleep to avoid the overhead
        of JS viewport queries on quick skims.
        """
        if seconds < 0.8:
            precise_sleep(seconds)
            return
        end = time.time() + seconds
        while True:
            remaining = end - time.time()
            if remaining <= 0:
                break
            sit = min(random.uniform(1.0, 3.0), remaining)
            precise_sleep(sit)
            if end - time.time() <= 0.15:
                break
            if random.random() < 0.5:
                try:
                    # Close any media overlay that was accidentally opened before
                    # firing the cursor drift — avoids moving the cursor on top of
                    # the overlay's fixed-position elements.
                    _close_media_overlay(driver)
                    vw_r = driver.execute_script("return window.innerWidth")
                    vh_r = driver.execute_script("return window.innerHeight")
                    # Local drift — stays near current position, Gaussian spread
                    cx = max(int(vw_r * 0.08), min(int(vw_r * 0.92),
                             _cursor_pos[0] + int(random.gauss(0, vw_r * 0.10))))
                    cy = max(int(vh_r * 0.10), min(int(vh_r * 0.90),
                             _cursor_pos[1] + int(random.gauss(0, vh_r * 0.09))))
                    bezier_move_to_coords(driver, cx, cy, tag="reading-wander")
                except Exception:
                    pass

    deadline = time.time() + total_seconds
    log.info("[ SCROLL ]  scrolling for %.0fs", total_seconds)
    # Scroll-chunk nudge counter — fire a small cursor shift every 3-5 chunks
    # to model the hand resting on the desk and shifting while scrolling.
    _nudge_after = random.randint(3, 5)
    _chunk_count = 0
    while time.time() < deadline:
        distance = random.randint(280, 650)
        step_px  = random.randint(4, 9)
        tick_ms  = random.randint(12, 20)
        smooth_scroll_chunk(driver, distance, step_px, tick_ms)
        _chunk_count += 1

        # brief pause after scroll lands (hand leaving wheel)
        precise_sleep(random.uniform(0.15, 0.45))

        # Occasional hand-shift nudge between scroll chunks.
        # Fires after every _nudge_after chunks; threshold is re-randomised
        # each time so the interval is never periodic.
        if _chunk_count >= _nudge_after:
            _chunk_count = 0
            _nudge_after = random.randint(3, 5)
            try:
                # Close any media overlay before shifting the cursor between
                # scroll chunks — prevents the drift arc landing on overlay UI.
                _close_media_overlay(driver)
                vw_n = driver.execute_script("return window.innerWidth")
                vh_n = driver.execute_script("return window.innerHeight")
                nx = max(int(vw_n * 0.08), min(int(vw_n * 0.92),
                         _cursor_pos[0] + int(random.gauss(0, vw_n * 0.12))))
                ny = max(int(vh_n * 0.10), min(int(vh_n * 0.90),
                         _cursor_pos[1] + int(random.gauss(0, vh_n * 0.10))))
                bezier_move_to_coords(driver, nx, ny, tag="scroll-drift")
            except Exception:
                pass

        # 4-tier reading pause — cursor drifts throughout via _reading_pause()
        tier = random.random()
        if tier < 0.03:
            _reading_pause(random.uniform(8.0, 15.0))   # distraction
        elif tier < 0.18:
            _reading_pause(random.uniform(4.5, 9.0))    # long read
        elif tier < 0.35:
            _reading_pause(random.uniform(0.3, 1.2))    # quick skim
        else:
            _reading_pause(random.uniform(1.5, 4.0))    # normal read
        # occasional upward drift — small (re-reading) or large (going back to a post)
        if random.random() < 0.22:
            # 20 % of drift events scroll back a large amount (really went too far)
            up_px = (
                random.randint(200, 600) if random.random() < 0.20
                else random.randint(80, 160)
            )
            smooth_scroll_chunk(driver, -up_px, step_px=5, tick_ms=18)
            dwell = random.uniform(1.5, 4.0) if up_px >= 200 else random.uniform(0.4, 1.2)
            precise_sleep(dwell)

        if time.time() >= deadline:
            break


# ================================================================== #
#  PRE-FLIGHT  (Wikipedia only)
# ================================================================== #

def run_preflight(driver) -> None:
    """
    Browse a random selection of pre-flight sites to seed a varied, natural
    browsing history before navigating to Threads.

    Each run samples 2-4 sites from PREFLIGHT_SITES_POOL so the history is
    never identical across sessions, reducing the pattern of a single
    Wikipedia visit that always precedes Threads activity.
    """
    k     = random.randint(PREFLIGHT_SITES_MIN, PREFLIGHT_SITES_MAX)
    sites = random.sample(PREFLIGHT_SITES_POOL, k=min(k, len(PREFLIGHT_SITES_POOL)))
    log.info("Pre-flight: visiting %d site(s): %s", len(sites), [s.split('/')[2] for s in sites])
    for site in sites:
        dwell = random.uniform(PREFLIGHT_DWELL_MIN, PREFLIGHT_DWELL_MAX)
        log.info("Pre-flight: %s  (%.0fs)", site, dwell)
        navigate_to(driver, site)
        stochastic_scroll(driver, total_seconds=dwell)


# ================================================================== #
#  THREADS LIKE ENGINE
# ================================================================== #
#
# From inspecting the live Threads DOM, the like button structure is:
#
#   div.x4vbgl9  (action bar container)
#     div[role="button"].x165d6jo  (like wrapper — x165d6jo is unique to like)
#       div > div
#         svg[aria-label="Like"]   style="--x-fill: transparent"   (un-liked)
#         svg[aria-label="Unlike"] style="--x-fill: currentColor"  (liked)
#
# Strategy — try in order, stop at first that returns results:
#   1. JS-based search (most reliable — reads live DOM, no stale refs)
#   2. XPath on the wrapper div containing the Like SVG
#   3. CSS :has() on the wrapper div
# ================================================================== #

# JavaScript that finds all un-liked like buttons currently in the viewport.
# Returns the clickable wrapper div[role="button"], not the SVG itself,
# so Selenium's .click() lands on the correct interactive element.
_JS_FIND_LIKE_BTNS = """
(function() {
    var vp   = window.innerHeight;
    var svgs = document.querySelectorAll('svg[aria-label="Like"]');
    var seen = new Set();
    var count = 0;
    for (var i = 0; i < svgs.length; i++) {
        var btn = svgs[i].closest('div[role="button"]');
        if (!btn || seen.has(btn)) continue;
        seen.add(btn);
        var r = btn.getBoundingClientRect();
        var label = (btn.getAttribute('aria-label') || '').toLowerCase();
        if (label === 'unlike') continue;
        // Count buttons plausibly on-screen (generous tolerance)
        if (r.height > 0 && r.top > -100 && r.top < vp + 100) count++;
    }
    // Return count only — DOM node serialisation via CDP is unreliable in
    // Orbita and yields empty arrays despite real results.
    return count;
})();
"""

# XPath fallback: wrapper div containing an un-liked Like SVG
_LIKE_XPATH_FALLBACK = [
    "//div[@role='button'][.//*[local-name()='svg'][@aria-label='Like']]",
]

# CSS fallback: :has() — Chromium 105+ supports this
_LIKE_CSS_FALLBACK = [
    "div[role='button']:has(svg[aria-label='Like'])",
]


def _is_already_liked(el) -> bool:
    """Return True if this element represents an already-liked post."""
    try:
        label = (el.get_attribute("aria-label") or "").lower()
        if label == "unlike":
            return True
        if el.get_attribute("aria-pressed") == "true":
            return True
    except WebDriverException:
        pass
    return False


def _find_unliked_buttons(driver) -> list:
    """
    Return all clickable like-button wrapper divs that are visible in the
    current viewport and have NOT been liked yet.

    XPath via find_elements is the primary strategy — it uses a proper CDP
    DOM query and reliably returns WebElements.  The JS path only returns a
    count (DOM node serialisation through Orbita's CDP is unreliable and
    yields empty arrays).  CSS :has() is the fallback.
    """
    results = []
    viewport_h = driver.execute_script("return window.innerHeight")

    # Diagnostic only — JS count lets us see if SVGs exist at all.
    try:
        js_count = driver.execute_script(_JS_FIND_LIKE_BTNS)
        if js_count is not None:
            log.debug("JS like-btn diagnostic count: %s", js_count)
    except Exception as e:
        log.debug("JS like-btn diagnostic failed: %s", e)

    # 1. XPath — primary strategy (proven reliable with Orbita CDP)
    for xp in _LIKE_XPATH_FALLBACK:
        try:
            for el in driver.find_elements(By.XPATH, xp):
                try:
                    r = driver.execute_script(
                        "var r=arguments[0].getBoundingClientRect();"
                        "return {top:r.top,height:r.height};", el)
                    if r["height"] > 0 and -50 <= r["top"] <= viewport_h + 50:
                        if el.is_displayed() and not _is_already_liked(el):
                            results.append(el)
                except Exception:
                    continue
        except (NoSuchElementException, WebDriverException):
            continue
        if results:
            log.info("XPath: %d unliked like button(s) in viewport", len(results))
            break

    # 2. CSS :has() fallback
    if not results:
        for sel in _LIKE_CSS_FALLBACK:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        r = driver.execute_script(
                            "var r=arguments[0].getBoundingClientRect();"
                            "return {top:r.top,height:r.height};", el)
                        if r["height"] > 0 and -50 <= r["top"] <= viewport_h + 50:
                            if el.is_displayed() and not _is_already_liked(el):
                                results.append(el)
                    except Exception:
                        continue
            except (NoSuchElementException, WebDriverException):
                continue
            if results:
                log.info("CSS: %d unliked like button(s) in viewport", len(results))
                break

    if not results:
        log.info("No likeable posts visible in viewport right now")

    return results


def scroll_element_into_loose_view(driver, element) -> None:
    """
    Scroll the page until the element is loosely visible — somewhere in
    the viewport, not mathematically centered.  Mimics a human scrolling
    until they can see what they're looking for and stopping.

    Each scroll chunk is routed through smooth_scroll_chunk so it inherits
    the same sine ease-in/ease-out velocity curve used during stochastic
    browsing — slow start, peak in the middle, deceleration to stop.
    Step sizes and tick rates are randomised per chunk so consecutive
    scroll events vary the way real scroll-wheel flicks do.
    """
    for _ in range(12):  # max attempts before giving up
        rect = driver.execute_script(
            "var r=arguments[0].getBoundingClientRect();"
            "return {top:r.top, bottom:r.bottom, height:r.height};",
            element,
        )
        vh = driver.execute_script("return window.innerHeight")

        # Element is comfortably visible — not within 15 % of either edge
        margin = vh * 0.15
        if margin < rect["top"] and rect["bottom"] < (vh - margin):
            break

        if rect["top"] < margin:
            # Element above viewport (or too close to top) — scroll up
            step = -random.randint(80, 220)
        else:
            # Element below viewport (or too close to bottom) — scroll down
            step = random.randint(80, 220)

        # Use smooth_scroll_chunk so each scroll chunk gets the same sine
        # ease-in/ease-out physics as normal browsing — not a raw instant jump.
        smooth_scroll_chunk(
            driver, step,
            step_px=random.randint(4, 8),
            tick_ms=random.randint(13, 20),
        )
        # Brief inter-chunk pause — hand rests between flicks
        precise_sleep(random.uniform(0.08, 0.25))

    # Imprecise final pause — not a fixed sleep
    precise_sleep(random.uniform(0.3, 0.7))


def _attempt_like(driver, element) -> bool:
    """
    Perform one like action with human-like timing:
      1. Scroll post into view
      2. Pause — as if reading the post before liking
      3. Bezier mouse curve to the like button
      4. Hover pause (hand settling)
      5. Click (Selenium first, JS fallback on intercept)
      6. Post-click pause (watching the heart animation)
    Returns True on success.
    """
    try:
        log.info("[ LIKE ]  scrolling post into view + clicking like")
        scroll_element_into_loose_view(driver, element)

        # Content-aware guard: skip posts with no visible engagement signal.
        # Real users predominantly like content that already has replies, likes,
        # or reposts.  Liking zero-engagement posts in bulk is a bot pattern.
        # 70 % skip rate when no digit is visible — leaves a small chance so
        # the bot can occasionally like emerging posts as a real user would.
        try:
            has_signal = driver.execute_script("""
                var btn  = arguments[0];
                var post = btn.closest('article') ||
                           btn.closest('[data-pressable-container]');
                if (!post) return true;
                return /\\d/.test(post.innerText || '');
            """, element)
            if not has_signal and random.random() < 0.70:
                log.debug("[ LIKE ]  skipping zero-engagement post (content-aware guard)")
                return False
        except Exception:
            pass

        # Reading pause before liking — humans read before they react
        precise_sleep(random.uniform(0.8, 2.5))

        bezier_move(driver, element)
        precise_sleep(random.uniform(0.2, 0.6))   # hand settling on the button

        try:
            _cdp_click(driver)
        except WebDriverException:
            log.debug("Selenium click intercepted — JS click fallback")
            driver.execute_script("arguments[0].click();", element)
        debug_cursor_state(driver, "like-click")

        # Watch the heart animation
        precise_sleep(random.uniform(0.8, 2.0))
        log.info("Like delivered successfully")
        return True

    except (NoSuchElementException, WebDriverException) as exc:
        log.debug("Like attempt failed: %s", exc)
        return False


# ================================================================== #
#  LOGIN GUARD
# ================================================================== #

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

def check_login_status(driver) -> bool:
    """
    Return True if the current Threads page shows a logged-in feed.

    Detects:
    - Logged-out state via /login redirect.
    - Challenge / verification screens via specific URL paths and structural
      DOM elements — avoiding false positives from user-generated content.
      A challenge page can look logged-in (no /login in URL, feed elements
      absent) so explicit challenge detection is required.
    """
    try:
        url = driver.current_url.lower()

        # Definite logged-out
        if any(s in url for s in ("/login", "/accounts/login")):
            log.warning("Login redirect detected: %s", url)
            return False

        # URL-based challenge detection — specific paths only
        if any(s in url for s in CHALLENGE_URL_PATHS):
            log.warning("Challenge URL detected: %s", url)
            return False

        # DOM-based challenge detection — structural elements only
        for sel in CHALLENGE_DOM_SELECTORS:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    log.warning("Challenge DOM element detected: %s", sel)
                    return False
            except NoSuchElementException:
                continue

        # Feed present — logged in
        articles = driver.find_elements(
            By.CSS_SELECTOR,
            "article, div[data-pressable-container='true']",
        )
        if articles:
            return True

        # Fallback: on threads domain with no challenge signals
        if "threads.net" in url or "threads.com" in url:
            return True

    except WebDriverException:
        pass
    return False


# ================================================================== #
#  ENGAGEMENT VARIETY ACTIONS
# ================================================================== #

def _is_visually_visible(driver, el) -> bool:
    """
    Return True only if el is genuinely visible to the user.

    Guards against honeypot elements that pass is_displayed() but are
    invisible via CSS tricks — opacity:0, visibility:hidden, zero size,
    or off-screen positioning (left:-9999px).
    """
    try:
        return driver.execute_script(
            """
            var s   = window.getComputedStyle(arguments[0]);
            var r   = arguments[0].getBoundingClientRect();
            return s.display     !== 'none'
                && s.visibility  !== 'hidden'
                && s.opacity     !== '0'
                && parseFloat(s.opacity) > 0
                && parseInt(s.width)  > 0
                && parseInt(s.height) > 0
                && r.width  > 0
                && r.height > 0
                && r.right  > 0
                && r.bottom > 0;
            """,
            el,
        )
    except Exception:
        return False


def _get_own_profile_href(driver) -> str:
    """
    Return the href of the logged-in user's own nav-bar profile link,
    or '' on failure.

    The nav icon is the only a[href^="/@"] that wraps an
    svg[aria-label="Profile"] — all post-author links use text/avatars.
    Caching it before each candidate scan prevents the bot from navigating
    to its own account.
    """
    try:
        # Avoid CSS :has() — walk up from the Profile SVG to its <a> ancestor
        el = driver.execute_script("""
            var svgs = document.querySelectorAll('svg[aria-label="Profile"]');
            for (var i = 0; i < svgs.length; i++) {
                var node = svgs[i].parentElement;
                for (var d = 0; d < 6; d++) {
                    if (!node) break;
                    var href = node.getAttribute('href') || '';
                    if (node.tagName === 'A' && href.startsWith('/@')) return node;
                    node = node.parentElement;
                }
            }
            return null;
        """)
        if el is None:
            return ""
        return (el.get_attribute("href") or "").rstrip("/")
    except Exception:
        return ""

def view_profile_from_feed(driver) -> bool:
    """
    Click a random post-author username link in the feed to visit their
    profile, scroll it, optionally follow, then navigate back.

    Has a 15 % probabilistic gate for calling follow_from_profile_page().
    """
    try:
        own_href = _get_own_profile_href(driver)
        candidates = []
        for el in driver.find_elements(By.CSS_SELECTOR, FEED_PROFILE_LINK):
            try:
                if not el.is_displayed():
                    continue
                # Skip timestamp links (same selector but contain <time>)
                if driver.execute_script(
                    "return arguments[0].querySelector('time') !== null;", el
                ):
                    continue
                href = el.get_attribute("href") or ""
                if "/post/" in href or "/t/" in href:
                    continue
                if own_href and href.rstrip("/") == own_href:
                    continue
                if href.rstrip("/") in _session_followed:
                    continue
                candidates.append(el)
            except Exception:
                continue

        if not candidates:
            log.debug("No feed profile links found")
            return False

        target = random.choice(candidates[:15])
        profile_url = target.get_attribute("href")
        if not profile_url:
            return False

        # Re-validate — element may have scrolled off-screen since the candidate
        # list was built (page could have loaded more content / user scroll).
        _rect = driver.execute_script(
            "var r=arguments[0].getBoundingClientRect();"
            "return {y: r.top, h: r.height};",
            target,
        )
        _vh = driver.execute_script("return window.innerHeight")
        if _rect["h"] == 0 or _rect["y"] < 0 or _rect["y"] > _vh:
            log.debug("Profile link scrolled off-screen since scan — skipping")
            return False

        # Scroll the link loosely into view before moving the cursor to it.
        scroll_element_into_loose_view(driver, target)

        _session_followed.add(profile_url.rstrip("/"))
        log.info("Viewing profile from feed: %s", profile_url[:60])
        bezier_move(driver, target)
        precise_sleep(random.uniform(0.5, 1.5))

        # ~25 % of visits: open the profile in a new tab (mirrors Ctrl+click /
        # middle-click behaviour a real user exhibits occasionally).
        use_new_tab = (random.random() < 0.25)
        original_handle = driver.current_window_handle

        if use_new_tab:
            log.info("[ NAV ]  opening profile in new tab")
            driver.execute_script("window.open(arguments[0], '_blank');", profile_url)
            precise_sleep(random.uniform(0.4, 0.9))   # brief pause while tab opens
            driver.switch_to.window(driver.window_handles[-1])
            try:
                WebDriverWait(driver, 10).until(lambda d: "/@" in d.current_url)
            except TimeoutException:
                pass
            inject_cursor_overlay(driver)
            init_cursor_pos(driver)
        else:
            _cdp_click(driver)
            debug_cursor_state(driver, "profile-nav-click")
            try:
                WebDriverWait(driver, 10).until(lambda d: "/@" in d.current_url)
            except TimeoutException:
                pass

        precise_sleep(random.uniform(1.5, 3.0))
        stochastic_scroll(driver, total_seconds=random.uniform(2, 4))

        # Follow gate — 15 % probabilistic
        if random.random() < 0.15:
            follow_from_profile_page(driver)

        if use_new_tab:
            # Close the profile tab and return focus to the feed tab.
            precise_sleep(random.uniform(0.5, 1.2))
            log.info("[ NAV ]  closing profile tab, returning to feed")
            driver.close()
            driver.switch_to.window(original_handle)
            precise_sleep(random.uniform(0.8, 1.8))
        else:
            # Return via Home nav button — more human-like than the back button.
            # Falls back to navigate_history only if the nav icon is not found.
            if not click_home_button(driver):
                log.debug("view_profile_from_feed: home button not found — back fallback")
                navigate_history(driver, "back")
            precise_sleep(random.uniform(1.0, 2.5))
        return True

    except (TimeoutException, WebDriverException) as exc:
        log.debug("View profile from feed failed: %s", exc)
        try:
            if not click_home_button(driver):
                navigate_to(driver, TARGET_SOCIAL_URL)
        except Exception:
            pass
        return False

def follow_from_feed(driver) -> bool:
    """
    Follow a user directly from the feed via the hover-card that Threads
    renders when the cursor rests over a post-author username.

    Flow:
      1. Find a visible feed profile link and scroll it into view.
      2. Bezier-arc to the username (hover only — no click).
      3. Wait up to 2 s for a text-based Follow button to appear in the
         hover card (it is absent before the hover fires).
      4. Bezier-arc to that Follow button and click it.
      5. Move the cursor back to a neutral mid-feed position so the hover
         card dismisses naturally and scrolling can continue.
    """
    try:
        # ── 1. Collect visible, non-timestamp feed profile links ──────────────
        own_href = _get_own_profile_href(driver)
        candidates = []
        for el in driver.find_elements(By.CSS_SELECTOR, FEED_PROFILE_LINK):
            try:
                if not el.is_displayed():
                    continue
                if driver.execute_script(
                    "return arguments[0].querySelector('time') !== null;", el
                ):
                    continue
                href = el.get_attribute("href") or ""
                if "/post/" in href or "/t/" in href:
                    continue
                if own_href and href.rstrip("/") == own_href:
                    continue
                if href.rstrip("/") in _session_followed:
                    continue
                # Avatar links contain <img>; username links contain only text.
                # We want textual username links — avatar hover triggers the
                # quick-follow SVG, not the text-based hover-card Follow button.
                if driver.execute_script(
                    "return arguments[0].querySelector('img') !== null;", el
                ):
                    continue
                rect = driver.execute_script(
                    "var r=arguments[0].getBoundingClientRect();"
                    "return {y:r.top, h:r.height};",
                    el,
                )
                vh_c = driver.execute_script("return window.innerHeight")
                if rect["h"] == 0 or rect["y"] < 0 or rect["y"] > vh_c:
                    continue
                candidates.append(el)
            except Exception:
                continue

        if not candidates:
            log.debug("follow_from_feed: no visible feed profile links")
            return False

        username_el = random.choice(candidates[:10])

        # ── 2. Scroll username into view, then hover (no click) ───────────────
        scroll_element_into_loose_view(driver, username_el)

        # Snapshot of text-based Follow buttons already in DOM before hover
        pre_follow_ids = set(
            el.id for el in driver.find_elements(By.XPATH, FOLLOW_BTN_XPATH)
        )

        bezier_move(driver, username_el)          # hover — ActionChains fires mouseenter
        log.info("follow_from_feed: hovering username to trigger hover card")

        # ── 3. Wait for the hover card's Follow button to appear ──────────────
        follow_btn = None
        try:
            def _new_follow_btn(d):
                for el in d.find_elements(By.XPATH, FOLLOW_BTN_XPATH):
                    if el.id not in pre_follow_ids and el.is_displayed():
                        return el
                return None

            follow_btn = WebDriverWait(driver, 2).until(_new_follow_btn)
        except TimeoutException:
            log.debug("follow_from_feed: hover card Follow button did not appear")
            # Drift cursor far from the hover card, then scroll to guarantee dismissal
            try:
                vw_e = driver.execute_script("return window.innerWidth")
                vh_e = driver.execute_script("return window.innerHeight")
                bezier_move_to_coords(
                    driver,
                    random.randint(int(vw_e * 0.20), int(vw_e * 0.80)),
                    random.randint(int(vh_e * 0.45), int(vh_e * 0.75)),
                    tag="idle-settle",
                )
                precise_sleep(random.uniform(0.2, 0.4))
                # Small scroll to force any lingering hover card off the screen
                smooth_scroll_chunk(driver, random.randint(60, 130), step_px=5, tick_ms=16)
            except Exception:
                pass
            return False

        # ── 4. Arc to the Follow button and click ─────────────────────────────
        precise_sleep(random.uniform(0.3, 0.7))      # eye settling on the card
        bezier_move(driver, follow_btn)
        precise_sleep(random.uniform(0.3, 0.8))
        _cdp_click(driver)
        debug_cursor_state(driver, "follow-feed-click")
        precise_sleep(random.uniform(0.8, 1.5))
        log.info("follow_from_feed: follow clicked via hover card")
        _session_followed.add((username_el.get_attribute("href") or "").rstrip("/"))

        # ── 5. Drift cursor to mid-feed + scroll to guarantee card dismissal ──
        try:
            vw_e = driver.execute_script("return window.innerWidth")
            vh_e = driver.execute_script("return window.innerHeight")
            bezier_move_to_coords(
                driver,
                random.randint(int(vw_e * 0.20), int(vw_e * 0.80)),
                random.randint(int(vh_e * 0.45), int(vh_e * 0.75)),
                tag="idle-settle",
            )
            precise_sleep(random.uniform(0.2, 0.4))
            # Scroll down slightly — moves the hovered username off-screen so
            # the hover card closes even if cursor proximity keeps it open.
            smooth_scroll_chunk(driver, random.randint(80, 160), step_px=5, tick_ms=16)
        except Exception:
            pass

        return True

    except (NoSuchElementException, WebDriverException) as exc:
        log.debug("follow_from_feed failed: %s", exc)
        return False

def follow_from_profile_page(driver) -> bool:
    """
    Click the Follow button on a loaded profile page.
    Only call this when already on a profile URL (/@username).

    Flow:
      1. Smooth scroll to top (follow button lives in the profile header).
      2. Drift cursor to the header region before committing to the button.
      3. Deliberate deciding pause.
      4. Bezier arc to follow button + click.
      5. Post-follow drift toward the mid-feed so navigate_history() has a
         realistic arc length when it parks the cursor at y=0.
    """
    log.info("[ FOLLOW ]  attempting follow from profile page")
    try:
        if "/@" not in driver.current_url:
            log.debug("Not on a profile page — skipping follow")
            return False

        # 1. Smooth scroll to top — the follow button is in the profile header.
        #    Use smooth_scroll_chunk in small upward steps so it looks like a
        #    human scrolling back up after reading, rather than instant jump.
        try:
            current_scroll = driver.execute_script("return window.scrollY")
            if current_scroll > 50:
                # Scroll up in one smooth chunk
                smooth_scroll_chunk(driver, -current_scroll, step_px=8, tick_ms=14)
                precise_sleep(random.uniform(0.4, 0.9))
        except WebDriverException:
            pass

        # 2. Wait for the follow button to appear in the now-visible header.
        btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, FOLLOW_BTN_XPATH))
        )
        if not _is_visually_visible(driver, btn):
            return False

        # Scroll it loosely into view (handles any residual offset)
        scroll_element_into_loose_view(driver, btn)

        # 3. Drift cursor into the header area before aiming at the button —
        #    simulates the eye landing on the profile header after scrolling up.
        try:
            vw_f = driver.execute_script("return window.innerWidth")
            vh_f = driver.execute_script("return window.innerHeight")
            pre_x = random.randint(int(vw_f * 0.10), int(vw_f * 0.60))
            pre_y = random.randint(int(vh_f * 0.10), int(vh_f * 0.30))
            bezier_move_to_coords(driver, pre_x, pre_y, tag="nav-hover")
        except WebDriverException:
            pass

        # 4. Deliberate deciding pause + bezier arc to button + click.
        precise_sleep(random.uniform(2.0, 5.0))
        bezier_move(driver, btn)
        precise_sleep(random.uniform(0.3, 0.8))
        _cdp_click(driver)
        debug_cursor_state(driver, "follow-profile-click")
        precise_sleep(random.uniform(0.8, 1.5))

        try:
            WebDriverWait(driver, 5).until(
                lambda d: len(d.find_elements(
                    By.XPATH,
                    '//div[@role="button" and (.//div[normalize-space(text())="Following"]'
                    ' or .//div[normalize-space(text())="Requested"])]',
                )) > 0
            )
            log.info("Follow confirmed on profile page")
        except TimeoutException:
            log.debug("Follow state change not confirmed — may still have worked")

        # 5. Post-follow drift toward mid-feed so the upcoming navigate_history()
        #    park arc has a realistic length rather than a near-zero hop from y≈0.
        try:
            vw_f = driver.execute_script("return window.innerWidth")
            vh_f = driver.execute_script("return window.innerHeight")
            drift_x = random.randint(int(vw_f * 0.20), int(vw_f * 0.80))
            drift_y = random.randint(int(vh_f * 0.40), int(vh_f * 0.70))
            bezier_move_to_coords(driver, drift_x, drift_y, tag="idle-settle")
        except WebDriverException:
            pass

        return True

    except (TimeoutException, NoSuchElementException, WebDriverException) as exc:
        log.debug("Follow from profile failed: %s", exc)
        return False


def _find_nav_btn_by_label(driver, aria_label: str):
    """
    Find a nav bar anchor/button by its SVG aria-label using a JS DOM walk.
    Avoids CSS :has() which has unreliable support in ChromeDriver.
    Returns the clickable <a> or role="button" ancestor element, or None.
    """
    return driver.execute_script("""
        var svgs = document.querySelectorAll('svg[aria-label="' + arguments[0] + '"]');
        for (var i = 0; i < svgs.length; i++) {
            var svg = svgs[i];
            var rect = svg.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            var el = svg.parentElement;
            for (var d = 0; d < 6; d++) {
                if (!el) break;
                if (el.tagName === 'A' || el.getAttribute('role') === 'button') {
                    return el;
                }
                el = el.parentElement;
            }
        }
        return null;
    """, aria_label)


def _click_nav_btn(driver, aria_label: str, label: str) -> bool:
    """
    Generic helper: find a nav bar icon by its SVG aria-label, bezier-move
    to it, and click it.
    aria_label  — the SVG aria-label value (e.g. "Home", "Search", "Notifications").
    label       — human-readable name used only for log messages.
    Returns True on success, False if not found / not clickable.
    """
    try:
        btn = _find_nav_btn_by_label(driver, aria_label)
        if not btn:
            log.debug("_click_nav_btn: '%s' not found", label)
            return False
        WebDriverWait(driver, 4).until(lambda d: btn.is_displayed())
        bezier_move(driver, btn)
        precise_sleep(random.uniform(0.3, 0.7))
        _cdp_click(driver)
        debug_cursor_state(driver, f"nav-btn/{label}")
        precise_sleep(random.uniform(0.8, 1.8))   # SPA transition settle
        log.debug("_click_nav_btn: clicked '%s'", label)
        return True
    except WebDriverException as exc:
        log.debug("_click_nav_btn: WebDriverException on '%s': %s", label, exc)
        return False


def click_home_button(driver) -> bool:
    """
    Click the Home or Threads-logo nav button to return to the feed.
    Tries "Home" SVG label first (compact nav), then "Threads" (sidebar logo).
    Returns True on success, False if neither is found.
    """
    # "Home" label used by compact/mobile nav; "Threads" by the sidebar logo
    for aria_label in ("Home", "Threads"):
        if _click_nav_btn(driver, aria_label, aria_label):
            return True
    log.debug("click_home_button: no home button found")
    return False


def check_notifications_action(driver) -> None:
    """
    Click the Notifications nav button and dwell briefly.
    Simulates a user checking who liked or replied to them.
    Uses bezier_move() + click on the nav icon — no URL-bar navigation.
    """
    log.info("Checking notifications via nav button")
    try:
        if not _click_nav_btn(driver, "Notifications", "Notifications"):
            log.debug("Notifications button not found — skipping")
            return
        precise_sleep(random.uniform(2.0, 5.0))
        stochastic_scroll(driver, total_seconds=random.uniform(5, 15))
        # Return to feed by clicking the Home nav button
        if not click_home_button(driver):
            navigate_to(driver, TARGET_SOCIAL_URL)
        precise_sleep(random.uniform(1.0, 2.5))
    except (TimeoutException, WebDriverException) as exc:
        log.debug("Notification check failed: %s", exc)


def return_to_top_action(driver) -> None:
    """Click the Home / Threads-logo nav button to scroll back to the top of the feed."""
    if not click_home_button(driver):
        log.debug("Return to top failed — home button not found")


def visit_search_action(driver) -> None:
    """
    Click the Search nav icon, dwell briefly (no typing), then return home.
    Simulates a user opening search to inspect trending topics or profiles
    without committing to a query.
    """
    log.info("Visiting search page via nav button")
    try:
        if not _click_nav_btn(driver, "Search", "Search"):
            log.debug("Search button not found — skipping")
            return
        # Dwell as if scanning the search page
        precise_sleep(random.uniform(3.0, 8.0))
        # Return to feed
        if not click_home_button(driver):
            navigate_to(driver, TARGET_SOCIAL_URL)
        precise_sleep(random.uniform(1.0, 2.0))
    except (TimeoutException, WebDriverException) as exc:
        log.debug("visit_search_action failed: %s", exc)


# ================================================================== #
#  PASSIVE / ACTIVE ACTIONS  +  SESSION LOOP
# ================================================================== #

_MEDIA_URL_RE = re.compile(
    r"https?://(?:www\.)?threads\.(?:com|net)/@[^/]+/post/[^/]+/media",
    re.IGNORECASE,
)


def _close_media_overlay(driver) -> bool:
    """
    Detect and dismiss an accidental media-viewer overlay
    (URL pattern: /@<user>/post/<id>/media).

    Strategy:
      1. Check the current URL against the media-viewer pattern.
      2. Find the Close button via JS: locate ALL svg[aria-label="Close"]
         elements, filter to those that are visible AND positioned in the
         top-left quadrant of the viewport (top < 30 % vh, left < 30 % vw),
         then walk up to the nearest div[role="button"] ancestor.
         This avoids matching other Close SVGs (reply boxes, modals, etc.)
         that may also appear on the page.
      3. Bezier-arc to the button and click using ActionChains; JS .click()
         is NOT used as it bypasses the real pointer event the overlay needs.
      4. Wait up to 5 s for the URL to leave the /media path.
         Returns True ONLY when the URL has actually changed; returns False
         (so the caller can fall back to home nav) if the click had no effect.

    Returns True if the overlay was successfully dismissed, False otherwise.
    """
    try:
        current = driver.current_url
    except WebDriverException:
        return False

    if not _MEDIA_URL_RE.match(current):
        return False

    log.info("[ PASSIVE ]  media overlay detected (%s) — closing", current[:80])

    # Locate the Close button that is visible AND in the top-left corner of the
    # viewport (where the media viewer's X lives).  Iterates all Close SVGs and
    # picks the topmost-leftmost one so we never confuse it with inline close
    # buttons inside the feed or comment modals.
    close_btn = driver.execute_script("""
        var vw = window.innerWidth;
        var vh = window.innerHeight;
        var svgs = document.querySelectorAll('svg[aria-label="Close"]');
        var best = null;
        var bestScore = Infinity;   // lower (top-left) wins
        for (var i = 0; i < svgs.length; i++) {
            var svg = svgs[i];
            var rect = svg.getBoundingClientRect();
            // Must be visible
            if (rect.width === 0 || rect.height === 0) continue;
            if (rect.top < 0 || rect.left < 0) continue;
            // Must be in the top-left 30% of viewport
            if (rect.top  > vh * 0.30) continue;
            if (rect.left > vw * 0.30) continue;
            // Walk up to nearest div[role="button"]
            var node = svg.parentElement;
            for (var d = 0; d < 6; d++) {
                if (!node) break;
                if (node.getAttribute('role') === 'button') {
                    var score = rect.top + rect.left;
                    if (score < bestScore) {
                        bestScore = score;
                        best = node;
                    }
                    break;
                }
                node = node.parentElement;
            }
        }
        return best;
    """)

    if close_btn is None:
        log.debug("[ PASSIVE ]  Close button not found in media overlay top-left — skipping")
        return False

    try:
        # Brief pause — user realises they're in the media viewer
        precise_sleep(random.uniform(0.6, 1.4))
        bezier_move(driver, close_btn)
        precise_sleep(random.uniform(0.2, 0.5))
        # Use ActionChains only — the overlay listens for real pointer events;
        # JS .click() does not fire the pointer / mouse events the React handler needs.
        _cdp_click(driver)
        debug_cursor_state(driver, "media-overlay-close")

        # Wait for URL to actually leave the /media path (up to 5 s).
        url_changed = False
        try:
            WebDriverWait(driver, 5).until(
                lambda d: not _MEDIA_URL_RE.match(d.current_url)
            )
            url_changed = True
        except TimeoutException:
            log.debug("[ PASSIVE ]  URL did not change after Close click — trying Escape")

        # Human fallback: press Escape (natural dismissal for any modal/overlay)
        if not url_changed:
            try:
                ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                WebDriverWait(driver, 3).until(
                    lambda d: not _MEDIA_URL_RE.match(d.current_url)
                )
                url_changed = True
                log.debug("[ PASSIVE ]  Escape dismissed the media overlay")
            except (TimeoutException, WebDriverException):
                log.debug("[ PASSIVE ]  Escape also failed — deferring to home nav")

        if not url_changed:
            return False   # let caller fall back to home nav

        precise_sleep(random.uniform(0.8, 1.6))  # settle on the post/feed page
        log.info("[ PASSIVE ]  media overlay closed — back on feed/post")
        return True
    except WebDriverException as exc:
        log.debug("[ PASSIVE ]  Close button click failed: %s", exc)
        return False


def passive_action(driver) -> None:
    """
    Passive action: scroll, with occasional browser back/forward
    to break the perfectly linear navigation graph.

    Feed guard: if the current URL is not the Threads feed homepage
    (e.g. an unintended click during a prior action landed elsewhere),
    the home nav button is used to return before scrolling starts.
    A hard navigate_to fallback fires if the button is not found.
    """
    # ── Feed URL guard ────────────────────────────────────────────────
    _FEED_ROOTS = ("https://www.threads.com/", "https://www.threads.net/")
    try:
        current = driver.current_url

        # ── Media-viewer overlay: close with the X button, then re-check URL ─
        if _close_media_overlay(driver):
            current = driver.current_url   # refresh after close

        on_feed = any(current.rstrip("/") + "/" == root or current == root
                      for root in _FEED_ROOTS)
        if not on_feed:
            log.info(
                "[ PASSIVE ]  off-feed URL detected (%s) — returning to feed",
                current[:80],
            )
            if not click_home_button(driver):
                log.debug("[ PASSIVE ]  home button not found — hard navigate fallback")
                navigate_to(driver, TARGET_SOCIAL_URL)
            precise_sleep(random.uniform(1.2, 2.5))  # settle after nav
    except WebDriverException as exc:
        log.debug("[ PASSIVE ]  URL guard error: %s", exc)
    # ─────────────────────────────────────────────────────────────────

    scroll_time = random.uniform(25, 75)
    log.info("[ PASSIVE ]  scroll %.0fs", scroll_time)
    stochastic_scroll(driver, total_seconds=scroll_time)

    # Pause after scrolling stops — user finishes reading the post
    precise_sleep(random.uniform(1.0, 3.0))


def active_action(driver) -> None:
    """
    Active action — likes only.

    Improvements over the original:
    - URL-guarded: only runs on threads.net to avoid scanning the login page.
    - Stochastic pre-scroll: 50 % short, 30 % medium, 20 % none at all.
    - Variable like count: 0 (skip, 15%), 1 (50%), 2 (25%), 3 (10%).
    """
    current_url = driver.current_url
    # Accept both threads.net and threads.com — the browser redirects .net → .com
    on_threads = "threads.net" in current_url or "threads.com" in current_url
    if not on_threads:
        log.info(
            "[ ACTIVE ]  not on threads (%s) — passive scroll instead",
            current_url[:60],
        )
        stochastic_scroll(driver, total_seconds=random.uniform(15, 30))
        return

    log.info("[ ACTIVE ]  scanning for likes  url=%s", current_url[:60])
    liked = 0
    try:
        # Stochastic pre-scroll — 50% short, 30% medium, 20% skip entirely
        pre_roll = random.random()
        if pre_roll < 0.50:
            smooth_scroll_chunk(driver, random.randint(150, 400), step_px=5)
            precise_sleep(random.uniform(1.0, 3.0))
        elif pre_roll < 0.80:
            smooth_scroll_chunk(driver, random.randint(400, 800), step_px=6)
            precise_sleep(random.uniform(2.0, 4.0))
        else:
            # No pre-scroll — cursor is already resting on feed content
            precise_sleep(random.uniform(0.5, 1.5))

        candidates = _find_unliked_buttons(driver)
        if not candidates:
            log.info("No unliked posts in viewport — passive scroll instead")
            stochastic_scroll(driver, total_seconds=random.uniform(15, 30))
            return

        # Weighted like count: 75% 1-like, 25% 2-likes
        like_roll = random.random()
        if like_roll < 0.75:
            n_targets = 1
        else:
            n_targets = 2

        n_targets = min(n_targets, len(candidates))
        targets   = random.sample(candidates, n_targets)

        for btn in targets:
            if _attempt_like(driver, btn):
                liked += 1
                if liked < len(targets):
                    # Pause between likes — user glances at feed between hearts
                    precise_sleep(random.uniform(2.0, 5.0))

        # After liking, scroll slightly to load fresh content
        if liked > 0:
            precise_sleep(random.uniform(0.5, 1.5))
            smooth_scroll_chunk(driver, random.randint(250, 500), step_px=6)
            precise_sleep(random.uniform(1.0, 2.0))

    except (NoSuchElementException, WebDriverException) as exc:
        log.debug("Active action error: %s", exc)

    log.info("Active action complete. Likes delivered: %d", liked)


# ================================================================== #
#  READ-POST ACTION
# ================================================================== #

def read_post_action(driver) -> bool:
    """
    Click into a thread post to read the full reply chain, dwell naturally,
    then navigate back to the feed.

    This models the common behaviour of a user tapping a post to read the
    comments — a signal that strongly differentiates real users from bots
    that only scroll-and-like without ever opening individual threads.

    Flow:
      1. Find visible post links (href contains /post/ or /t/).
      2. Scroll the chosen link into loose view.
      3. Bezier-arc to the link and click.
      4. Dwell 5–18 s with stochastic scrolling through the reply chain.
      5. Navigate back to the feed.
    """
    try:
        current_url = driver.current_url
        if "threads.net" not in current_url and "threads.com" not in current_url:
            log.debug("read_post_action: not on Threads — skipping")
            return False

        # Collect visible post links
        links = driver.find_elements(
            By.CSS_SELECTOR,
            'a[href*="/post/"], a[href*="/t/"]',
        )
        visible = []
        for lnk in links:
            try:
                if lnk.is_displayed():
                    r = driver.execute_script(
                        "var r=arguments[0].getBoundingClientRect();"
                        "return {top:r.top, h:r.height};",
                        lnk,
                    )
                    vh = driver.execute_script("return window.innerHeight")
                    if r["h"] > 0 and 0 <= r["top"] <= vh:
                        visible.append(lnk)
            except Exception:
                continue

        if not visible:
            log.debug("read_post_action: no visible post links found")
            return False

        target = random.choice(visible[:10])
        scroll_element_into_loose_view(driver, target)

        # Deliberate hover pause — user deciding to click
        bezier_move(driver, target)
        precise_sleep(random.uniform(0.4, 1.2))

        _cdp_click(driver)
        debug_cursor_state(driver, "read-post-click")
        try:
            WebDriverWait(driver, 15).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException:
            pass
        inject_cursor_overlay(driver)
        init_cursor_pos(driver)

        dwell = random.uniform(5.0, 18.0)
        log.info("[ READ POST ]  reading thread for %.0fs", dwell)
        precise_sleep(random.uniform(1.0, 2.5))  # initial read of the post itself
        stochastic_scroll(driver, total_seconds=dwell)

        # Return via Home nav button — clicking the logo is more natural than
        # the browser back button when finishing reading a thread.
        # Falls back to navigate_history only if the nav icon is not found.
        if not click_home_button(driver):
            log.debug("read_post_action: home button not found — back fallback")
            navigate_history(driver, "back")
        precise_sleep(random.uniform(1.0, 2.5))
        log.info("[ READ POST ]  returned to feed")
        return True

    except (NoSuchElementException, TimeoutException, WebDriverException) as exc:
        log.debug("read_post_action failed: %s", exc)
        try:
            if driver.current_url != current_url:
                if not click_home_button(driver):
                    navigate_history(driver, "back")
        except Exception:
            pass
        return False


# ================================================================== #
#  COMMENT ACTION
# ================================================================== #

def comment_on_post(driver) -> bool:
    """
    Leave a short, natural comment on a random visible post in the feed.

    Flow:
      1. Find visible Reply buttons in the current viewport.
      2. Pick one at random; scroll it loosely into view.
      3. Read-pause — user finishes reading the post before replying.
      4. Bezier-arc to the Reply button and click.
      5. Wait up to 8 s for the comment text field to appear.
      6. Bezier-arc to the text field; type a random comment from COMMENT_POOL
         via human_type() (log-normal keystroke timing).
      7. Re-reading pause — user proofreads before posting.
      8. Find the Post button closest to the text field (avoids matching the
         global “New post” compose button); bezier-arc and click.
      9. Post-click pause — watching the reply appear.
    Returns True on success.
    """
    try:
        current_url = driver.current_url
        on_threads  = "threads.net" in current_url or "threads.com" in current_url
        if not on_threads:
            log.debug("comment_on_post: not on Threads — skipping")
            return False

        # 1. Collect visible Reply buttons
        reply_btns = driver.find_elements(By.CSS_SELECTOR, REPLY_BTN_CSS)
        visible = []
        for btn in reply_btns:
            try:
                r  = driver.execute_script(
                    "var r=arguments[0].getBoundingClientRect();"
                    "return {top:r.top, h:r.height};",
                    btn,
                )
                vh = driver.execute_script("return window.innerHeight")
                if r["h"] > 0 and 0 <= r["top"] <= vh and btn.is_displayed():
                    visible.append(btn)
            except Exception:
                continue

        if not visible:
            log.debug("comment_on_post: no visible reply buttons found")
            return False

        target_btn = random.choice(visible[:8])
        scroll_element_into_loose_view(driver, target_btn)

        # 2. Read-pause — user reads the post before deciding to reply
        precise_sleep(random.uniform(2.5, 6.0))

        # 3. Bezier-arc to Reply and click
        bezier_move(driver, target_btn)
        precise_sleep(random.uniform(0.3, 0.7))
        try:
            _cdp_click(driver)
        except WebDriverException:
            driver.execute_script("arguments[0].click();", target_btn)
        debug_cursor_state(driver, "comment-reply-click")

        # 4. Wait for the comment box to appear
        try:
            comment_box = WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, COMMENT_BOX_CSS))
            )
        except TimeoutException:
            log.debug("comment_on_post: comment box did not appear after Reply click")
            return False

        precise_sleep(random.uniform(0.5, 1.2))   # settle before moving to box

        # 5. Bezier-arc to text field, then type
        bezier_move(driver, comment_box)
        precise_sleep(random.uniform(0.3, 0.6))
        comment = random.choice(COMMENT_POOL)
        log.info("[ COMMENT ]  typing reply: %r", comment)
        human_type(comment_box, comment, driver)

        # 6. Re-reading pause
        precise_sleep(random.uniform(1.2, 3.0))

        # 7. Find the Post button.
        #    Multiple matches can exist (one per visible reply form).
        #    We pick the one whose vertical midpoint is closest to the comment
        #    box — this reliably targets the active reply form’s submit button
        #    without misidentifying the global compose button.
        try:
            box_mid = driver.execute_script(
                "var r=arguments[0].getBoundingClientRect();"
                "return r.top + r.height / 2;",
                comment_box,
            )
            post_btns = driver.find_elements(By.XPATH, COMMENT_POST_XPATH)
            post_btns = [b for b in post_btns if b.is_displayed()]
            if not post_btns:
                raise NoSuchElementException("Post button not found")
            # Sort by distance from the comment box midpoint
            def _dist(b):
                try:
                    r = driver.execute_script(
                        "var r=arguments[0].getBoundingClientRect();"
                        "return r.top + r.height / 2;",
                        b,
                    )
                    return abs(r - box_mid)
                except Exception:
                    return 9999
            post_btn = min(post_btns, key=_dist)
        except (NoSuchElementException, WebDriverException):
            log.debug("comment_on_post: Post button not found — aborting")
            return False

        scroll_element_into_loose_view(driver, post_btn)
        bezier_move(driver, post_btn)
        precise_sleep(random.uniform(0.3, 0.6))
        try:
            _cdp_click(driver)
        except WebDriverException:
            driver.execute_script("arguments[0].click();", post_btn)
        debug_cursor_state(driver, "comment-post-click")

        # 8. Post-click pause — watching the reply appear
        precise_sleep(random.uniform(1.5, 3.5))
        log.info("[ COMMENT ]  comment posted successfully")
        return True

    except (NoSuchElementException, TimeoutException, WebDriverException) as exc:
        log.debug("comment_on_post failed: %s", exc)
        return False


# ================================================================== #
#  POSTING ENGINE
# ================================================================== #
# Creates original posts from POST_CAPTION_POOL with optional media from
# MEDIA_POOL_DIR.  A persistent state file (POST_STATE_FILE) tracks per-
# profile daily counts and account age to enforce a progressive ramp-up:
#   Days  1– 5 : 0 posts/day  (account establishing credibility)
#   Days  6–10 : 1 post/day
#   Days 11–14 : 2 posts/day
#   Day  15+   : 3 posts/day
# A hard 2-hour minimum gap (POST_MIN_INTERVAL_SEC) between posts is
# enforced on top of the daily quota.
# ================================================================== #

def _load_post_state() -> dict:
    """Load per-profile posting state from POST_STATE_FILE (creates if absent)."""
    if os.path.exists(POST_STATE_FILE):
        try:
            with open(POST_STATE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("post_state load failed (%s) — starting fresh", exc)
    return {}


def _save_post_state(state: dict) -> None:
    try:
        with open(POST_STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except OSError as exc:
        log.warning("post_state save failed: %s", exc)


def _post_daily_quota(days_old: int) -> int:
    """Max posts per day for an account days_old days old (0-indexed)."""
    if days_old < 5:   return 0   # days 1–5:  no posts
    if days_old < 10:  return 1   # days 6–10: 1/day
    if days_old < 14:  return 2   # days 11–14: 2/day
    return 3                       # day 15+: 3/day


def _ensure_profile_in_state(profile_id: str, state: dict) -> None:
    """Register profile's first-seen date if not already recorded."""
    if profile_id and profile_id not in state:
        today = date.today().isoformat()
        state[profile_id] = {
            "first_seen":    today,
            "daily_counts":  {},
            "last_post_ts":  0.0,
            "next_post_ts":  0.0,   # Poisson-sampled; 0 = no restriction yet
        }
        _save_post_state(state)
        log.info("[ POST ]  registered account start date: %s  (day 1 of ramp-up)", today)


def _next_post_delay_sec(days_old: int) -> float:
    """
    Sample the next inter-post gap using an exponential distribution
    (continuous Poisson inter-arrival model).  The mean gap and floor/ceiling
    are tuned per ramp-up phase so natural clustering *and* silence emerge:

      Days  6–10  mean=36h  floor=8h  ceil=84h  (mostly one post every 1-2 days)
      Days 11–14  mean=24h  floor=6h  ceil=52h  (1-2 per day or a skip day)
      Day  15+    mean=18h  floor=4h  ceil=40h  (tighter cadence, still varies)

    exponential(1/mean) produces a right-skewed distribution: most gaps cluster
    near the mean while the long tail models multi-day silences organically.
    """
    if days_old < 10:
        mean_h, floor_h, ceil_h = 36.0, 8.0, 84.0
    elif days_old < 14:
        mean_h, floor_h, ceil_h = 24.0, 6.0, 52.0
    else:
        mean_h, floor_h, ceil_h = 18.0, 4.0, 40.0
    gap_h = max(floor_h, min(random.expovariate(1.0 / mean_h), ceil_h))
    return gap_h * 3600.0


def _can_post_now(profile_id: str, state: dict) -> bool:
    """Return True if this profile is allowed to post right now."""
    if not profile_id or profile_id in ("manual", ""):
        log.debug("_can_post_now: no meaningful profile_id — post suppressed")
        return False

    _ensure_profile_in_state(profile_id, state)
    entry = state[profile_id]
    today = date.today().isoformat()

    # Poisson-sampled next-post gate (plus absolute floor)
    now = time.time()
    next_ts = entry.get("next_post_ts", 0.0)
    if now < next_ts:
        wait_min = (next_ts - now) / 60
        log.info("[ POST ]  skipping — next post allowed in %.0f min (Poisson gate)", wait_min)
        return False
    # Always enforce hard floor as well
    elapsed = now - entry.get("last_post_ts", 0.0)
    if elapsed < POST_MIN_GAP_SEC:
        log.info(
            "[ POST ]  skipping — %.0f min since last post (hard floor)",
            elapsed / 60,
        )
        return False

    # Account age ramp-up
    days_old = (
        date.fromisoformat(today) - date.fromisoformat(entry["first_seen"])
    ).days
    quota = _post_daily_quota(days_old)
    if quota == 0:
        log.info(
            "[ POST ]  skipping — account age %d day(s), quota=0 during ramp-up",
            days_old,
        )
        return False

    # Daily cap
    today_count = entry.get("daily_counts", {}).get(today, 0)
    if today_count >= quota:
        log.info(
            "[ POST ]  skipping — daily quota %d reached (%d posted today)",
            quota, today_count,
        )
        return False

    return True


def _record_post(profile_id: str, state: dict) -> None:
    """Increment daily count, update last-post timestamp, and sample next-post gate."""
    today = date.today().isoformat()
    _ensure_profile_in_state(profile_id, state)
    now = time.time()
    state[profile_id]["last_post_ts"] = now

    # Sample next-post allowed time from an exponential distribution.
    days_old = (
        date.fromisoformat(today) - date.fromisoformat(state[profile_id]["first_seen"])
    ).days
    gap_sec = _next_post_delay_sec(days_old)
    state[profile_id]["next_post_ts"] = now + gap_sec
    log.info(
        "[ POST ]  next post allowed in %.1f h (Poisson gap=%.1fh)",
        gap_sec / 3600, gap_sec / 3600,
    )

    daily = state[profile_id].setdefault("daily_counts", {})
    daily[today] = daily.get(today, 0) + 1
    _save_post_state(state)
    log.info(
        "[ POST ]  state updated  |  profile=%s  today=%d  first_seen=%s",
        profile_id, daily[today], state[profile_id]["first_seen"],
    )


# ================================================================== #
#  IMAGE UNIQUIFIER  (per-profile re-encode)
# ================================================================== #

def _prepare_image_for_profile(src_path: str, profile_id: str) -> str:
    """
    Return a uniquified, re-encoded copy of src_path scoped to profile_id.

    Transformations applied every call so the output file always differs,
    even when two profiles choose the same source image:

      1. Random 1–3 px crop on every edge  (geometry changes)
      2. ±2 % brightness adjustment         (pixel values change)
      3. ±2 % contrast adjustment           (pixel values change)
      4. Re-encode at randomised quality    (file bytes change)
         JPEG/WebP: base-88 ± 2–5 pts;
         PNG: lossless but fresh encoding.
      5. Strip all original EXIF metadata.
      6. Inject synthetic EXIF DateTimeOriginal within the last 48 h
         (requires piexif; silently skipped if not installed).

    The output is written to a per-profile scratch folder inside the OS
    temp directory.  The folder is named after a prefix of profile_id so
    stale copies from previous sessions are easy to identify and the same
    profile never sees another profile's scratch files.
    """
    from PIL import Image, ImageEnhance  # Pillow – always installed

    try:
        import piexif as _piexif
        _have_piexif = True
    except ImportError:
        _have_piexif = False
        log.debug("_prepare_image_for_profile: piexif not installed — EXIF injection skipped")

    # Per-profile scratch directory
    safe_pid = (profile_id or "anon")[:16].replace("-", "")
    profile_dir = os.path.join(_POST_TEMP_DIR, safe_pid)
    os.makedirs(profile_dir, exist_ok=True)

    img = Image.open(src_path).convert("RGB")
    w, h = img.size

    # 1. Tiny random crop — different geometry per call
    left   = random.randint(1, 3)
    top    = random.randint(1, 3)
    right  = random.randint(1, 3)
    bottom = random.randint(1, 3)
    img = img.crop((left, top, w - right, h - bottom))

    # 2. Brightness ±2 %
    b_factor = 1.0 + random.uniform(-0.02, 0.02)
    img = ImageEnhance.Brightness(img).enhance(b_factor)

    # 3. Contrast ±2 %
    c_factor = 1.0 + random.uniform(-0.02, 0.02)
    img = ImageEnhance.Contrast(img).enhance(c_factor)

    # 4. Re-encode at randomised quality
    ext = os.path.splitext(src_path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        save_fmt  = "JPEG"
        quality   = 88 + random.randint(-5, 5)
    elif ext == ".webp":
        save_fmt  = "WEBP"
        quality   = 88 + random.randint(-5, 5)
    else:  # .png
        save_fmt  = "PNG"
        quality   = None

    # 5+6. Build synthetic EXIF — random timestamp in the last 48 h
    exif_bytes = b""
    if _have_piexif and save_fmt in ("JPEG", "WEBP"):
        try:
            ts = datetime.fromtimestamp(time.time() - random.randint(0, 48 * 3600))
            ts_str = ts.strftime("%Y:%m:%d %H:%M:%S").encode()
            exif_dict = {
                "0th":  {},
                "Exif": {
                    _piexif.ExifIFD.DateTimeOriginal:  ts_str,
                    _piexif.ExifIFD.DateTimeDigitized: ts_str,
                },
                "GPS":  {},
                "1st":  {},
            }
            exif_bytes = _piexif.dump(exif_dict)
        except Exception as exc:
            log.debug("_prepare_image_for_profile: EXIF build failed (%s)", exc)
            exif_bytes = b""

    # Unique output filename — hash of inputs + wall clock so every call differs
    uid = hashlib.md5(f"{src_path}{time.time_ns()}{profile_id}".encode()).hexdigest()[:10]
    out_path = os.path.join(profile_dir, f"post_{uid}{ext}")

    save_kwargs: dict = {}
    if quality is not None:
        save_kwargs["quality"] = quality
    if save_fmt == "JPEG":
        save_kwargs["optimize"] = True
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes
    elif save_fmt == "WEBP" and exif_bytes:
        save_kwargs["exif"] = exif_bytes

    img.save(out_path, format=save_fmt, **save_kwargs)
    log.info(
        "[ POST ]  image uniquified  |  crop=(%d,%d,%d,%d)  "
        "b=%.3f  c=%.3f  q=%s  → %s",
        left, top, right, bottom, b_factor, c_factor,
        quality if quality else "lossless",
        os.path.basename(out_path),
    )
    return out_path


# ================================================================== #
#  CAPTION HUMANISER
# ================================================================== #

def _mutate_caption(text: str) -> str:
    """
    Apply a random subset of natural social-media imperfections to a single
    caption string.  Mutations are probabilistic and can stack.

    Rules:
      • Remove Oxford comma (, and / , or)           — 60 % of eligible
      • Drop leading capital (casual register)        — 30 %
      • Strip terminal period                         — 40 %  (leaves !? alone)
      • Replace terminal period with "…"              — 15 %
      • Replace mid-sentence " I " with " i "         —  8 % of eligible
      • Append an emoji from POST_CAPTION_EMOJIS      — 35 %
      • Append a casual filler word (tbh/ngl/idk …)   — 12 %
    """
    # Oxford comma removal
    if random.random() < 0.60:
        text = re.sub(r",\s+(and|or)\s+", lambda m: f" {m.group(1)} ", text)

    # Lowercase first character
    if text and random.random() < 0.30:
        text = text[0].lower() + text[1:]

    # Terminal-period mutation
    if text.endswith("."):
        r = random.random()
        if r < 0.40:
            text = text[:-1]        # bare end
        elif r < 0.55:
            text = text[:-1] + "…"  # ellipsis

    # Mid-sentence "I" → "i" (casual / typo)
    if random.random() < 0.08:
        text = re.sub(r"(?<=\s)I(?=\s)", "i", text)

    # Trailing emoji
    if random.random() < 0.35:
        text = text.rstrip() + " " + random.choice(POST_CAPTION_EMOJIS)

    # Casual filler
    if random.random() < 0.12:
        filler = random.choice(["tbh", "ngl", "idk", "lol", "honestly", "fr"])
        text = text.rstrip(".!?…").rstrip() + f" {filler}"

    return text


def _humanize_caption(pool: list) -> str:
    """
    Select a caption and apply realistic length + imperfection variation.

    Length tiers (proportional to authentic posting behaviour):
      5 %  emoji-only      — single character from POST_CAPTION_EMOJIS
     10 %  short fragment  — 1-3 casual words from POST_CAPTION_SHORTS
     60 %  single sentence — one entry from pool, mutated via _mutate_caption()
     25 %  double          — two mutated pool entries joined with a line-break,
                             comma, or em-dash (varied per call)

    Ensures no two profiles ever get the same output even from the same pool.
    """
    tier = random.random()

    if tier < 0.05:
        # Emoji-only — optionally doubled
        e = random.choice(POST_CAPTION_EMOJIS)
        if random.random() < 0.3:
            e += " " + random.choice(POST_CAPTION_EMOJIS)
        return e

    if tier < 0.15:
        # Short fragment
        base = random.choice(POST_CAPTION_SHORTS)
        if random.random() < 0.40:
            base += " " + random.choice(POST_CAPTION_EMOJIS)
        return base

    if tier < 0.75:
        # Single sentence, mutated
        return _mutate_caption(random.choice(pool))

    # Double: two independently mutated sentences
    a = _mutate_caption(random.choice(pool))
    b = _mutate_caption(random.choice(pool))
    join = random.choice(["newline", "comma", "dash"])
    if join == "newline":
        return f"{a}\n{b}"
    if join == "comma":
        return f"{a.rstrip('.!?…')}, {b}"
    return f"{a.rstrip('.!?…')} — {b}"


def create_post(driver, profile_id: str) -> bool:
    """
    Create an original Threads post with a caption from POST_CAPTION_POOL
    and optionally an image from MEDIA_POOL_DIR.

    Flow:
      1.  Guard — _can_post_now() checks quota + 2-hour cooldown.
      2.  Pick a random caption and (if MEDIA_POOL_DIR is set) a random image.
      3.  Find the compose / New post button in the nav sidebar.
      4.  Bezier-arc to the button and click to open the compose modal.
      5.  Attach image via the hidden <input type="file"> if a path was picked.
      6.  Bezier-arc to the textbox and type the caption with human_type().
      7.  Re-read pause (1.5–4 s) — mimics proof-reading before posting.
      8.  Find the Post button in the modal and click it.
      9.  Wait for the modal to dismiss, then call _record_post().

    Selector notes:
      COMPOSE_BTN — tried in priority order; first visible match wins.
      File input   — made temporarily visible via JS so send_keys works on
                     the hidden <input type="file"> without a click chain.
      Post button  — reuses COMMENT_POST_XPATH (same "Post" text node).
    """
    state = _load_post_state()
    if not _can_post_now(profile_id, state):
        return False

    if not POST_CAPTION_POOL:
        log.warning("create_post: POST_CAPTION_POOL is empty — cannot post")
        return False

    # Pick media — cross-profile deduplication: never allow two profiles to
    # post from the same source file.  The global "_used_images" list in state
    # tracks source basenames across all profiles so platform-side perceptual
    # hashing cannot link accounts even when the encoded bytes differ.
    image_path = None
    if MEDIA_POOL_DIR and os.path.isdir(MEDIA_POOL_DIR):
        all_images = [
            os.path.abspath(os.path.join(MEDIA_POOL_DIR, f))
            for f in os.listdir(MEDIA_POOL_DIR)
            if os.path.splitext(f)[1].lower() in POST_MEDIA_EXTENSIONS
        ]
        if all_images:
            used_globally = set(state.get("_used_images", []))
            fresh = [p for p in all_images if os.path.basename(p) not in used_globally]
            if not fresh:
                # Pool exhausted — reset and cycle (all images used at least once)
                log.info("[ POST ]  image pool fully cycled — resetting cross-profile dedup list")
                state["_used_images"] = []
                _save_post_state(state)
                fresh = all_images
            src = random.choice(fresh)
            # Mark this source as globally used BEFORE uniquifying, so concurrent
            # sessions (different profiles) cannot race and pick the same file.
            used_list = state.setdefault("_used_images", [])
            basename = os.path.basename(src)
            if basename not in used_list:
                used_list.append(basename)
                _save_post_state(state)
            try:
                image_path = _prepare_image_for_profile(src, profile_id)
            except Exception as exc:
                log.warning("[ POST ]  image uniquification failed (%s) — using original", exc)
                image_path = src

    caption = _humanize_caption(POST_CAPTION_POOL)
    log.info(
        "[ POST ]  composing  |  caption=%r  |  media=%s",
        caption,
        os.path.basename(image_path) if image_path else "none",
    )

    try:
        # 1. Ensure we're on the Threads feed (compose button lives in the nav)
        url = driver.current_url or ""
        if "threads.net" not in url and "threads.com" not in url:
            navigate_to(driver, TARGET_SOCIAL_URL)

        # 2. Find the compose / New post button (multiple selector fallbacks)
        compose_btn = None
        for kind, sel in COMPOSE_BTN_SELECTORS:
            by = By.CSS_SELECTOR if kind == "css" else By.XPATH
            visible = [el for el in driver.find_elements(by, sel) if el.is_displayed()]
            if visible:
                compose_btn = visible[0]
                break

        if not compose_btn:
            log.debug("create_post: compose button not found — aborting")
            return False

        scroll_element_into_loose_view(driver, compose_btn)
        bezier_move(driver, compose_btn)
        precise_sleep(random.uniform(0.4, 0.9))
        try:
            _cdp_click(driver)
        except WebDriverException:
            driver.execute_script("arguments[0].click();", compose_btn)

        # 3. Wait for the compose modal's contenteditable text area.
        #    Use the compose-specific selector (data-lexical-editor + aria-placeholder)
        #    so we never accidentally match a reply/comment box still in the DOM.
        try:
            text_box = WebDriverWait(driver, 10).until(
                lambda d: next(
                    (
                        el for el in d.find_elements(By.CSS_SELECTOR, COMPOSE_TEXTBOX_CSS)
                        if el.is_displayed()
                    ),
                    None,
                )
            )
            if not text_box:
                raise TimeoutException("compose textbox not visible")
        except TimeoutException:
            log.debug("create_post: compose modal textarea did not appear")
            driver.execute_script(
                "document.dispatchEvent(new KeyboardEvent('keydown',"
                "{key:'Escape',keyCode:27,bubbles:true}));"
            )
            return False

        # Brief settle — SPA modal animation
        precise_sleep(random.uniform(0.6, 1.2))

        # 4. Attach image — click the Attach-media button (opens OS file dialog),
        #    simulate human file-locate time, then dismiss the dialog by pasting
        #    the absolute path into the filename field via pyautogui + pyperclip.
        #    Falls back to the hidden-input send_keys trick if either library is absent.
        if image_path:
            try:
                attach_btns = [
                    el for el in driver.find_elements(
                        By.CSS_SELECTOR, COMPOSE_ATTACH_BTN_CSS
                    )
                    if el.is_displayed()
                ]
                if not attach_btns:
                    raise WebDriverException("Attach media button not found")

                bezier_move(driver, attach_btns[0])
                precise_sleep(random.uniform(0.3, 0.6))

                # Click → OS file dialog opens
                try:
                    _cdp_click(driver)
                except WebDriverException:
                    driver.execute_script("arguments[0].click();", attach_btns[0])

                try:
                    import pyautogui as _pag
                    import pyperclip as _ppc

                    # ── Simulate locating the file in the file manager ──────────
                    # Dialog is open; human browses folders, scrolls, finds file.
                    # Truncated-normal centred at 5 s, clamped to [3, 9] s.
                    _locate_delay = max(3.0, min(9.0, random.gauss(5.0, 1.5)))
                    log.debug("create_post: OS dialog open — file-locate pause %.1fs", _locate_delay)
                    precise_sleep(_locate_delay)

                    # ── Type path via clipboard paste → Enter ────────────────────
                    # Copy the absolute path to the system clipboard so we don't
                    # have to deal with backslashes / special chars char-by-char.
                    _ppc.copy(os.path.abspath(image_path))
                    precise_sleep(random.uniform(0.10, 0.25))   # clipboard settle
                    _pag.hotkey("ctrl", "a")                  # select existing text in field
                    precise_sleep(random.uniform(0.06, 0.14))
                    _pag.hotkey("ctrl", "v")                  # paste absolute path
                    precise_sleep(random.uniform(0.15, 0.35))
                    _pag.press("enter")                       # confirm → dialog closes
                    log.info("[ POST ]  media attached via OS dialog: %s",
                             os.path.basename(image_path))
                    # Allow SPA to receive the file-change event and start upload
                    precise_sleep(random.uniform(1.5, 2.5))

                except ImportError:
                    # ── Fallback: inject path into hidden <input type="file"> ────
                    log.debug("create_post: pyautogui/pyperclip missing — hidden-input fallback"
                              " (pip install pyautogui pyperclip to enable OS-dialog path)")
                    # Brief wait for the SPA to create / unhide the input
                    precise_sleep(random.uniform(0.4, 0.8))
                    file_inputs = driver.find_elements(By.CSS_SELECTOR, COMPOSE_FILE_INPUT_CSS)
                    if file_inputs:
                        fi = file_inputs[0]
                        driver.execute_script(
                            "arguments[0].style.display    = 'block';"
                            "arguments[0].style.visibility = 'visible';"
                            "arguments[0].style.opacity    = '1';",
                            fi,
                        )
                        fi.send_keys(image_path)
                        log.info("[ POST ]  media attached (fallback): %s",
                                 os.path.basename(image_path))
                    else:
                        log.debug("create_post: no file input found — text-only")
                        image_path = None

                # Wait for upload thumbnail / preview to render
                if image_path:
                    precise_sleep(random.uniform(2.0, 4.0))

            except WebDriverException as exc:
                log.debug("create_post: media attach failed (%s) — text-only fallback", exc)
                image_path = None

        # 5. Re-query the compose textbox before typing.
        #    The OS file dialog interaction (pyautogui paste + Enter) causes
        #    React to re-render the compose modal, which invalidates the
        #    WebElement reference captured before the dialog was opened.
        #    Using a stale reference in human_type() raises
        #    StaleElementReferenceException → the outer handler fires Escape
        #    and returns False, making the session loop retry indefinitely.
        if image_path:
            try:
                text_box = WebDriverWait(driver, 8).until(
                    lambda d: next(
                        (
                            el for el in d.find_elements(By.CSS_SELECTOR, COMPOSE_TEXTBOX_CSS)
                            if el.is_displayed()
                        ),
                        None,
                    )
                )
                if not text_box:
                    raise TimeoutException("compose textbox vanished after media attach")
                log.debug("create_post: textbox re-queried after media attach")
            except TimeoutException:
                log.debug("create_post: could not re-find textbox after media attach — aborting")
                driver.execute_script(
                    "document.dispatchEvent(new KeyboardEvent('keydown',"
                    "{key:'Escape',keyCode:27,bubbles:true}));"
                )
                return False

        bezier_move(driver, text_box)
        precise_sleep(random.uniform(0.3, 0.7))
        human_type(text_box, caption, driver)

        # 6. Re-read pause — mimics proof-reading before hitting Post
        reread_s = random.uniform(1.5, 4.0)
        log.info("[ POST ]  re-reading before submit (%.1fs)…", reread_s)
        precise_sleep(reread_s)

        # 7. Find the Post submit button scoped to the compose modal.
        #
        #    Confirmed DOM (from live inspection):
        #      <div role="button"><div>Post</div></div>
        #
        #    IMPORTANT: identical Post buttons exist in every visible comment
        #    reply form in the feed behind the modal.  A global XPath therefore
        #    returns the wrong element.  Instead we walk UP from text_box to
        #    find the modal's own container, then search within it.
        #
        #    Strategy:
        #      Pass A — JS ancestor walk from text_box (most reliable, scoped).
        #      Pass B — JS global scan as last resort, logging all visible
        #               button texts as a diagnostic if it also fails.
        #    Each pass is retried up to 3 times (2 s apart) so React has time
        #    to activate the button after processing the typed text.
        post_btn = None

        for _attempt in range(3):
            # Pass A: walk up from text_box → find Post button in same container
            post_btn = driver.execute_script("""
                var textbox = arguments[0];
                // Walk up ancestors looking for a container that owns a Post button
                var node = textbox.parentElement;
                for (var depth = 0; depth < 20; depth++) {
                    if (!node) break;
                    // Look for a direct-child-div Post button within this ancestor
                    var btns = node.querySelectorAll('div[role="button"]');
                    for (var i = 0; i < btns.length; i++) {
                        var btn = btns[i];
                        if (btn.offsetParent === null) continue;  // not visible
                        // Direct child <div> whose sole text is "Post"
                        var kids = btn.children;
                        for (var k = 0; k < kids.length; k++) {
                            if (kids[k].tagName === 'DIV' &&
                                (kids[k].innerText || '').trim() === 'Post') {
                                return btn;
                            }
                        }
                        // Also accept aria-label="Post" directly on the button
                        if ((btn.getAttribute('aria-label') || '').trim() === 'Post') {
                            return btn;
                        }
                    }
                    node = node.parentElement;
                }
                return null;
            """, text_box)

            if post_btn:
                log.debug("create_post: Post button found via modal-scoped ancestor walk (attempt %d)", _attempt + 1)
                break

            # Pass B: global scan as fallback
            log.debug("create_post: ancestor walk attempt %d missed — global JS scan", _attempt + 1)
            post_btn = driver.execute_script("""
                var btns = document.querySelectorAll('div[role="button"]');
                for (var i = 0; i < btns.length; i++) {
                    var btn = btns[i];
                    if (btn.offsetParent === null) continue;
                    var kids = btn.children;
                    for (var k = 0; k < kids.length; k++) {
                        if (kids[k].tagName === 'DIV' &&
                            (kids[k].innerText || '').trim() === 'Post') {
                            return btn;
                        }
                    }
                    if ((btn.getAttribute('aria-label') || '').trim() === 'Post') {
                        return btn;
                    }
                }
                return null;
            """)
            if post_btn:
                log.debug("create_post: Post button found via global JS scan (attempt %d)", _attempt + 1)
                break

            precise_sleep(2.0)   # let React activate the button after text entry

        if not post_btn:
            try:
                btn_texts = driver.execute_script("""
                    var els = document.querySelectorAll('div[role="button"], button');
                    var out = [];
                    for (var i = 0; i < els.length; i++) {
                        var el = els[i];
                        if (el.offsetParent !== null) {
                            out.push((el.innerText || el.getAttribute('aria-label') || '')
                                     .trim().replace(/\\n/g,' ').slice(0,40));
                        }
                    }
                    return out;
                """)
                log.warning(
                    "create_post: Post button NOT found after 3 attempts — "
                    "visible buttons in DOM: %s", btn_texts[:25],
                )
            except Exception:
                log.warning("create_post: Post button NOT found and diagnostic also failed")
            driver.execute_script(
                "document.dispatchEvent(new KeyboardEvent('keydown',"
                "{key:'Escape',keyCode:27,bubbles:true}));"
            )
            return False

        scroll_element_into_loose_view(driver, post_btn)
        bezier_move(driver, post_btn)
        precise_sleep(random.uniform(0.4, 0.9))
        try:
            _cdp_click(driver)
        except WebDriverException:
            driver.execute_script("arguments[0].click();", post_btn)
        debug_cursor_state(driver, "post-submit-click")

        # 8. Wait for modal to close (compose textbox disappears on success)
        try:
            WebDriverWait(driver, 12).until(
                lambda d: not d.find_elements(By.CSS_SELECTOR, COMPOSE_TEXTBOX_CSS)
            )
        except TimeoutException:
            pass
        precise_sleep(random.uniform(1.5, 3.0))

        _record_post(profile_id, state)
        log.info("[ POST ]  new post published successfully")

        # 9. Post-dwell: stay on own post watching for early reactions.
        #    Real users re-read their caption, watch the like count, maybe
        #    read the first comment.  30–65 s with organic cursor movement.
        _dwell_secs = random.uniform(30.0, 65.0)
        log.info("[ POST ]  post-dwell %.0fs — watching for reactions", _dwell_secs)
        _dwell_end = time.time() + _dwell_secs
        while time.time() < _dwell_end:
            remaining = _dwell_end - time.time()
            sit = min(random.uniform(4.0, 12.0), remaining)
            precise_sleep(sit)
            if time.time() >= _dwell_end:
                break
            # Occasional small cursor drift — eye scanning caption / like count
            if random.random() < 0.55:
                try:
                    vw_d = driver.execute_script("return window.innerWidth")
                    vh_d = driver.execute_script("return window.innerHeight")
                    nx = max(int(vw_d * 0.10), min(int(vw_d * 0.90),
                             _cursor_pos[0] + int(random.gauss(0, vw_d * 0.06))))
                    ny = max(int(vh_d * 0.20), min(int(vh_d * 0.80),
                             _cursor_pos[1] + int(random.gauss(0, vh_d * 0.06))))
                    bezier_move_to_coords(driver, nx, ny, tag="post-dwell")
                except Exception:
                    pass
        log.info("[ POST ]  post-dwell complete — returning to normal browse")
        return True

    except (NoSuchElementException, TimeoutException, WebDriverException) as exc:
        log.debug("create_post failed: %s", exc)
        try:
            driver.execute_script(
                "document.dispatchEvent(new KeyboardEvent('keydown',"
                "{key:'Escape',keyCode:27,bubbles:true}));"
            )
        except Exception:
            pass
        return False


def post_action(driver, profile_id: str) -> None:
    """Session-loop dispatch wrapper for create_post."""
    log.info("[ POST ACTION ]  creating a new post")
    create_post(driver, profile_id)


def run_social_session(
    driver,
    session_seconds: float,
    w_like=None,
    w_notify: float = 0.03,
    w_profile: float = 0.06,
    w_read: float = 0.08,
    w_comment: float = 0.05,
    w_follow: float = 0.03,
    w_top: float = 0.03,
    w_search: float = 0.06,
    w_post: float = 0.02,
    profile_id: str = "",
) -> None:
    """
    Session loop with:
    - Per-session randomised passive/active split (truncated normal, not fixed 80/20).
    - Engagement variety: occasional notification check or profile view.
    - Guaranteed at least one active action per session (forced if < 60 s remain).
    """
    global _session_followed
    _session_followed = set()          # reset per-session seen-profile cache

    session_start_ts = time.time()     # used to enforce passive phase before posting
    deadline    = session_start_ts + session_seconds
    count       = 0
    active_done = False

    # Draw per-session active probability from truncated normal (mean 0.22, SD 0.08)
    # clamped to [0.20, 0.45] — sessions range from mostly-passive to moderately active.
    active_prob = w_like if w_like is not None else max(0.20, min(0.45, random.gauss(0.22, 0.08)))
    log.info(
        "Session active probability this run: %.2f  (weights: notify=%.2f profile=%.2f "
        "read=%.2f comment=%.2f follow=%.2f top=%.2f search=%.2f post=%.2f)",
        active_prob, w_notify, w_profile, w_read, w_comment, w_follow, w_top, w_search, w_post,
    )

    # Precompute cumulative dispatch thresholds from individual weights.
    t_notify  = active_prob + w_notify
    t_profile = t_notify   + w_profile
    t_read    = t_profile  + w_read
    t_comment = t_read     + w_comment
    t_follow  = t_comment  + w_follow
    t_top     = t_follow   + w_top
    t_search  = t_top      + w_search
    t_post    = t_search   + w_post

    while time.time() < deadline:
        time_left = deadline - time.time()

        # Force active if we haven’t done one yet and time is almost up
        if not active_done and time_left < 60:
            log.info("Forcing active action (session guarantee).")
            active_action(driver)
            active_done = True
        else:
            roll = random.random()
            if roll < active_prob:
                active_action(driver)
                active_done = True
            elif roll < t_notify:
                # notify weight (default ~3 %): check notifications
                check_notifications_action(driver)
            elif roll < t_profile:
                # profile weight (default ~6 %): click feed author link, browse profile
                view_profile_from_feed(driver)
            elif roll < t_read:
                # read weight (default ~8 %): click into a thread, read reply chain, go back
                read_post_action(driver)
            elif roll < t_comment:
                # comment weight (default ~5 %): leave a short comment on a feed post
                comment_on_post(driver)
            elif roll < t_follow:
                # follow weight (default ~3 %): quick-follow from feed (+) button
                follow_from_feed(driver)
            elif roll < t_top:
                # top weight (default ~3 %): return to top via logo
                return_to_top_action(driver)
            elif roll < t_search:
                # search weight (default ~6 %): open search page, dwell, return home
                visit_search_action(driver)
            elif roll < t_post:
                # post weight (default ~2 %): create a new original post
                # Gate: only after a meaningful passive phase so the session
                # pattern is scroll→read→decide-to-post, never post-immediately.
                passive_elapsed = time.time() - session_start_ts
                if passive_elapsed >= POST_PASSIVE_PHASE_SEC:
                    post_action(driver, profile_id)
                else:
                    wait_min = (POST_PASSIVE_PHASE_SEC - passive_elapsed) / 60
                    log.info(
                        "[ POST ]  passive phase not complete (%.1f min remaining) "
                        "— deferring post to scroll",
                        wait_min,
                    )
                    passive_action(driver)
            else:
                passive_action(driver)

        count += 1
        precise_sleep(random.uniform(1, 3))

    log.info("Session complete. Total actions: %d", count)


# ================================================================== #
#  SINGLE PROFILE WARM-UP ORCHESTRATOR
# ================================================================== #

def warm_profile(profile_id: str, weights: dict | None = None) -> None:
    """Full end-to-end warm-up for one NstBrowser profile."""
    driver   = None
    launched = False

    try:
        # 1. Launch browser via POST /api/v2/browsers/{profileId}
        info     = start_profile(profile_id)
        launched = True

        # 2. Attach Selenium via CDP
        driver = connect_selenium(info["webSocketDebuggerUrl"])
        driver.set_page_load_timeout(30)
        init_cursor_pos(driver)    # seed a random start position so the first park arc is never flat at y=0

        # 3. Pre-flight: Wikipedia only
        run_preflight(driver)

        # 4. Navigate to Threads
        log.info("Navigating to %s", TARGET_SOCIAL_URL)
        navigate_to(driver, TARGET_SOCIAL_URL)
        precise_sleep(random.uniform(2, 5))

        # 4b. Verify the profile is logged in before wasting a session
        if not check_login_status(driver):
            log.error(
                "Profile %s appears logged out — skipping session for this profile.",
                profile_id,
            )
            return

        # 5. Main activity session — 15 % chance of a long binge session
        if random.random() < SESSION_LONG_PROB:
            session_sec = random.uniform(SESSION_LONG_MIN * 60, SESSION_LONG_MAX * 60)
            log.info("Long session drawn: %.1f min  |  profile: %s", session_sec / 60, profile_id)
        else:
            session_sec = random.uniform(SESSION_MIN_MIN * 60, SESSION_MAX_MIN * 60)
            log.info("Session: %.1f min  |  profile: %s", session_sec / 60, profile_id)
        run_social_session(driver, session_sec, profile_id=profile_id, **(weights or {}))

    except (TimeoutException, RuntimeError, WebDriverException) as exc:
        log.error("Error on profile %s: %s", profile_id, exc)
        if driver:
            ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(SCREENSHOT_DIR, f"error_{profile_id}_{ts}.png")
            try:
                driver.save_screenshot(path)
                log.info("Screenshot saved: %s", path)
            except Exception as ss_err:
                log.warning("Screenshot failed: %s", ss_err)

    finally:
        if driver:
            try:
                # Vary close behaviour — sometimes linger before quitting so
                # session-duration metadata isn’t perfectly uniform across profiles.
                if random.random() < 0.40:
                    time.sleep(random.uniform(2.0, 7.0))
                driver.quit()
            except Exception:
                pass
        if launched:
            stop_profile(profile_id)


def warm_profile_attached(
    debugger_address: str,
    profile_id: str = "manual",
    skip_preflight: bool = False,
    close_after: bool = False,
    weights: dict | None = None,
) -> None:
    """
    Run a warm-up session on a browser that is *already open* in NstBrowser.

    No ``start_profile`` / ``stop_profile`` API call is made, so the daily
    open quota is not consumed.

    Parameters
    ----------
    debugger_address : str
        CDP host:port, e.g. ``127.0.0.1:9222``.  Obtain it from:
          - NstBrowser UI -> right-click running profile -> Remote Debug /
            Copy Debug Address
          - ``GET /api/v2/browsers`` -> ``port`` field  (use ``--attach-profile``
            to resolve this automatically)
    profile_id : str
        Label for log messages and screenshot filenames only.
    skip_preflight : bool
        Skip the Wikipedia pre-flight (use when the profile already has a
        warm browsing history from an earlier run).
    close_after : bool
        Quit the browser after the session.  Default: leave it open.
    """
    driver  = None
    address = debugger_address.replace("ws://", "").split("/")[0]
    ws_url  = f"ws://{address}"

    try:
        driver = connect_selenium(ws_url)
        driver.set_page_load_timeout(30)
        init_cursor_pos(driver)    # seed a random start position so the first park arc is never flat at y=0
        log.info("Attached to already-open browser  |  address=%s  |  label=%s",
                 address, profile_id)

        if skip_preflight:
            log.info("Preflight skipped (--no-preflight).")
        else:
            run_preflight(driver)

        log.info("Navigating to %s", TARGET_SOCIAL_URL)
        navigate_to(driver, TARGET_SOCIAL_URL)
        precise_sleep(random.uniform(2, 5))

        if not check_login_status(driver):
            log.error("Profile '%s' appears logged out -- skipping session.",
                      profile_id)
            return

        if random.random() < SESSION_LONG_PROB:
            session_sec = random.uniform(SESSION_LONG_MIN * 60, SESSION_LONG_MAX * 60)
            log.info("Long session drawn: %.1f min  |  profile: %s", session_sec / 60, profile_id)
        else:
            session_sec = random.uniform(SESSION_MIN_MIN * 60, SESSION_MAX_MIN * 60)
            log.info("Session: %.1f min  |  profile: %s", session_sec / 60, profile_id)
        run_social_session(driver, session_sec, profile_id=profile_id, **(weights or {}))

    except (TimeoutException, RuntimeError, WebDriverException) as exc:
        log.error("Error on attached profile '%s': %s", profile_id, exc)
        if driver:
            ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(SCREENSHOT_DIR, f"error_{profile_id}_{ts}.png")
            try:
                driver.save_screenshot(path)
                log.info("Screenshot saved: %s", path)
            except Exception as ss_err:
                log.warning("Screenshot failed: %s", ss_err)

    finally:
        if driver:
            if close_after:
                try:
                    if random.random() < 0.40:
                        time.sleep(random.uniform(2.0, 7.0))
                    driver.quit()
                except Exception:
                    pass
            else:
                log.info("Browser left open (pass --close to quit after session).")


def _resolve_attached_address(profile_id: str) -> str:
    """
    Query ``GET /api/v2/browsers`` to find the CDP debug address for a
    profile that is currently open in NstBrowser.

    Returns ``host:port`` string or raises ``RuntimeError``.
    """
    running = get_running_browsers()
    if not running:
        raise RuntimeError(
            "No running browsers reported by GET /api/v2/browsers.\n"
            "Make sure the profile is open in NstBrowser first."
        )

    for b in running:
        pid = b.get("profileId") or b.get("profile_id") or b.get("id") or ""
        if pid != profile_id:
            continue

        # Try the ws URL first (most reliable)
        for ws_key in ("webSocketDebuggerUrl", "wsUrl", "ws", "wsDebugUrl"):
            ws = b.get(ws_key, "")
            if ws:
                return ws.replace("ws://", "").split("/")[0]

        # Fall back to bare port (try all known field names across API versions)
        port = (
            b.get("remoteDebuggingPort")
            or b.get("port")
            or b.get("debugPort")
            or b.get("remote_debugging_port")
        )
        if port:
            return f"127.0.0.1:{port}"

        # Profile matched but no address extractable
        raise RuntimeError(
            f"Profile '{profile_id}' is running but no debug address could be "
            "resolved from the API response.  "
            "Pass --attach HOST:PORT directly."
        )

    ids = [b.get("profileId") or b.get("id", "?") for b in running]
    raise RuntimeError(
        f"Profile '{profile_id}' was not found in the running browsers list.\n"
        f"Currently running: {ids}\n"
        "Open the profile in NstBrowser before using --attach-profile."
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nstbrowser_warmer",
        description="NstBrowser Threads warm-up automation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        MODES
        -----
        Normal (no flags)
          Opens every profile in PROFILE_IDS via the NstBrowser API, runs a
          full warm-up session for each, then closes them (consumes daily opens).

        --attach HOST:PORT            [saves daily opens]
          Connects directly to an already-open NstBrowser profile via its CDP
          debug address.  Get the address from NstBrowser UI:
            right-click running profile -> Remote Debug / Copy Debug Address

        --attach-profile PROFILE_ID   [saves daily opens]
          Queries GET /api/v2/browsers to resolve the debug address of a
          running profile automatically, then attaches to it.

        EXAMPLES
        --------
          python nstbrowser_warmer.py
          python nstbrowser_warmer.py --attach 127.0.0.1:9222
          python nstbrowser_warmer.py --attach 127.0.0.1:9222 --no-preflight
          python nstbrowser_warmer.py --attach 127.0.0.1:9222 --label myaccount
          python nstbrowser_warmer.py --attach-profile 251894f1-0abc-4e5b-831c-1d3d594de9aa
          python nstbrowser_warmer.py --attach-profile 251894f1-... --no-preflight --close
        """),
    )

    attach_group = p.add_mutually_exclusive_group()
    attach_group.add_argument(
        "--attach",
        metavar="HOST:PORT",
        help=(
            "CDP debug address of an already-open NstBrowser profile "
            "(e.g. 127.0.0.1:9222).  Browser is NOT opened or closed by "
            "this script."
        ),
    )
    attach_group.add_argument(
        "--attach-profile",
        metavar="PROFILE_ID",
        help=(
            "UUID of a profile already open in NstBrowser.  The script "
            "calls GET /api/v2/browsers to resolve the debug address "
            "automatically."
        ),
    )
    p.add_argument(
        "--label",
        metavar="NAME",
        default=None,
        help=(
            "Human-readable label for log messages and screenshot filenames "
            "when using attach mode (default: the profile UUID or HOST:PORT)."
        ),
    )
    p.add_argument(
        "--no-preflight",
        action="store_true",
        help=(
            "Skip the Wikipedia pre-flight.  Useful when the profile already "
            "has a warm browsing history from an earlier run."
        ),
    )
    p.add_argument(
        "--close",
        action="store_true",
        help=(
            "Quit the browser after the session even in attach mode.  "
            "Default: browser is left open when attaching."
        ),
    )

    # ------------------------------------------------------------------ #
    #  Per-action weight overrides
    # ------------------------------------------------------------------ #
    wg = p.add_argument_group(
        "action weights",
        "Override the probability weight of individual session actions. "
        "Weights are a per-iteration fraction (0.0–1.0). "
        "Unspecified weights use the built-in defaults shown in parentheses.",
    )
    wg.add_argument(
        "--like",
        metavar="WEIGHT",
        type=float,
        default=None,
        help=(
            "Base like/active-action probability per iteration "
            "(default: ~0.20–0.45 drawn randomly each session)."
        ),
    )
    wg.add_argument(
        "--notify",
        metavar="WEIGHT",
        type=float,
        default=None,
        help="Notification-check weight per iteration (default: 0.03).",
    )
    wg.add_argument(
        "--profile",
        metavar="WEIGHT",
        type=float,
        default=None,
        help="Profile-view weight per iteration (default: 0.06).",
    )
    wg.add_argument(
        "--read-post",
        metavar="WEIGHT",
        type=float,
        default=None,
        dest="read_post",
        help="Read-post weight per iteration (default: 0.08).",
    )
    wg.add_argument(
        "--comment",
        metavar="WEIGHT",
        type=float,
        default=None,
        help="Comment weight per iteration (default: 0.05).",
    )
    wg.add_argument(
        "--follow",
        metavar="WEIGHT",
        type=float,
        default=None,
        help="Follow-from-feed weight per iteration (default: 0.03).",
    )
    wg.add_argument(
        "--scroll",
        metavar="WEIGHT",
        type=float,
        default=None,
        help="Return-to-top scroll weight per iteration (default: 0.03).",
    )
    wg.add_argument(
        "--search",
        metavar="WEIGHT",
        type=float,
        default=None,
        help="Search-page visit weight per iteration (default: 0.06).",
    )
    wg.add_argument(
        "--post",
        metavar="WEIGHT",
        type=float,
        default=None,
        help=(
            "New-post creation weight per iteration (default: 0.02). "
            "Subject to daily quota ramp-up and 2-hour cooldown regardless of weight."
        ),
    )

    return p


# ================================================================== #
#  MAIN
# ================================================================== #

def main() -> None:
    args = _build_parser().parse_args()

    log.info("=" * 60)
    log.info("NstBrowser Warmer (API v2) -- %s",
             datetime.now().strftime("%Y-%m-%d %H:%M"))

    # Build per-action weight overrides dict — only keys explicitly passed by
    # the user are included; omitted keys fall back to run_social_session defaults.
    weights: dict = {}
    if args.like      is not None: weights["w_like"]    = args.like
    if args.notify    is not None: weights["w_notify"]  = args.notify
    if args.profile   is not None: weights["w_profile"] = args.profile
    if args.read_post is not None: weights["w_read"]    = args.read_post
    if args.comment   is not None: weights["w_comment"] = args.comment
    if args.follow    is not None: weights["w_follow"]  = args.follow
    if args.scroll    is not None: weights["w_top"]     = args.scroll
    if args.search    is not None: weights["w_search"]  = args.search
    if args.post      is not None: weights["w_post"]    = args.post
    if weights:
        log.info("Custom action weights: %s", weights)

    # ---------------------------------------------------------------- #
    #  ATTACH MODE  — reuse already-open browser, no daily open consumed
    # ---------------------------------------------------------------- #
    if args.attach or args.attach_profile:
        if args.attach_profile:
            pid   = args.attach_profile
            label = args.label or pid
            log.info("Attach-profile mode  |  profile=%s", pid)
            try:
                address = _resolve_attached_address(pid)
            except RuntimeError as exc:
                log.error("%s", exc)
                return
            log.info("Resolved debug address: %s", address)
        else:
            address = args.attach
            label   = args.label or address
            log.info("Attach mode  |  address=%s", address)

        warm_profile_attached(
            debugger_address=address,
            profile_id=label,
            skip_preflight=args.no_preflight,
            close_after=args.close,
            weights=weights or None,
        )
        log.info("=" * 60)
        log.info("Done.")
        return

    # ---------------------------------------------------------------- #
    #  NORMAL MODE  — open every profile via the API
    # ---------------------------------------------------------------- #

    # Inactive-day simulation — models days when a real user never opens Threads.
    if random.random() < INACTIVE_DAY_PROB:
        log.info(
            "Simulating inactive day (%.0f%% probability) — no profiles run today.",
            INACTIVE_DAY_PROB * 100,
        )
        return

    # Time-of-day scheduling guard — refuse to run outside natural waking hours.
    _now_hour = datetime.now().hour
    if not (ACTIVE_HOURS_RANGE[0] <= _now_hour <= ACTIVE_HOURS_RANGE[1]):
        log.warning(
            "Outside active hours (%02d:00\u2013%02d:00 local) — not running.",
            ACTIVE_HOURS_RANGE[0], ACTIVE_HOURS_RANGE[1],
        )
        return

    log.info("Target: %s  |  Profiles: %d", TARGET_SOCIAL_URL, len(PROFILE_IDS))

    running = get_running_browsers()
    if running:
        log.info("Already running at startup: %s",
                 [b.get("profileId") for b in running])

    profile_order = PROFILE_IDS.copy()
    random.shuffle(profile_order)
    log.info("Execution order: %s", profile_order)

    for idx, profile_id in enumerate(profile_order):
        log.info("-" * 60)
        log.info("[%d/%d] Starting: %s", idx + 1, len(profile_order), profile_id)
        warm_profile(profile_id, weights=weights or None)

        if idx < len(profile_order) - 1:
            if random.random() < BUFFER_LONG_PROB:
                buf = random.uniform(BUFFER_LONG_MIN * 60, BUFFER_LONG_MAX * 60)
                log.info("Extended buffer: %.1f min before next profile...", buf / 60)
            else:
                buf = random.uniform(BUFFER_MIN_MIN * 60, BUFFER_MAX_MIN * 60)
                log.info("Buffer: %.1f min before next profile...", buf / 60)
            time.sleep(buf)

    log.info("=" * 60)
    log.info("All profiles warmed. Done.")


if __name__ == "__main__":
    main()
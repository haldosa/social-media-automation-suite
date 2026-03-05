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
import threading
import contextlib
import dataclasses
import unittest.mock
import glob as _glob
import re
import textwrap
import argparse
import tempfile
import hashlib
import shutil
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
    "https://www.theguardian.com",
    "https://www.espn.com",
    "https://news.ycombinator.com",
]
PREFLIGHT_SITES_MIN  = 2    # minimum number of pre-flight sites to visit
PREFLIGHT_SITES_MAX  = 4    # maximum number of pre-flight sites to visit
PREFLIGHT_DWELL_MIN  = 18   # minimum seconds on each pre-flight site
PREFLIGHT_DWELL_MAX  = 55   # maximum seconds on each pre-flight site

# Session duration — smooth log-normal distribution.
# Real social-media session lengths follow a right-skewed continuous
# distribution (many short sessions, occasional long ones) — NOT the
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

# Time-of-day scheduling — the warmer will refuse to run outside these hours
# (24-hour local time).  Set ACTIVE_HOURS_RANGE = (0, 23) to disable.
ACTIVE_HOURS_RANGE  = (8, 23)   # only run between 08:00 and 23:00 local time
# Simulated inactive day — skip the entire run with this probability.
# Models the natural days when a real user simply doesn't open Threads.
INACTIVE_DAY_PROB   = 0 #0.18
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
# Relative paths are resolved against the directory that contains this script
# so the bot works regardless of the working directory it is launched from.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_POOL_DIR        = os.path.join(_SCRIPT_DIR, "media")   # e.g. "media_pool"
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
def _draw_passive_phase_sec() -> float:
    """Draw a per-session passive-phase minimum (seconds).

    Re-drawn every session so the same profile doesn't always start
    posting at the same elapsed time.  Module-level constant was shared
    across all profiles in a multi-profile invocation.
    """
    return random.uniform(5 * 60, 10 * 60)   # 5–10 min, per-session

# Soft floor between any two posts per profile.
# Instead of a fixed 4-hour hard floor (which eliminates natural post-
# clustering behavior), we use a Gaussian-sampled floor that averages
# ~2 hours but can go as low as 45 min or as high as 4 hours.
# This preserves the organic "two posts within an hour" pattern that real
# users sometimes exhibit while still preventing machine-gun posting.
def _sample_post_min_gap_sec() -> float:
    """Sample a soft minimum inter-post gap (seconds).

    Distribution: Gaussian(mean=2h, sigma=40min), clamped to [45min, 4h].
    The result is different each time so clusters can emerge naturally.
    """
    gap_h = random.gauss(2.0, 0.67)   # mean=2h, sigma=40min
    gap_h = max(0.75, min(gap_h, 4.0))  # clamp [45min, 4h]
    return gap_h * 3600.0

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
DEBUG_CURSOR_OVERLAY= False             # True = inject red dot overlay to visualise cursor movement

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
# Compose / New-post button in the nav sidebar (aria-label="Post")
COMPOSE_BTN_SELECTORS = [
    # aria-label="New post" — current Threads desktop nav (2025+)
    ("css",   '[aria-label="New post"]'),
    ("css",   '[aria-label="New Post"]'),
    # aria-label on the SVG itself — older builds
    ("css",   'div[role="button"]:has(svg[aria-label="Post"])'),
    ("css",   'a[role="link"]:has(svg[aria-label="Post"])'),
    ("css",   'div[role="button"][aria-label="Post"]'),
    # aria-label variants seen across locales / A/B tests
    ("css",   '[aria-label="Create"]'),
    ("css",   '[aria-label="Compose"]'),
    ("xpath", '//div[@role="button" and contains(translate(@aria-label,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"new post")]'),
    ("xpath", '//*[@role="button" and .//*[local-name()="svg"][@aria-label="Post"]]'),
    ("xpath", '//*[@role="button" and .//*[local-name()="svg"][@aria-label="New post"]]'),
]
# Compose modal textbox (new-post box, not the comment/reply box)
COMPOSE_TEXTBOX_CSS = 'div[data-lexical-editor="true"][contenteditable="true"]'
# "Attach media" button inside the compose modal
COMPOSE_ATTACH_BTN_CSS = 'div[role="button"]:has(svg[aria-label="Attach media"])'

# ── Multi-signal element resolution ────────────────────────────────────────── #
# Instead of relying solely on aria-label selectors (which Meta A/B tests,
# localizes, and rotates), element identification uses a composite scoring
# system that evaluates multiple independent signals:
#   1. Structural position within the post component
#   2. SVG path geometry (heart shape fingerprint)
#   3. Fill state (transparent vs currentColor)
#   4. Sibling context (action bar grouping)
#   5. ARIA labels as a LOW-WEIGHT fallback signal
# When the top candidate scores below ELEMENT_CONFIDENCE_THRESHOLD the action
# is skipped entirely and the failure is logged for offline selector review.
ELEMENT_CONFIDENCE_THRESHOLD = 0.45

# Known aria-label values across locales — LOW-WEIGHT signal only.
_KNOWN_LIKE_LABELS = frozenset({
    "like", "love", "heart", "me gusta", "j'aime", "gefällt mir",
    "いいね", "curtir", "좋아요", "赞", "mi piace", "thích",
})
_KNOWN_UNLIKE_LABELS = frozenset({
    "unlike", "unlove", "no me gusta", "je n'aime plus",
    "gefällt mir nicht mehr", "いいね取消", "descurtir",
    "좋아요 취소", "取消赞", "non mi piace più",
})
_KNOWN_REPLY_LABELS = frozenset({
    "reply", "comment", "respond", "responder", "répondre",
    "antworten", "返信", "comentar", "댓글", "评论", "rispondi",
})
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
# Main log file captures everything down to DEBUG; console stays at INFO.
_file_h.setLevel(logging.DEBUG)
_stream_h.setLevel(logging.INFO)
logging.basicConfig(level=logging.DEBUG, handlers=[_file_h, _stream_h])
# Suppress noisy HTTP debug lines from urllib3 / selenium remote connection
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("selenium.webdriver.remote.remote_connection").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

# Dedicated mouse-movement logger — writes to its own file at DEBUG level.
# Arc summaries are always written; per-step positions only when MOUSE_TRACE=True.
_mouse_fh = logging.FileHandler(MOUSE_LOG_FILE, encoding="utf-8")
_mouse_fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
_mlog = logging.getLogger("mouse")
_mlog.setLevel(logging.DEBUG)
_mlog.addHandler(_mouse_fh)
_mlog.propagate = False  # keep mouse events out of the main log

# ── DEBUG LOGGING: audit logger — writes into the same log file as `log` ─────
# All [TIMING], [ELEMENT], [MOUSE ARC], [CLICK], [CURSOR MOVE] etc. go to
# nstbrowser_warmer.log at DEBUG level.  The console handler is not attached
# so verbose debug lines don't clutter the terminal.
_dlog = logging.getLogger("audit")
_dlog.setLevel(logging.DEBUG)
_dlog.addHandler(_file_h)     # same file handler as main log
_dlog.propagate = False       # prevent double-printing via root logger
# ─────────────────────────────────────────────────────────────────────────────#

# ── DEBUG LOGGING: session metrics accumulator ───────────────────────────────
# Reset by run_social_session() at the start of every session.
# Default values kept here for reference only; live state lives in
# SessionContext (see _get_ctx() below).
_SESSION_METRICS_DEFAULTS: dict = {
    "actions_dispatched": 0,
    "likes":    0,
    "comments": 0,
    "follows":  0,
    "posts":    0,
    "passive":  0,
    "reads":    0,
    "profile_visits": 0,
    "searches": 0,
    "last_action": None,          # used for consecutive-action RISK WARN
    "consecutive_same": 0,
}
# ─────────────────────────────────────────────────────────────────────────────#


# ── Per-session mutable state: thread-local SessionContext ───────────────────
# All state that was previously scattered across module-level globals is now
# encapsulated here.  threading.local() ensures that concurrent sessions
# running in separate threads each have their own independent copy.
@dataclasses.dataclass
class SessionContext:
    """Isolated mutable state for a single social session."""
    cursor_pos: list          = dataclasses.field(default_factory=lambda: [0, 0])
    last_bezier_end_ts: float = 0.0
    cdp_consecutive_failures: int = 0
    session_followed: set     = dataclasses.field(default_factory=set)
    session_metrics: dict     = dataclasses.field(
        default_factory=lambda: dict(_SESSION_METRICS_DEFAULTS)
    )
    active_typing_dna: dict   = dataclasses.field(default_factory=dict)


_session_local = threading.local()


def _get_ctx() -> SessionContext:
    """Return the SessionContext for the current thread, creating one if needed."""
    ctx = getattr(_session_local, 'ctx', None)
    if ctx is None:
        _session_local.ctx = SessionContext()
    return _session_local.ctx
# ─────────────────────────────────────────────────────────────────────────────#


# ── DEBUG LOGGING: timing check helper ───────────────────────────────────────
def _timing_check(context: str, sampled_s: float,
                  expected_min_s: float, expected_max_s: float) -> float:
    """Log a timing sample to nstbrowser_warmer.log; emit WARN if outside expected range.

    Uses a z-score approximation: if sampled_s is more than 2 SD outside the
    expected range (treating the range as ±2 SD from the midpoint), emit WARN.
    Returns sampled_s unchanged so callers can inline the call.
    """
    mid  = (expected_min_s + expected_max_s) / 2.0
    sd   = (expected_max_s - expected_min_s) / 4.0  # range ≈ 4 SD
    within = expected_min_s <= sampled_s <= expected_max_s
    z_lo = (expected_min_s - sampled_s) / sd if sd > 0 else 0.0
    z_hi = (sampled_s - expected_max_s) / sd if sd > 0 else 0.0
    z_score = max(0.0, z_lo, z_hi)
    msg = (
        f"[TIMING]  context={context}  sampled={sampled_s*1000:.1f}ms"
        f"  expected_range={expected_min_s*1000:.0f}-{expected_max_s*1000:.0f}ms"
        f"  within_bounds={within}  z_score={z_score:.2f}"
    )
    if z_score > 2.0:
        _dlog.warning("[TIMING WARN]  z=%.2f  %s", z_score, msg)
    else:
        _dlog.debug(msg)
    return sampled_s


# ── DEBUG LOGGING: element interaction auditor ───────────────────────────────
def _log_element_interaction(driver, element, action: str) -> None:
    """Query element geometry + visibility via JS; emit [ELEMENT] log line.

    All execute_script calls are guarded so a JS error never crashes automation.
    Routes to nstbrowser_warmer.log (DEBUG) on success; emits [ELEMENT WARN] to the
    main log if element is invisible or off-viewport.
    """
    try:
        info = driver.execute_script("""
            var el = arguments[0];
            try {
                var r  = el.getBoundingClientRect();
                var cs = window.getComputedStyle(el);
                var vw = window.innerWidth;
                var vh = window.innerHeight;
                var inVp = r.width > 0 && r.height > 0
                           && r.right > 0 && r.bottom > 0
                           && r.left < vw && r.top < vh;
                var visible = cs.display     !== 'none'
                           && cs.visibility  !== 'hidden'
                           && parseFloat(cs.opacity || '1') > 0
                           && r.width  > 0 && r.height > 0;
                return {
                    tag:        el.tagName ? el.tagName.toLowerCase() : '?',
                    role:       el.getAttribute('role') || '',
                    aria_label: el.getAttribute('aria-label') || '',
                    visible:    visible,
                    in_viewport: inVp,
                    rect: [Math.round(r.left), Math.round(r.top),
                           Math.round(r.width), Math.round(r.height)]
                };
            } catch(e) {
                return {error: e.toString()};
            }
        """, element)
        if not info or "error" in info:
            _dlog.debug("[ELEMENT]  action=%s  query_error=%s", action,
                        info.get("error") if info else "null")
            return
        base_msg = (
            f"[ELEMENT]  action={action}  tag={info['tag']}"
            f"  role={info['role'] or 'n/a'}  aria_label={info['aria_label'] or 'n/a'!r}"
            f"  visible={info['visible']}  in_viewport={info['in_viewport']}"
            f"  rect={tuple(info['rect'])}"
        )
        if not info["visible"] or not info["in_viewport"]:
            _dlog.warning("[ELEMENT WARN]  %s", base_msg)
            log.warning("[ELEMENT WARN]  action=%s  tag=%s  visible=%s  in_viewport=%s",
                        action, info["tag"], info["visible"], info["in_viewport"])
        else:
            _dlog.debug(base_msg)
    except Exception as exc:
        _dlog.debug("[ELEMENT]  action=%s  exception=%s", action, exc)
# ─────────────────────────────────────────────────────────────────────────────#


# ── DEBUG LOGGING: element tag helper ────────────────────────────────────────
def _safe_tag(element) -> str:
    """Return element.tag_name without raising on stale references."""
    try:
        return element.tag_name
    except Exception:
        return "?"


# ── DEBUG LOGGING: page-state snapshot ───────────────────────────────────────
def _log_page_state(driver, context: str) -> None:
    """Emit a snapshot of the current browser state to the main INFO log.

    Reports URL, page title, scrollY, scroll percentage, and visible feed-item
    count so a reader of the log can reconstruct exactly what was on screen at
    every action boundary without opening the browser.
    """
    try:
        url   = driver.current_url
        title = driver.title
        scroll_y = driver.execute_script("return window.scrollY")
        scroll_pct = driver.execute_script(
            "var h=document.body.scrollHeight-window.innerHeight;"
            "return h>0?Math.round(window.scrollY/h*100):0;"
        )
        articles = len(driver.find_elements(
            By.CSS_SELECTOR, "article, div[data-pressable-container='true']"
        ))
        log.debug(
            "[PAGE STATE]  context=%s  url=%s  title=%r"
            "  scrollY=%dpx  scroll_pct=%d%%  feed_items=%d",
            context, url[:80], title[:40], scroll_y, scroll_pct, articles,
        )
    except Exception as exc:
        log.debug("[PAGE STATE]  context=%s  error=%s", context, exc)
# ─────────────────────────────────────────────────────────────────────────────#


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
        log.debug("Windows timer resolution raised to 1 ms (timeBeginPeriod)")
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

    Copies the binary to a stable per-original temp path before patching so
    the original executable (potentially locked by the OS or used by other
    runs) is never modified in-place.  The copy path is keyed to the original
    path via its MD5 digest so the same original always maps to the same copy.
    A .patched marker on the copy path makes the operation idempotent.
    """
    _tmp_root = os.path.join(tempfile.gettempdir(), "nstbrowser_cd_patch")
    os.makedirs(_tmp_root, exist_ok=True)
    name_hash = hashlib.md5(os.path.abspath(path).encode()).hexdigest()[:12]
    suffix = ".exe" if sys.platform == "win32" else ""
    copy_path = os.path.join(_tmp_root, f"chromedriver_{name_hash}{suffix}")
    patched_marker = copy_path + ".patched"
    if os.path.exists(patched_marker) and os.path.exists(copy_path):
        return copy_path
    try:
        shutil.copy2(path, copy_path)
        with open(copy_path, "rb") as f:
            data = f.read()
        pattern = re.compile(rb'\$cdc_[a-zA-Z0-9]{22}_')
        matches = list(pattern.finditer(data))
        if not matches:
            log.info("ChromeDriver binary: no $cdc_ pattern found (already clean or new version)")
            open(patched_marker, "w").close()
            return copy_path
        for m in reversed(matches):
            replacement = b'$xxx_' + b'a' * (len(m.group()) - 5)
            data = data[:m.start()] + replacement + data[m.end():]
        with open(copy_path, "wb") as f:
            f.write(data)
        open(patched_marker, "w").close()
        log.info("ChromeDriver binary patched: %d $cdc_ occurrence(s) replaced → %s",
                 len(matches), copy_path)
    except PermissionError:
        log.warning("ChromeDriver binary patch failed: permission denied — using original")
        return path
    except Exception as exc:
        log.warning("ChromeDriver binary patch failed: %s — using original", exc)
        return path
    return copy_path


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

    # Fix #14: run Layer 3 (runtime verification) FIRST.
    # If the binary patch already removed all $cdc_ properties, skipping the
    # Page.addScriptToEvaluateOnNewDocument injection avoids creating the
    # non-configurable property-descriptor side-effects that Object.
    # getOwnPropertyDescriptor() can expose as a bot-detection signal.
    _cdc_found = []
    try:
        _cdc_found = driver.execute_script(
            "return Object.getOwnPropertyNames(document)"
            ".filter(function(p){return /\\$[a-z]dc_/.test(p)});"
        ) or []
    except WebDriverException:
        pass

    if _cdc_found:
        # Properties still present — fix the current context immediately
        # and register the mask for all subsequent page loads (Layer 1).
        log.warning("$cdc_ variables detected — applying runtime fix + pre-page mask: %s", _cdc_found)
        try:
            driver.execute_script(_CDC_MASK_JS)
        except WebDriverException:
            pass
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": _CDC_MASK_JS,
            })
        except WebDriverException as exc:
            log.debug("$cdc_ pre-page mask injection failed: %s", exc)
    else:
        # Binary patch succeeded — no $cdc_ properties exist.  Skipping the
        # addScriptToEvaluateOnNewDocument injection entirely prevents the
        # non-configurable getter side-effect that anti-bot probes look for.
        log.debug("$cdc_ mask skipped: runtime verification confirms no ChromeDriver variables")

    log.info("Selenium attached successfully.")
    return driver


# ================================================================== #
#  HUMAN-LIKE INTERACTION PRIMITIVES
# ================================================================== #

# Bigram pairs that are naturally slow for most touch-typists — awkward
# hand transitions that produce longer inter-key intervals in corpus data.
# Fix #13: expanded from 10 to 50 bigrams.  The original 10 were all rare;
# common high-frequency English bigrams also require slower transitions and
# their absence means any corpus classifier will fail to reproduce the right
# n-gram timing distribution across real captions.
_SLOW_BIGRAMS = {
    # Original rare/awkward bigrams
    'qu', 'wr', 'xc', 'zx', 'bv', 'vb', 'pq', 'yw', 'wq', 'xz',
    # Common English bigrams with cross-hand or stretch transitions
    'th', 'he', 'in', 'er', 'an', 're', 'on', 'en', 'at', 'es',
    'ed', 'or', 'ti', 'hi', 'as', 'to', 'ou', 'ha', 'it', 'nd',
    'st', 'ng', 'nt', 'is', 'le', 'al', 'ar', 'se', 'te', 've',
    # Additional awkward index-to-pinky / same-hand stretch pairs
    'br', 'cr', 'dr', 'fr', 'gr', 'pr', 'tr', 'bl', 'cl', 'pl',
    'ct', 'ft', 'lt', 'pt', 'ny', 'ly', 'my', 'ry', 'ty', 'gy',
}

# QWERTY keyboard adjacency map for realistic typo generation.
# Each key maps to its physically adjacent keys on a standard US layout.
_QWERTY_ADJACENCY = {
    'q': ['w', 'a'],           'w': ['q', 'e', 'a', 's'],
    'e': ['w', 'r', 's', 'd'], 'r': ['e', 't', 'd', 'f'],
    't': ['r', 'y', 'f', 'g'], 'y': ['t', 'u', 'g', 'h'],
    'u': ['y', 'i', 'h', 'j'], 'i': ['u', 'o', 'j', 'k'],
    'o': ['i', 'p', 'k', 'l'], 'p': ['o', 'l'],
    'a': ['q', 'w', 's', 'z'], 's': ['w', 'e', 'a', 'd', 'z', 'x'],
    'd': ['e', 'r', 's', 'f', 'x', 'c'],
    'f': ['r', 't', 'd', 'g', 'c', 'v'],
    'g': ['t', 'y', 'f', 'h', 'v', 'b'],
    'h': ['y', 'u', 'g', 'j', 'b', 'n'],
    'j': ['u', 'i', 'h', 'k', 'n', 'm'],
    'k': ['i', 'o', 'j', 'l', 'm'],       'l': ['o', 'p', 'k'],
    'z': ['a', 's', 'x'],     'x': ['s', 'd', 'z', 'c'],
    'c': ['d', 'f', 'x', 'v'], 'v': ['f', 'g', 'c', 'b'],
    'b': ['g', 'h', 'v', 'n'], 'n': ['h', 'j', 'b', 'm'],
    'm': ['j', 'k', 'n'],
}

# Active typing DNA is now tracked per-thread in SessionContext.active_typing_dna.
# See _get_ctx() and _get_typing_dna().


def _generate_typing_dna() -> dict:
    """Generate a stable per-profile typing fingerprint ('typing DNA').

    Each person has idiosyncratic keystroke dynamics -- their personal
    mu/sigma for inter-key intervals, burst length range, bigram-specific
    penalties, punctuation pause personality, error propensity, and fatigue
    drift rate.  Sampled once and persisted in post_state.json so the same
    profile always types with the same rhythm.
    """
    burst_min = random.randint(2, 5)
    burst_max = burst_min + random.randint(2, 5)
    return {
        "base_mu":              random.uniform(math.log(0.065), math.log(0.110)),
        "base_sigma":           random.uniform(0.30, 0.55),
        "burst_min":            burst_min,
        "burst_max":            burst_max,
        "space_pause_lo":       random.uniform(0.03, 0.08),
        "space_pause_hi":       random.uniform(0.12, 0.22),
        "punct_pause_lo":       random.uniform(0.15, 0.30),
        "punct_pause_hi":       random.uniform(0.40, 0.70),
        "burst_gap_lo":         random.uniform(0.04, 0.08),
        "burst_gap_hi":         random.uniform(0.12, 0.25),
        "hesitation_prob":      random.uniform(0.02, 0.07),
        "hesitation_lo":        random.uniform(0.20, 0.40),
        "hesitation_hi":        random.uniform(0.60, 1.00),
        "bigram_penalty_lo":    random.uniform(1.2, 1.6),
        "bigram_penalty_hi":    random.uniform(1.6, 2.2),
        "error_rate":           random.uniform(0.01, 0.06),
        "correction_prob":      random.uniform(0.70, 0.95),
        "detection_delay_mean": random.uniform(0.5, 2.5),
        "fatigue_drift":        random.uniform(0.002, 0.012),
    }


def _get_typing_dna(profile_id: str) -> dict:
    """Load or generate the typing DNA for a profile.

    On first call for a profile, generates a new typing fingerprint and
    persists it in post_state.json.  Subsequent calls return the stored
    fingerprint so the same profile always types with the same rhythm.
    """
    if not profile_id:
        return _generate_typing_dna()

    with _post_state_locked():
        state = _load_post_state()
        _ensure_profile_in_state(profile_id, state)

        profile = state.get(profile_id, {})
        dna = profile.get("typing_dna")
        if dna and isinstance(dna, dict) and "base_mu" in dna:
            return dna

        dna = _generate_typing_dna()
        state[profile_id]["typing_dna"] = dna
        _save_post_state(state)
    log.info("[ TYPING DNA ]  generated fingerprint for %s  "
             "mu=%.3f sigma=%.2f err=%.1f%%",
             profile_id[:8], dna["base_mu"], dna["base_sigma"],
             dna["error_rate"] * 100)
    return dna


def _pick_error_char(char: str):
    """Pick an error replacement character based on QWERTY adjacency.

    Error taxonomy (weights):
      Adjacent key:       45 %  (neighbouring QWERTY key)
      Same key repeat:    15 %  (double-strike)
      Second-order:       10 %  (2 keys away -- wrong-hand mirror)
      Random adjacent:    30 %  (fallback to any adjacent key)
    Returns None if no error can be generated for this character.
    """
    lower = char.lower()
    is_upper = char.isupper()
    adj = _QWERTY_ADJACENCY.get(lower, [])
    if not adj:
        return None

    roll = random.random()
    if roll < 0.45:
        result = random.choice(adj)
    elif roll < 0.60:
        result = lower  # double-strike
    elif roll < 0.70:
        second = []
        for a in adj:
            second.extend(_QWERTY_ADJACENCY.get(a, []))
        second = [k for k in set(second) if k != lower]
        result = random.choice(second) if second else random.choice(adj)
    else:
        result = random.choice(adj)

    return result.upper() if is_upper else result


def _build_typo_sequence(text: str, error_rate: float,
                         correction_prob: float,
                         detection_delay_mean: float) -> list:
    """Build a character-action sequence with realistic typos and corrections.

    For each character position, with probability *error_rate* an error is
    injected.  The wrong character is typed, then *detection_delay* more
    correct characters follow before the error is noticed.  Backspace
    erases back to the error, then the correct characters are retyped.

    Errors are more likely near word boundaries (first/last 2 chars of a
    word) where motor-planning transitions are less rehearsed.

    Returns a list of action dicts:
      {"char": "a"}                    -- type character 'a'
      {"char": "", "backspace": True}  -- press Backspace
    """
    if error_rate <= 0:
        return [{"char": c} for c in text]

    actions = []
    i = 0
    # Pre-compute word boundary positions for error clustering
    word_positions = set()
    word_start = 0
    for idx, ch in enumerate(text):
        if ch == ' ':
            if idx > 0:
                word_positions.add(idx - 1)
                word_positions.add(max(0, idx - 2))
            word_start = idx + 1
        elif idx == word_start or idx == word_start + 1:
            word_positions.add(idx)

    while i < len(text):
        char = text[i]
        eff_rate = error_rate * (1.5 if i in word_positions else 1.0)

        if char.isalpha() and random.random() < eff_rate:
            error_char = _pick_error_char(char)
            if error_char is None:
                actions.append({"char": char})
                i += 1
                continue

            # Type the wrong character
            actions.append({"char": error_char})

            if random.random() < correction_prob:
                # Detection delay: type a few more correct chars before noticing
                delay = min(
                    max(0, int(random.expovariate(
                        1.0 / max(0.5, detection_delay_mean)))),
                    min(4, len(text) - i - 1),
                )
                lookahead = text[i + 1: i + 1 + delay]
                for la in lookahead:
                    actions.append({"char": la})
                # Backspace over lookahead + error
                for _ in range(len(lookahead) + 1):
                    actions.append({"char": "", "backspace": True})
                # Retype correctly
                actions.append({"char": char})
                for la in lookahead:
                    actions.append({"char": la})
                i += 1 + len(lookahead)
            else:
                i += 1  # uncorrected error
        else:
            actions.append({"char": char})
            i += 1

    return actions


# ── CDP keystroke dispatch ────────────────────────────────────────────────────
# Replaces Selenium element.send_keys() to avoid StaleElementReferenceException
# when React/Lexical re-renders the contenteditable <div> mid-typing.
# Also produces isTrusted:true keyboard events.

# Fix #13: mapping from printable ASCII char → (code, windowsVirtualKeyCode, modifiers).
# `code` is the KeyboardEvent.code (physical key); `modifiers` bit 3 = Shift.
# Absence of `code` causes KeyboardEvent.code to read as "" in JS — detectable.
_ASCII_KEY_INFO: dict[str, tuple[str, int, int]] = {
    # Lowercase letters — location 0, no shift
    **{c: (f"Key{c.upper()}", ord(c.upper()), 0) for c in "abcdefghijklmnopqrstuvwxyz"},
    # Uppercase letters — same physical key, shift modifier (bit 3 = 8)
    **{c: (f"Key{c}", ord(c), 8) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    # Digits — no shift
    "0": ("Digit0", 48, 0), "1": ("Digit1", 49, 0), "2": ("Digit2", 50, 0),
    "3": ("Digit3", 51, 0), "4": ("Digit4", 52, 0), "5": ("Digit5", 53, 0),
    "6": ("Digit6", 54, 0), "7": ("Digit7", 55, 0), "8": ("Digit8", 56, 0),
    "9": ("Digit9", 57, 0),
    # Space
    " ": ("Space", 32, 0),
    # Punctuation — unshifted
    "`": ("Backquote", 192, 0), "-": ("Minus",     189, 0), "=": ("Equal",       187, 0),
    "[": ("BracketLeft", 219, 0), "]": ("BracketRight", 221, 0), "\\": ("Backslash", 220, 0),
    ";": ("Semicolon", 186, 0), "'": ("Quote",   222, 0), ",": ("Comma",  188, 0),
    ".": ("Period",    190, 0), "/": ("Slash",   191, 0),
    # Punctuation — shifted variants (+8 modifiers)
    "~": ("Backquote", 192, 8), "_": ("Minus",     189, 8), "+": ("Equal",       187, 8),
    "{": ("BracketLeft", 219, 8), "}": ("BracketRight", 221, 8), "|": ("Backslash", 220, 8),
    ":": ("Semicolon", 186, 8), '"': ("Quote",   222, 8), "<": ("Comma",  188, 8),
    ">": ("Period",    190, 8), "?": ("Slash",   191, 8),
    "!": ("Digit1", 49, 8), "@": ("Digit2", 50, 8), "#": ("Digit3", 51, 8),
    "$": ("Digit4", 52, 8), "%": ("Digit5", 53, 8), "^": ("Digit6", 54, 8),
    "&": ("Digit7", 55, 8), "*": ("Digit8", 56, 8), "(": ("Digit9", 57, 8),
    ")": ("Digit0", 48, 8),
}


def _cdp_type_key(driver, char: str) -> None:
    """Type a single character via CDP Input.dispatchKeyEvent / insertText.

    ASCII printable chars get full keyDown+keyUp so the browser generates
    the complete keydown → beforeinput → input → keyup event chain, with
    all required KeyboardEvent fields populated (key, code,
    windowsVirtualKeyCode, nativeVirtualKeyCode, location, modifiers).
    Non-ASCII (emoji, accented chars) use Input.insertText which fires
    beforeinput → input — matching real IME / emoji-picker behaviour.
    """
    if len(char) == 1 and 32 <= ord(char) < 127:
        info = _ASCII_KEY_INFO.get(char)
        if info:
            code, vk, mods = info
        else:
            code, vk, mods = f"Key{char.upper()}", ord(char.upper()), 0
        down: dict = {
            "type": "keyDown",
            "key": char,
            "code": code,
            "text": char,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "location": 0,
            "modifiers": mods,
        }
        up: dict = {
            "type": "keyUp",
            "key": char,
            "code": code,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "location": 0,
            "modifiers": mods,
        }
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", down)
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", up)
    else:
        driver.execute_cdp_cmd("Input.insertText", {"text": char})


def _cdp_backspace(driver) -> None:
    """Press Backspace via CDP Input.dispatchKeyEvent."""
    driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
        "type": "keyDown",
        "key": "Backspace",
        "code": "Backspace",
        "windowsVirtualKeyCode": 8,
        "nativeVirtualKeyCode": 8,
    })
    driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
        "type": "keyUp",
        "key": "Backspace",
        "code": "Backspace",
        "windowsVirtualKeyCode": 8,
        "nativeVirtualKeyCode": 8,
    })


def human_type(element, text: str, driver=None, typing_dna: dict = None) -> None:
    """
    Type text with a realistic, per-profile keystroke timing model.

    Improvements over the baseline:
    1. Per-profile 'typing DNA' -- each profile has its own stable rhythm
       parameters (mu, sigma, burst range, error rate) persisted in
       post_state.json.
    2. Typo/correction model -- injects realistic errors (adjacent-key,
       double-strike, wrong-hand) with delayed detection and backspace
       correction.
    3. Fatigue drift -- typing gradually slows over a long text.

    When typing_dna is None, falls back to the session-level
    _get_ctx().active_typing_dna set by run_social_session(), or default mid-range
    parameters if neither exists.
    """
    # -- DEBUG LOGGING: typing audit ----------------------------------------
    _type_t0 = time.perf_counter()
    log.info("[TYPE]  chars=%d  preview=%r  element_tag=%s",
             len(text), text[:30], _safe_tag(element))
    _dlog.debug("[TYPE START]  full_text=%r  chars=%d  dna=%s",
                text, len(text), "custom" if typing_dna else "session")
    # -----------------------------------------------------------------------
    if driver is not None:
        # CDP click at wherever the bezier arc landed -- no centre-snap.
        _cdp_click(driver)
    else:
        element.click()   # fallback when driver is unavailable
    precise_sleep(random.uniform(0.08, 0.25))   # focus-settle after click

    # Resolve typing DNA (session-level or defaults)
    dna        = typing_dna or _get_ctx().active_typing_dna or {}
    _mu        = dna.get("base_mu",            math.log(0.08))
    _sigma     = dna.get("base_sigma",         0.40)
    _burst_min = dna.get("burst_min",          3)
    _burst_max = dna.get("burst_max",          7)
    _sp_lo     = dna.get("space_pause_lo",     0.05)
    _sp_hi     = dna.get("space_pause_hi",     0.18)
    _pp_lo     = dna.get("punct_pause_lo",     0.20)
    _pp_hi     = dna.get("punct_pause_hi",     0.60)
    _bg_lo     = dna.get("burst_gap_lo",       0.06)
    _bg_hi     = dna.get("burst_gap_hi",       0.20)
    _hes_prob  = dna.get("hesitation_prob",    0.04)
    _hes_lo    = dna.get("hesitation_lo",      0.30)
    _hes_hi    = dna.get("hesitation_hi",      0.80)
    _bp_lo     = dna.get("bigram_penalty_lo",  1.4)
    _bp_hi     = dna.get("bigram_penalty_hi",  2.0)
    _err_rate  = dna.get("error_rate",         0.0)
    _corr_prob = dna.get("correction_prob",    0.85)
    _det_delay = dna.get("detection_delay_mean", 1.5)
    _fatigue   = dna.get("fatigue_drift",      0.005)

    # Build the character sequence with injected typos
    typed_sequence = _build_typo_sequence(text, _err_rate, _corr_prob,
                                          _det_delay)

    prev        = ''
    word_len    = 0
    burst_rem   = random.randint(_burst_min, _burst_max)
    chars_typed = 0

    for action in typed_sequence:
        char = action["char"]
        is_backspace = action.get("backspace", False)

        if is_backspace:
            # Backspace timing: faster than normal keystrokes, short
            # reaction-driven delay.
            base = random.lognormvariate(math.log(0.05), 0.30)
            base = max(0.03, min(base, 0.15))
            if driver is not None:
                _cdp_backspace(driver)
            else:
                element.send_keys(Keys.BACKSPACE)
            precise_sleep(base)
            continue

        # Log-normal base with per-profile parameters + fatigue drift.
        fatigue_mult = 1.0 + _fatigue * (chars_typed / 100.0)
        base = random.lognormvariate(_mu, _sigma) * fatigue_mult
        base = max(0.04, min(base, 0.60))

        # Slow bigram penalty
        if (prev + char).lower() in _SLOW_BIGRAMS:
            base *= random.uniform(_bp_lo, _bp_hi)

        # Word boundary
        if char == ' ':
            base += random.uniform(_sp_lo, _sp_hi)
            word_len = 0
        else:
            word_len += 1

        # Post-sentence punctuation re-reading pause
        if prev in '.!?':
            base += random.uniform(_pp_lo, _pp_hi)

        # Rare mid-word hesitation
        if word_len > 4 and random.random() < _hes_prob:
            base += random.uniform(_hes_lo, _hes_hi)

        # Burst gap: extra pause at end of each burst
        burst_rem -= 1
        if burst_rem <= 0:
            base += random.uniform(_bg_lo, _bg_hi)
            burst_rem = random.randint(_burst_min, _burst_max)

        # -- DEBUG LOGGING: keystroke timing audit --------------------------
        _timing_check("human_type_key", base, 0.040, 0.600)
        if base < 0.030:
            _dlog.warning(
                "[RISK WARN]  keystroke interval %.1fms < 30ms floor"
                " -- unnatural speed", base * 1000)
        elif base > 0.700:
            _dlog.warning(
                "[RISK WARN]  keystroke interval %.1fms > 700ms ceiling"
                " -- outside corpus range", base * 1000)
        # -------------------------------------------------------------------

        if driver is not None:
            _cdp_type_key(driver, char)
        else:
            element.send_keys(char)
        precise_sleep(base)
        prev = char
        chars_typed += 1

    # -- DEBUG LOGGING: type complete ---------------------------------------
    _n_typos = sum(1 for a in typed_sequence if a.get("backspace"))
    log.info("[TYPE END]  chars=%d  duration=%.1fs  typos_injected=%d",
             len(text), time.perf_counter() - _type_t0, _n_typos)
    # -----------------------------------------------------------------------


def _bezier_point(p0, p1, p2, t):
    """Quadratic Bezier interpolation between three 2-D control points."""
    x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
    y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
    return int(x), int(y)


# _ease_in_out_sine() REMOVED — symmetric sine ease was a fingerprint-level
# tell detectable by trajectory classifiers.  Replaced by _min_jerk_basis()
# which produces the asymmetric velocity profile of real arm movements.


def _min_jerk_basis(t: float) -> float:
    """Minimum-jerk position basis: 10t^3 - 15t^4 + 6t^5  (Flash & Hogan 1985).

    Produces an asymmetric velocity profile peaking at t ~ 0.47 -- faster
    acceleration and slower deceleration -- matching real arm-movement
    kinematics.  Replaces the symmetric _ease_in_out_sine() which was a
    fingerprint-level tell detectable by trajectory classifiers.
    """
    t2 = t * t
    t3 = t2 * t
    return t3 * (10.0 - 15.0 * t + 6.0 * t2)


def _min_jerk_velocity(t: float) -> float:
    """Normalised minimum-jerk speed: 30t^2 - 60t^3 + 30t^4.

    Derivative of _min_jerk_basis().  Peak value is ~1.875 at t ~ 0.5.
    """
    t2 = t * t
    return 30.0 * t2 - 60.0 * t2 * t + 30.0 * t2 * t2


def _fitts_duration_ms(distance: float, target_width: float = 40.0) -> float:
    """Fitts's Law movement time:  T = a + b * log2(D / W + 1).

    Parameters from motor-control literature with +/-15 % jitter so the
    duration is plausible but never deterministic.
        a ~ 150 ms  (reaction + initiation overhead)
        b ~ 120 ms  (information-processing rate)
    """
    a = 150.0 * random.uniform(0.85, 1.15)
    b = 120.0 * random.uniform(0.85, 1.15)
    id_bits = math.log2(max(1.0, distance) / max(1.0, target_width) + 1.0)
    return max(180.0, a + b * id_bits)


def _make_tremor_components(n: int = 3) -> list:
    """Generate physiological-tremor sinusoid parameters at 8-12 Hz.

    Human hand tremor is narrow-band (8-12 Hz) -- not white Gaussian noise.
    Returns list of (freq_hz, amplitude, phase) tuples.
    """
    return [
        (random.uniform(8.0, 12.0), random.uniform(0.3, 1.5),
         random.uniform(0.0, 2.0 * math.pi))
        for _ in range(n)
    ]


# Cursor pos, last bezier timestamp, and CDP failure count are now tracked
# per-thread in SessionContext (see _get_ctx()).  Access them via
# _get_ctx().cursor_pos / .last_bezier_end_ts / .cdp_consecutive_failures.
_CDP_FAILURE_THRESHOLD: int = 5


class CDPConnectionDead(WebDriverException):
    """Raised when the CDP circuit breaker trips."""
    pass


def _cdp_record_success() -> None:
    """Reset the consecutive-failure counter on a successful CDP call."""
    _get_ctx().cdp_consecutive_failures = 0


def _cdp_record_failure(context: str, exc: Exception) -> None:
    """Record a CDP failure and trip the circuit breaker if threshold exceeded."""
    ctx = _get_ctx()
    ctx.cdp_consecutive_failures += 1
    log.warning(
        "[CDP CIRCUIT]  failure %d/%d  context=%s  error=%s",
        ctx.cdp_consecutive_failures, _CDP_FAILURE_THRESHOLD, context, exc,
    )
    if ctx.cdp_consecutive_failures >= _CDP_FAILURE_THRESHOLD:
        log.error(
            "[CDP CIRCUIT]  TRIPPED — %d consecutive failures.  "
            "Browser connection presumed dead.", ctx.cdp_consecutive_failures,
        )
        raise CDPConnectionDead(
            f"CDP circuit breaker tripped after {ctx.cdp_consecutive_failures} "
            f"consecutive failures (last context: {context})"
        ) from exc


def _set_cursor(x: int, y: int, tag: str = "") -> None:
    """
    Update _cursor_pos and emit a compact one-line position log to BOTH the
    dedicated mouse-movement file (_mlog) AND the main console (log.info),
    so every cursor coordinate change is visible in the live run output.

    Low-level arc detail (ARC / STEP lines) continues to go only to _mlog.
    This function covers the final settled position after each move.
    """
    _get_ctx().cursor_pos[0], _get_ctx().cursor_pos[1] = x, y
    label = f"  [{tag}]" if tag else ""
    _mlog.debug("CURSOR  (%d, %d)%s", x, y, label)


# Followed/visited profile tracking is now in SessionContext.session_followed.
# See _get_ctx().


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
                 _get_ctx().cursor_pos[0], _get_ctx().cursor_pos[1],
                 dom_pos['x'], dom_pos['y'],
                 dom_pos['exists'])
        if dom_pos['exists']:
            drift = math.hypot(dom_pos['x'] - _get_ctx().cursor_pos[0],
                               dom_pos['y'] - _get_ctx().cursor_pos[1])
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
    # Fitts's Law: total arc duration based on distance and target size.
    total_ms   = _fitts_duration_ms(_arc_dist, 40.0)
    steps      = max(20, min(90, int(_arc_dist / 3.5)))  # ~3.5 px/step; clamp 20-90
    step_ms    = total_ms / steps   # derived from Fitts duration, not fixed
    # Physiological tremor: 2-3 narrow-band sinusoids at 8-12 Hz.
    _tremor_components = _make_tremor_components(random.randint(2, 3))
    points     = []
    delays     = []
    prev       = (x0, y0)
    dist_scale = max(0.30, min(_arc_dist / 500.0, 1.0))
    drift_x    = 0.0
    drift_y    = 0.0
    # Corrective sub-movement for long arcs (>200 px):
    # At t ~ 0.75 a small positional correction creates the velocity
    # "notch" characteristic of real Fitts-paradigm pointing movements.
    _has_sub   = _arc_dist > 200 and random.random() < 0.70
    _sub_t     = random.uniform(0.70, 0.82) if _has_sub else 2.0
    _sub_ox    = random.gauss(0, max(2.0, _arc_dist * 0.008))
    _sub_oy    = random.gauss(0, max(2.0, _arc_dist * 0.006))
    _sub_done  = False
    _arc_t0    = time.perf_counter()
    for i in range(1, steps + 1):
        t_raw  = i / steps
        t      = _min_jerk_basis(t_raw)
        nx, ny = _bezier_point((x0, y0), cp, (x1, y1), t)
        # Corrective sub-movement nudge
        if not _sub_done and t_raw >= _sub_t:
            nx = int(nx + _sub_ox)
            ny = int(ny + _sub_oy)
            _sub_done = True
        if i < steps:
            # Physiological tremor: narrow-band sinusoids (8-12 Hz)
            # replacing previous Gaussian white-noise model.
            elapsed = time.perf_counter() - _arc_t0
            bleed_steps    = max(1, min(3, steps // 4))
            steps_from_end = steps - i
            bleed_factor   = (
                steps_from_end / (bleed_steps + 1)
                if steps_from_end <= bleed_steps else 1.0
            )
            # Velocity-dependent amplitude: tremor strongest at endpoints
            # (low velocity), weakest mid-arc (peak velocity).
            vel_norm   = _min_jerk_velocity(t_raw) / 1.88
            vel_factor = 1.0 - vel_norm * 0.55
            approach   = max(0.0, (t_raw - 0.80) / 0.20) if t_raw > 0.80 else 0.0
            tremor_amp = (0.9 * vel_factor + approach * 1.0) * dist_scale * bleed_factor
            tremor_x = sum(
                a * tremor_amp * math.sin(2.0 * math.pi * f * elapsed + p)
                for f, a, p in _tremor_components)
            tremor_y = sum(
                a * tremor_amp * 0.75 * math.sin(2.0 * math.pi * f * elapsed + p + 1.2)
                for f, a, p in _tremor_components)
            nx = int(nx + tremor_x)
            ny = int(ny + tremor_y)
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
        # Per-step delay from minimum-jerk velocity profile.
        vel_n = _min_jerk_velocity(t_raw) / 1.88
        d_ms  = step_ms * (1.4 - vel_n * 0.7) + random.gauss(0, 1.8)
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
    # ── DEBUG LOGGING: MOUSE ARC structured audit ─────────────────────────────
    _cp_offset = int(math.hypot(cp[0] - _mid_x, cp[1] - _mid_y))
    _dlog.debug(
        "[MOUSE ARC]  from=(%d,%d)  to=(%d,%d)  arc_dist=%.0fpx"
        "  steps=%d  duration_ms=%.0f  cp_offset=%dpx  step_ms=%.1f  exact_end=%s",
        x0, y0, x1, y1, _arc_dist, steps, cum_ms, _cp_offset, step_ms, exact_end,
    )
    if _arc_dist > 300 and cum_ms < 150:
        _dlog.warning(
            "[RISK WARN]  unnatural arc speed  dist=%.0fpx  duration=%.0fms"
            " — human minimum ~150ms for 300px+",
            _arc_dist, cum_ms,
        )
    _timing_check("bezier_arc", cum_ms / 1000.0,
                  max(0.10, _arc_dist / 4000.0), max(1.0, _arc_dist / 500.0))
    # ─────────────────────────────────────────────────────────────────────────
    # ── DEBUG LOGGING: update arc-completion timestamp for _cdp_click RISK WARN ──
    _get_ctx().last_bezier_end_ts = time.perf_counter()
    # ─────────────────────────────────────────────────────────────────────────
    if MOUSE_TRACE:
        for i, ((nx, ny, dx, dy), t_ms) in enumerate(zip(points, step_times), 1):
            _mlog.debug("STEP  i=%02d  t=+%.0fms  pos=(%d,%d)  delta=(%+d,%+d)",
                        i, t_ms, nx, ny, dx, dy)
    # Dispatch via CDP Input.dispatchMouseEvent — produces isTrusted:true
    # events with the full pointermove → mousemove chain that real input
    # produces.  Per-step round-trips are ~1 ms over localhost, well within
    # the 8–22 ms inter-step budget.
    for pt, d_ms in zip(points, delays):
        # Fix #6: measure CDP round-trip and subtract from sleep so the actual
        # inter-step interval matches the biomechanical model rather than
        # inflating it by the ~1-2 ms localhost RTT every step.
        _step_t0 = time.perf_counter()
        try:
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": pt[0],
                "y": pt[1],
                # Fix #5: pointer fields required for a fully-spec-compliant
                # PointerEvent; absent fields default to undefined in Chrome's
                # input pipeline, which is detectable via performance.getEntries.
                "pointerType": "mouse",
                "pressure": 0.0,
                "tiltX": 0,
                "tiltY": 0,
                "twist": 0,
            })
            _cdp_record_success()
        except WebDriverException as exc:
            _cdp_record_failure("bezier_arc_step", exc)
        precise_sleep(max(0.0, d_ms / 1000.0 - (time.perf_counter() - _step_t0)))
    return points, delays


def _cdp_click(driver, x: int = None, y: int = None) -> None:
    """Dispatch a trusted click via CDP Input.dispatchMouseEvent.

    If x, y are omitted, clicks at the current _cursor_pos.
    Produces mousePressed + mouseReleased with a realistic inter-event
    gap drawn from a human-like distribution.
    """
    cx = x if x is not None else _get_ctx().cursor_pos[0]
    cy = y if y is not None else _get_ctx().cursor_pos[1]
    # ── DEBUG LOGGING: every click ───────────────────────────────────────────
    log.debug("[CLICK]  pos=(%d,%d)  source=%s", cx, cy,
             "explicit" if x is not None else "cursor_pos")
    # ────────────────────────────────────────────────────────────────────────
    driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
        "type": "mousePressed",
        "x": cx, "y": cy,
        "button": "left",
        "clickCount": 1,
        "pointerType": "mouse",
        "pressure": 0.5,
        "tiltX": 0,
        "tiltY": 0,
        "twist": 0,
    })
    precise_sleep(random.uniform(0.04, 0.11))
    driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
        "type": "mouseReleased",
        "x": cx, "y": cy,
        "button": "left",
        "clickCount": 1,
        "pointerType": "mouse",
        "pressure": 0.0,
        "tiltX": 0,
        "tiltY": 0,
        "twist": 0,
    })
    # ── DEBUG LOGGING: RISK WARN — click within 50ms of bezier completion ────
    try:
        gap_ms = (time.perf_counter() - _get_ctx().last_bezier_end_ts) * 1000
        if 0 < gap_ms < 50:
            _dlog.warning(
                "[RISK WARN]  cdp_click fired %.1fms after bezier arc end"
                " — unnaturally fast (threshold 50ms)  pos=(%d,%d)",
                gap_ms, cx, cy,
            )
        else:
            _dlog.debug("[CLICK]  pos=(%d,%d)  gap_from_arc=%.1fms", cx, cy, gap_ms)
    except Exception:
        pass
    # ────────────────────────────────────────────────────────────────────────

def init_cursor_pos(driver) -> None:
    """
    Silently set _cursor_pos to a random position within the current viewport.

    No DOM event is dispatched — a single-step jump from (0,0) to a random
    coordinate is a detectable bot signal.  The first real cursor event the
    page sees will be the drift arc from _navigate_and_settle or the first
    bezier_move call, both of which start from this seeded position.
    """
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
    # ── DEBUG LOGGING: element interaction audit ──────────────────────────────
    try:
        _log_element_interaction(driver, target_element, "hover")
    except Exception:
        pass
    # ─────────────────────────────────────────────────────────────────────────
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
        x0 = max(0, min(_get_ctx().cursor_pos[0], int(vw)))
        y0 = max(0, min(_get_ctx().cursor_pos[1], int(vh)))
        # Proximity guard: cursor already within 25 px — treat as hovering.
        if math.hypot(x1 - x0, y1 - y0) < 25:
            _cdp_x = int(rect["x"]) + off_dx
            _cdp_y = int(rect["y"]) + off_dy
            try:
                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mouseMoved",
                    "x": _cdp_x,
                    "y": _cdp_y,
                    "pointerType": "mouse",
                    "pressure": 0.0,
                    "tiltX": 0,
                    "tiltY": 0,
                    "twist": 0,
                })
                _cdp_record_success()
            except WebDriverException as exc:
                _cdp_record_failure("bezier_dwell", exc)
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
            # ── DEBUG LOGGING: MOUSE SNAP structured audit ────────────────────
            _dlog.debug(
                "[MOUSE SNAP]  python_pos=(%d,%d)  snap_target=(%d,%d)  drift=%.1fpx",
                last_syn_x, last_syn_y, snap_x, snap_y, snap_gap,
            )
            if snap_gap > 15:
                _dlog.warning(
                    "[MOUSE SNAP]  WARN drift=%.1fpx > 15px threshold"
                    "  python=(%d,%d)  target=(%d,%d)",
                    snap_gap, last_syn_x, last_syn_y, snap_x, snap_y,
                )
            # ──────────────────────────────────────────────────────────────────
        # CDP dispatch already produced trusted events at the exact
        # endpoint — no Phase 2 ActionChains snap needed.
        _set_cursor(snap_x, snap_y, "elem-hover")
        debug_cursor_state(driver, "bezier-snap")

    except CDPConnectionDead:
        raise   # circuit breaker — propagate immediately
    except WebDriverException as exc:
        log.debug("bezier_move failed: %s", exc)

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
    try:
        vw = driver.execute_script("return window.innerWidth")
        vh = driver.execute_script("return window.innerHeight")
        x0 = max(0, min(_get_ctx().cursor_pos[0], int(vw) - 1))
        y0 = max(0, min(_get_ctx().cursor_pos[1], int(vh) - 1))
        x1 = max(0, min(x1, int(vw) - 1))
        y1 = max(0, min(y1, int(vh) - 1))
        if x0 == x1 and y0 == y1:
            return
        # ── DEBUG LOGGING ──────────────────────────────────────────────────
        _dlog.debug("[CURSOR MOVE]  tag=%s  from=(%d,%d)  to=(%d,%d)  dist=%.0fpx",
                    tag, x0, y0, x1, y1, math.hypot(x1 - x0, y1 - y0))
        # ──────────────────────────────────────────────────────────────────
        _fire_bezier_arc(driver, x0, y0, x1, y1, vw, vh, exact_end=True)
        _set_cursor(x1, y1, tag)
        debug_cursor_state(driver, f"bezier-coords/{tag}")
    except CDPConnectionDead:
        raise   # circuit breaker — propagate immediately
    except WebDriverException as exc:
        log.debug("bezier_move_to_coords failed: %s", exc)


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
    # 1. Park at address-bar row
    try:
        vw = driver.execute_script("return window.innerWidth")
    except Exception:
        vw = 1280
    park_x = random.randint(int(vw * 0.25), int(vw * 0.75))
    bezier_move_to_coords(driver, park_x, 0, tag="nav-park")

    # 2. Navigate
    action()
    # ── DEBUG LOGGING: NAV timing markers ────────────────────────────────────
    _nav_t0 = time.perf_counter()
    # ─────────────────────────────────────────────────────────────────────────
    # Phase 1 — wait for the browser's resource-load signal.
    try:
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        pass
    _nav_readystate_ms = (time.perf_counter() - _nav_t0) * 1000
    # Phase 2 — SPA content check: wait for a feed article or pressable
    # container to appear.  readyState fires before React has rendered any
    # feed cards, so without this the settle pause and idle-settle drift
    # happen against a blank loading screen.
    _nav_spa_t0 = time.perf_counter()
    try:
        WebDriverWait(driver, 15).until(
            lambda d: d.find_elements(
                By.CSS_SELECTOR,
                "article, div[data-pressable-container='true']",
            )
        )
    except TimeoutException:
        pass  # fall through — page may still be partially usable
    _nav_spa_ms = (time.perf_counter() - _nav_spa_t0) * 1000

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
    _nav_settle_s = random.uniform(1.5, 3.5)
    precise_sleep(_nav_settle_s)

    # ── DEBUG LOGGING: [NAV] summary ─────────────────────────────────────────
    _overlay_present = False
    try:
        _overlay_present = bool(driver.execute_script(
            "return document.getElementById('__cursor_debug_dot') !== null;"
        ))
    except Exception:
        pass
    log.debug(
        "[NAV]  readystate_wait=%.0fms  spa_wait=%.0fms  settle_wait=%.0fms"
        "  overlay_present=%s  cursor_seeded=(%d,%d)",
        _nav_readystate_ms, _nav_spa_ms, _nav_settle_s * 1000,
        _overlay_present, _get_ctx().cursor_pos[0], _get_ctx().cursor_pos[1],
    )
    # ─────────────────────────────────────────────────────────────────────────

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
    try:
        _from = driver.current_url
    except Exception:
        _from = "unknown"
    log.info("[NAV]  type=goto  from=%s  to=%s", _from[:80], url[:80])
    _navigate_and_settle(driver, lambda: driver.get(url))
    try:
        log.info("[NAV]  landed_url=%s  title=%r",
                 driver.current_url[:80], driver.title[:40])
    except Exception:
        pass


def navigate_history(driver, direction: str = "back") -> None:
    """Go back or forward in history with human-like cursor park → restore → drift."""
    try:
        _from = driver.current_url
    except Exception:
        _from = "unknown"
    log.info("[NAV]  type=%s  from=%s", direction, _from[:80])
    fn = driver.back if direction == "back" else driver.forward
    _navigate_and_settle(driver, fn)
    try:
        log.info("[NAV]  landed_url=%s  title=%r",
                 driver.current_url[:80], driver.title[:40])
    except Exception:
        pass


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
    # ── DEBUG LOGGING: SCROLL CHUNK tracking ──────────────────────────────────
    _sc_t0    = time.perf_counter()
    _sc_dir   = "down" if distance_px >= 0 else "up"
    # ────────────────────────────────────────────────────────────────────
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
            "x": _get_ctx().cursor_pos[0],
            "y": _get_ctx().cursor_pos[1],
            "deltaX": 0,
            "deltaY": direction * move,
            "pointerType": "mouse",
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
            "x": _get_ctx().cursor_pos[0],
            "y": _get_ctx().cursor_pos[1],
            "deltaX": 0,
            "deltaY": direction * remainder,
            "pointerType": "mouse",
        })
    # ── DEBUG LOGGING: [SCROLL CHUNK] summary ──────────────────────────────────
    _dlog.debug(
        "[SCROLL CHUNK]  distance=%dpx  direction=%s  step_px=%d  tick_ms=%d"
        "  steps=%d  actual_duration=%.0fms",
        total, _sc_dir, step_px, tick_ms, steps,
        (time.perf_counter() - _sc_t0) * 1000,
    )
    # ────────────────────────────────────────────────────────────────────


def _sample_scroll_notches() -> int:
    """
    Fix #8: 3-component mixture distribution for scroll distance in wheel notches.
    One notch = deltaY:100 deltaMode:1  ≈ 100-120 px at default browser line height.

      40 % short   2–3 notches  (lazy one-finger flick)
      40 % medium  4–7 notches  (normal reading scroll)
      20 % long    8–14 notches (fast sweep / skipping section)
    """
    r = random.random()
    if r < 0.40:
        return random.randint(2, 3)
    elif r < 0.80:
        return random.randint(4, 7)
    else:
        return random.randint(8, 14)


def _notched_scroll_burst(driver, n_notches: int, direction: int = 1) -> None:
    """
    Fix #7: Dispatch n_notches discrete mouse-wheel notches via CDP using
    deltaMode:1 (line units, deltaY=±100) with natural burst-silence timing.

    Real USB mice report wheel events in firmware-timed bursts of 1-4 notches
    at the USB polling interval, followed by 80-800 ms of silence between
    mechanical detents.  A uniform pixel-delta stream (deltaMode:0) at 12-20 ms
    intervals matches no real input device and is a detectable fingerprint.

    This model:
      - fires 1-4 notches per burst (weighted toward smaller bursts)
      - uses log-normal intra-burst gaps  (μ=25 ms, clamped 6-80 ms)
      - uses log-normal inter-burst silences (μ=140 ms, clamped 80-250 ms)

    direction: 1 = scroll down, -1 = scroll up
    """
    remaining = n_notches
    while remaining > 0:
        burst = min(remaining, random.choices(
            [1, 2, 3, 4], weights=[40, 30, 20, 10]
        )[0])

        for i in range(burst):
            try:
                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type":        "mouseWheel",
                    "x":           _get_ctx().cursor_pos[0],
                    "y":           _get_ctx().cursor_pos[1],
                    "deltaX":      0,
                    "deltaY":      direction * 100,
                    "deltaMode":   1,
                    "pointerType": "mouse",
                })
                _cdp_record_success()
            except WebDriverException as exc:
                _cdp_record_failure("notched_scroll", exc)

            if i < burst - 1:
                # intra-burst: log-normal ≈ 25 ms (USB polling rhythm)
                intra = max(0.006, min(
                    random.lognormvariate(math.log(0.025), 0.35),
                    0.080,
                ))
                precise_sleep(intra)

        remaining -= burst

        if remaining > 0:
            # inter-burst silence: log-normal ≈ 140 ms (hand pause between detents)
            silence = max(0.080, min(
                random.lognormvariate(math.log(0.140), 0.45),
                0.250,
            ))
            precise_sleep(silence)


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
                             _get_ctx().cursor_pos[0] + int(random.gauss(0, vw_r * 0.10))))
                    cy = max(int(vh_r * 0.10), min(int(vh_r * 0.90),
                             _get_ctx().cursor_pos[1] + int(random.gauss(0, vh_r * 0.09))))
                    bezier_move_to_coords(driver, cx, cy, tag="reading-wander")
                except Exception:
                    pass

    deadline = time.time() + total_seconds
    log.info("[ SCROLL ]  scrolling for %.0fs", total_seconds)
    # Scroll-chunk nudge counter — fire a small cursor shift every 3-5 chunks
    # to model the hand resting on the desk and shifting while scrolling.
    _nudge_after  = random.randint(3, 5)
    _chunk_count  = 0
    _total_chunks = 0   # ─ DEBUG: cumulative chunk counter for progress logs
    while time.time() < deadline:
        # Fix #7+#8: discrete notched wheel events with mixture distance distribution
        n_notches = _sample_scroll_notches()
        _notched_scroll_burst(driver, n_notches, direction=1)
        _chunk_count  += 1
        _total_chunks += 1

        # ── DEBUG LOGGING: scroll progress every 5 chunks ─────────────────────
        if _total_chunks % 5 == 0:
            try:
                _sy  = driver.execute_script("return window.scrollY")
                _pct = driver.execute_script(
                    "var h=document.body.scrollHeight-window.innerHeight;"
                    "return h>0?Math.round(window.scrollY/h*100):0;"
                )
                log.debug(
                    "[SCROLL PROGRESS]  chunks=%d  scrollY=%dpx  page_pct=%d%%"
                    "  time_left=%.0fs",
                    _total_chunks, _sy, _pct, deadline - time.time(),
                )
            except Exception:
                pass
        # ───────────────────────────────────────────────────────────────────────

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
                         _get_ctx().cursor_pos[0] + int(random.gauss(0, vw_n * 0.12))))
                ny = max(int(vh_n * 0.10), min(int(vh_n * 0.90),
                         _get_ctx().cursor_pos[1] + int(random.gauss(0, vh_n * 0.10))))
                bezier_move_to_coords(driver, nx, ny, tag="scroll-drift")
            except Exception:
                pass

        # 4-tier reading pause — cursor drifts throughout via _reading_pause()
        tier = random.random()
        if tier < 0.03:
            _pause_tier = "distraction"
            _pause_s    = random.uniform(8.0, 15.0)
        elif tier < 0.18:
            _pause_tier = "long"
            _pause_s    = random.uniform(4.5, 9.0)
        elif tier < 0.35:
            _pause_tier = "skim"
            _pause_s    = random.uniform(0.3, 1.2)
        else:
            _pause_tier = "normal"
            _pause_s    = random.uniform(1.5, 4.0)
        # ── DEBUG LOGGING: [SCROLL CHUNK] with tier info ────────────────────────
        _dlog.debug(
            "[SCROLL CHUNK]  pause_tier=%s  pause_duration=%.1fs",
            _pause_tier, _pause_s,
        )
        _timing_check(f"reading_pause_{_pause_tier}", _pause_s,
                      {"distraction": 8.0, "long": 4.5, "skim": 0.3, "normal": 1.5}[_pause_tier],
                      {"distraction": 15.0, "long": 9.0, "skim": 1.2, "normal": 4.0}[_pause_tier])
        # ────────────────────────────────────────────────────────────────────
        _reading_pause(_pause_s)
        # occasional upward drift — small (re-reading) or large (going back to a post)
        if random.random() < 0.22:
            # 20 % of drift events scroll back a large amount (really went too far)
            up_notches = (
                random.randint(4, 8) if random.random() < 0.20
                else random.randint(1, 3)
            )
            _notched_scroll_burst(driver, up_notches, direction=-1)
            dwell = random.uniform(1.5, 4.0) if up_notches >= 4 else random.uniform(0.4, 1.2)
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
        try:
            navigate_to(driver, site)
        except (TimeoutException, WebDriverException):
            log.warning("Pre-flight: %s timed out — skipping", site)
            continue
        stochastic_scroll(driver, total_seconds=dwell)


# ================================================================== #
#  MULTI-SIGNAL LIKE ENGINE
# ================================================================== #
#
# Element identification uses composite scoring across multiple signals
# instead of relying solely on fragile aria-label selectors:
#   1. Structural position in the action bar (like = 1st button)
#   2. SVG path geometry (heart = many Bezier curves, ~square viewBox)
#   3. Fill state (transparent = un-liked, currentColor = liked)
#   4. Sibling context (3-5 icon-button siblings = action bar)
#   5. ARIA labels (low-weight fallback covering known locales)
#
# Self-healing: when no candidate passes ELEMENT_CONFIDENCE_THRESHOLD
# a DOM snapshot is logged for offline selector maintenance.  Legacy
# XPath/CSS selectors serve as a fallback during transitions.
# ================================================================== #

# JavaScript: multi-signal like-button scorer.
# Returns [[WebElement, score, positionIdx, siblingCount, ariaLabel], ...].
# Selenium deserialises returned DOM nodes as WebElements.
_JS_MULTI_SIGNAL_LIKE = r"""
(function(threshold) {
    var vp = window.innerHeight;
    var results = [];
    var posts = document.querySelectorAll(
        'article, [data-pressable-container="true"]'
    );

    for (var p = 0; p < posts.length; p++) {
        var post = posts[p];
        var pr   = post.getBoundingClientRect();
        if (pr.bottom < -100 || pr.top > vp + 100 || pr.height === 0) continue;

        /* -- Find the action bar structurally --
           The action bar is a container whose direct children include
           3-6 role="button" elements each wrapping an SVG icon.
           We pick the one lowest (largest relative-Y) in the post. */
        var allDivs  = post.querySelectorAll('div');
        var barBtns  = [];
        var bestRelY = -Infinity;

        for (var d = 0; d < allDivs.length; d++) {
            var ctr  = allDivs[d];
            var btns = [];
            for (var c = 0; c < ctr.children.length; c++) {
                var ch  = ctr.children[c];
                var rb  = (ch.getAttribute && ch.getAttribute('role') === 'button')
                          ? ch
                          : (ch.querySelector ? ch.querySelector('[role="button"]') : null);
                if (rb && rb.querySelector('svg')) btns.push(rb);
            }
            if (btns.length < 3 || btns.length > 6) continue;
            var cr   = ctr.getBoundingClientRect();
            var relY = cr.top - pr.top;
            if (relY > bestRelY) { bestRelY = relY; barBtns = btns; }
        }
        if (!barBtns.length) continue;

        /* -- Score each button in the action bar -- */
        for (var i = 0; i < barBtns.length; i++) {
            var btn = barBtns[i];
            var svg = btn.querySelector('svg');
            if (!svg) continue;
            var br = btn.getBoundingClientRect();
            if (br.height === 0 || br.width === 0) continue;

            var s = 0.0;

            /* Signal 1 - Position: like is almost always first */
            if      (i === 0) s += 0.25;
            else if (i === 1) s += 0.08;
            else              s -= 0.15;

            /* Signal 2 - SVG path geometry: heart = many Bezier curves */
            var paths = svg.querySelectorAll('path');
            for (var pp = 0; pp < paths.length; pp++) {
                var dd     = paths[pp].getAttribute('d') || '';
                var curves = (dd.match(/[CcQqSsAa]/g) || []).length;
                if (curves >= 4) { s += 0.15; break; }
            }

            /* Signal 3 - ViewBox aspect ratio: hearts are roughly square */
            var vb = (svg.getAttribute('viewBox') || '').split(/[\s,]+/);
            if (vb.length === 4) {
                var rat = parseFloat(vb[2]) / Math.max(1, parseFloat(vb[3]));
                if (rat > 0.8 && rat < 1.3) s += 0.10;
            }

            /* Signal 4 - Fill state: transparent = NOT yet liked */
            var sty  = svg.getAttribute('style') || '';
            var fill = svg.getAttribute('fill')  || '';
            var isTransparent = (sty.indexOf('transparent') > -1 ||
                                 fill === 'transparent' || fill === 'none');
            var isFilled      = (sty.indexOf('currentColor') > -1 ||
                                 fill === 'currentColor');
            if (isTransparent) s += 0.15;
            if (isFilled)      s -= 0.50;

            /* Signal 5 - ARIA label (low weight fallback) */
            var lbl = (svg.getAttribute('aria-label') ||
                       btn.getAttribute('aria-label') || '').toLowerCase();
            var likeL   = ['like','love','heart','me gusta',"j'aime",
                           'curtir','gefällt mir','\u3044\u3044\u306d','\uC88B\uC544\uC694',
                           '\u8D5E','mi piace','thích'];
            var unlikeL = ['unlike','unlove','no me gusta','descurtir',
                           "je n'aime plus",'\u3044\u3044\u306d\u53d6\u6d88',
                           '\uC88B\uC544\uC694 \uCDE8\uC18C','\u53d6\u6d88\u8d5e'];
            for (var kl = 0; kl < likeL.length;   kl++) {
                if (lbl === likeL[kl])   { s += 0.12; break; }
            }
            for (var ku = 0; ku < unlikeL.length; ku++) {
                if (lbl === unlikeL[ku]) { s -= 0.60; break; }
            }

            /* Signal 6 - aria-pressed */
            if (btn.getAttribute('aria-pressed') === 'true') s -= 0.40;

            /* Signal 7 - Sibling context: 3-5 icon-button siblings */
            if (barBtns.length >= 3 && barBtns.length <= 5) s += 0.08;

            if (s >= threshold) {
                results.push([btn, s, i, barBtns.length, lbl || '']);
            }
        }
    }

    results.sort(function(a, b) { return b[1] - a[1]; });
    return results;
})(arguments[0]);
"""

# JavaScript: multi-signal reply-button scorer.
# Reply is typically the 2nd button in the action bar; its SVG has a
# speech-bubble shape (mix of curves and lines, moderate total commands).
_JS_MULTI_SIGNAL_REPLY = r"""
(function(threshold) {
    var vp = window.innerHeight;
    var results = [];
    var posts = document.querySelectorAll(
        'article, [data-pressable-container="true"]'
    );

    for (var p = 0; p < posts.length; p++) {
        var post = posts[p];
        var pr   = post.getBoundingClientRect();
        if (pr.bottom < -100 || pr.top > vp + 100 || pr.height === 0) continue;

        var allDivs  = post.querySelectorAll('div');
        var barBtns  = [];
        var bestRelY = -Infinity;

        for (var d = 0; d < allDivs.length; d++) {
            var ctr  = allDivs[d];
            var btns = [];
            for (var c = 0; c < ctr.children.length; c++) {
                var ch  = ctr.children[c];
                var rb  = (ch.getAttribute && ch.getAttribute('role') === 'button')
                          ? ch
                          : (ch.querySelector ? ch.querySelector('[role="button"]') : null);
                if (rb && rb.querySelector('svg')) btns.push(rb);
            }
            if (btns.length < 3 || btns.length > 6) continue;
            var cr   = ctr.getBoundingClientRect();
            var relY = cr.top - pr.top;
            if (relY > bestRelY) { bestRelY = relY; barBtns = btns; }
        }
        if (!barBtns.length) continue;

        for (var i = 0; i < barBtns.length; i++) {
            var btn = barBtns[i];
            var svg = btn.querySelector('svg');
            if (!svg) continue;
            var br = btn.getBoundingClientRect();
            if (br.height === 0) continue;

            var s = 0.0;

            /* Position: reply is typically second (index 1) */
            if      (i === 1) s += 0.30;
            else if (i === 0) s -= 0.10;
            else if (i === 2) s += 0.05;
            else              s -= 0.15;

            /* SVG geometry: speech bubbles have moderate curve+line mix */
            var paths = svg.querySelectorAll('path');
            for (var pp = 0; pp < paths.length; pp++) {
                var dd     = paths[pp].getAttribute('d') || '';
                var curves = (dd.match(/[CcQqSsAa]/g) || []).length;
                var lines  = (dd.match(/[LlHhVv]/g) || []).length;
                if (curves >= 2 && curves <= 8 && (curves + lines) >= 3) {
                    s += 0.15; break;
                }
            }

            /* ViewBox aspect: speech bubbles are often roughly square-ish */
            var vb = (svg.getAttribute('viewBox') || '').split(/[\s,]+/);
            if (vb.length === 4) {
                var rat = parseFloat(vb[2]) / Math.max(1, parseFloat(vb[3]));
                if (rat > 0.85 && rat < 1.4) s += 0.08;
            }

            /* ARIA label (low weight) */
            var lbl = (svg.getAttribute('aria-label') ||
                       btn.getAttribute('aria-label') || '').toLowerCase();
            var replyL = ['reply','comment','respond','responder','répondre',
                          'antworten','\u8FD4\u4FE1','comentar','\uB313\uAE00',
                          '\u8BC4\u8BBA','rispondi'];
            for (var k = 0; k < replyL.length; k++) {
                if (lbl === replyL[k]) { s += 0.15; break; }
            }

            /* Sibling context */
            if (barBtns.length >= 3 && barBtns.length <= 5) s += 0.08;

            if (s >= threshold) results.push([btn, s, i]);
        }
    }

    results.sort(function(a, b) { return b[1] - a[1]; });
    return results;
})(arguments[0]);
"""


def _log_selector_failure(driver, element_type: str) -> None:
    """Log a DOM snapshot for offline selector maintenance when scoring fails."""
    try:
        snapshot = driver.execute_script("""
            var posts = document.querySelectorAll(
                'article, [data-pressable-container="true"]'
            );
            var out = [];
            for (var i = 0; i < Math.min(posts.length, 3); i++) {
                var p = posts[i];
                var btns = p.querySelectorAll('div[role="button"]');
                var info = [];
                for (var b = 0; b < Math.min(btns.length, 8); b++) {
                    var svg = btns[b].querySelector('svg');
                    info.push({
                        aria: btns[b].getAttribute('aria-label') || '',
                        svg_aria: svg ? (svg.getAttribute('aria-label') || '') : '',
                        pressed: btns[b].getAttribute('aria-pressed') || '',
                        visible: btns[b].offsetParent !== null,
                    });
                }
                out.push({ btn_count: info.length, buttons: info });
            }
            return out;
        """)
        log.warning(
            "[SELF-HEAL]  element=%s  score_failure  dom_snapshot=%s",
            element_type, json.dumps(snapshot)[:500],
        )
    except Exception:
        log.warning(
            "[SELF-HEAL]  element=%s  score_failure  snapshot_unavailable",
            element_type,
        )


def _find_unliked_buttons_fallback(driver) -> list:
    """Legacy XPath/CSS fallback for like buttons — used when JS scoring fails."""
    results = []
    viewport_h = driver.execute_script("return window.innerHeight")

    for xp in [
        "//div[@role='button'][.//*[local-name()='svg'][@aria-label='Like']]",
    ]:
        try:
            for el in driver.find_elements(By.XPATH, xp):
                try:
                    r = driver.execute_script(
                        "var r=arguments[0].getBoundingClientRect();"
                        "return {top:r.top,height:r.height};", el)
                    if r["height"] > 0 and -50 <= r["top"] <= viewport_h + 50:
                        if el.is_displayed():
                            pressed = el.get_attribute("aria-pressed")
                            label   = (el.get_attribute("aria-label") or "").lower()
                            if pressed != "true" and label not in _KNOWN_UNLIKE_LABELS:
                                results.append(el)
                except Exception:
                    continue
        except (NoSuchElementException, WebDriverException):
            continue
        if results:
            log.info("[FALLBACK]  XPath: %d unliked like button(s)", len(results))
            break

    if not results:
        for sel in ["div[role='button']:has(svg[aria-label='Like'])"]:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        r = driver.execute_script(
                            "var r=arguments[0].getBoundingClientRect();"
                            "return {top:r.top,height:r.height};", el)
                        if r["height"] > 0 and -50 <= r["top"] <= viewport_h + 50:
                            if el.is_displayed():
                                pressed = el.get_attribute("aria-pressed")
                                label   = (el.get_attribute("aria-label") or "").lower()
                                if pressed != "true" and label not in _KNOWN_UNLIKE_LABELS:
                                    results.append(el)
                    except Exception:
                        continue
            except (NoSuchElementException, WebDriverException):
                continue
            if results:
                log.info("[FALLBACK]  CSS: %d unliked like button(s)", len(results))
                break

    if not results:
        log.info("No likeable posts visible in viewport (fallback)")
    return results


def _find_unliked_buttons(driver) -> list:
    """
    Return all clickable like-button wrapper divs that are visible in the
    current viewport and have NOT been liked yet.

    Uses multi-signal composite scoring:
      1. Structural position in the action bar
      2. SVG path geometry (heart fingerprint)
      3. Fill state (transparent = un-liked)
      4. Sibling context (3-5 icon buttons)
      5. ARIA labels (low-weight fallback)

    Elements scoring below ELEMENT_CONFIDENCE_THRESHOLD are discarded.
    Falls back to legacy XPath/CSS selectors if JS scoring fails.
    """
    results = []
    try:
        raw = driver.execute_script(
            _JS_MULTI_SIGNAL_LIKE, ELEMENT_CONFIDENCE_THRESHOLD
        )

        if not raw:
            log.info("[MULTI-SIGNAL]  no like-button candidates found in viewport")
            _log_selector_failure(driver, "like")
            # Try legacy fallback
            return _find_unliked_buttons_fallback(driver)

        for item in raw:
            el    = item[0]
            score = item[1]
            pos   = item[2]
            sibs  = item[3]
            label = item[4] if len(item) > 4 else ""
            try:
                if not el.is_displayed():
                    continue
                results.append(el)
                log.debug(
                    "[MULTI-SIGNAL]  like candidate  score=%.2f  pos=%d/%d  aria=%r",
                    score, pos, sibs, label,
                )
            except Exception:
                continue

        if results:
            log.info(
                "[MULTI-SIGNAL]  %d unliked like button(s) found (best_score=%.2f)",
                len(results), raw[0][1] if raw else 0,
            )
        else:
            log.info("[MULTI-SIGNAL]  candidates found but none displayed")
            _log_selector_failure(driver, "like")

    except WebDriverException as exc:
        log.debug("[MULTI-SIGNAL]  like-button scan error: %s", exc)
        results = _find_unliked_buttons_fallback(driver)

    return results


def _find_reply_buttons(driver) -> list:
    """
    Return visible reply buttons using multi-signal structural scoring.

    The reply button is identified as the 2nd button in the action bar
    with speech-bubble SVG geometry.  Falls back to the legacy CSS
    selector (REPLY_BTN_CSS) if JS scoring fails.
    """
    results = []
    try:
        raw = driver.execute_script(
            _JS_MULTI_SIGNAL_REPLY, ELEMENT_CONFIDENCE_THRESHOLD
        )
        if raw:
            for item in raw:
                el = item[0]
                try:
                    if el.is_displayed():
                        r = driver.execute_script(
                            "var r=arguments[0].getBoundingClientRect();"
                            "return {top:r.top,h:r.height};", el)
                        vh = driver.execute_script("return window.innerHeight")
                        if r["h"] > 0 and 0 <= r["top"] <= vh:
                            results.append(el)
                except Exception:
                    continue
        if results:
            log.info("[MULTI-SIGNAL]  %d reply button(s) found", len(results))
            return results
    except WebDriverException:
        pass

    # Legacy fallback
    for btn in driver.find_elements(By.CSS_SELECTOR, REPLY_BTN_CSS):
        try:
            r = driver.execute_script(
                "var r=arguments[0].getBoundingClientRect();"
                "return {top:r.top,h:r.height};", btn)
            vh = driver.execute_script("return window.innerHeight")
            if r["h"] > 0 and 0 <= r["top"] <= vh and btn.is_displayed():
                results.append(btn)
        except Exception:
            continue
    if results:
        log.info("[FALLBACK]  %d reply button(s) found via CSS", len(results))
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
            log.warning("[LOGIN]  status=logged_out  reason=login_redirect  url=%s", url[:80])
            return False

        # URL-based challenge detection — specific paths only
        if any(s in url for s in CHALLENGE_URL_PATHS):
            log.warning("[LOGIN]  status=challenge  reason=challenge_url  url=%s", url[:80])
            return False

        # DOM-based challenge detection — structural elements only
        for sel in CHALLENGE_DOM_SELECTORS:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    log.warning("[LOGIN]  status=challenge  reason=dom_element  selector=%s", sel)
                    return False
            except NoSuchElementException:
                continue

        # Feed present — logged in
        articles = driver.find_elements(
            By.CSS_SELECTOR,
            "article, div[data-pressable-container='true']",
        )
        if articles:
            log.info("[LOGIN]  status=logged_in  feed_items=%d  url=%s",
                     len(articles), url[:80])
            return True

        # Fallback: on threads domain with no challenge signals
        if "threads.net" in url or "threads.com" in url:
            log.info("[LOGIN]  status=logged_in(presumed)  url=%s", url[:80])
            return True

    except CDPConnectionDead:
        raise   # circuit breaker — propagate immediately
    except WebDriverException as exc:
        log.debug("check_login_status failed: %s", exc)
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
    _action_t0 = time.perf_counter()
    log.info("[ACTION START]  action=profile_view")
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
                if href.rstrip("/") in _get_ctx().session_followed:
                    continue
                # Viewport filter — only keep elements currently on-screen
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
            log.info("[ACTION SKIP]  action=profile_view  reason=no_feed_profile_links")
            return False

        # Try up to 3 candidates in case one goes stale between scan and click
        random.shuffle(candidates)
        for attempt, target in enumerate(candidates[:3]):
            profile_url = target.get_attribute("href")
            if not profile_url:
                continue

            # Re-validate — element may have scrolled off-screen since the candidate
            # list was built (page could have loaded more content / user scroll).
            _rect = driver.execute_script(
                "var r=arguments[0].getBoundingClientRect();"
                "return {y: r.top, h: r.height};",
                target,
            )
            _vh = driver.execute_script("return window.innerHeight")
            if _rect["h"] == 0 or _rect["y"] < 0 or _rect["y"] > _vh:
                log.debug("Profile link scrolled off-screen since scan — trying next candidate (%d)", attempt + 1)
                continue

            # Found a valid on-screen candidate — proceed with it
            break
        else:
            log.debug("Profile link scrolled off-screen since scan — all candidates exhausted")
            return False

        # Scroll the link loosely into view before moving the cursor to it.
        scroll_element_into_loose_view(driver, target)

        _get_ctx().session_followed.add(profile_url.rstrip("/"))
        log.info("[PROFILE VIEW]  candidates=%d  target=%s",
                 len(candidates[:15]), profile_url[:60])
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
        log.info("[ACTION END]  action=profile_view  result=success  duration=%.1fs",
                 time.perf_counter() - _action_t0)
        return True

    except (TimeoutException, WebDriverException) as exc:
        log.warning("[ACTION END]  action=profile_view  result=failure  error=%s", exc)
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
    _action_t0 = time.perf_counter()
    log.info("[ACTION START]  action=follow_feed")
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
                if href.rstrip("/") in _get_ctx().session_followed:
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
            log.info("[ACTION SKIP]  action=follow_feed  reason=no_visible_feed_profile_links")
            return False

        log.info("[FOLLOW FEED]  candidates=%d", len(candidates))
        username_el = random.choice(candidates[:10])

        # ── 2. Scroll username into view, then hover (no click) ───────────────
        scroll_element_into_loose_view(driver, username_el)

        # Snapshot of text-based Follow buttons already in DOM before hover
        pre_follow_ids = set(
            el.id for el in driver.find_elements(By.XPATH, FOLLOW_BTN_XPATH)
        )

        bezier_move(driver, username_el)          # hover — ActionChains fires mouseenter

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
        _get_ctx().session_followed.add((username_el.get_attribute("href") or "").rstrip("/"))

        # ── DEBUG LOGGING: ACTION END (success) ──────────────────────────────────
        _get_ctx().session_metrics["follows"] += 1
        _get_ctx().session_metrics["actions_dispatched"] += 1
        log.info("[ACTION END]  action=follow_feed  result=success")
        # ────────────────────────────────────────────────────────────────────

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
        log.warning("[ACTION END]  action=follow_feed  result=failure  error=%s", exc)
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
                "[ACTION SKIP]  action=passive  reason=off_feed  recovered=True  url=%s",
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
    # ── DEBUG LOGGING: ACTION START ────────────────────────────────────────────
    _action_t0 = time.perf_counter()
    _get_ctx().session_metrics["actions_dispatched"] += 1
    log.info("[ACTION START]  action=passive")
    # ────────────────────────────────────────────────────────────────────
    log.info("[ PASSIVE ]  scroll %.0fs", scroll_time)
    stochastic_scroll(driver, total_seconds=scroll_time)

    # Pause after scrolling stops — user finishes reading the post
    precise_sleep(random.uniform(1.0, 3.0))
    # ── DEBUG LOGGING: ACTION END ────────────────────────────────────────────
    _get_ctx().session_metrics["passive"] += 1
    _log_page_state(driver, "passive_end")
    log.info("[ACTION END]  action=passive  result=success  duration=%.1fs",
             time.perf_counter() - _action_t0)
    # ────────────────────────────────────────────────────────────────────


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
            "[ACTION SKIP]  action=active  reason=not_on_threads  url=%s",
            current_url[:60],
        )
        stochastic_scroll(driver, total_seconds=random.uniform(15, 30))
        return

    # ── DEBUG LOGGING: ACTION START ────────────────────────────────────────────
    _action_t0 = time.perf_counter()
    _get_ctx().session_metrics["actions_dispatched"] += 1
    _log_page_state(driver, "active_start")
    log.info("[ACTION START]  action=active  url=%s", current_url[:60])
    # ────────────────────────────────────────────────────────────────────
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
        log.info("[ACTIVE]  unliked_buttons_found=%d", len(candidates))
        if not candidates:
            log.info("[ACTION SKIP]  action=active  reason=no_likeable_posts"
                     "  fallback=passive_scroll")
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
        log.warning("[ACTIVE]  error: %s", exc)

    # ── DEBUG LOGGING: ACTION END ────────────────────────────────────────────
    _get_ctx().session_metrics["likes"] += liked
    _log_page_state(driver, "active_end")
    log.info("[ACTION END]  action=active  result=success  likes=%d  duration=%.1fs",
             liked, time.perf_counter() - _action_t0)
    # ────────────────────────────────────────────────────────────────────


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
            log.info("[ACTION SKIP]  action=read_post  reason=not_on_threads  url=%s",
                     current_url[:60])
            return False
        # ── DEBUG LOGGING: ACTION START ─────────────────────────────────────────
        _action_t0 = time.perf_counter()
        _get_ctx().session_metrics["actions_dispatched"] += 1
        log.info("[ACTION START]  action=read_post")
        # ────────────────────────────────────────────────────────────────────

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

        log.info("[READ POST]  visible_post_links=%d", len(visible))
        if not visible:
            log.info("[ACTION SKIP]  action=read_post  reason=no_visible_post_links")
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
        # ── DEBUG LOGGING: ACTION END (success) ──────────────────────────────────
        _get_ctx().session_metrics["reads"] += 1
        log.info("[ACTION END]  action=read_post  result=success  duration=%.1fs",
                 time.perf_counter() - _action_t0)
        # ────────────────────────────────────────────────────────────────────
        return True

    except (NoSuchElementException, TimeoutException, WebDriverException) as exc:
        log.warning("[ACTION END]  action=read_post  result=failure  error=%s", exc)
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
            log.info("[ACTION SKIP]  action=comment  reason=not_on_threads  url=%s",
                     current_url[:60])
            return False
        # ── DEBUG LOGGING: ACTION START ─────────────────────────────────────────
        _action_t0 = time.perf_counter()
        _get_ctx().session_metrics["actions_dispatched"] += 1
        log.info("[ACTION START]  action=comment")
        # ────────────────────────────────────────────────────────────────────

        # 1. Collect visible Reply buttons using multi-signal scoring
        visible = _find_reply_buttons(driver)

        log.info("[COMMENT]  visible_reply_buttons=%d", len(visible))
        if not visible:
            log.info("[ACTION SKIP]  action=comment  reason=no_visible_reply_buttons")
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
        # ── DEBUG LOGGING: ACTION END (success) ──────────────────────────────────
        _get_ctx().session_metrics["comments"] += 1
        log.info("[ACTION END]  action=comment  result=success  duration=%.1fs",
                 time.perf_counter() - _action_t0)
        # ────────────────────────────────────────────────────────────────────
        return True

    except (NoSuchElementException, TimeoutException, WebDriverException) as exc:
        log.warning("[ACTION END]  action=comment  result=failure  error=%s", exc)
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

# ── Cross-process state safety ───────────────────────────────────────────────
# Every load/modify/save cycle on POST_STATE_FILE must happen inside a
# "with _post_state_locked():" block.  The context manager holds:
#   1. threading.Lock()   — in-process serialisation between threads.
#   2. OS file lock on POST_STATE_FILE + ".lock" — cross-process exclusive
#      access when multiple profiles are launched in parallel.
# _save_post_state() uses a tmp-fsync-replace pattern so a crash between
# truncate and write can never produce an empty or partial state file.
# ─────────────────────────────────────────────────────────────────────────────
_POST_STATE_TLOCK = threading.Lock()


@contextlib.contextmanager
def _post_state_locked():
    """Acquire exclusive access to POST_STATE_FILE for a load-modify-save cycle.

    Combines an in-process threading.Lock with an OS-level file lock on
    ``POST_STATE_FILE + ".lock"`` so that concurrent threads *and* concurrent
    processes (parallel profile runs) are both serialised.

    Windows uses ``msvcrt.locking`` (LK_LOCK = blocking exclusive byte-range
    lock). POSIX uses ``fcntl.flock(LOCK_EX)``.

    Usage::

        with _post_state_locked():
            state = _load_post_state()
            # ... mutate state ...
            _save_post_state(state)
    """
    lock_path = POST_STATE_FILE + ".lock"
    with _POST_STATE_TLOCK:
        lf = open(lock_path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt as _msvcrt
                # LK_LOCK retries every 1 s for up to 10 s then raises OSError.
                lf.seek(0)
                _msvcrt.locking(lf.fileno(), _msvcrt.LK_LOCK, 1)
            else:
                import fcntl as _fcntl
                _fcntl.flock(lf.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                if sys.platform == "win32":
                    import msvcrt as _msvcrt
                    lf.seek(0)
                    _msvcrt.locking(lf.fileno(), _msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl as _fcntl
                    _fcntl.flock(lf.fileno(), _fcntl.LOCK_UN)
        finally:
            lf.close()


def _load_post_state() -> dict:
    """Read posting state from POST_STATE_FILE.

    Must be called from within a ``_post_state_locked()`` context whenever the
    caller intends to mutate and save.  Safe to call outside the lock for
    read-only queries where a slightly stale snapshot is acceptable.
    """
    if os.path.exists(POST_STATE_FILE):
        try:
            with open(POST_STATE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("post_state load failed (%s) — starting fresh", exc)
    return {}


def _save_post_state(state: dict) -> None:
    """Atomically persist *state* to POST_STATE_FILE.

    Writes to a sibling ``.tmp`` file, fsyncs to flush kernel buffers, then
    calls ``os.replace()`` which is atomic on both Windows (Vista+) and POSIX.
    A crash before ``os.replace()`` leaves the original file intact; a crash
    after leaves the complete new file.  Either way the state is never empty.

    Must be called from within a ``_post_state_locked()`` context.
    """
    tmp_path = POST_STATE_FILE + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, POST_STATE_FILE)
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
        # ── DEBUG LOGGING: [POST GATE] blocked by Poisson ────────────────────
        days_old_pg = (date.fromisoformat(today) - date.fromisoformat(entry["first_seen"])).days
        log.info(
            "[POST GATE]  profile=%s  days_old=%d  quota=%d  today_count=%d"
            "  last_post_ago=%.1fh  next_post_in=%.1fh  poisson_gate=block"
            "  result=blocked_cooldown",
            profile_id, days_old_pg, _post_daily_quota(days_old_pg),
            entry.get("daily_counts", {}).get(today, 0),
            (now - entry.get("last_post_ts", now)) / 3600,
            wait_min / 60,
        )
        # ────────────────────────────────────────────────────────────────────
        log.info("[ POST ]  skipping — next post allowed in %.0f min (Poisson gate)", wait_min)
        return False
    # Always enforce hard floor as well
    elapsed = now - entry.get("last_post_ts", 0.0)
    _soft_floor = _sample_post_min_gap_sec()
    if elapsed < _soft_floor:
        days_old_pg2 = (date.fromisoformat(today) - date.fromisoformat(entry["first_seen"])).days
        # ── DEBUG LOGGING: [POST GATE] hard floor ─────────────────────────────
        log.info(
            "[POST GATE]  profile=%s  days_old=%d  quota=%d  today_count=%d"
            "  last_post_ago=%.1fh  next_post_in=%.1fh  poisson_gate=pass"
            "  result=blocked_cooldown",
            profile_id, days_old_pg2, _post_daily_quota(days_old_pg2),
            entry.get("daily_counts", {}).get(today, 0),
            elapsed / 3600,
            max(0, (entry.get("next_post_ts", 0) - now)) / 3600,
        )
        # ────────────────────────────────────────────────────────────────────
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
        # ── DEBUG LOGGING: [POST GATE] ramp-up block ───────────────────────────
        log.info(
            "[POST GATE]  profile=%s  days_old=%d  quota=0  today_count=%d"
            "  last_post_ago=%.1fh  next_post_in=n/a  poisson_gate=pass"
            "  result=blocked_rampup",
            profile_id, days_old,
            entry.get("daily_counts", {}).get(today, 0),
            (now - entry.get("last_post_ts", now)) / 3600,
        )
        # ────────────────────────────────────────────────────────────────────
        log.info(
            "[ POST ]  skipping — account age %d day(s), quota=0 during ramp-up",
            days_old,
        )
        return False

    # Daily cap
    today_count = entry.get("daily_counts", {}).get(today, 0)
    if today_count >= quota:
        # ── DEBUG LOGGING: [POST GATE] daily quota exhausted ──────────────────────
        log.info(
            "[POST GATE]  profile=%s  days_old=%d  quota=%d  today_count=%d"
            "  last_post_ago=%.1fh  next_post_in=%.1fh  poisson_gate=pass"
            "  result=blocked_quota",
            profile_id, days_old, quota, today_count,
            (now - entry.get("last_post_ts", now)) / 3600,
            max(0, (entry.get("next_post_ts", 0) - now)) / 3600,
        )
        # ────────────────────────────────────────────────────────────────────
        log.info(
            "[ POST ]  skipping — daily quota %d reached (%d posted today)",
            quota, today_count,
        )
        return False

    # ── DEBUG LOGGING: [POST GATE] allowed ────────────────────────────────────
    log.info(
        "[POST GATE]  profile=%s  days_old=%d  quota=%d  today_count=%d"
        "  last_post_ago=%.1fh  next_post_in=%.1fh  poisson_gate=pass"
        "  result=allowed",
        profile_id, days_old, quota, today_count,
        (now - entry.get("last_post_ts", now)) / 3600,
        max(0, (entry.get("next_post_ts", 0) - now)) / 3600,
    )
    # ────────────────────────────────────────────────────────────────────
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
      1b. Horizontal flip — 50 % chance    (pHash distance +8–15 bits)
      1c. Random rotation 2–5 °            (geometry/DCT fingerprint shift)
      2. ±12 % brightness adjustment       (DCT coefficient shift)
      3. ±12 % contrast adjustment         (DCT coefficient shift)
      3b. Invisible corner stamp           (raw pixel alteration)
      4. Re-encode at randomised quality   (file bytes change)
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
    from PIL import Image, ImageDraw, ImageEnhance  # Pillow – always installed

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

    # 1b. Horizontal flip — 50 % chance.  A flip changes every pixel's
    #     position, pushing the pHash distance to 8–15 bits vs the source,
    #     which is well above any practical near-duplicate threshold.
    _flip = random.random() < 0.5
    if _flip:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)

    # 1c. Small random rotation (2–5 °) in a random direction.  Combined with
    #     the optional flip this ensures geometry-based hashes (dHash, aHash)
    #     also differ significantly on every call.
    angle = random.uniform(2.0, 5.0) * random.choice([-1, 1])
    img = img.rotate(angle, resample=Image.BICUBIC, expand=False)

    # 2. Brightness ±12 % (was ±2 %) — wider luminance shift moves DCT
    #    coefficients far outside the ±1-LSB neighbourhood that pHash
    #    near-duplicate detection relies on.
    b_factor = 1.0 + random.uniform(-0.12, 0.12)
    img = ImageEnhance.Brightness(img).enhance(b_factor)

    # 3. Contrast ±12 % (was ±2 %)
    c_factor = 1.0 + random.uniform(-0.12, 0.12)
    img = ImageEnhance.Contrast(img).enhance(c_factor)

    # 3b. Invisible corner stamp — two-digit number drawn in a colour sampled
    #     from the corner pixel ± a small random offset so it is imperceptible
    #     to a human reviewer but alters the raw pixel values and the DCT block
    #     in that corner region.
    try:
        _cw, _ch = img.size
        _corner_x = random.choice([2, _cw - 14])
        _corner_y = random.choice([2, _ch - 14])
        _sample_rgb = img.getpixel((_corner_x, _corner_y))
        _overlay_rgb = tuple(
            max(0, min(255, _sample_rgb[c] + random.randint(-18, 18)))
            for c in range(3)
        )
        _draw = ImageDraw.Draw(img)
        _draw.text((_corner_x, _corner_y), f"{random.randint(10, 99)}", fill=_overlay_rgb)
        del _draw
    except Exception:
        pass  # never block the upload path

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
        "[ POST ]  image uniquified  |  crop=(%d,%d,%d,%d)  flip=%s  "
        "rot=%.1f°  b=%.3f  c=%.3f  q=%s  → %s",
        left, top, right, bottom, _flip, angle, b_factor, c_factor,
        quality if quality else "lossless",
        os.path.basename(out_path),
    )
    return out_path


def _cleanup_post_scratch(profile_id: str, max_age_sec: float = 3600.0) -> None:
    """Remove stale uniquified image files from the per-profile scratch dir.

    Called after every successful post so the temp directory doesn't grow
    indefinitely.  Removes files older than *max_age_sec* (default 1 hour)
    and deletes entirely empty profile subdirectories.

    Any OS errors (permission, concurrent access) are logged and swallowed
    so cleanup never blocks the main session flow.
    """
    try:
        if not os.path.isdir(_POST_TEMP_DIR):
            return
        now = time.time()
        safe_pid = (profile_id or "anon")[:16].replace("-", "")
        profile_dir = os.path.join(_POST_TEMP_DIR, safe_pid)

        # Phase 1: purge stale files in THIS profile's scratch dir
        if os.path.isdir(profile_dir):
            for fname in os.listdir(profile_dir):
                fpath = os.path.join(profile_dir, fname)
                try:
                    if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > max_age_sec:
                        os.remove(fpath)
                except OSError:
                    pass
            # Remove empty dir
            try:
                if not os.listdir(profile_dir):
                    os.rmdir(profile_dir)
            except OSError:
                pass

        # Phase 2: opportunistically purge OTHER profiles' stale dirs
        # (handles profiles that crashed without cleanup)
        for entry in os.listdir(_POST_TEMP_DIR):
            subdir = os.path.join(_POST_TEMP_DIR, entry)
            if not os.path.isdir(subdir):
                continue
            try:
                for fname in os.listdir(subdir):
                    fpath = os.path.join(subdir, fname)
                    if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > max_age_sec * 4:
                        os.remove(fpath)
                if not os.listdir(subdir):
                    os.rmdir(subdir)
            except OSError:
                pass

        # Phase 3: remove top-level dir if completely empty
        try:
            if not os.listdir(_POST_TEMP_DIR):
                os.rmdir(_POST_TEMP_DIR)
        except OSError:
            pass

        log.debug("[ POST ]  scratch cleanup complete for %s", profile_id)
    except Exception as exc:
        log.debug("[ POST ]  scratch cleanup error: %s", exc)


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


# ================================================================== #
#  BEHAVIORAL TEXTBOX DETECTION
# ================================================================== #
# Identifies the compose text input by behavioral characteristics rather
# than framework-specific attributes (data-lexical-editor) which Meta
# renames whenever the Lexical editor framework is refactored.
#
# Detection strategy (priority order):
#   1. role="textbox" + contenteditable="true" in a modal/overlay context
#      (ARIA role is legally mandated for accessibility — stable).
#   2. Sole visible contenteditable="true" element above the fold
#      (compose modal is always the topmost layer).
#   3. document.activeElement after programmatic focus into the compose
#      area — completely framework-agnostic.
#   4. Legacy data-lexical-editor attribute (lowest priority fallback).
# ================================================================== #

def _find_compose_textbox(driver, timeout: float = 10.0):
    """Find the compose modal's text input using behavioral detection.

    Returns the WebElement or None if no suitable textbox is found.
    """
    end = time.time() + timeout
    while time.time() < end:
        # Strategy 1: role="textbox" + contenteditable in modal context
        try:
            candidates = driver.find_elements(
                By.CSS_SELECTOR,
                '[contenteditable="true"][role="textbox"]',
            )
            for el in candidates:
                if not el.is_displayed():
                    continue
                # Verify it's inside a modal/overlay (high z-index or dialog role)
                is_compose = driver.execute_script("""
                    var el = arguments[0];
                    var r  = el.getBoundingClientRect();
                    if (r.height === 0) return false;
                    var node = el;
                    for (var d = 0; d < 15; d++) {
                        if (!node) break;
                        var z = parseInt(window.getComputedStyle(node).zIndex);
                        if (z > 100) return true;
                        var role = node.getAttribute('role');
                        if (role === 'dialog' || role === 'presentation') return true;
                        node = node.parentElement;
                    }
                    return false;
                """, el)
                if is_compose:
                    log.debug("[TEXTBOX]  found via role=textbox + contenteditable (modal context)")
                    return el
        except Exception:
            pass

        # Strategy 2: sole visible contenteditable element
        try:
            editables = driver.find_elements(
                By.CSS_SELECTOR, '[contenteditable="true"]'
            )
            visible_editables = []
            for el in editables:
                try:
                    if not el.is_displayed():
                        continue
                    r = driver.execute_script(
                        "var r=arguments[0].getBoundingClientRect();"
                        "return {h:r.height, top:r.top};", el)
                    vh = driver.execute_script("return window.innerHeight")
                    if r["h"] > 0 and r["top"] < vh:
                        visible_editables.append(el)
                except Exception:
                    continue
            if len(visible_editables) == 1:
                log.debug("[TEXTBOX]  found via unique visible contenteditable")
                return visible_editables[0]
        except Exception:
            pass

        # Strategy 3: document.activeElement after focus attempt
        try:
            active = driver.execute_script("""
                var editables = document.querySelectorAll('[contenteditable="true"]');
                for (var i = 0; i < editables.length; i++) {
                    var el = editables[i];
                    if (el.offsetParent === null) continue;
                    var r = el.getBoundingClientRect();
                    if (r.height === 0) continue;
                    el.focus();
                    if (document.activeElement === el) return el;
                }
                return null;
            """)
            if active and active.is_displayed():
                # Verify it's in a compose context (not a comment box)
                is_modal = driver.execute_script("""
                    var node = arguments[0];
                    for (var d = 0; d < 15; d++) {
                        if (!node) break;
                        var z = parseInt(window.getComputedStyle(node).zIndex);
                        if (z > 100) return true;
                        var role = node.getAttribute('role');
                        if (role === 'dialog' || role === 'presentation') return true;
                        node = node.parentElement;
                    }
                    return false;
                """, active)
                if is_modal:
                    log.debug("[TEXTBOX]  found via activeElement focus probe")
                    return active
        except Exception:
            pass

        # Strategy 4: legacy data-lexical-editor fallback
        try:
            legacy = driver.find_elements(
                By.CSS_SELECTOR,
                'div[data-lexical-editor="true"][contenteditable="true"]',
            )
            for el in legacy:
                if el.is_displayed():
                    log.debug("[TEXTBOX]  found via legacy data-lexical-editor fallback")
                    return el
        except Exception:
            pass

        precise_sleep(0.5)

    log.debug("[TEXTBOX]  no compose textbox found within %.0fs", timeout)
    return None


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
    if not POST_CAPTION_POOL:
        log.warning("create_post: POST_CAPTION_POOL is empty — cannot post")
        return False

    # === Fix #11: locked pre-post transaction — gate check + image reservation =
    # The lock is held only for the short read-modify-write cycle on state.
    # Heavy browser automation below runs entirely outside the lock so parallel
    # profiles are not blocked for the duration of the UI interaction.
    image_path = None
    _src_for_uniquify = None
    with _post_state_locked():
        state = _load_post_state()
        if not _can_post_now(profile_id, state):
            return False

        # Pick media — cross-profile deduplication: a single locked read-write
        # ensures two parallel profiles cannot reserve the same source file.
        if MEDIA_POOL_DIR and not os.path.isdir(MEDIA_POOL_DIR):
            log.warning(
                "[ POST ]  MEDIA_POOL_DIR not found — posting text-only  "
                "(configured path: %s)", MEDIA_POOL_DIR
            )
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
                    log.info("[ POST ]  image pool fully cycled — resetting cross-profile dedup list")
                    state["_used_images"] = []
                    fresh = all_images
                _src_for_uniquify = random.choice(fresh)
                used_list = state.setdefault("_used_images", [])
                basename = os.path.basename(_src_for_uniquify)
                if basename not in used_list:
                    used_list.append(basename)
        _save_post_state(state)  # one atomic write covers gate + image reservation
    # ===========================================================================

    if _src_for_uniquify:
        try:
            image_path = _prepare_image_for_profile(_src_for_uniquify, profile_id)
        except Exception as exc:
            log.warning("[ POST ]  image uniquification failed (%s) — using original", exc)
            image_path = _src_for_uniquify

    # ── DEBUG LOGGING: POST FLOW timer ────────────────────────────────────────
    _pf_t0 = time.perf_counter()
    # ──────────────────────────────────────────────────────────────────────────

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
            # Diagnostic: dump visible nav buttons so selectors can be updated
            try:
                btn_info = driver.execute_script("""
                    return Array.from(document.querySelectorAll(
                        '[role="button"],[role="link"]'
                    )).filter(el => {
                        var r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 &&
                               r.top >= 0 && r.top < window.innerHeight;
                    }).slice(0, 30).map(el => ({
                        tag:   el.tagName,
                        role:  el.getAttribute('role'),
                        label: el.getAttribute('aria-label') || '',
                        svgLabels: Array.from(el.querySelectorAll('svg[aria-label]'))
                                       .map(s => s.getAttribute('aria-label')),
                        text:  (el.innerText || '').slice(0, 40).replace(/\\n/g,' '),
                    }));
                """)
                log.warning(
                    "create_post: compose button not found — visible role=button/link elements: %s",
                    btn_info,
                )
            except Exception as _diag_exc:
                log.debug("create_post: compose button not found (diag failed: %s)", _diag_exc)
            return False

        scroll_element_into_loose_view(driver, compose_btn)
        bezier_move(driver, compose_btn)
        precise_sleep(random.uniform(0.4, 0.9))
        try:
            _cdp_click(driver)
        except WebDriverException:
            driver.execute_script("arguments[0].click();", compose_btn)

        # 3. Wait for the compose modal's contenteditable text area.
        #    Uses behavioral detection (role=textbox, contenteditable, modal
        #    context) rather than framework-specific data-lexical-editor.
        text_box = _find_compose_textbox(driver, timeout=10.0)
        if not text_box:
            log.debug("create_post: compose modal textarea did not appear")
            driver.execute_script(
                "document.dispatchEvent(new KeyboardEvent('keydown',"
                "{key:'Escape',keyCode:27,bubbles:true}));"
            )
            return False

        # Brief settle — SPA modal animation
        precise_sleep(random.uniform(0.6, 1.2))
        log.info("[POST FLOW]  step=compose_open  success=True  duration=%.0fms  detail=textbox_visible",
                 (time.perf_counter() - _pf_t0) * 1000)

        # 4. Attach image.
        #
        # Primary: pyautogui OS file dialog (visually natural — opens the real
        # system file picker).  Fallback: hidden <input type="file"> send_keys
        # (no dialog, used when pyautogui/pyperclip are not installed or the
        # attach button is not visible in the DOM).
        if image_path:
            try:
                # Short settle after the modal animation
                precise_sleep(random.uniform(0.3, 0.6))

                _media_attached = False

                # Primary: click the attach button → OS file dialog → pyautogui paste.
                try:
                    import pyautogui as _pag
                    import pyperclip as _ppc

                    attach_btns = [
                        el for el in driver.find_elements(
                            By.CSS_SELECTOR, COMPOSE_ATTACH_BTN_CSS
                        )
                        if el.is_displayed()
                    ]
                    if attach_btns:
                        bezier_move(driver, attach_btns[0])
                        precise_sleep(random.uniform(0.3, 0.6))
                        try:
                            _cdp_click(driver)
                        except WebDriverException:
                            driver.execute_script("arguments[0].click();", attach_btns[0])

                        _locate_delay = max(3.0, min(9.0, random.gauss(5.0, 1.5)))
                        log.debug("create_post: OS dialog open — file-locate pause %.1fs", _locate_delay)
                        precise_sleep(_locate_delay)

                        try:
                            _FindWindow  = ctypes.windll.user32.FindWindowW
                            _SetFG       = ctypes.windll.user32.SetForegroundWindow
                            _BringToTop  = ctypes.windll.user32.BringWindowToTop
                            _ShowWindow  = ctypes.windll.user32.ShowWindow
                            _dialog_hwnd = _FindWindow("#32770", None)
                            if _dialog_hwnd:
                                _ShowWindow(_dialog_hwnd, 5)
                                _BringToTop(_dialog_hwnd)
                                _SetFG(_dialog_hwnd)
                                precise_sleep(0.3)
                        except Exception as _fg_exc:
                            log.debug("create_post: SetForegroundWindow failed: %s", _fg_exc)

                        _ppc.copy(os.path.abspath(image_path))
                        precise_sleep(random.uniform(0.10, 0.25))
                        _pag.hotkey("ctrl", "a")
                        precise_sleep(random.uniform(0.06, 0.14))
                        _pag.hotkey("ctrl", "v")
                        precise_sleep(random.uniform(0.15, 0.35))
                        _pag.press("enter")
                        _ppc.copy("")  # clear clipboard immediately after use
                        log.info("[ POST ]  media attached via OS dialog: %s",
                                 os.path.basename(image_path))
                        log.info("[POST FLOW]  step=media_attach  success=True  method=os_dialog  detail=%s",
                                 os.path.basename(image_path))
                        precise_sleep(random.uniform(1.5, 2.5))
                        _media_attached = True
                    else:
                        log.debug("create_post: attach button not visible — falling back to hidden-input")

                except ImportError:
                    log.debug("create_post: pyautogui/pyperclip not installed — falling back to hidden-input")

                if not _media_attached:
                    # Fallback: inject path directly into the hidden file input.
                    # Works without opening any dialog; reliable for unattended runs.
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
                        log.info("[ POST ]  media attached (hidden-input fallback): %s",
                                 os.path.basename(image_path))
                        log.info("[POST FLOW]  step=media_attach  success=True  method=hidden_input  detail=%s",
                                 os.path.basename(image_path))
                        _media_attached = True
                    else:
                        log.warning("create_post: no attach button or file input found — skipping media")
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
            text_box = _find_compose_textbox(driver, timeout=8.0)
            if not text_box:
                log.debug("create_post: could not re-find textbox after media attach — aborting")
                driver.execute_script(
                    "document.dispatchEvent(new KeyboardEvent('keydown',"
                    "{key:'Escape',keyCode:27,bubbles:true}));"
                )
                return False
            log.debug("create_post: textbox re-queried after media attach")

        bezier_move(driver, text_box)
        precise_sleep(random.uniform(0.3, 0.7))
        human_type(text_box, caption, driver)
        log.info("[POST FLOW]  step=caption_type  success=True  detail=%r",
                 caption[:40])

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
        log.info("[POST FLOW]  step=submit_click  success=True  detail=post_btn_clicked")
        debug_cursor_state(driver, "post-submit-click")

        # 8. Wait for modal to close (compose textbox disappears on success)
        #    Uses web-standards selectors for textbox detection.
        try:
            WebDriverWait(driver, 12).until(
                lambda d: not [
                    el for el in d.find_elements(
                        By.CSS_SELECTOR,
                        '[contenteditable="true"][role="textbox"]'
                    ) if el.is_displayed()
                ]
            )
        except TimeoutException:
            pass
        precise_sleep(random.uniform(1.5, 3.0))
        _modal_still_open = bool([
            el for el in driver.find_elements(
                By.CSS_SELECTOR,
                '[contenteditable="true"][role="textbox"]'
            ) if el.is_displayed()
        ])
        log.info("[POST FLOW]  step=modal_close  success=%s  duration=%.0fms  detail=compose_textbox_gone",
                 not _modal_still_open,
                 (time.perf_counter() - _pf_t0) * 1000)

        # Fix #11: reload state inside a fresh locked transaction so any
        # concurrent profile's daily-count writes are not overwritten.
        with _post_state_locked():
            _rp_state = _load_post_state()
            _record_post(profile_id, _rp_state)
        _cleanup_post_scratch(profile_id)
        log.info("[ POST ]  new post published successfully")
        return True

    except (NoSuchElementException, TimeoutException, WebDriverException) as exc:
        log.warning("create_post failed: %s", exc)
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
    # ── DEBUG LOGGING: ACTION START ────────────────────────────────────────────
    _action_t0 = time.perf_counter()
    _get_ctx().session_metrics["actions_dispatched"] += 1
    log.info("[ACTION START]  action=post")
    # ────────────────────────────────────────────────────────────────────
    result = create_post(driver, profile_id)
    # ── DEBUG LOGGING: ACTION END ────────────────────────────────────────────
    if result:
        _get_ctx().session_metrics["posts"] += 1
    log.info("[ACTION END]  action=post  result=%s  duration=%.1fs",
             "success" if result else "failure", time.perf_counter() - _action_t0)
    # ────────────────────────────────────────────────────────────────────


# ================================================================== #
#  MARKOV CHAIN ACTION DISPATCH ENGINE
# ================================================================== #
#
# Replaces the flat i.i.d. random dispatch with a first-order Markov
# chain where P(next_action | current_action) encodes real behavioral
# autocorrelation patterns:
#
#   • After reading → scroll (30%) or like (25%), rarely search (5%)
#   • After liking  → scroll (45%), read (20%), rarely like again (8%)
#   • After comment → forced passive pause (55%), scroll down (12%)
#   • After notify  → profile visit (15%) or scroll (40%)
#   • After posting → passive scroll (60%), never immediate re-post
#
# Context modifiers layer on top of the base transition matrix:
#   • Session phase: early=passive, mid=active peak, late=wind-down
#   • Cumulative fatigue: engagement probability decays per action count
#   • Consecutive suppression: geometric penalty on same-action repeats
#   • Account maturity: young accounts heavily favour passive actions
#
# The transition matrix can evolve per-profile over time — a new account's
# matrix heavily favours passive, while a mature account allows full range.
# ================================================================== #

_MARKOV_STATES = [
    "passive", "active", "notify", "profile_view",
    "read_post", "comment", "follow", "return_top",
    "search", "post",
]

# Base transition matrix: P(next | current).
# Columns: passive  active  notify  profile  read  comment  follow  top  search  post
# Each row sums to ~1.0.
_BASE_TRANSITION_MATRIX = {
    "passive":      [0.35, 0.22, 0.03, 0.08, 0.14, 0.04, 0.03, 0.04, 0.05, 0.02],
    "active":       [0.45, 0.08, 0.03, 0.06, 0.20, 0.05, 0.03, 0.04, 0.04, 0.02],
    "notify":       [0.40, 0.10, 0.02, 0.15, 0.15, 0.03, 0.04, 0.03, 0.06, 0.02],
    "profile_view": [0.45, 0.15, 0.03, 0.04, 0.15, 0.04, 0.03, 0.04, 0.05, 0.02],
    "read_post":    [0.30, 0.25, 0.02, 0.06, 0.10, 0.12, 0.04, 0.04, 0.05, 0.02],
    "comment":      [0.55, 0.08, 0.04, 0.05, 0.12, 0.02, 0.03, 0.04, 0.05, 0.02],
    "follow":       [0.45, 0.12, 0.04, 0.08, 0.15, 0.03, 0.02, 0.04, 0.05, 0.02],
    "return_top":   [0.40, 0.18, 0.04, 0.06, 0.15, 0.04, 0.03, 0.02, 0.06, 0.02],
    "search":       [0.45, 0.12, 0.03, 0.08, 0.15, 0.04, 0.03, 0.03, 0.05, 0.02],
    "post":         [0.60, 0.05, 0.04, 0.08, 0.10, 0.02, 0.02, 0.04, 0.04, 0.01],
}


def _apply_session_phase_modifier(probs: list, elapsed_frac: float) -> list:
    """Shift transition probabilities based on session phase.

    elapsed_frac  0.0 = session start, 1.0 = session end.
      Early  (0-25%):  boost passive, suppress active engagement.
      Mid    (25-75%): slight boost to active actions (peak engagement).
      Late   (75-100%): wind down — boost passive, suppress active.
    """
    modified = probs[:]
    n = len(modified)
    if elapsed_frac < 0.25:
        boost = 0.15 * (1.0 - elapsed_frac / 0.25)
        modified[0] += boost
        active_sum = sum(modified[1:]) or 1.0
        for i in range(1, n):
            modified[i] *= max(0.0, 1.0 - boost / active_sum)
    elif elapsed_frac > 0.75:
        wind = (elapsed_frac - 0.75) / 0.25
        boost = 0.20 * wind
        modified[0] += boost
        modified[9] *= 0.1        # almost never post near session end
        active_sum = sum(modified[1:]) or 1.0
        for i in range(1, n):
            modified[i] *= max(0.0, 1.0 - boost / active_sum)
    else:
        mid_boost = 0.05
        modified[0] -= mid_boost
        modified[1] += mid_boost * 0.4    # active (like)
        modified[4] += mid_boost * 0.3    # read_post
        modified[5] += mid_boost * 0.2    # comment
        modified[3] += mid_boost * 0.1    # profile_view
    return modified


def _apply_fatigue_modifier(probs: list, metrics: dict) -> list:
    """Diminish engagement actions that have been performed many times.

    Uses exponential decay: P *= exp(-count / decay_constant).
    """
    modified = probs[:]
    fatigue_map = {
        1: ("likes",    8.0),          # active
        5: ("comments", 4.0),          # comment
        6: ("follows",  5.0),          # follow
        9: ("posts",    2.0),          # post
        3: ("profile_visits", 6.0),    # profile_view
        8: ("searches", 5.0),          # search
    }
    for idx, (key, decay) in fatigue_map.items():
        cnt = metrics.get(key, 0)
        if cnt > 0:
            modified[idx] *= math.exp(-cnt / decay)
    return modified


def _apply_consecutive_suppression(probs: list, current_state: str,
                                    consecutive_count: int) -> list:
    """Geometric penalty on repeating the same action: P *= 0.4^count."""
    if consecutive_count <= 0:
        return probs
    try:
        idx = _MARKOV_STATES.index(current_state)
    except ValueError:
        return probs
    modified = probs[:]
    modified[idx] *= 0.4 ** consecutive_count
    return modified


def _normalize_probs(probs: list) -> list:
    """Normalize probabilities to sum to 1.0."""
    total = sum(probs)
    if total <= 0:
        return [1.0 / len(probs)] * len(probs)
    return [p / total for p in probs]


def _markov_sample_next_action(
    current_state: str,
    session_elapsed_frac: float,
    metrics: dict,
    consecutive_same: int,
    account_days_old: int = 15,
) -> str:
    """Sample the next action from the Markov chain with context modifiers.

    Parameters
    ----------
    current_state         : what the bot just did
    session_elapsed_frac  : 0.0 → 1.0 how far through the session
    metrics               : _session_metrics accumulator
    consecutive_same      : how many times current_state repeated in a row
    account_days_old      : for account-maturity adjustment
    """
    base = list(_BASE_TRANSITION_MATRIX.get(
        current_state, _BASE_TRANSITION_MATRIX["passive"]
    ))

    # Account maturity: young accounts heavily favour passive
    if account_days_old < 7:
        base[0] += 0.30
        for i in range(1, len(base)):
            base[i] *= 0.30
    elif account_days_old < 14:
        for i in [5, 6, 9]:           # comment, follow, post
            base[i] *= 0.70

    # Layer modifiers
    probs = _apply_session_phase_modifier(base, session_elapsed_frac)
    probs = _apply_fatigue_modifier(probs, metrics)
    probs = _apply_consecutive_suppression(probs, current_state, consecutive_same)
    probs = _normalize_probs(probs)

    # Sample
    r = random.random()
    cumulative = 0.0
    for i, p in enumerate(probs):
        cumulative += p
        if r < cumulative:
            return _MARKOV_STATES[i]
    return _MARKOV_STATES[0]


def _sample_session_duration_sec() -> float:
    """Sample session length from a smooth log-normal distribution.

    Eliminates the old bimodal uniform draw (6-32 / 40-70 min gap) that
    created a fingerprint-level tell — real social-media sessions follow
    a right-skewed continuous curve.
    """
    minutes = random.lognormvariate(SESSION_LOGNORMAL_MU, SESSION_LOGNORMAL_SIGMA)
    minutes = max(SESSION_CLAMP_MIN, min(minutes, SESSION_CLAMP_MAX))
    return minutes * 60.0


def _distraction_pause(driver) -> None:
    """Simulate a brief multitasking distraction mid-session.

    Real users don't maintain unbroken focus for an entire session — they
    check another tab, glance at their phone, reply to a message, etc.
    This produces a visible pause (no scroll, no click) of 8–45 s that
    breaks the otherwise metronomic action cadence.

    ~12 % of session ticks trigger a distraction (called from the main loop).
    """
    pause_sec = random.uniform(8.0, 45.0)
    log.info("[ DISTRACTION ]  pausing %.0fs (simulated tab-switch / phone check)", pause_sec)

    # Occasionally move the cursor to a neutral spot first — user's hand
    # drifts as attention shifts away from the feed.
    if random.random() < 0.4:
        try:
            vw = driver.execute_script("return window.innerWidth")
            vh = driver.execute_script("return window.innerHeight")
            drift_x = random.randint(int(vw * 0.05), int(vw * 0.95))
            drift_y = random.randint(int(vh * 0.30), int(vh * 0.80))
            bezier_move_to_coords(driver, drift_x, drift_y, tag="distraction-drift")
        except Exception:
            pass

    precise_sleep(pause_sec)


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
    Session loop driven by a first-order Markov chain.

    Each action is sampled from a transition matrix conditioned on the
    previous action, with layered context modifiers:
      - Session phase (early=passive, mid=active, late=wind-down)
      - Cumulative fatigue (engagement probability decays with count)
      - Consecutive suppression (geometric penalty on same-action repeats)
      - Account maturity (young accounts favour passive actions)

    The w_* weight parameters are kept for CLI backward compatibility
    but no longer directly control dispatch.  They are used to scale
    the base transition matrix when explicitly overridden by the user.
    """
    # ── Reset all per-session mutable state in this thread's context ──────────
    # Replacing the SessionContext object atomically resets cursor_pos,
    # cdp_consecutive_failures, session_followed, session_metrics, and
    # active_typing_dna in one step — safe for concurrent profiles running in
    # separate threads because each has its own threading.local slot.
    _session_local.ctx = SessionContext()
    _session_local.ctx.active_typing_dna = _get_typing_dna(profile_id)
    # ─────────────────────────────────────────────────────────────────────────

    # ── Break cross-profile RNG correlation ───────────────────────────────
    # The global random module uses a single Mersenne Twister.  Without
    # reseeding, sequential profiles produce statistically correlated
    # random sequences (same PRNG state continues).  Reseed with 32
    # bytes from the OS CSPRNG so each session is independent.
    random.seed(os.urandom(32))

    # Draw a fresh passive-phase duration for this specific session.
    # Previously a module-level constant shared across all profiles.
    _session_passive_phase_sec = _draw_passive_phase_sec()
    log.debug("[ SESSION ]  passive phase drawn: %.1f min", _session_passive_phase_sec / 60)
    session_start_ts = time.time()
    deadline    = session_start_ts + session_seconds
    count       = 0
    active_done = False

    # Resolve account age for maturity modifier
    _account_days = 15   # default: mature
    try:
        _post_state = _load_post_state()
        _ensure_profile_in_state(profile_id, _post_state)
        if profile_id in _post_state:
            _first = _post_state[profile_id].get("first_seen", "")
            if _first:
                _account_days = (date.today() - date.fromisoformat(_first)).days
    except Exception:
        pass

    # Current Markov state — start with passive (user just opened the feed)
    current_state = "passive"

    # CLI weight override: if user explicitly passed weights, scale the
    # base transition probabilities so the Markov chain respects them.
    _user_weights = {}
    if w_like    is not None: _user_weights["active"]       = w_like
    if w_notify  != 0.03:    _user_weights["notify"]        = w_notify
    if w_profile != 0.06:    _user_weights["profile_view"]  = w_profile
    if w_read    != 0.08:    _user_weights["read_post"]     = w_read
    if w_comment != 0.05:    _user_weights["comment"]       = w_comment
    if w_follow  != 0.03:    _user_weights["follow"]        = w_follow
    if w_top     != 0.03:    _user_weights["return_top"]    = w_top
    if w_search  != 0.06:    _user_weights["search"]        = w_search
    if w_post    != 0.02:    _user_weights["post"]          = w_post

    # If user overrides are present, patch every row of the base matrix
    # so the Markov chain honours them while preserving transition structure.
    if _user_weights:
        for state_key in _BASE_TRANSITION_MATRIX:
            row = list(_BASE_TRANSITION_MATRIX[state_key])
            for action_name, desired_w in _user_weights.items():
                try:
                    idx = _MARKOV_STATES.index(action_name)
                    row[idx] = desired_w
                except ValueError:
                    pass
            total = sum(row)
            if total > 0:
                _BASE_TRANSITION_MATRIX[state_key] = [p / total for p in row]

    log.info(
        "Session Markov chain  |  account_age=%d days  |  user_overrides=%s",
        _account_days, _user_weights or "none",
    )

    # ── STATE SNAPSHOT: session start ────────────────────────────────────────
    try:
        _snap_url = driver.current_url
        _snap_vp  = driver.execute_script("return [window.innerWidth, window.innerHeight]")
    except Exception:
        _snap_url, _snap_vp = "unknown", [-1, -1]
    log.debug(
        "[STATE SNAPSHOT]  event=session_start  profile=%s  session_sec=%.0f"
        "  account_days=%d  cursor_pos=(%d,%d)  viewport=(%dx%d)  page_url=%s",
        profile_id, session_seconds, _account_days,
        _get_ctx().cursor_pos[0], _get_ctx().cursor_pos[1], _snap_vp[0], _snap_vp[1], _snap_url[:80],
    )
    # ────────────────────────────────────────────────────────────────────

    # Action dispatch map — maps Markov state names to callables.
    def _dispatch(action: str) -> None:
        nonlocal active_done
        if action == "passive":
            passive_action(driver)
        elif action == "active":
            active_action(driver)
            active_done = True
        elif action == "notify":
            check_notifications_action(driver)
        elif action == "profile_view":
            view_profile_from_feed(driver)
            _get_ctx().session_metrics["profile_visits"] += 1
        elif action == "read_post":
            read_post_action(driver)
        elif action == "comment":
            comment_on_post(driver)
        elif action == "follow":
            follow_from_feed(driver)
        elif action == "return_top":
            return_to_top_action(driver)
        elif action == "search":
            visit_search_action(driver)
            _get_ctx().session_metrics["searches"] += 1
        elif action == "post":
            passive_elapsed = time.time() - session_start_ts
            if passive_elapsed >= _session_passive_phase_sec:
                post_action(driver, profile_id)
            else:
                wait_min = (_session_passive_phase_sec - passive_elapsed) / 60
                log.info(
                    "[ POST ]  passive phase not complete (%.1f min remaining) "
                    "-- deferring post to scroll", wait_min,
                )
                passive_action(driver)
        else:
            passive_action(driver)

    while time.time() < deadline:
        time_left = deadline - time.time()
        elapsed_frac = min(1.0, (time.time() - session_start_ts) / max(1, session_seconds))

        # Force active if we haven't done one yet and time is almost up
        if not active_done and time_left < 60:
            log.info("Forcing active action (session guarantee).")
            selected_action = "active"
            _dispatch(selected_action)
            active_done = True
        else:
            # Sample from the Markov chain
            selected_action = _markov_sample_next_action(
                current_state=current_state,
                session_elapsed_frac=elapsed_frac,
                metrics=_get_ctx().session_metrics,
                consecutive_same=_get_ctx().session_metrics["consecutive_same"],
                account_days_old=_account_days,
            )
            _dispatch(selected_action)
            if selected_action == "active":
                active_done = True

        # Update Markov state
        current_state = selected_action

        # ── DEBUG LOGGING: [SESSION TICK] + consecutive-action tracking ───────
        _sess_elapsed = time.time() - session_start_ts
        if _get_ctx().session_metrics["last_action"] == selected_action:
            _get_ctx().session_metrics["consecutive_same"] += 1
        else:
            _get_ctx().session_metrics["consecutive_same"] = 0
        _get_ctx().session_metrics["last_action"] = selected_action
        log.info(
            "[SESSION TICK]  iteration=%d  markov_state=%s  selected=%s"
            "  session_elapsed=%.0fs  elapsed_frac=%.2f"
            "  active_done=%s  actions=%d  consecutive_same=%d"
            "  account_days=%d",
            count + 1, current_state, selected_action,
            _sess_elapsed, elapsed_frac,
            active_done, _get_ctx().session_metrics["actions_dispatched"],
            _get_ctx().session_metrics["consecutive_same"], _account_days,
        )
        if _get_ctx().session_metrics["consecutive_same"] >= 2:
            _dlog.warning(
                "[RISK WARN]  consecutive_same=%d  action=%s -- "
                "Markov suppression should reduce this",
                _get_ctx().session_metrics["consecutive_same"] + 1, selected_action,
            )
        # ───────────────────────────────────────────────────────────────────

        count += 1

        # ── Distraction / multitasking injection ─────────────────────────
        # ~12 % of ticks: pause as if the user switched tabs or checked
        # their phone.  Skipped in the first 2 min (user is still engaged)
        # and the last 1 min (session is winding down).
        _elapsed_s = time.time() - session_start_ts
        if (_elapsed_s > 120
                and (deadline - time.time()) > 60
                and random.random() < 0.12):
            _distraction_pause(driver)
        else:
            # Fix #8: log-normal inter-action gap — median 1.5 s, σ=0.6 on the
            # log scale.  Produces a right-skewed distribution that matches
            # observed human reaction-time between browsing actions far better
            # than a flat uniform(1,3) which a classifier can trivially identify.
            precise_sleep(max(0.5, min(15.0, random.lognormvariate(math.log(1.5), 0.6))))

    # ── POST-SESSION DIAGNOSTICS ─────────────────────────────────────────────
    if _get_ctx().session_metrics["passive"] == 0:
        _dlog.warning(
            "[RISK WARN]  session ended with 0 passive actions "
            "-- pure engagement bot pattern"
        )
    try:
        _end_url = driver.current_url
    except Exception:
        _end_url = "unknown"
    log.info(
        "[STATE SNAPSHOT]  event=session_end  profile=%s  session_sec=%.0f"
        "  account_days=%d  cursor_pos=(%d,%d)"
        "  session_followed=%d  actions_dispatched=%d"
        "  likes=%d  comments=%d  follows=%d  posts=%d  passive=%d  reads=%d"
        "  profile_visits=%d  searches=%d  page_url=%s",
        profile_id, session_seconds, _account_days,
        _get_ctx().cursor_pos[0], _get_ctx().cursor_pos[1],
        len(_get_ctx().session_followed), _get_ctx().session_metrics["actions_dispatched"],
        _get_ctx().session_metrics["likes"], _get_ctx().session_metrics["comments"],
        _get_ctx().session_metrics["follows"], _get_ctx().session_metrics["posts"],
        _get_ctx().session_metrics["passive"], _get_ctx().session_metrics["reads"],
        _get_ctx().session_metrics["profile_visits"], _get_ctx().session_metrics["searches"],
        _end_url[:80],
    )
    # ────────────────────────────────────────────────────────────────────

    log.info("Session complete. Total actions: %d", count)


# ================================================================== #
#  SINGLE PROFILE WARM-UP ORCHESTRATOR
# ================================================================== #

def warm_profile(profile_id: str, weights: dict | None = None) -> bool:
    """Full end-to-end warm-up for one NstBrowser profile.

    Returns True if a Threads session was actually started, False if the
    profile failed or was skipped before reaching Threads.
    """
    driver      = None
    launched    = False
    ran_session = False

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
            return False

        # 5. Main activity session — smooth log-normal duration
        ran_session = True
        session_sec = _sample_session_duration_sec()
        log.info("Session: %.1f min  |  profile: %s", session_sec / 60, profile_id)
        run_social_session(driver, session_sec, profile_id=profile_id, **(weights or {}))

    except (TimeoutException, RuntimeError, WebDriverException, CDPConnectionDead) as exc:
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

    return ran_session


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

        session_sec = _sample_session_duration_sec()
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

    p.add_argument(
        "--test-actions",
        nargs="?",
        const="__all__",
        default=None,
        metavar="ACTION",
        help=(
            "Run a single-pass diagnostic session.  Without a value, executes "
            "every action exactly once.  With an action name (e.g. "
            "--test-actions post, --test-actions like, --test-actions comment) "
            "runs only that single action.  Valid names: passive, like, "
            "notifications, profile, follow, read, comment, post, search, "
            "home, top.  Results are written to both console and test_actions.log."
        ),
    )

    return p


# ================================================================== #
#  TEST-ACTIONS DIAGNOSTIC RUNNER
# ================================================================== #

_TEST_LOG_FILE = "test_actions.log"


def _setup_test_logger() -> logging.Logger:
    """Create a dedicated logger that writes to both console and test_actions.log."""
    tlog = logging.getLogger("test_actions")
    tlog.setLevel(logging.DEBUG)
    tlog.propagate = False
    # Remove stale handlers from a previous invocation in the same process
    tlog.handlers.clear()
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")
    fh = logging.FileHandler(_TEST_LOG_FILE, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    if hasattr(sh.stream, "reconfigure"):
        try:
            sh.stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    tlog.addHandler(fh)
    tlog.addHandler(sh)
    return tlog


def _dom_health_check(driver, tlog: logging.Logger) -> bool:
    """Lightweight DOM health check between test actions.

    Verifies:
      1. Page is still on a valid Threads URL.
      2. Feed or current page is still rendering content.
      3. No login/challenge redirect has occurred.
    Returns True if healthy.
    """
    try:
        url = driver.current_url
    except Exception as exc:
        tlog.error("[HEALTH FAIL]  driver.current_url raised: %s", exc)
        return False

    on_threads = "threads.net" in url or "threads.com" in url
    if not on_threads:
        tlog.warning("[HEALTH FAIL]  URL is not on threads: %s", url[:120])
        return False

    # Login / challenge redirect detection
    if "/login" in url or "/challenge" in url or "/accounts/" in url:
        tlog.warning("[HEALTH FAIL]  login/challenge redirect detected: %s", url[:120])
        return False

    # Check for visible content
    try:
        articles = len(driver.find_elements(
            By.CSS_SELECTOR, "article, div[data-pressable-container='true']"
        ))
        body_len = driver.execute_script(
            "return (document.body && document.body.innerText) ? document.body.innerText.length : 0;"
        )
    except Exception as exc:
        tlog.warning("[HEALTH FAIL]  DOM query error: %s", exc)
        return False

    if articles == 0 and body_len < 200:
        tlog.warning(
            "[HEALTH FAIL]  page appears empty  articles=%d  body_text_len=%d  url=%s",
            articles, body_len, url[:120],
        )
        return False

    tlog.debug(
        "[HEALTH OK]  url=%s  articles=%d  body_text_len=%d",
        url[:80], articles, body_len,
    )
    return True


def _browser_is_alive(driver) -> bool:
    """Return True if the browser session is still responsive."""
    try:
        _ = driver.current_url
        return True
    except Exception:
        return False


def run_test_actions(driver, profile_id: str = "test",
                     filter_action: str | None = None) -> None:
    """Execute every action exactly once in isolation with full diagnostics.

    Designed for the ``--test-actions`` CLI flag.  Skips normal session loop,
    daily quotas, probability gates, and session time limits.

    Parameters
    ----------
    filter_action : str or None
        If provided, only the action whose name or short alias matches this
        string will be executed.  None (or ``"__all__"``) runs all actions.
    """
    import traceback as _tb

    tlog = _setup_test_logger()

    # Load per-profile typing DNA so human_type() uses realistic per-profile
    # keystroke dynamics (including the typo/correction model) even outside
    # the normal run_social_session() path.
    _get_ctx().active_typing_dna = _get_typing_dna(profile_id)

    # ── Define the ordered list of actions to test ────────────────────────────
    # Each entry: (action_name, callable_that_returns_something)
    # We create wrapper lambdas so every action has a uniform call signature.

    def _test_passive_scroll():
        # Cap scroll time to 30 s max in test mode (normal range is 25-75 s)
        scroll_time = random.uniform(10, 30)
        log.info("[ PASSIVE TEST ]  capped scroll %.0fs", scroll_time)
        _FEED_ROOTS = ("https://www.threads.com/", "https://www.threads.net/")
        try:
            current = driver.current_url
            on_feed = any(current.rstrip("/") + "/" == root or current == root
                          for root in _FEED_ROOTS)
            if not on_feed:
                if not click_home_button(driver):
                    navigate_to(driver, TARGET_SOCIAL_URL)
                precise_sleep(random.uniform(1.2, 2.5))
        except WebDriverException:
            pass
        stochastic_scroll(driver, total_seconds=scroll_time)
        precise_sleep(random.uniform(1.0, 3.0))
        return True

    def _test_active_like():
        active_action(driver)
        return True

    def _test_check_notifications():
        check_notifications_action(driver)
        return True

    def _test_view_profile():
        return view_profile_from_feed(driver)

    def _test_follow_from_feed():
        return follow_from_feed(driver)

    def _test_read_post():
        return read_post_action(driver)

    def _test_comment_on_post():
        return comment_on_post(driver)

    def _test_visit_search():
        visit_search_action(driver)
        return True

    def _test_click_home():
        return click_home_button(driver)

    def _test_return_to_top():
        return_to_top_action(driver)
        return True

    def _test_create_post():
        # Bypass _can_post_now and _record_post using unittest.mock so the
        # patch is automatically reverted — even on exception — and never
        # leaks into concurrent sessions sharing the same module namespace.
        def _noop_record(pid, state):
            tlog.info("[TEST]  _record_post SKIPPED — test post not recorded in %s", POST_STATE_FILE)
        _mod = sys.modules[__name__]
        with unittest.mock.patch.object(_mod, '_can_post_now', lambda pid, state: True):
            with unittest.mock.patch.object(_mod, '_record_post', _noop_record):
                return create_post(driver, profile_id)

    # Full action list with short aliases for --test-actions <name> filtering
    _all_actions = [
        ("passive_scroll",        _test_passive_scroll,       {"passive", "scroll"}),
        ("active_like",           _test_active_like,          {"like", "active"}),
        ("check_notifications",   _test_check_notifications,  {"notifications", "notify"}),
        ("view_profile_from_feed", _test_view_profile,        {"profile", "view_profile"}),
        ("follow_from_feed",      _test_follow_from_feed,     {"follow"}),
        ("read_post",             _test_read_post,            {"read", "read_post"}),
        ("comment_on_post",       _test_comment_on_post,      {"comment"}),
        ("visit_search",          _test_visit_search,         {"search"}),
        ("click_home",            _test_click_home,           {"home", "click_home"}),
        ("return_to_top",         _test_return_to_top,        {"top", "return_to_top"}),        
        ("create_post",           _test_create_post,          {"post", "create_post"}),
    ]

    # Filter to a single action if requested
    if filter_action and filter_action != "__all__":
        needle = filter_action.lower().strip()
        matched = [
            (name, fn, aliases) for name, fn, aliases in _all_actions
            if needle == name.lower() or needle in aliases
        ]
        if not matched:
            valid = ", ".join(
                sorted({a for _, _, aliases in _all_actions for a in aliases})
            )
            tlog.error(
                "Unknown action '%s'.  Valid names: %s", filter_action, valid,
            )
            return
        _all_actions = matched
        tlog.info("Filtering to single action: %s", matched[0][0])

    actions = [(name, fn) for name, fn, _ in _all_actions]
    total = len(actions)
    results = []  # list of dicts: {index, name, status, duration_ms, note}
    overall_t0 = time.perf_counter()
    cursor_drift_actions = []

    tlog.info("=" * 72)
    tlog.info("  TEST-ACTIONS DIAGNOSTIC SESSION")
    tlog.info("  Profile : %s", profile_id)
    tlog.info("  Actions : %d", total)
    tlog.info("  Started : %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    tlog.info("=" * 72)

    for idx, (name, fn) in enumerate(actions, start=1):
        # ── Check if browser is alive before each action ──────────────────
        if not _browser_is_alive(driver):
            tlog.error("BROWSER UNRESPONSIVE — aborting test run at action %d/%d (%s)", idx, total, name)
            # Record remaining actions as ERROR
            for j in range(idx, total + 1):
                remaining_name = actions[j - 1][0] if j <= total else "?"
                results.append({
                    "index": j, "name": remaining_name,
                    "status": "ERROR", "duration_ms": 0,
                    "note": "BROWSER UNRESPONSIVE — aborted",
                })
            break

        header = f"[TEST {idx}/{total}] {name}"
        tlog.info("")
        tlog.info("-" * 60)
        tlog.info("%s", header)
        tlog.info("-" * 60)

        t0 = time.perf_counter()
        status = "PASS"
        note = ""
        return_value = None

        try:
            return_value = fn()
            elapsed_ms = (time.perf_counter() - t0) * 1000

            if return_value is False:
                status = "FAIL"
                note = "returned False"
            else:
                note = f"returned {return_value!r}" if return_value is not True else ""

            tlog.info(
                "%s  status=%s  duration=%.0fms  return=%r",
                header, status, elapsed_ms, return_value,
            )

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            status = "ERROR"
            note = f"{type(exc).__name__}: {exc}"
            tlog.error(
                "%s  status=ERROR  duration=%.0fms  exception=%s: %s",
                header, elapsed_ms, type(exc).__name__, exc,
            )
            tlog.error("Full traceback:\n%s", _tb.format_exc())

        results.append({
            "index": idx,
            "name": name,
            "status": status,
            "duration_ms": round(elapsed_ms),
            "note": note[:120],
        })

        # ── Cursor drift check ────────────────────────────────────────────
        try:
            debug_cursor_state(driver, f"test-{name}")
        except Exception:
            pass
        # A simplistic drift detection: if _cursor_pos is at (0,0) after an
        # action that should have moved it, record a warning.
        if _get_ctx().cursor_pos[0] == 0 and _get_ctx().cursor_pos[1] == 0:
            cursor_drift_actions.append(name)
            tlog.warning("[CURSOR DRIFT]  cursor at (0,0) after action %s", name)

        # ── DOM health check ──────────────────────────────────────────────
        if _browser_is_alive(driver):
            healthy = _dom_health_check(driver, tlog)
            if not healthy:
                tlog.warning("[HEALTH FAIL]  after action %s — continuing to next action", name)
        else:
            tlog.error("[HEALTH FAIL]  browser unresponsive after action %s", name)

        # ── Human-like pause between actions (3–8 s) ─────────────────────
        if idx < total:
            pause = random.uniform(3.0, 8.0)
            tlog.debug("Inter-action pause: %.1fs", pause)
            precise_sleep(pause)

    overall_elapsed_ms = (time.perf_counter() - overall_t0) * 1000

    # ── Build the summary report ─────────────────────────────────────────────
    pass_count  = sum(1 for r in results if r["status"] == "PASS")
    fail_count  = sum(1 for r in results if r["status"] == "FAIL")
    error_count = sum(1 for r in results if r["status"] == "ERROR")

    # Column widths
    col_idx  = 5
    col_name = max(len(r["name"]) for r in results) if results else 20
    col_stat = 6
    col_dur  = 10
    col_note = 50
    row_w = col_idx + col_name + col_stat + col_dur + col_note + 16  # separators + padding

    border = "+" + "-" * (row_w - 2) + "+"
    hdr_fmt = "| {:<{idx}}  {:<{nm}}  {:<{st}}  {:>{dur}}  {:<{nt}} |"
    row_fmt = "| {:<{idx}}  {:<{nm}}  {:<{st}}  {:>{dur}}  {:<{nt}} |"

    lines = []
    lines.append("")
    lines.append(border)
    lines.append("| {:^{w}} |".format("TEST-ACTIONS SUMMARY REPORT", w=row_w - 4))
    lines.append(border)
    lines.append(hdr_fmt.format(
        "#", "ACTION", "STATUS", "DURATION", "NOTE",
        idx=col_idx, nm=col_name, st=col_stat, dur=col_dur, nt=col_note,
    ))
    lines.append(border)
    for r in results:
        dur_str = f"{r['duration_ms']}ms"
        lines.append(row_fmt.format(
            r["index"], r["name"], r["status"], dur_str,
            (r["note"][:col_note] if r["note"] else ""),
            idx=col_idx, nm=col_name, st=col_stat, dur=col_dur, nt=col_note,
        ))
    lines.append(border)
    lines.append(f"| PASS: {pass_count}   FAIL: {fail_count}   ERROR: {error_count}")
    lines.append(f"| Total elapsed: {overall_elapsed_ms:.0f}ms ({overall_elapsed_ms / 1000:.1f}s)")
    if cursor_drift_actions:
        lines.append(f"| Cursor drift warnings: {', '.join(cursor_drift_actions)}")
    lines.append(border)
    lines.append("")

    report = "\n".join(lines)
    tlog.info(report)
    # Also push to the main log so it persists in nstbrowser_warmer.log
    log.info(report)


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

        # ── TEST-ACTIONS diagnostic mode ─────────────────────────────────
        if args.test_actions:
            log.info("--test-actions mode: running single-pass diagnostic session")
            driver = None
            addr = address.replace("ws://", "").split("/")[0]
            ws_url = f"ws://{addr}"
            try:
                driver = connect_selenium(ws_url)
                driver.set_page_load_timeout(30)
                init_cursor_pos(driver)

                if not args.no_preflight:
                    run_preflight(driver)

                log.info("Navigating to %s", TARGET_SOCIAL_URL)
                navigate_to(driver, TARGET_SOCIAL_URL)
                precise_sleep(random.uniform(2, 5))

                if not check_login_status(driver):
                    log.error("Profile '%s' appears logged out -- cannot run test-actions.", label)
                    return

                _filter = args.test_actions if args.test_actions != "__all__" else None
                run_test_actions(driver, profile_id=label, filter_action=_filter)
            except (TimeoutException, RuntimeError, WebDriverException) as exc:
                log.error("test-actions error on '%s': %s", label, exc)
            finally:
                if driver and args.close:
                    try:
                        driver.quit()
                    except Exception:
                        pass
            log.info("=" * 60)
            log.info("Done (test-actions).")
            return
        # ─────────────────────────────────────────────────────────────────

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

    # --test-actions in normal mode: skip inactive-day / time-of-day guards,
    # open the first profile via the API, run the diagnostic, then close it.
    if args.test_actions:
        pid = PROFILE_IDS[0] if PROFILE_IDS else None
        if not pid:
            log.error("No profiles configured in PROFILE_IDS — cannot run test-actions.")
            return
        log.info("--test-actions (normal mode): testing profile %s", pid)
        driver = None
        launched = False
        try:
            info = start_profile(pid)
            launched = True
            driver = connect_selenium(info["webSocketDebuggerUrl"])
            driver.set_page_load_timeout(30)
            init_cursor_pos(driver)

            if not args.no_preflight:
                run_preflight(driver)

            log.info("Navigating to %s", TARGET_SOCIAL_URL)
            navigate_to(driver, TARGET_SOCIAL_URL)
            precise_sleep(random.uniform(2, 5))

            if not check_login_status(driver):
                log.error("Profile '%s' appears logged out -- cannot run test-actions.", pid)
                return

            _filter = args.test_actions if args.test_actions != "__all__" else None
            run_test_actions(driver, profile_id=pid, filter_action=_filter)
        except (TimeoutException, RuntimeError, WebDriverException) as exc:
            log.error("test-actions error on '%s': %s", pid, exc)
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            if launched:
                stop_profile(pid)
        log.info("=" * 60)
        log.info("Done (test-actions).")
        return

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
        ran_session = warm_profile(profile_id, weights=weights or None)

        if idx < len(profile_order) - 1:
            if ran_session:
                if random.random() < BUFFER_LONG_PROB:
                    buf = random.uniform(BUFFER_LONG_MIN * 60, BUFFER_LONG_MAX * 60)
                    log.info("Extended buffer: %.1f min before next profile...", buf / 60)
                else:
                    buf = random.uniform(BUFFER_MIN_MIN * 60, BUFFER_MAX_MIN * 60)
                    log.info("Buffer: %.1f min before next profile...", buf / 60)
                time.sleep(buf)
            else:
                log.info("Profile failed before reaching Threads — skipping inter-profile buffer.")

    log.info("=" * 60)
    log.info("All profiles warmed. Done.")


if __name__ == "__main__":
    main()
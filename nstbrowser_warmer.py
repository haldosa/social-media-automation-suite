"""
NstBrowser Social Media Account Warmer  (API v2)
=================================================
Target platform : threads.net
API reference   : https://apidocs.nstbrowser.io/

Requirements:
    pip install selenium requests webdriver-manager

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
import glob as _glob
import re
import textwrap
import argparse
import requests
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
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
PREFLIGHT_SITES     = ["https://www.wikipedia.org",]
PREFLIGHT_DWELL_MIN = 5      # minimum seconds on each pre-flight site (testing)
PREFLIGHT_DWELL_MAX = 5      # maximum seconds on each pre-flight site (increase for prod)
SESSION_MIN_MIN     = 2     # minimum session length (minutes)
SESSION_MAX_MIN     = 2     # maximum session length (minutes)
BUFFER_MIN_MIN      = 0     # minimum buffer between profiles (minutes)
BUFFER_MAX_MIN      = 0     # maximum buffer between profiles (minutes)
SCREENSHOT_DIR      = "screenshots"
LOG_FILE            = "nstbrowser_warmer.log"
MOUSE_LOG_FILE      = "mouse_moves.log"  # dedicated cursor movement log
MOUSE_TRACE         = True              # True = log every Bezier step (verbose)
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
                return hits[0]

    # 2. webdriver-manager
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        path = ChromeDriverManager(driver_version=str(major)).install()
        log.info("webdriver-manager chromedriver: %s", path)
        return path
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

def human_type(element, text: str) -> None:
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
    """
    element.click()
    time.sleep(random.uniform(0.08, 0.25))   # focus-settle after click
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
        time.sleep(base)
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
    var ID  = '__dbg_cursor_dot__';
    var LID = '__dbg_cursor_lbl__';
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

    document.body.appendChild(dot);
    document.body.appendChild(lbl);

    document.addEventListener('mousemove', function (e) {
        var x = e.clientX, y = e.clientY;
        dot.style.left = x + 'px';
        dot.style.top  = y + 'px';
        lbl.style.left = (x + 14) + 'px';
        lbl.style.top  = (y -  8) + 'px';
        lbl.textContent = x + ', ' + y;
    }, true);
})();
"""

def inject_cursor_overlay(driver) -> None:
    """Inject the visual cursor overlay into the current page.
    Cursor placement is now managed by navigate_to / navigate_history;
    this function only handles the visual debug dot."""
    if not DEBUG_CURSOR_OVERLAY:
        return
    try:
        driver.execute_script(_CURSOR_OVERLAY_JS)
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
    step_ms    = random.uniform(14.0, 18.0)
    points     = []
    delays     = []
    prev       = (x0, y0)
    dist_scale = max(0.15, min(_arc_dist / 500.0, 1.0))
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
            tremor_sd = (1.5 * (1.0 - velocity * 0.55) + approach * 1.5) * dist_scale
            nx = int(nx + random.gauss(0, tremor_sd))
            ny = int(ny + random.gauss(0, tremor_sd * 0.75))
            # Low-frequency drift: correlated wrist/arm oscillation.
            drift_x = drift_x * 0.88 + random.gauss(0, 0.55 * dist_scale)
            drift_y = drift_y * 0.88 + random.gauss(0, 0.40 * dist_scale)
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
        d_ms = step_ms * (1.5 - vel * 0.7) + random.gauss(0, 0.9)
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
    driver.execute_script(_BEZIER_DISPATCH_JS, points, delays)
    time.sleep(sum(d / 1000.0 for d in delays) + 0.05)
    return points, delays


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
    at a true 60 fps frame rate.

    Two-phase approach:
      Phase 1 — JS animation via _fire_bezier_arc(): all points are
        pre-computed in Python and dispatched to the browser in ONE
        execute_script call.  JS fires DOM mousemove events at the computed
        per-step interval; Python sleeps for the arc duration.  No per-step
        Python ↔ browser round-trips — genuine ~60 fps.

      Phase 2 — CDP hover: a single ActionChains.move_to_element_with_offset()
        fires the real browser hover events (CSS :hover, mouseenter, etc.).
        The aim-offset scatter (off_dx/off_dy) is applied only here so the
        DOM sees at most a few pixels' gap between the last JS event and the
        real pointer.

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
            ActionChains(driver).move_to_element_with_offset(
                target_element, off_dx, off_dy
            ).perform()
            _set_cursor(int(rect["x"]) + off_dx, int(rect["y"]) + off_dy, "hover-dwell")
            _mlog.debug("DWELL  cursor within 25px of target  dist=%.1fpx",
                        math.hypot(x1 - x0, y1 - y0))
            return
        # Off-viewport correction: scroll element into view then re-query.
        if x1 < 0 or y1 < 0 or x1 > int(vw) or y1 > int(vh):
            driver.execute_script(
                "arguments[0].scrollIntoView({behavior:'instant', block:'center'});",
                target_element,
            )
            time.sleep(random.uniform(0.3, 0.6))
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
        # Phase 2 — CDP hover fires real browser hover/mouseenter events.
        ActionChains(driver).move_to_element_with_offset(
            target_element, off_dx, off_dy
        ).perform()
        _set_cursor(snap_x, snap_y, "elem-hover")

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

    Phase 1: JS mousemove dispatch via _fire_bezier_arc() with exact_end=True
             so the arc lands exactly on the target coordinate.
    Phase 2: ActionBuilder.move_to_location() — absolute W3C pointer move
             that corrects any drift between _cursor_pos and the real pointer.
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
        # Phase 2 — absolute pointer correction so real W3C pointer tracks
        # with _cursor_pos instead of accumulating relative drift.
        try:
            ab = ActionBuilder(driver,
                               mouse=PointerInput(interaction.POINTER_MOUSE, "mouse"))
            ab.pointer_action.move_to_location(x1, y1)
            ab.perform()
        except WebDriverException:
            pass
        _set_cursor(x1, y1, tag)
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
    try:
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        pass

    # 3. Overlay
    inject_cursor_overlay(driver)

    # 4. Silent position set — fresh page has no cursor history.
    #    Cursor was at (park_x, 0) before navigation; it's still conceptually
    #    there.  No dispatch needed — the drift arc below is the first event
    #    the new page sees, which avoids a detectable in-place jump on load.
    _set_cursor(park_x, 0, "fresh-page")

    # 5. Settle
    time.sleep(random.uniform(0.6, 1.4))

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
        driver.execute_script("window.scrollBy(0, arguments[0]);", direction * move)
        scrolled += move
        # Tick duration varies inversely with velocity (slow ends, fast middle)
        delay = (tick_ms / 1000.0) * (0.5 + (1.0 - velocity) * 1.0)
        time.sleep(delay + random.uniform(-0.003, 0.003))

    # Flush any sub-step remainder
    remainder = total - scrolled
    if remainder > 0:
        driver.execute_script("window.scrollBy(0, arguments[0]);", direction * remainder)


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
            time.sleep(seconds)
            return
        end = time.time() + seconds
        while True:
            remaining = end - time.time()
            if remaining <= 0:
                break
            sit = min(random.uniform(1.0, 3.0), remaining)
            time.sleep(sit)
            if end - time.time() <= 0.15:
                break
            if random.random() < 0.5:
                try:
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
        time.sleep(random.uniform(0.15, 0.45))

        # Occasional hand-shift nudge between scroll chunks.
        # Fires after every _nudge_after chunks; threshold is re-randomised
        # each time so the interval is never periodic.
        if _chunk_count >= _nudge_after:
            _chunk_count = 0
            _nudge_after = random.randint(3, 5)
            try:
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
            time.sleep(dwell)

        if time.time() >= deadline:
            break


# ================================================================== #
#  PRE-FLIGHT  (Wikipedia only)
# ================================================================== #

def run_preflight(driver) -> None:
    """
    Browse pre-flight sites for a randomly drawn dwell time to seed a natural
    browsing history before navigating to Threads.
    """
    for site in PREFLIGHT_SITES:
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
    // Only select SVGs whose aria-label is exactly "Like" (un-liked state).
    // After liking, Threads flips it to "Unlike" — so this naturally excludes them.
    var svgs = document.querySelectorAll('svg[aria-label="Like"]');
    var seen = new Set();
    var hits = [];
    for (var i = 0; i < svgs.length; i++) {
        var svg = svgs[i];

        // Walk up to the nearest div[role="button"] ancestor — that is the
        // clickable wrapper, not the SVG itself.
        var btn = svg.closest('div[role="button"]');
        if (!btn) continue;

        // Deduplicate (multiple SVGs may share a wrapper)
        if (seen.has(btn)) continue;
        seen.add(btn);

        // Must be inside the visible viewport (with small tolerance)
        var r = btn.getBoundingClientRect();
        if (r.height <= 0 || r.top < -20 || r.top > vp + 20) continue;

        // Skip if the wrapper itself carries aria-label="Unlike"
        var wrapperLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
        if (wrapperLabel === 'unlike') continue;

        hits.push(btn);
    }
    return hits;
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
    Return all clickable like-button wrapper divs in the current viewport
    that have NOT been liked yet.

    Priority:
      1. JS query (fastest, handles stale refs, checks fill style)
      2. XPath fallback
      3. CSS :has() fallback
    """
    results = []

    # 1. JS pass — most reliable
    try:
        js_results = driver.execute_script(_JS_FIND_LIKE_BTNS)
        if js_results:
            results = [el for el in js_results if not _is_already_liked(el)]
            log.info("JS finder: %d unliked like button(s) in viewport", len(results))
    except Exception as e:
        log.debug("JS finder failed: %s", e)

    # 2. XPath fallback
    if not results:
        viewport_h = driver.execute_script("return window.innerHeight")
        for xp in _LIKE_XPATH_FALLBACK:
            try:
                for el in driver.find_elements(By.XPATH, xp):
                    try:
                        r = driver.execute_script(
                            "var r=arguments[0].getBoundingClientRect();"
                            "return {top:r.top,height:r.height};", el)
                        if r["height"] > 0 and -20 <= r["top"] <= viewport_h + 20:
                            if el.is_displayed() and not _is_already_liked(el):
                                results.append(el)
                    except Exception:
                        continue
            except (NoSuchElementException, WebDriverException):
                continue
            if results:
                log.info("XPath fallback: %d unliked like button(s)", len(results))
                break

    # 3. CSS :has() fallback
    if not results:
        viewport_h = driver.execute_script("return window.innerHeight")
        for sel in _LIKE_CSS_FALLBACK:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        r = driver.execute_script(
                            "var r=arguments[0].getBoundingClientRect();"
                            "return {top:r.top,height:r.height};", el)
                        if r["height"] > 0 and -20 <= r["top"] <= viewport_h + 20:
                            if el.is_displayed() and not _is_already_liked(el):
                                results.append(el)
                    except Exception:
                        continue
            except (NoSuchElementException, WebDriverException):
                continue
            if results:
                log.info("CSS fallback: %d unliked like button(s)", len(results))
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
        time.sleep(random.uniform(0.08, 0.25))

    # Imprecise final pause — not a fixed sleep
    time.sleep(random.uniform(0.3, 0.7))


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

        # Reading pause before liking — humans read before they react
        time.sleep(random.uniform(0.8, 2.5))

        bezier_move(driver, element)
        time.sleep(random.uniform(0.2, 0.6))   # hand settling on the button

        try:
            ActionChains(driver).click().perform()
        except WebDriverException:
            log.debug("Selenium click intercepted — JS click fallback")
            driver.execute_script("arguments[0].click();", element)

        # Watch the heart animation
        time.sleep(random.uniform(0.8, 2.0))
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

def view_profile_from_feed(driver, force_follow: bool = False) -> bool:
    """
    Click a random post-author username link in the feed to visit their
    profile, scroll it, optionally follow, then navigate back.

    force_follow=True guarantees follow_from_profile_page() is called
    (used by follow_mode).  Default is a 15 % probabilistic gate.
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
        time.sleep(random.uniform(0.5, 1.5))

        # ~25 % of visits: open the profile in a new tab (mirrors Ctrl+click /
        # middle-click behaviour a real user exhibits occasionally).
        use_new_tab = (random.random() < 0.25)
        original_handle = driver.current_window_handle

        if use_new_tab:
            log.info("[ NAV ]  opening profile in new tab")
            driver.execute_script("window.open(arguments[0], '_blank');", profile_url)
            time.sleep(random.uniform(0.4, 0.9))   # brief pause while tab opens
            driver.switch_to.window(driver.window_handles[-1])
            try:
                WebDriverWait(driver, 10).until(lambda d: "/@" in d.current_url)
            except TimeoutException:
                pass
            inject_cursor_overlay(driver)
            init_cursor_pos(driver)
        else:
            ActionChains(driver).click().perform()
            try:
                WebDriverWait(driver, 10).until(lambda d: "/@" in d.current_url)
            except TimeoutException:
                pass

        time.sleep(random.uniform(1.5, 3.0))
        stochastic_scroll(driver, total_seconds=random.uniform(2, 4))

        # Follow gate — forced (follow_mode) or probabilistic (normal mode)
        if force_follow or random.random() < 0.15:
            follow_from_profile_page(driver)

        if use_new_tab:
            # Close the profile tab and return focus to the feed tab.
            time.sleep(random.uniform(0.5, 1.2))
            log.info("[ NAV ]  closing profile tab, returning to feed")
            driver.close()
            driver.switch_to.window(original_handle)
            time.sleep(random.uniform(0.8, 1.8))
        else:
            navigate_history(driver, "back")
            time.sleep(random.uniform(1.0, 2.5))
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
    DISABLED — hover-card follow action commented out.

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
    # NOTE: Disabled — return immediately without doing anything.
    log.debug("follow_from_feed: disabled, skipping")
    return False
    # ── DISABLED BODY BELOW ───────────────────────────────────────────────────
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
                time.sleep(random.uniform(0.2, 0.4))
                # Small scroll to force any lingering hover card off the screen
                smooth_scroll_chunk(driver, random.randint(60, 130), step_px=5, tick_ms=16)
            except Exception:
                pass
            return False

        # ── 4. Arc to the Follow button and click ─────────────────────────────
        time.sleep(random.uniform(0.3, 0.7))      # eye settling on the card
        bezier_move(driver, follow_btn)
        time.sleep(random.uniform(0.3, 0.8))
        ActionChains(driver).click().perform()
        time.sleep(random.uniform(0.8, 1.5))
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
            time.sleep(random.uniform(0.2, 0.4))
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
                time.sleep(random.uniform(0.4, 0.9))
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
        time.sleep(random.uniform(2.0, 5.0))
        bezier_move(driver, btn)
        time.sleep(random.uniform(0.3, 0.8))
        ActionChains(driver).click().perform()
        time.sleep(random.uniform(0.8, 1.5))

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
        time.sleep(random.uniform(0.3, 0.7))
        ActionChains(driver).click().perform()
        time.sleep(random.uniform(0.8, 1.8))   # SPA transition settle
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
        time.sleep(random.uniform(2.0, 5.0))
        stochastic_scroll(driver, total_seconds=random.uniform(5, 15))
        # Return to feed by clicking the Home nav button
        if not click_home_button(driver):
            navigate_to(driver, TARGET_SOCIAL_URL)
        time.sleep(random.uniform(1.0, 2.5))
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
        time.sleep(random.uniform(3.0, 8.0))
        # Return to feed
        if not click_home_button(driver):
            navigate_to(driver, TARGET_SOCIAL_URL)
        time.sleep(random.uniform(1.0, 2.0))
    except (TimeoutException, WebDriverException) as exc:
        log.debug("visit_search_action failed: %s", exc)


# ================================================================== #
#  PASSIVE / ACTIVE ACTIONS  +  SESSION LOOP
# ================================================================== #

def passive_action(driver) -> None:
    """
    Passive action: scroll, with occasional browser back/forward
    to break the perfectly linear navigation graph.
    """
    scroll_time = random.uniform(25, 75)
    log.info("[ PASSIVE ]  scroll %.0fs", scroll_time)
    stochastic_scroll(driver, total_seconds=scroll_time)

    # Pause after scrolling stops — user finishes reading the post
    time.sleep(random.uniform(1.0, 3.0))


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
            time.sleep(random.uniform(1.0, 3.0))
        elif pre_roll < 0.80:
            smooth_scroll_chunk(driver, random.randint(400, 800), step_px=6)
            time.sleep(random.uniform(2.0, 4.0))
        else:
            # No pre-scroll — cursor is already resting on feed content
            time.sleep(random.uniform(0.5, 1.5))

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
                    time.sleep(random.uniform(2.0, 5.0))

        # After liking, scroll slightly to load fresh content
        if liked > 0:
            time.sleep(random.uniform(0.5, 1.5))
            smooth_scroll_chunk(driver, random.randint(250, 500), step_px=6)
            time.sleep(random.uniform(1.0, 2.0))

    except (NoSuchElementException, WebDriverException) as exc:
        log.debug("Active action error: %s", exc)

    log.info("Active action complete. Likes delivered: %d", liked)


def run_social_session(driver, session_seconds: float, follow_mode: bool = False) -> None:
    """
    Session loop with:
    - Per-session randomised passive/active split (truncated normal, not fixed 80/20).
    - Engagement variety: occasional notification check or profile view.
    - Guaranteed at least one active action per session (forced if < 60 s remain).

    follow_mode (--follow flag): replaces the normal dispatch with a
    heavily-weighted follow loop for testing follow actions end-to-end.
    """
    global _session_followed
    _session_followed = set()          # reset per-session seen-profile cache

    deadline    = time.time() + session_seconds
    count       = 0
    active_done = False

    # ------------------------------------------------------------------
    # FOLLOW MODE — heavy follow weighting for testing
    # ------------------------------------------------------------------
    if follow_mode:
        log.info("[FOLLOW MODE] Session running with heavy follow weighting")
        while time.time() < deadline:
            roll = random.random()
            # follow_from_feed disabled — block commented out
            # if roll < 0.0:
            #     # 35 %: hover username in feed → Follow via hover card
            #     follow_from_feed(driver)
            # elif roll < 0.80:
            if roll < 0.80:
                # 30 %: navigate to profile page → guaranteed follow_from_profile_page
                view_profile_from_feed(driver, force_follow=True)
            #elif roll < 0.0:
            #    # 15 %: browse suggested-for-you cards (occasional follow)
            #    interact_with_suggested_section(driver)
            else:
                # 20 %: brief passive scroll to surface fresh content
                stochastic_scroll(driver, total_seconds=random.uniform(8, 20))
            count += 1
            time.sleep(random.uniform(1, 3))
        log.info("[FOLLOW MODE] Session complete. Total actions: %d", count)
        return
    # ------------------------------------------------------------------

    # Draw per-session active probability from truncated normal (mean 0.22, SD 0.08)
    # clamped to [0.20, 0.45] — sessions range from mostly-passive to moderately active.
    active_prob = max(0.20, min(0.45, random.gauss(0.22, 0.08)))
    log.info("Session active probability this run: %.2f", active_prob)

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
            elif roll < active_prob + 0.03:
                # ~6 % of iterations: check notifications
                check_notifications_action(driver)
            elif roll < active_prob + 0.09:
                # ~6 % of iterations: click feed author link, browse profile
                view_profile_from_feed(driver)
            # follow_from_feed disabled — block commented out
            # elif roll < active_prob + 0.15:
            #     # ~3 % of iterations: quick-follow from feed (+) button
            #     follow_from_feed(driver)
            #elif roll < active_prob + 0.45:
            #    # ~3 % of iterations: browse suggested-for-you cards
            #    interact_with_suggested_section(driver)
            elif roll < active_prob + 0.12:
                # ~3 % of iterations: return to top via logo
                return_to_top_action(driver)
            elif roll < active_prob + 0.18:
                # ~3 % of iterations: open search page, dwell, return home
                visit_search_action(driver)
            else:
                passive_action(driver)

        count += 1
        time.sleep(random.uniform(1, 3))

    log.info("Session complete. Total actions: %d", count)


# ================================================================== #
#  SINGLE PROFILE WARM-UP ORCHESTRATOR
# ================================================================== #

def warm_profile(profile_id: str, follow_mode: bool = False) -> None:
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
        time.sleep(random.uniform(2, 5))

        # 4b. Verify the profile is logged in before wasting a session
        if not check_login_status(driver):
            log.error(
                "Profile %s appears logged out — skipping session for this profile.",
                profile_id,
            )
            return

        # 5. Main activity session
        session_sec = random.uniform(SESSION_MIN_MIN * 60, SESSION_MAX_MIN * 60)
        log.info("Session: %.1f min  |  profile: %s", session_sec / 60, profile_id)
        run_social_session(driver, session_sec, follow_mode=follow_mode)

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
    follow_mode: bool = False,
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
        time.sleep(random.uniform(2, 5))

        if not check_login_status(driver):
            log.error("Profile '%s' appears logged out -- skipping session.",
                      profile_id)
            return

        session_sec = random.uniform(SESSION_MIN_MIN * 60, SESSION_MAX_MIN * 60)
        log.info("Session: %.1f min  |  profile: %s", session_sec / 60, profile_id)
        run_social_session(driver, session_sec, follow_mode=follow_mode)

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
        "--follow",
        action="store_true",
        help=(
            "Follow-testing mode: replaces the normal session dispatch with a "
            "heavily-weighted follow loop (40%% quick-follow, 35%% profile-follow, "
            "15%% suggested section, 10%% passive scroll).  "
            "Useful for testing and debugging follow actions end-to-end."
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
    return p


# ================================================================== #
#  MAIN
# ================================================================== #

def main() -> None:
    args = _build_parser().parse_args()

    log.info("=" * 60)
    log.info("NstBrowser Warmer (API v2) -- %s",
             datetime.now().strftime("%Y-%m-%d %H:%M"))

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
            follow_mode=args.follow,
        )
        log.info("=" * 60)
        log.info("Done.")
        return

    # ---------------------------------------------------------------- #
    #  NORMAL MODE  — open every profile via the API
    # ---------------------------------------------------------------- #
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
        warm_profile(profile_id, follow_mode=args.follow)

        if idx < len(profile_order) - 1:
            buf = random.uniform(BUFFER_MIN_MIN * 60, BUFFER_MAX_MIN * 60)
            log.info("Buffer: %.1f min before next profile...", buf / 60)
            time.sleep(buf)

    log.info("=" * 60)
    log.info("All profiles warmed. Done.")


if __name__ == "__main__":
    main()
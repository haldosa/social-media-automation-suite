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
from selenium.webdriver.support.ui import WebDriverWait
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

def human_type(element, text: str) -> None:
    """Type text one character at a time with randomised keystroke delays."""
    element.click()
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(0.1, 0.3))


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

def _human_click_offset(element_width: int, element_height: int) -> tuple:
    """
    Return a Gaussian (dx, dy) offset from the element's geometric centre.

    Real humans do not click precisely on the centre of a button — they aim
    within a scatter zone whose spread is proportional to the target's size.
    Sigma is bounded to the range 5–10 px (human precision limits), so large
    elements get the full ±10 px scatter and tiny elements get ±5 px minimum.
    The offset is clamped so the click always lands within 80 % of the element
    bounds, preventing accidental Selenium misses on very small icons.

    DISABLED — currently not called.  Re-enable by uncommenting the call in
    bezier_move() and removing the direct x1/y1 = rect centre assignment.
    """
    sigma_x = max(5.0, min(element_width  * 0.20, 10.0))
    sigma_y = max(5.0, min(element_height * 0.20, 10.0))
    dx = random.gauss(0, sigma_x)
    dy = random.gauss(0, sigma_y)
    dx = max(-element_width  * 0.4, min(dx,  element_width  * 0.4))
    dy = max(-element_height * 0.4, min(dy,  element_height * 0.4))
    return int(dx), int(dy)


def _maybe_add_overshoot(
    x0: int, y0: int, x1: int, y1: int,
    element_w: int, element_h: int,
) -> tuple:
    """
    For small targets (area < 4000 px²), 30 % chance of overshooting 4–10 px
    past the intended aim point in the same direction, followed by a micro-arc
    correction back.  Simulates the corrective sub-movements humans make when
    homing onto small interactive elements (like buttons or icons).

    Returns (overshoot_x, overshoot_y, did_overshoot).
    When did_overshoot is False the caller uses (x1, y1) unchanged.

    DISABLED — currently not called.  Re-enable by uncommenting the call in
    bezier_move() and removing the direct arc_x/arc_y = x1, y1 assignment.
    """
    if element_w * element_h > 4000 or random.random() > 0.30:
        return x1, y1, False
    dist = math.hypot(x1 - x0, y1 - y0)
    if dist < 1:
        return x1, y1, False
    ux = (x1 - x0) / dist
    uy = (y1 - y0) / dist
    overshoot_px = random.uniform(4, 10)
    ox = int(x1 + ux * overshoot_px)
    oy = int(y1 + uy * overshoot_px)
    return ox, oy, True


def bezier_move(driver, target_element) -> None:
    """
    Move the mouse to target_element along a randomised quadratic Bezier curve
    at a true 60 fps frame rate.

    Why the previous approach was ~3.4 fps:
      ActionChains(driver).move_by_offset(dx, dy).perform() is a synchronous
      HTTP round-trip: Python → ChromeDriver HTTP → CDP WebSocket → browser.
      Each call costs ~280 ms regardless of the intended step_sec delay.

    Fix — two-phase approach:
      Phase 1 (animation):
        Pre-compute all Bezier points in Python, pass the full path array to
        the browser in ONE execute_script call, and let JavaScript dispatch
        DOM mousemove events via setTimeout at the intended interval.
        This drives the visual cursor overlay at genuine 60 fps because JS
        runs inside the browser with no per-step Python ↔ browser latency.
        Python sleeps for the full animation duration while JS runs.

      Phase 2 (hover):
        A single ActionChains.move_to_element() call at the end fires the
        real browser hover events (CSS :hover, mouseenter, etc.) on the target.
        Only one HTTP round-trip total instead of 35-55.

    Cursor continuity: uses _cursor_pos as the start point and updates it
    after each call, so the hover snap never teleports between moves.
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
        # Arc targets the element's geometric centre directly.
        # --- Click-offset (disabled) -------------------------------------------
        # To re-enable Gaussian aim scatter, replace the two lines below with:
        #   off_dx, off_dy = _human_click_offset(int(rect["w"]), int(rect["h"]))
        #   x1 = int(rect["x"]) + off_dx
        #   y1 = int(rect["y"]) + off_dy
        x1 = int(rect["x"])
        y1 = int(rect["y"])
        # Start from last known position, clamped to current viewport
        x0 = max(0, min(_cursor_pos[0], int(vw)))
        y0 = max(0, min(_cursor_pos[1], int(vh)))
        # Proximity guard: if cursor is already within 10 px of the target,
        # skip the arc entirely — a degenerate zero-travel arc produces
        # in-place jitter that is an obvious bot signal.
        if math.hypot(x1 - x0, y1 - y0) < 10:
            ActionChains(driver).move_to_element(target_element).perform()
            _cursor_pos[0], _cursor_pos[1] = x1, y1
            _mlog.debug("SKIP  already near target  pos=(%d,%d)", x1, y1)
            return
        # --- Overshoot (disabled) ----------------------------------------------
        # To re-enable: replace the line below with:
        #   arc_x, arc_y, overshot = _maybe_add_overshoot(
        #       x0, y0, x1, y1, int(rect["w"]), int(rect["h"])
        #   )
        arc_x, arc_y = x1, y1
        # Control point — 25 % of arcs use an excursion point placed
        # perpendicularly outside the start→target bounding box, producing
        # a noticeable outward curve instead of an always-efficient arc.
        if random.random() < 0.25:
            mid_x = (x0 + arc_x) // 2
            mid_y = (y0 + arc_y) // 2
            perp_offset = random.randint(30, 80) * random.choice([-1, 1])
            cp = (mid_x + perp_offset, mid_y + perp_offset)
            cp = (max(0, min(cp[0], int(vw))), max(0, min(cp[1], int(vh))))
        else:
            cp = (
                random.randint(min(x0, arc_x), max(x0, arc_x) + 1),
                random.randint(min(y0, arc_y), max(y0, arc_y) + 1),
            )
        _arc_dist = math.hypot(arc_x - x0, arc_y - y0)
        steps   = max(20, min(90, int(_arc_dist / 3.5)))   # ~3.5 px/step net; clamp 20-90
        step_ms = random.uniform(14.0, 18.0)            # base 14-18 ms per step

        # Pre-compute all points in Python with easing + micro-jitter.
        # _ease_in_out_sine maps the linear step fraction to an S-curve position
        # so the cursor accelerates out of the start, sweeps fast through the
        # middle, and decelerates smoothly onto the target (Fitts's Law).
        # Gaussian noise is added to every intermediate point to prevent
        # perfectly geometric arcs that anti-fraud systems flag as non-human.
        points = []
        delays = []
        prev   = (x0, y0)
        for i in range(1, steps + 1):
            t_raw  = i / steps
            t      = _ease_in_out_sine(t_raw)           # S-curve position
            nx, ny = _bezier_point((x0, y0), cp, (arc_x, arc_y), t)

            # Velocity-scaled tremor with Fitts's Law approach factor.
            # Jitter SD is high at departure (hand lifting), minimal at peak speed
            # mid-arc, then rises again in the final 20 % as the hand homes onto
            # the target (fine corrective micro-movements near small elements).
            if i < steps:
                velocity  = math.sin(math.pi * t_raw)                       # bell 0→1→0
                approach  = max(0.0, (t_raw - 0.80) / 0.20) if t_raw > 0.80 else 0.0
                tremor_sd = 0.8 * (1.0 - velocity * 0.8) + approach * 0.8  # ~0.8 start, ~0.16 mid, ~1.3 final
                nx = max(0, min(int(nx + random.gauss(0, tremor_sd * 1.0)), int(vw) - 1))
                ny = max(0, min(int(ny + random.gauss(0, tremor_sd * 0.7)), int(vh) - 1))

            dx, dy = nx - prev[0], ny - prev[1]
            points.append([nx, ny, dx, dy])
            prev = (nx, ny)
            # Per-step delay from the velocity bell-curve:
            # sin(π·t) peaks at t=0.5 (mid-arc) and is ~0 at both endpoints.
            # Delay is long (~22 ms) when slow, short (~10 ms) at peak speed.
            vel   = math.sin(math.pi * t_raw)           # 0 at ends, 1 at mid
            d_ms  = step_ms * (1.5 - vel * 0.7) + random.gauss(0, 0.9)
            delays.append(max(8.0, d_ms))

        # Pre-compute cumulative intended fire times (ms from arc dispatch).
        # Used to annotate STEP log lines with realistic timing instead of
        # Python computation timestamps, which are all near-simultaneous.
        cum_ms = 0.0
        step_times = []
        for d in delays:
            step_times.append(cum_ms)
            cum_ms += d
        total_arc_ms = cum_ms

        # Arc summary log
        _mlog.debug(
            "ARC  from=(%d,%d)  cp=(%d,%d)  to=(%d,%d)  steps=%d  ms/step=%.1f  dur=%.0fms",
            x0, y0, cp[0], cp[1], arc_x, arc_y, steps, step_ms, total_arc_ms,
        )
        if MOUSE_TRACE:
            for i, ((nx, ny, dx, dy), t_ms) in enumerate(zip(points, step_times), 1):
                _mlog.debug("STEP  i=%02d  t=+%.0fms  pos=(%d,%d)  delta=(%+d,%+d)",
                            i, t_ms, nx, ny, dx, dy)

        # Phase 1 — dispatch the entire path inside the browser via JS setTimeout
        # using per-step variable delays.  One execute_script call; JS fires
        # mousemove events at each step's delay with no Python round-trips between
        # steps, achieving genuine variable-rate ~60 fps movement that is slow at
        # the arc's endpoints and fastest through the middle.
        driver.execute_script(
            """
            (function(pts, delays) {
                var i = 0;
                function tick() {
                    if (i >= pts.length) return;
                    var p = pts[i];
                    var d = delays[i];
                    i++;
                    document.dispatchEvent(new MouseEvent('mousemove', {
                        clientX: p[0], clientY: p[1],
                        bubbles: true, cancelable: true, view: window
                    }));
                    setTimeout(tick, d);
                }
                tick();
            })(arguments[0], arguments[1]);
            """,
            points,
            delays,
        )
        # Sleep for the total arc duration (sum of all variable per-step delays).
        time.sleep(sum(d / 1000.0 for d in delays) + 0.05)

        # --- Overshoot correction (disabled) ----------------------------------
        # To re-enable, restore the overshot flag from _maybe_add_overshoot and
        # uncomment:
        # if overshot:
        #     _mlog.debug("OVERSHOOT  past=(%d,%d)  correct_to=(%d,%d)", arc_x, arc_y, x1, y1)
        #     time.sleep(random.uniform(0.04, 0.10))
        #     bezier_move_to_coords(driver, x1, y1)

        # Phase 2 — single ActionChains call to fire real hover/mouseenter events.
        # This snaps to the element's true centre regardless of where the arc aimed,
        # so record the actual snap position (rect centre) not the offset aim point.
        ActionChains(driver).move_to_element(target_element).perform()
        snap_x, snap_y = int(rect["x"]), int(rect["y"])
        _cursor_pos[0], _cursor_pos[1] = snap_x, snap_y
        _mlog.debug("SNAP  final=(%d,%d)", snap_x, snap_y)

    except WebDriverException:
        pass


def init_cursor_pos(driver) -> None:
    """
    Place the synthetic cursor at a uniformly random position within the
    current viewport and fire a single mousemove DOM event there.

    Called once per page load (via inject_cursor_overlay) so the first
    bezier_move arc starts from a plausible random location rather than
    from (0, 0) or any other hard-coded corner — both of which are
    immediate bot signals on monitor-refresh-rate timing analysis.
    """
    global _cursor_pos
    try:
        vw = driver.execute_script("return window.innerWidth")
        vh = driver.execute_script("return window.innerHeight")
        # Avoid the extreme edges and the browser chrome at the top
        x = random.randint(int(vw * 0.10), int(vw * 0.90))
        y = random.randint(int(vh * 0.15), int(vh * 0.85))
        driver.execute_script(
            "document.dispatchEvent(new MouseEvent('mousemove', {"
            "  clientX: arguments[0], clientY: arguments[1],"
            "  bubbles: true, cancelable: true, view: window"
            "}));",
            x, y,
        )
        _cursor_pos[0], _cursor_pos[1] = x, y
        _mlog.debug("INIT  pos=(%d,%d)  vp=(%dx%d)", x, y, vw, vh)
    except WebDriverException as exc:
        log.debug("init_cursor_pos failed: %s", exc)


def bezier_move_to_coords(driver, x1: int, y1: int) -> None:
    """
    Animate the cursor from _cursor_pos to explicit viewport coordinates
    (x1, y1) along a randomised quadratic Bezier S-curve at ~60 fps.

    Unlike bezier_move(), no DOM element is required and no ActionChains
    hover is fired — pure JS mousemove dispatch only.  Used for:
      • parking the cursor at y=0 before any page navigation
      • idle cursor wanders between scroll-rest reading pauses
      • post-load cursor drift onto fresh content
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
        cp = (
            random.randint(min(x0, x1), max(x0, x1) + 1),
            random.randint(min(y0, y1), max(y0, y1) + 1),
        )
        _arc_dist = math.hypot(x1 - x0, y1 - y0)
        steps   = max(20, min(70, int(_arc_dist / 3.5)))   # ~3.5 px/step net; clamp 20-70
        step_ms = random.uniform(14.0, 18.0)
        points = []
        delays = []
        prev = (x0, y0)
        for i in range(1, steps + 1):
            t_raw = i / steps
            t     = _ease_in_out_sine(t_raw)
            nx, ny = _bezier_point((x0, y0), cp, (x1, y1), t)
            if i == steps:
                nx, ny = x1, y1          # land exactly on the target
            dx, dy = nx - prev[0], ny - prev[1]
            points.append([nx, ny, dx, dy])
            prev = (nx, ny)
            vel  = math.sin(math.pi * t_raw)
            d_ms = step_ms * (1.5 - vel * 0.7) + random.gauss(0, 0.9)
            delays.append(max(8.0, d_ms))

        # Pre-compute cumulative intended fire times for accurate STEP logging.
        cum_ms = 0.0
        step_times = []
        for d in delays:
            step_times.append(cum_ms)
            cum_ms += d
        total_arc_ms = cum_ms

        _mlog.debug(
            "ARC  from=(%d,%d)  cp=(%d,%d)  to=(%d,%d)  steps=%d  ms/step=%.1f  dur=%.0fms",
            x0, y0, cp[0], cp[1], x1, y1, steps, step_ms, total_arc_ms,
        )
        if MOUSE_TRACE:
            for i, ((nx, ny, dx, dy), t_ms) in enumerate(zip(points, step_times), 1):
                _mlog.debug("STEP  i=%02d  t=+%.0fms  pos=(%d,%d)  delta=(%+d,%+d)",
                            i, t_ms, nx, ny, dx, dy)
        driver.execute_script(
            """
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
            """,
            points, delays,
        )
        time.sleep(sum(d / 1000.0 for d in delays) + 0.05)
        _cursor_pos[0], _cursor_pos[1] = x1, y1
        _mlog.debug("SNAP  final=(%d,%d)", x1, y1)
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
      4. Inject the visual debug overlay without changing the cursor position.
      5. Restore the cursor to its pre-navigation coordinate via a single
         synthetic mousemove — it was 'already there' while the page loaded.
      6. Brief settle pause (user's eye scans the freshly rendered page).
      7. Drift the cursor to a random viewport position — where the eye
         naturally lands on the first piece of new content.
    """
    global _cursor_pos
    try:
        vw = driver.execute_script("return window.innerWidth")
    except Exception:
        vw = 1280
    # 1. Park
    park_x = random.randint(int(vw * 0.25), int(vw * 0.75))
    bezier_move_to_coords(driver, park_x, 0)
    saved_x, saved_y = _cursor_pos[0], _cursor_pos[1]
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
    # 4. Restore cursor
    try:
        driver.execute_script(
            "document.dispatchEvent(new MouseEvent('mousemove',{"
            "clientX:arguments[0],clientY:arguments[1],"
            "bubbles:true,cancelable:true,view:window}));",
            saved_x, saved_y,
        )
        _cursor_pos[0], _cursor_pos[1] = saved_x, saved_y
        _mlog.debug("RESTORE  pos=(%d,%d)", saved_x, saved_y)
    except WebDriverException:
        pass
    # 5. Settle
    time.sleep(random.uniform(0.4, 1.0))
    # 6. Drift to random position
    try:
        vw2 = driver.execute_script("return window.innerWidth")
        vh2 = driver.execute_script("return window.innerHeight")
        rx = random.randint(int(vw2 * 0.10), int(vw2 * 0.90))
        ry = random.randint(int(vh2 * 0.20), int(vh2 * 0.80))
        bezier_move_to_coords(driver, rx, ry)
    except Exception:
        pass


def navigate_to(driver, url: str) -> None:
    """Navigate to url with human-like cursor park → restore → drift."""
    _navigate_and_settle(driver, lambda: driver.get(url))


def navigate_history(driver, direction: str = "back") -> None:
    """Go back or forward in history with human-like cursor park → restore → drift."""
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
    steps     = max(1, total // max(1, step_px))
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
    deadline = time.time() + total_seconds
    while time.time() < deadline:
        distance = random.randint(280, 650)
        step_px  = random.randint(4, 9)
        tick_ms  = random.randint(12, 20)
        smooth_scroll_chunk(driver, distance, step_px, tick_ms)

        # brief pause after scroll lands (hand leaving wheel)
        time.sleep(random.uniform(0.15, 0.45))

        # 4-tier reading pause
        tier = random.random()
        if tier < 0.03:
            time.sleep(random.uniform(8.0, 15.0))   # distraction
        elif tier < 0.18:
            time.sleep(random.uniform(4.5, 9.0))    # long read
        elif tier < 0.35:
            time.sleep(random.uniform(0.3, 1.2))    # quick skim
        else:
            time.sleep(random.uniform(1.5, 4.0))    # normal read

        # Cursor idle wander — between scroll rests the cursor drifts over the
        # content the user is 'reading' rather than freezing in one place.
        # Skipped ~35 % of the time (quick skims where the hand stays put).
        if random.random() < 0.65 and time.time() < deadline:
            try:
                vw_s = driver.execute_script("return window.innerWidth")
                vh_s = driver.execute_script("return window.innerHeight")
                wx = random.randint(int(vw_s * 0.08), int(vw_s * 0.92))
                wy = random.randint(int(vh_s * 0.10), int(vh_s * 0.90))
                bezier_move_to_coords(driver, wx, wy)
            except Exception:
                pass

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
#  THREADS-SPECIFIC HOVERING
# ================================================================== #

# Selectors for content worth hovering over while reading the Threads feed
_THREADS_HOVER_SELECTORS = [
    "article",
    "div[data-pressable-container='true']",   # Threads post card wrapper
    "img[src*='cdninstagram']",               # Threads/Meta CDN images
    "img[src*='fbcdn']",
    "video",
    "a[href*='/@']",                          # @username profile links
    "a[href*='/t/']",                         # individual thread links
    "p",
    "span[dir='auto']",                       # localised post body text
]


def _hover_random_element(driver) -> None:
    """
    Move the cursor to a random visible Threads content element and linger.
    Iterates selectors in priority order; stops at the first match found.
    Only elements inside the current viewport are considered.
    """
    try:
        viewport_h = driver.execute_script("return window.innerHeight")
        for sel in _THREADS_HOVER_SELECTORS:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            visible = []
            for el in elems:
                try:
                    if not el.is_displayed():
                        continue
                    rect = driver.execute_script(
                        "var r=arguments[0].getBoundingClientRect();"
                        "return {top:r.top, bottom:r.bottom, height:r.height};", el
                    )
                    if rect["height"] > 8 and 0 <= rect["top"] <= viewport_h:
                        visible.append(el)
                except Exception:
                    continue
            if visible:
                # Sort by vertical centre, split into thirds, weight bucket selection
                # so the middle third (where a real user's eye rests) is most likely.
                visible.sort(key=lambda e: e._id if hasattr(e, '_id') else 0)
                try:
                    visible.sort(
                        key=lambda e: driver.execute_script(
                            "return arguments[0].getBoundingClientRect().top;", e
                        )
                    )
                except Exception:
                    pass
                n   = len(visible)
                t1  = n // 3
                t2  = t1 * 2
                top_third    = visible[:t1]    if t1 > 0  else visible
                mid_third    = visible[t1:t2]  if t2 > t1 else visible
                bot_third    = visible[t2:]    if t2 < n  else visible
                bucket_roll  = random.random()
                if bucket_roll < 0.25:
                    pool = top_third
                elif bucket_roll < 0.75:
                    pool = mid_third
                else:
                    pool = bot_third
                if not pool:
                    pool = visible
                bezier_move(driver, random.choice(pool))
                time.sleep(random.uniform(0.5, 2.5))
                return
    except (NoSuchElementException, WebDriverException):
        pass


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


def _scroll_post_into_center(driver, element) -> None:
    """
    Scroll the like button's parent post into the vertical centre of
    the viewport, then wait for the scroll to settle.
    """
    driver.execute_script(
        "arguments[0].scrollIntoView({behavior:'smooth', block:'center'});",
        element,
    )
    time.sleep(random.uniform(0.5, 1.0))


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
        _scroll_post_into_center(driver, element)

        # Reading pause before liking — humans read before they react
        time.sleep(random.uniform(0.8, 2.5))

        bezier_move(driver, element)
        time.sleep(random.uniform(0.2, 0.6))   # hand settling on the button

        try:
            element.click()
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

def check_login_status(driver) -> bool:
    """
    Return True if the current Threads page shows a logged-in feed.
    Detects logged-out state by redirect to /login or absence of feed DOM.
    """
    try:
        url = driver.current_url
        if any(s in url for s in ("/login", "/accounts/login", "instagram.com/accounts")):
            log.warning("Login redirect detected — profile may be logged out: %s", url)
            return False
        # Feed content present — logged in
        articles = driver.find_elements(
            By.CSS_SELECTOR,
            "article, div[data-pressable-container='true']",
        )
        if articles:
            return True
        # Fallback: URL is on threads.net or threads.com (redirect target)
        if "threads.net" in url or "threads.com" in url:
            return True
    except WebDriverException:
        pass
    return False


# ================================================================== #
#  ENGAGEMENT VARIETY ACTIONS
# ================================================================== #

def check_notifications_action(driver) -> None:
    """
    Visit the activity/notifications tab and dwell briefly.
    Simulates a user checking who liked or replied to them.
    """
    notif_urls = [
        "https://www.threads.net/activity",
        "https://www.threads.net/notifications",
    ]
    target = random.choice(notif_urls)
    log.info("Checking notifications: %s", target)
    try:
        navigate_to(driver, target)
        time.sleep(random.uniform(2.0, 5.0))
        stochastic_scroll(driver, total_seconds=random.uniform(5, 15))
        # Return to feed
        navigate_to(driver, TARGET_SOCIAL_URL)
        time.sleep(random.uniform(1.0, 2.5))
    except (TimeoutException, WebDriverException) as exc:
        log.debug("Notification check failed: %s", exc)


def view_profile_action(driver) -> None:
    """
    Click a random @username link in the feed, browse that profile briefly,
    then navigate back.  Simulates organic profile-discovery behaviour.
    """
    log.info("Viewing a random profile...")
    try:
        links = [
            el for el in driver.find_elements(By.CSS_SELECTOR, "a[href*='/@']")
            if el.is_displayed()
        ]
        if not links:
            log.debug("No profile links visible — skipping profile view")
            return
        target = random.choice(links[:20])
        profile_url = target.get_attribute("href")
        if not profile_url:
            return
        log.info("Visiting profile: %s", profile_url[:60])
        navigate_to(driver, profile_url)
        time.sleep(random.uniform(2.0, 5.0))
        stochastic_scroll(driver, total_seconds=random.uniform(8, 20))
        navigate_history(driver, "back")
    except (TimeoutException, WebDriverException) as exc:
        log.debug("Profile view failed (%s) — returning to feed", exc)
        try:
            navigate_to(driver, TARGET_SOCIAL_URL)
        except Exception:
            pass


# ================================================================== #
#  PASSIVE / ACTIVE ACTIONS  +  SESSION LOOP
# ================================================================== #

def passive_action(driver) -> None:
    """
    Passive action: scroll + hover, with occasional browser back/forward
    to break the perfectly linear navigation graph.
    """
    scroll_time = random.uniform(25, 75)
    log.debug("Passive: scrolling %.0fs", scroll_time)
    stochastic_scroll(driver, total_seconds=scroll_time)

    # Pause after scrolling stops — user finishes reading the post
    time.sleep(random.uniform(1.0, 3.0))

    _hover_random_element(driver)
    if random.random() < 0.30:
        time.sleep(random.uniform(1.0, 2.5))
        _hover_random_element(driver)

    # 8 % chance: brief back then forward (mis-tap or curiosity)
    if random.random() < 0.08:
        try:
            navigate_history(driver, "back")
            time.sleep(random.uniform(0.5, 1.5))
            navigate_history(driver, "forward")
        except WebDriverException:
            pass


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
            "Active: not on threads.net/com (%s) — passive scroll instead",
            current_url[:60],
        )
        stochastic_scroll(driver, total_seconds=random.uniform(15, 30))
        return

    log.info("Active: scanning for like buttons on %s", current_url[:60])
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

        # Weighted like count: 15% skip, 50% 1-like, 25% 2-likes, 10% 3-likes
        like_roll = random.random()
        if like_roll < 0.15:
            log.info("Active: decided to scroll past without liking")
            stochastic_scroll(driver, total_seconds=random.uniform(10, 25))
            return
        elif like_roll < 0.65:
            n_targets = 1
        elif like_roll < 0.90:
            n_targets = 2
        else:
            n_targets = 3

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


def run_social_session(driver, session_seconds: float) -> None:
    """
    Session loop with:
    - Per-session randomised passive/active split (truncated normal, not fixed 80/20).
    - Engagement variety: occasional notification check or profile view.
    - Guaranteed at least one active action per session (forced if < 60 s remain).
    """
    deadline    = time.time() + session_seconds
    count       = 0
    active_done = False

    # Draw per-session active probability from truncated normal (mean 0.22, SD 0.08)
    # clamped to [0.10, 0.45] — sessions range from mostly-passive to moderately active.
    active_prob = max(0.10, min(0.45, random.gauss(0.22, 0.08)))
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
            elif roll < active_prob + 0.06:
                # ~6 % of iterations: check notifications
                check_notifications_action(driver)
            elif roll < active_prob + 0.10:
                # ~4 % of iterations: visit a profile
                view_profile_action(driver)
            else:
                passive_action(driver)

        count += 1
        time.sleep(random.uniform(1, 3))

    log.info("Session complete. Total actions: %d", count)


# ================================================================== #
#  SINGLE PROFILE WARM-UP ORCHESTRATOR
# ================================================================== #

def warm_profile(profile_id: str) -> None:
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
        run_social_session(driver, session_sec)

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
        run_social_session(driver, session_sec)

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
        warm_profile(profile_id)

        if idx < len(profile_order) - 1:
            buf = random.uniform(BUFFER_MIN_MIN * 60, BUFFER_MAX_MIN * 60)
            log.info("Buffer: %.1f min before next profile...", buf / 60)
            time.sleep(buf)

    log.info("=" * 60)
    log.info("All profiles warmed. Done.")


if __name__ == "__main__":
    main()
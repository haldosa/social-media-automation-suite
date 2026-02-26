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
SESSION_MIN_MIN     = 1     # minimum session length (minutes)
SESSION_MAX_MIN     = 1     # maximum session length (minutes)
BUFFER_MIN_MIN      = 0     # minimum buffer between profiles (minutes)
BUFFER_MAX_MIN      = 0     # maximum buffer between profiles (minutes)
SCREENSHOT_DIR      = "screenshots"
LOG_FILE            = "nstbrowser_warmer.log"
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


# Persistent cursor state — updated after every bezier_move so subsequent
# movements start from the real last-known position instead of a random
# viewport coordinate that would cause detectable teleports in CDP event stream.
_cursor_pos: list = [0, 0]


def bezier_move(driver, target_element) -> None:
    """
    Move the mouse to target_element along a randomised quadratic Bezier curve.

    Cursor continuity: uses _cursor_pos as the start point and updates it
    after each call, so the mouse never teleports between movements.
    """
    global _cursor_pos
    try:
        vw   = driver.execute_script("return window.innerWidth")
        vh   = driver.execute_script("return window.innerHeight")
        rect = driver.execute_script(
            "var r=arguments[0].getBoundingClientRect();"
            "return {x:r.left+r.width/2, y:r.top+r.height/2};",
            target_element,
        )
        x1, y1 = int(rect["x"]), int(rect["y"])
        # Start from last known position, clamped to current viewport
        x0 = max(0, min(_cursor_pos[0], int(vw)))
        y0 = max(0, min(_cursor_pos[1], int(vh)))
        # Random control point creates a unique curved path each time
        cp = (
            random.randint(min(x0, x1), max(x0, x1) + 1),
            random.randint(min(y0, y1), max(y0, y1) + 1),
        )
        steps    = random.randint(35, 55)        # more steps = smoother arc
        step_sec = random.uniform(0.008, 0.018)  # 8-18 ms per step ≈ 55-125 fps
        prev     = (x0, y0)

        for i in range(1, steps + 1):
            t      = i / steps
            nx, ny = _bezier_point((x0, y0), cp, (x1, y1), t)
            dx, dy = nx - prev[0], ny - prev[1]
            if dx != 0 or dy != 0:
                ActionChains(driver).move_by_offset(dx, dy).perform()
                time.sleep(step_sec)
            prev = (nx, ny)

        # Snap to centre — guarantees mouseenter fires on the target element
        ActionChains(driver).move_to_element(target_element).perform()
        # Update tracked position to element centre
        _cursor_pos[0], _cursor_pos[1] = x1, y1

    except WebDriverException:
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
        driver.get(site)
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
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
        driver.get(target)
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(random.uniform(3.0, 8.0))
        stochastic_scroll(driver, total_seconds=random.uniform(5, 15))
        # Return to feed
        driver.get(TARGET_SOCIAL_URL)
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(random.uniform(1.5, 3.5))
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
        driver.get(profile_url)
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(random.uniform(3.0, 8.0))
        stochastic_scroll(driver, total_seconds=random.uniform(8, 20))
        driver.back()
        time.sleep(random.uniform(1.5, 3.5))
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except (TimeoutException, WebDriverException) as exc:
        log.debug("Profile view failed (%s) — returning to feed", exc)
        try:
            driver.get(TARGET_SOCIAL_URL)
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
            driver.back()
            time.sleep(random.uniform(1.0, 3.0))
            driver.forward()
            time.sleep(random.uniform(0.5, 1.5))
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

        # 3. Pre-flight: Wikipedia only
        run_preflight(driver)

        # 4. Navigate to Threads
        log.info("Navigating to %s", TARGET_SOCIAL_URL)
        driver.get(TARGET_SOCIAL_URL)
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(random.uniform(3, 7))

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


# ================================================================== #
#  MAIN
# ================================================================== #

def main() -> None:
    log.info("=" * 60)
    log.info("NstBrowser Warmer (API v2) -- %s",
             datetime.now().strftime("%Y-%m-%d %H:%M"))
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
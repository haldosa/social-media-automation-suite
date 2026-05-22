import time
import random
import math
import re
from socket import timeout
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, WebDriverException
from utils import log, precise_sleep, _dlog, _timing_check, _get_ctx
from mouse import (
    bezier_move_to_coords,
    _cdp_click,
    _cdp_record_failure,
    _cdp_record_success,
    CDPConnectionDead,
    inject_cursor_overlay,
    DEBUG_CURSOR_OVERLAY,
    _set_cursor,
    bezier_move,
    debug_cursor_state
    )

def _navigate_and_settle(driver, action) -> None:
    """
    Shared navigation kernel used by navigate_to() and navigate_history().

    Steps:
      1. Park the cursor at the browser address-bar row (y=0, random x) via a
         smooth Bezier arc ,  simulating the user reaching for the URL bar.
      2. Execute the navigation action (driver.get / back / forward).
      3. Wait for DOMContentLoaded.
      4. Inject the visual debug overlay.
      5. Silent position set ,  cursor was at (park_x, 0) before navigation and
         is conceptually still there; no dispatch needed on the fresh page.
      6. Brief settle pause (user's eye scans the freshly rendered page).
      7. Drift the cursor into the feed ,  the first browser input event the new
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
    # â”€â”€ DEBUG LOGGING: NAV timing markers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    _nav_t0 = time.perf_counter()
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Phase 1 ,  wait for the browser's resource-load signal.
    try:
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        pass
    _nav_readystate_ms = (time.perf_counter() - _nav_t0) * 1000
    # Phase 2 ,  SPA content check: wait for a feed article or pressable
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
        pass  # fall through ,  page may still be partially usable
    _nav_spa_ms = (time.perf_counter() - _nav_spa_t0) * 1000

    # 3. Overlay ,  inject after readyState complete, then verify it survives
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
            log.warning("React wiped the overlay ,  re-injecting into documentElement")
            inject_cursor_overlay(driver)
    # 4. Silent position set ,  fresh page has no cursor history.
    #    Cursor was at (park_x, 0) before navigation; it's still conceptually
    #    there.  No dispatch needed ,  the drift arc below is the first event
    #    the new page sees, which avoids a repetitive in-place jump on load.
    _set_cursor(park_x, 0, "fresh-page")

    # 5. Settle ,  1.5â€“3.5 s to mimic a real user visually orienting
    #    after a full page navigation before moving the mouse.
    _nav_settle_s = random.uniform(1.5, 3.5)
    precise_sleep(_nav_settle_s)

    # â”€â”€ DEBUG LOGGING: [NAV] summary â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    # 6. Drift into content ,  first browser input event on the new page,
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
    """Navigate to url with operator-like cursor park â†’ restore â†’ drift."""
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
    """Go back or forward in history with operator-like cursor park â†’ restore â†’ drift."""
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
#  the Selenium "done" callback, which Orbita sometimes never triggers , 
#  causing a script timeout after 30 s.
#
#  Fix: use plain synchronous execute_script in a Python loop.
#  Each call moves `step_px` pixels and returns immediately.
#  We sleep `tick_ms` ms between calls in Python ,  same visual effect,
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
    # â”€â”€ DEBUG LOGGING: SCROLL CHUNK tracking â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    _sc_t0    = time.perf_counter()
    _sc_dir   = "down" if distance_px >= 0 else "up"
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    total     = abs(distance_px)
    direction = 1 if distance_px >= 0 else -1
    steps     = int(max(1, total // max(1, step_px)))
    scrolled  = 0

    for i in range(steps):
        # Sine-based ease-in/ease-out: slow at start and end, fast in middle
        t        = (i + 0.5) / steps                        # normalised 0..1
        velocity = 0.5 - 0.5 * math.cos(math.pi * t)       # bell curve 0..1
        # Step size scales with velocity: 1 px minimum, up to 2Ã— step_px peak
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
    # â”€â”€ DEBUG LOGGING: [SCROLL CHUNK] summary â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    _dlog.debug(
        "[SCROLL CHUNK]  distance=%dpx  direction=%s  step_px=%d  tick_ms=%d"
        "  steps=%d  actual_duration=%.0fms",
        total, _sc_dir, step_px, tick_ms, steps,
        (time.perf_counter() - _sc_t0) * 1000,
    )
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _sample_scroll_notches() -> int:
    """
    Fix #8: 3-component mixture distribution for scroll distance in wheel notches.
    One notch = deltaY:100 deltaMode:1  â‰ˆ 100-120 px at default browser line height.

      40 % short   2â€“3 notches  (lazy one-finger flick)
      40 % medium  4â€“7 notches  (normal reading scroll)
      20 % long    8â€“14 notches (fast sweep / skipping section)
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
    deltaMode:1 (line units, deltaY=Â±100) with natural burst-silence timing.

    Real USB mice report wheel events in firmware-timed bursts of 1-4 notches
    at the USB polling interval, followed by 80-800 ms of silence between
    mechanical detents.  A uniform pixel-delta stream (deltaMode:0) at 12-20 ms
    intervals matches no real input device and is a repetitive timing pattern.

    This model:
      - fires 1-4 notches per burst (weighted toward smaller bursts)
      - uses log-normal intra-burst gaps  (Î¼=25 ms, clamped 6-80 ms)
      - uses log-normal inter-burst silences (Î¼=140 ms, clamped 80-250 ms)

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
                # intra-burst: log-normal â‰ˆ 25 ms (USB polling rhythm)
                intra = max(0.006, min(
                    random.lognormvariate(math.log(0.025), 0.35),
                    0.080,
                ))
                precise_sleep(intra)

        remaining -= burst

        if remaining > 0:
            # inter-burst silence: log-normal â‰ˆ 140 ms (hand pause between detents)
            silence = max(0.080, min(
                random.lognormvariate(math.log(0.140), 0.45),
                0.250,
            ))
            precise_sleep(silence)

def _maybe_pause_for_video(driver) -> None:
    """
    Detect a <video> element visible in the viewport and, with 60 % chance,
    pause for 3â€“15 s to simulate watching it.

    Called from stochastic_scroll so video watch-time accumulates naturally
    as the feed scrolls past video posts.  Only fires on threads.net/com URLs
    so it is a no-op during Wikipedia pre-flight.
    """
    try:
        url = driver.current_url
        if "threads.net" not in url and "threads.com" not in url:
            return
        has_video = driver.execute_script("""
            var vids = document.querySelectorAll('video');
            for (var i = 0; i < vids.length; i++) {
                var r = vids[i].getBoundingClientRect();
                if (r.height > 30 && r.top >= 0 &&
                        r.bottom <= window.innerHeight + 80)
                    return true;
            }
            return false;
        """)
        if has_video and random.random() < 0.60:
            dwell = random.uniform(3.0, 15.0)
            log.info("[ VIDEO ]  video in viewport ,  watching for %.0fs", dwell)
            precise_sleep(dwell)
    except Exception:
        pass

def stochastic_scroll(driver, total_seconds: float) -> None:
    """
    Scroll the page for total_seconds with natural human variance.

    Reading pause tiers (per chunk):
      3%  distraction  8â€“15 s  (phone buzz, looking away)
     15%  long read    4.5â€“9 s  (interesting post)
     17%  quick skim   0.3â€“1.2 s (nothing to see, keep scrolling)
     65%  normal read  1.5â€“4 s
    """
    def _browser_alive(driver, timeout: float = 5.0) -> bool:
    #Quick CDP ping to verify browser is still responsive.  
        try:
            WebDriverWait(driver, timeout).until(
                lambda d: d.execute_script("return 1") == 1
            )
            return True
        except Exception:
            return False

    def _reading_pause(seconds: float) -> None:
        """
        Sleep for `seconds` while continuously drifting the cursor ,  mimicking
        a user's eyes and hand moving across content they're reading.

        Rather than a flat sleep followed by a single wander, the pause is
        broken into micro-segments of 0.6â€“2.0 s each.  After each segment there
        is a 72 % chance of a small cursor nudge.  Nudges are *local* ,  biased
        toward the current cursor position + Gaussian scatter ,  so the cursor
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
                    # firing the cursor drift ,  avoids moving the cursor on top of
                    # the overlay's fixed-position elements.
                    _close_media_overlay(driver)
                    vw_r = driver.execute_script("return window.innerWidth")
                    vh_r = driver.execute_script("return window.innerHeight")
                    # Local drift ,  stays near current position, Gaussian spread
                    cx = max(int(vw_r * 0.08), min(int(vw_r * 0.92),
                             _get_ctx().cursor_pos[0] + int(random.gauss(0, vw_r * 0.10))))
                    cy = max(int(vh_r * 0.10), min(int(vh_r * 0.90),
                             _get_ctx().cursor_pos[1] + int(random.gauss(0, vh_r * 0.09))))
                    bezier_move_to_coords(driver, cx, cy, tag="reading-wander")
                except Exception:
                    pass

    deadline = time.time() + total_seconds
    log.info("[ SCROLL ]  scrolling for %.0fs", total_seconds)
    # Scroll-chunk nudge counter ,  fire a small cursor shift every 3-5 chunks
    # to model the hand resting on the desk and shifting while scrolling.
    _nudge_after  = random.randint(3, 5)
    _chunk_count  = 0
    _total_chunks = 0   # â”€ DEBUG: cumulative chunk counter for progress logs
    while time.time() < deadline:
        # Fix #7+#8: discrete notched wheel events with mixture distance distribution
        n_notches = _sample_scroll_notches()
        _notched_scroll_burst(driver, n_notches, direction=1)
        _chunk_count  += 1
        _total_chunks += 1

        # â”€â”€ DEBUG LOGGING: scroll progress every 5 chunks â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
        # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
                # scroll chunks ,  prevents the drift arc landing on overlay UI.
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

        # 4-tier reading pause ,  cursor drifts throughout via _reading_pause()
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
        # â”€â”€ DEBUG LOGGING: [SCROLL CHUNK] with tier info â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        _dlog.debug(
            "[SCROLL CHUNK]  pause_tier=%s  pause_duration=%.1fs",
            _pause_tier, _pause_s,
        )
        _timing_check(f"reading_pause_{_pause_tier}", _pause_s,
                      {"distraction": 8.0, "long": 4.5, "skim": 0.3, "normal": 1.5}[_pause_tier],
                      {"distraction": 15.0, "long": 9.0, "skim": 1.2, "normal": 4.0}[_pause_tier])
        # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        _reading_pause(_pause_s)
        # Video dwell ,  fires on ~20% of normal/long chunks when a <video>
        # is visible in the viewport.  _maybe_pause_for_video() handles the
        # internal 60% probability gate and the Threads-URL guard.
        if _pause_tier in ("normal", "long") and random.random() < 0.20:
            _maybe_pause_for_video(driver)
        # occasional upward drift ,  small (re-reading) or large (going back to a post)
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
         is NOT used as it skips the real pointer event the overlay needs.
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

    log.info("[ PASSIVE ]  media overlay detected (%s) ,  closing", current[:80])

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
        log.debug("[ PASSIVE ]  Close button not found in media overlay top-left ,  skipping")
        return False

    try:
        # Brief pause ,  user realises they're in the media viewer
        precise_sleep(random.uniform(0.6, 1.4))
        bezier_move(driver, close_btn)
        precise_sleep(random.uniform(0.2, 0.5))
        # Use ActionChains only ,  the overlay listens for real pointer events;
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
            log.debug("[ PASSIVE ]  URL did not change after Close click ,  trying Escape")

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
                log.debug("[ PASSIVE ]  Escape also failed ,  deferring to home nav")

        if not url_changed:
            return False   # let caller fall back to home nav

        precise_sleep(random.uniform(0.8, 1.6))  # settle on the post/feed page
        log.info("[ PASSIVE ]  media overlay closed ,  back on feed/post")
        return True
    except WebDriverException as exc:
        log.debug("[ PASSIVE ]  Close button click failed: %s", exc)
        return False



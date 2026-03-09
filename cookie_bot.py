"""
cookie_robot.py
===============
Pre-warm NstBrowser profiles by accumulating cross-domain cookies,
browsing history, and cached assets before the first Threads session.

Usage:
    python cookie_robot.py --profile PROFILE_ID
    python cookie_robot.py --profile PROFILE_ID --runs 3
    python cookie_robot.py --attach 127.0.0.1:9222
    python cookie_robot.py --all-profiles

Run once daily for 3-5 days before the profile's first Threads session.
"""

import os
import signal
import heapq
import random
import time
import argparse
import logging
from datetime import datetime, date
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, WebDriverException, NoSuchElementException
)

from config import COOKIE_PROFILE_IDS, SCREENSHOT_DIR
from utils import log, precise_sleep
from api import start_profile, stop_profile
from browser import connect_selenium
from mouse import bezier_move_to_coords, bezier_move, init_cursor_pos
from scroll import stochastic_scroll, navigate_to

# ── Daemon config ─────────────────────────────────────────────────────────────
COOKIE_DAEMON_ACTIVE_HOURS  = (8, 23)   # only run between these hours
COOKIE_SESSIONS_PER_DAY_MIN = 2         # minimum sessions per profile per day
COOKIE_SESSIONS_PER_DAY_MAX = 4         # maximum sessions per profile per day
COOKIE_DAY_OFF_PROB         = 0.05      # 10% chance of skipping a profile for a day
COOKIE_INTER_SESSION_MIN    = 60        # minimum minutes between sessions
COOKIE_INTER_SESSION_MAX    = 180       # maximum minutes between sessions
COOKIE_STATE_FILE           = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cookie_state.json"
)

import json
import threading

_COOKIE_STATE_LOCK = threading.Lock()

def _load_cookie_state() -> dict:
    try:
        with open(COOKIE_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_cookie_state(state: dict) -> None:
    tmp = COOKIE_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, COOKIE_STATE_FILE)

def _ensure_profile_in_cookie_state(profile_id: str, state: dict) -> None:
    if profile_id not in state:
        state[profile_id] = {
            "next_run_ts": 0.0,
            "daily": {}
        }

def _cookie_clamp_to_active_hours(ts: float) -> float:
    """Push ts forward until it falls within COOKIE_DAEMON_ACTIVE_HOURS."""
    from datetime import datetime, timedelta
    dt = datetime.fromtimestamp(ts)
    start, end = COOKIE_DAEMON_ACTIVE_HOURS
    if dt.hour < start:
        dt = dt.replace(hour=start, minute=random.randint(0, 30),
                        second=0, microsecond=0)
    elif dt.hour >= end:
        dt = (dt + timedelta(days=1)).replace(
            hour=start, minute=random.randint(0, 30),
            second=0, microsecond=0)
    return dt.timestamp()


def _cookie_get_daily_target(profile_id: str, state: dict,
                              today_iso: str) -> int:
    """Get or generate today's session target for this profile."""
    profile = state.setdefault(profile_id, {"next_run_ts": 0.0, "daily": {}})
    daily   = profile.setdefault("daily", {})
    if today_iso not in daily:
        daily[today_iso] = {
            "target": random.randint(COOKIE_SESSIONS_PER_DAY_MIN,
                                     COOKIE_SESSIONS_PER_DAY_MAX),
            "done":   0,
            "day_off": random.random() < COOKIE_DAY_OFF_PROB,
        }
    return daily[today_iso]["target"]


def _cookie_get_daily_done(profile_id: str, state: dict,
                            today_iso: str) -> int:
    return state.get(profile_id, {}).get(
        "daily", {}).get(today_iso, {}).get("done", 0)


def _cookie_increment_done(profile_id: str, state: dict,
                            today_iso: str) -> None:
    state[profile_id]["daily"][today_iso]["done"] += 1


def _cookie_is_day_off(profile_id: str, state: dict,
                        today_iso: str) -> bool:
    _cookie_get_daily_target(profile_id, state, today_iso)  # ensure entry exists
    return state[profile_id]["daily"][today_iso].get("day_off", False)


def _cookie_schedule_next(profile_id: str, after_ts: float,
                           state: dict) -> float:
    """Schedule next session with random inter-session gap."""
    gap = random.uniform(COOKIE_INTER_SESSION_MIN * 60,
                         COOKIE_INTER_SESSION_MAX * 60)
    ts  = _cookie_clamp_to_active_hours(after_ts + gap)
    state[profile_id]["next_run_ts"] = ts
    return ts


def _cookie_prune_old_keys(state: dict) -> None:
    """Remove daily entries older than 30 days."""
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    for profile in state.values():
        daily = profile.get("daily", {})
        old   = [k for k in daily if k < cutoff]
        for k in old:
            del daily[k]

_cookie_shutdown = threading.Event()
_cookie_start_ts: float = 0.0

def _install_cookie_signals() -> None:
    def _handler(signum, frame):
        log.info("[COOKIE DAEMON]  shutdown signal — stopping after current session")
        _cookie_shutdown.set()
    signal.signal(signal.SIGINT,  _handler)
    signal.signal(signal.SIGTERM, _handler)

def _cookie_shutdown_watchdog() -> None:
    _cookie_shutdown.wait()
    log.info("[COOKIE DAEMON]  watchdog: force exit in 30s")
    time.sleep(30)
    log.info("[COOKIE DAEMON]  watchdog: force exit now")
    os._exit(1)

def _cookie_keyboard_listener() -> None:
    try:
        log.info("[COOKIE DAEMON]  press Enter to stop after current session")
        input()
        log.info("[COOKIE DAEMON]  Enter pressed — shutting down")
        _cookie_shutdown.set()
    except (KeyboardInterrupt, EOFError):
        _cookie_shutdown.set()

def cookie_daemon_main(COOKIE_PROFILE_IDS: list[str],
                       runs: int = 1,
                       weights: dict | None = None) -> None:
    """
    Persistent 24/7 cookie robot scheduler.
    Runs cookie sessions for each profile independently,
    scheduled across active hours with random buffers.
    """
    global _cookie_start_ts
    _cookie_start_ts = time.time()

    _install_cookie_signals()

    # Watchdog and keyboard listener for reliable Ctrl+C on Windows
    threading.Thread(target=_cookie_shutdown_watchdog,
                     daemon=True, name="cookie-watchdog").start()
    threading.Thread(target=_cookie_keyboard_listener,
                     daemon=True, name="cookie-keyboard").start()

    # Write PID file
    pid_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "cookie_daemon.pid")
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    log.info("=" * 60)
    log.info("[COOKIE DAEMON]  starting  |  profiles=%d  |  pid=%d",
             len(COOKIE_PROFILE_IDS), os.getpid())

    # ── Build initial heap ────────────────────────────────────────────────
    heap: list[tuple[float, str]] = []
    with _COOKIE_STATE_LOCK:
        state = _load_cookie_state()
        _cookie_prune_old_keys(state)
        now = time.time()
        for i, pid in enumerate(COOKIE_PROFILE_IDS):
            _ensure_profile_in_cookie_state(pid, state)
            stored_ts = state[pid].get("next_run_ts", 0.0)
            if stored_ts and stored_ts > now:
                ts = stored_ts
                log.info("[COOKIE DAEMON]  %s  scheduled: %s",
                         pid[:12],
                         datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"))
            else:
                # Stagger initial starts across first 60 minutes
                stagger = random.uniform(0, 60 * 60)
                ts = _cookie_clamp_to_active_hours(now + stagger)
                state[pid]["next_run_ts"] = ts
                log.info("[COOKIE DAEMON]  %s  initial schedule: %s  "
                         "(stagger=%.1f min)",
                         pid[:12],
                         datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),
                         stagger / 60)
            heapq.heappush(heap, (ts, pid))
        _save_cookie_state(state)

    log.info("[COOKIE DAEMON]  scheduler running")
    log.info("-" * 60)

    # ── Main scheduler loop ───────────────────────────────────────────────
    try:
        while not _cookie_shutdown.is_set():
            if not heap:
                log.error("[COOKIE DAEMON]  heap empty — no profiles")
                break

            next_ts, profile_id = heapq.heappop(heap)

            # Sleep until scheduled time
            wait_sec = next_ts - time.time()
            if wait_sec > 0:
                log.info("[COOKIE DAEMON]  sleeping %.1f min until %s for %s",
                         wait_sec / 60,
                         datetime.fromtimestamp(next_ts).strftime("%H:%M"),
                         profile_id[:12])
                if _cookie_shutdown.wait(timeout=wait_sec):
                    heapq.heappush(heap, (next_ts, profile_id))
                    break

            if _cookie_shutdown.is_set():
                heapq.heappush(heap, (next_ts, profile_id))
                break

            today_iso = date.today().isoformat()

            # ── Day-off check ─────────────────────────────────────────────
            with _COOKIE_STATE_LOCK:
                state = _load_cookie_state()
                _ensure_profile_in_cookie_state(profile_id, state)
                if _cookie_is_day_off(profile_id, state, today_iso):
                    log.info("[COOKIE DAEMON]  %s  day-off — skipping to tomorrow",
                             profile_id[:12])
                    next_ts = _cookie_clamp_to_active_hours(
                        time.time() + 86400 + random.uniform(0, 3600)
                    )
                    state[profile_id]["next_run_ts"] = next_ts
                    _save_cookie_state(state)
                    heapq.heappush(heap, (next_ts, profile_id))
                    continue

                target = _cookie_get_daily_target(profile_id, state, today_iso)
                done   = _cookie_get_daily_done(profile_id, state, today_iso)
                _save_cookie_state(state)

            # ── Daily target check ────────────────────────────────────────
            if done >= target:
                log.info("[COOKIE DAEMON]  %s  daily target reached (%d/%d)"
                         " — scheduling tomorrow",
                         profile_id[:12], done, target)
                with _COOKIE_STATE_LOCK:
                    state = _load_cookie_state()
                    next_ts = _cookie_clamp_to_active_hours(
                        time.time() + 86400 + random.uniform(0, 3600)
                    )
                    state[profile_id]["next_run_ts"] = next_ts
                    _save_cookie_state(state)
                heapq.heappush(heap, (next_ts, profile_id))
                continue

            # ── Active hours check ────────────────────────────────────────
            now_hour = datetime.now().hour
            start_h, end_h = COOKIE_DAEMON_ACTIVE_HOURS
            if not (start_h <= now_hour <= end_h):
                delayed_ts = _cookie_clamp_to_active_hours(time.time())
                log.info("[COOKIE DAEMON]  %s  outside active hours — "
                         "delaying to %s",
                         profile_id[:12],
                         datetime.fromtimestamp(delayed_ts).strftime("%H:%M"))
                with _COOKIE_STATE_LOCK:
                    state = _load_cookie_state()
                    state[profile_id]["next_run_ts"] = delayed_ts
                    _save_cookie_state(state)
                heapq.heappush(heap, (delayed_ts, profile_id))
                continue

            # ── Run session ───────────────────────────────────────────────
            log.info("[COOKIE DAEMON]  %s  session %d/%d",
                     profile_id[:12], done + 1, target)
            try:
                cookie_robot(profile_id, runs=runs)
                session_ok = True
            except Exception as exc:
                log.error("[COOKIE DAEMON]  %s  error: %s",
                          profile_id[:12], exc)
                session_ok = False

            # ── Post-session scheduling ───────────────────────────────────
            with _COOKIE_STATE_LOCK:
                state = _load_cookie_state()
                _ensure_profile_in_cookie_state(profile_id, state)
                if session_ok:
                    _cookie_increment_done(profile_id, state, today_iso)
                next_ts = _cookie_schedule_next(profile_id, time.time(), state)
                _save_cookie_state(state)

            heapq.heappush(heap, (next_ts, profile_id))
            log.info("[COOKIE DAEMON]  %s  next session: %s",
                     profile_id[:12],
                     datetime.fromtimestamp(next_ts).strftime("%Y-%m-%d %H:%M"))
            log.info("-" * 60)

    except KeyboardInterrupt:
        _cookie_shutdown.set()
        log.info("[COOKIE DAEMON]  interrupted")

    finally:
        # Save schedule for next startup
        with _COOKIE_STATE_LOCK:
            state = _load_cookie_state()
            for ts, pid in heap:
                state.setdefault(pid, {})["next_run_ts"] = ts
            _save_cookie_state(state)
        try:
            os.remove(pid_file)
        except OSError:
            pass
        log.info("[COOKIE DAEMON]  shutdown complete — schedule saved")
        log.info("=" * 60)

_CONSENT_SELECTORS = [
    # Generic patterns
    'button[id*="accept"]',
    'button[class*="accept"]',
    'a[id*="accept"]',
    '[aria-label*="Accept"]',
    '[aria-label*="accept"]',
    # Text-based (covers Guardian's "Yes, I accept")
    '//button[contains(normalize-space(text()), "Yes, I accept")]',
    '//button[contains(normalize-space(text()), "Accept all")]',
    '//button[contains(normalize-space(text()), "Accept cookies")]',
    '//button[contains(normalize-space(text()), "I agree")]',
    '//button[contains(normalize-space(text()), "Allow all")]',
    '//a[contains(normalize-space(text()), "Accept all")]',
]

def _dismiss_consent_banner(driver, timeout: float = 4.0) -> bool:
    """
    Attempt to find and click a cookie consent accept button.
    Returns True if a banner was dismissed, False if none found.
    Fails silently — a missed banner is not a fatal error.
    """
    try:
        for selector in _CONSENT_SELECTORS:
            try:
                # Determine if CSS or XPath
                if selector.startswith("//"):
                    by = By.XPATH
                else:
                    by = By.CSS_SELECTOR

                el = WebDriverWait(driver, 1.5).until(
                    EC.element_to_be_clickable((by, selector))
                )
                # Human-like: move to button then click
                precise_sleep(random.uniform(0.8, 2.5))
                bezier_move(driver, el)
                precise_sleep(random.uniform(0.3, 0.8))
                el.click()
                log.info("[COOKIE]  consent banner dismissed via: %s",
                         selector[:60])
                precise_sleep(random.uniform(0.5, 1.5))
                return True

            except (TimeoutException, NoSuchElementException,
                    WebDriverException):
                continue

    except Exception as exc:
        log.debug("[COOKIE]  consent check failed: %s", exc)

    return False

# ── Site pool ────────────────────────────────────────────────────────────────

COOKIE_SITE_POOL = {
    "news": [
        "https://www.bbc.com",
        "https://www.theguardian.com",
        "https://www.reuters.com",
        "https://www.apnews.com",
        "https://www.npr.org",
    ],
    "reference": [
        "https://www.wikipedia.org",
        "https://www.wikihow.com",
        "https://www.merriam-webster.com",
        "https://stackoverflow.com",
    ],
    "entertainment": [
        "https://www.imdb.com",
        "https://www.goodreads.com",
        "https://www.youtube.com",
    ],
    "lifestyle": [
        "https://www.weather.com",
        "https://www.allrecipes.com",
        "https://www.tripadvisor.com",
    ],
    "tech": [
        "https://news.ycombinator.com",
        "https://www.theverge.com",
        "https://techcrunch.com",
    ],
    "shopping": [
        "https://www.amazon.com",
        "https://www.etsy.com",
    ],
    "search": [
        "https://www.google.com",
        "https://www.bing.com",
    ],
}

DWELL_MIN           = 25
DWELL_MAX           = 90
INTERNAL_LINK_PROB  = 0.35
SITES_PER_RUN_MIN   = 5
SITES_PER_RUN_MAX   = 8
BETWEEN_SITES_MIN   = 8
BETWEEN_SITES_MAX   = 30


# ── Site selection ────────────────────────────────────────────────────────────

def _select_sites_for_run() -> list[str]:
    """
    Draw 1-2 sites from each category, shuffle, and return
    a list of SITES_PER_RUN_MIN to SITES_PER_RUN_MAX sites.
    Ensures categorical diversity on every run.
    """
    selected = []
    for category, sites in COOKIE_SITE_POOL.items():
        n = random.randint(1, min(2, len(sites)))
        selected.extend(random.sample(sites, n))

    random.shuffle(selected)
    target = random.randint(SITES_PER_RUN_MIN, SITES_PER_RUN_MAX)
    return selected[:target]


# ── Per-site behavior ─────────────────────────────────────────────────────────

def _visit_site(driver, url: str) -> bool:
    """
    Navigate to url, scroll realistically, optionally follow one
    internal link, then return True on success.
    """
    try:
        log.info("[COOKIE]  visiting %s", url)
        navigate_to(driver, url)

        landed = driver.current_url
        if not landed or landed in ("about:blank", "chrome://new-tab-page/"):
            log.warning("[COOKIE]  %s failed to load — skipping", url)
            return False

        # Check for challenge/captcha pages
        title = driver.title or ""
        if any(w in title.lower() for w in ("captcha", "verify", "robot", "blocked")):
            log.warning("[COOKIE]  %s returned challenge page — skipping", url)
            return False

        dwell = random.uniform(DWELL_MIN, DWELL_MAX)
        log.info("[COOKIE]  dwelling %.0fs on %s", dwell, url)

        _dismiss_consent_banner(driver)
        
        # Split dwell into scroll phase and optional internal link phase
        scroll_time = dwell * random.uniform(0.5, 0.75)
        stochastic_scroll(driver, total_seconds=scroll_time)

        # Occasionally follow one internal link
        if random.random() < INTERNAL_LINK_PROB:
            _follow_internal_link(driver, url)
            remaining = dwell - scroll_time
            if remaining > 5:
                stochastic_scroll(driver, total_seconds=remaining)
        else:
            remaining = dwell - scroll_time
            if remaining > 5:
                precise_sleep(random.uniform(remaining * 0.5, remaining))

        return True

    except (TimeoutException, WebDriverException) as exc:
        log.warning("[COOKIE]  %s failed: %s", url, exc)
        return False


def _follow_internal_link(driver, base_url: str) -> bool:
    """
    Find and click one internal link on the current page.
    Returns True if a link was followed.
    """
    try:
        # Collect visible internal links
        domain = base_url.split("/")[2]
        links = driver.find_elements(By.CSS_SELECTOR, "a[href]")
        internal = []
        for link in links:
            try:
                href = link.get_attribute("href") or ""
                if domain in href and href != driver.current_url:
                    if link.is_displayed():
                        internal.append(link)
            except Exception:
                continue

        if not internal:
            return False

        target = random.choice(internal[:20])  # limit to first 20 candidates
        log.info("[COOKIE]  following internal link on %s", domain)
        bezier_move(driver, target)
        precise_sleep(random.uniform(0.3, 0.8))
        target.click()
        precise_sleep(random.uniform(1.5, 3.5))
        return True

    except Exception as exc:
        log.debug("[COOKIE]  internal link follow failed: %s", exc)
        return False


# ── Run orchestration ─────────────────────────────────────────────────────────

def run_cookie_session(driver) -> dict:
    """
    Execute one full cookie robot session.
    Returns a summary dict with sites visited and results.
    """
    sites = _select_sites_for_run()
    log.info("[COOKIE]  session starting — %d sites selected", len(sites))
    log.info("[COOKIE]  sites: %s", [s.split("/")[2] for s in sites])

    results = {"visited": 0, "skipped": 0, "sites": []}

    for i, url in enumerate(sites):
        success = _visit_site(driver, url)
        if success:
            results["visited"] += 1
            results["sites"].append(url)
        else:
            results["skipped"] += 1

        # Pause between sites — except after the last one
        if i < len(sites) - 1:
            gap = random.uniform(BETWEEN_SITES_MIN, BETWEEN_SITES_MAX)
            log.info("[COOKIE]  pausing %.0fs before next site", gap)
            precise_sleep(gap)

    log.info(
        "[COOKIE]  session complete — visited=%d  skipped=%d",
        results["visited"], results["skipped"]
    )
    return results


def cookie_robot(profile_id: str, runs: int = 1) -> None:
    """
    Full pipeline: launch profile, run N cookie sessions, close profile.
    """
    driver   = None
    launched = False

    try:
        log.info("[COOKIE ROBOT]  profile=%s  runs=%d", profile_id[:12], runs)
        info     = start_profile(profile_id)
        launched = True
        driver   = connect_selenium(info["webSocketDebuggerUrl"])
        driver.set_page_load_timeout(25)
        init_cursor_pos(driver)

        for run_idx in range(runs):
            if runs > 1:
                log.info("[COOKIE ROBOT]  run %d/%d", run_idx + 1, runs)
            run_cookie_session(driver)

            # Between multiple runs: take a longer break
            if run_idx < runs - 1:
                gap = random.uniform(120, 300)
                log.info("[COOKIE ROBOT]  %.0f min break before next run",
                         gap / 60)
                precise_sleep(gap)

    except (TimeoutException, WebDriverException, RuntimeError) as exc:
        log.error("[COOKIE ROBOT]  error on %s: %s", profile_id[:12], exc)

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        if launched:
            stop_profile(profile_id)


def cookie_robot_attached(debugger_address: str,
                          profile_id: str = "manual",
                          runs: int = 1) -> None:
    """
    Run cookie robot on an already-open browser — no API calls made.
    """
    driver  = None
    address = debugger_address.replace("ws://", "").split("/")[0]
    ws_url  = f"ws://{address}"

    try:
        driver = connect_selenium(ws_url)
        driver.set_page_load_timeout(25)
        init_cursor_pos(driver)
        log.info("[COOKIE ROBOT]  attached  address=%s  runs=%d",
                 address, runs)

        for run_idx in range(runs):
            if runs > 1:
                log.info("[COOKIE ROBOT]  run %d/%d", run_idx + 1, runs)
            run_cookie_session(driver)

            if run_idx < runs - 1:
                gap = random.uniform(120, 300)
                log.info("[COOKIE ROBOT]  %.0f min break before next run",
                         gap / 60)
                precise_sleep(gap)

    except (TimeoutException, WebDriverException, RuntimeError) as exc:
        log.error("[COOKIE ROBOT]  error on attached %s: %s",
                  profile_id[:12], exc)

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cookie_robot",
        description="Pre-warm NstBrowser profiles with cross-domain cookies.",
        epilog="""
EXAMPLES
--------
  python cookie_robot.py --profile 251894f1-...
  python cookie_robot.py --profile 251894f1-... --runs 3
  python cookie_robot.py --attach 127.0.0.1:9222
  python cookie_robot.py --all-profiles
        """,
    )
    
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--profile",
        metavar="PROFILE_ID",
        help="UUID of the NstBrowser profile to warm.",
    )
    mode.add_argument(
        "--attach",
        metavar="HOST:PORT",
        help="CDP address of an already-open browser.",
    )
    mode.add_argument(
        "--all-profiles",
        action="store_true",
        help="Run cookie robot sequentially on all profiles in COOKIE_PROFILE_IDS.",
    )
    p.add_argument(
        "--daemon",
        action="store_true",
        help=(
            "Run as a persistent 24/7 scheduler. Manages all specified "
            "profiles independently with %d-%d sessions per day and "
            "%.0f%% day-off probability."
            % (COOKIE_SESSIONS_PER_DAY_MIN, COOKIE_SESSIONS_PER_DAY_MAX,
            COOKIE_DAY_OFF_PROB * 100)
        ),
    )    
    p.add_argument(
        "--runs",
        type=int,
        default=1,
        metavar="N",
        help="Number of browsing sessions per profile (default: 1).",
    )
    p.add_argument(
        "--label",
        metavar="NAME",
        default=None,
        help="Label for logs when using --attach.",
    )
    return p


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args = _build_parser().parse_args()

    if args.daemon:
        if args.attach:
            log.error("--daemon cannot be combined with --attach")
            return
        # Determine which profiles to manage
        if args.all_profiles:
            profile_ids = COOKIE_PROFILE_IDS
        elif args.profile:
            profile_ids = args.profile  # list after nargs="+"
        else:
            log.error("--daemon requires --profile or --all-profiles")
            return
        try:
            cookie_daemon_main(profile_ids, runs=args.runs)
        except KeyboardInterrupt:
            log.info("[COOKIE DAEMON]  KeyboardInterrupt")
        return

    if args.attach:
        label = args.label or args.attach
        cookie_robot_attached(args.attach,
                              profile_id=label,
                              runs=args.runs)

    elif args.all_profiles:
        log.info("[COOKIE ROBOT]  running all %d profiles", len(COOKIE_PROFILE_IDS))
        for i, pid in enumerate(COOKIE_PROFILE_IDS):
            log.info("[COOKIE ROBOT]  profile %d/%d: %s",
                     i + 1, len(COOKIE_PROFILE_IDS), pid[:12])
            cookie_robot(pid, runs=args.runs)
            if i < len(COOKIE_PROFILE_IDS) - 1:
                gap = random.uniform(60, 180)
                log.info("[COOKIE ROBOT]  %.0f min before next profile",
                         gap / 60)
                precise_sleep(gap)

    else:
        cookie_robot(args.profile, runs=args.runs)

    log.info("[COOKIE ROBOT]  done.")


if __name__ == "__main__":
    main()
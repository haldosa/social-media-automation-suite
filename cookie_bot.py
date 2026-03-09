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

import random
import time
import argparse
import logging
from datetime import datetime
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, WebDriverException, NoSuchElementException
)

from config import PROFILE_IDS, SCREENSHOT_DIR
from utils import log, precise_sleep
from api import start_profile, stop_profile
from browser import connect_selenium
from mouse import bezier_move_to_coords, bezier_move, init_cursor_pos
from scroll import stochastic_scroll, navigate_to

# In cookie_robot.py

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
        help="Run cookie robot sequentially on all profiles in PROFILE_IDS.",
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

    if args.attach:
        label = args.label or args.attach
        cookie_robot_attached(args.attach,
                              profile_id=label,
                              runs=args.runs)

    elif args.all_profiles:
        log.info("[COOKIE ROBOT]  running all %d profiles", len(PROFILE_IDS))
        for i, pid in enumerate(PROFILE_IDS):
            log.info("[COOKIE ROBOT]  profile %d/%d: %s",
                     i + 1, len(PROFILE_IDS), pid[:12])
            cookie_robot(pid, runs=args.runs)
            if i < len(PROFILE_IDS) - 1:
                gap = random.uniform(60, 180)
                log.info("[COOKIE ROBOT]  %.0f min before next profile",
                         gap / 60)
                precise_sleep(gap)

    else:
        cookie_robot(args.profile, runs=args.runs)

    log.info("[COOKIE ROBOT]  done.")


if __name__ == "__main__":
    main()
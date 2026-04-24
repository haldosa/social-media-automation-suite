"""
cookie_bot.py
==============
Pre-warm Chrome profiles by accumulating cross-domain cookies,
browsing history, and cached assets before Threads sessions.

Usage:
    python cookie_bot.py --profile PROFILE_ID
    python cookie_bot.py --profile PROFILE_ID --runs 3
    python cookie_bot.py --attach 127.0.0.1:9222
    python cookie_bot.py --all-profiles
"""

import random
import argparse
import logging
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, WebDriverException

from config import COOKIE_PROFILE_IDS
from utils import log, precise_sleep
from api import start_profile, stop_profile
from browser import connect_selenium
from mouse import bezier_move, init_cursor_pos
from scroll import stochastic_scroll, navigate_to
from pools import COOKIE_SITE_POOL


DWELL_MIN = 25
DWELL_MAX = 90
INTERNAL_LINK_PROB = 0.35
SITES_PER_RUN_MIN = 5
SITES_PER_RUN_MAX = 8
BETWEEN_SITES_MIN = 8
BETWEEN_SITES_MAX = 30


def _select_sites_for_run() -> list[str]:
    """
    Draw 1-2 sites from each category, shuffle, and return
    a list of SITES_PER_RUN_MIN to SITES_PER_RUN_MAX sites.
    """
    selected = []
    for _, sites in COOKIE_SITE_POOL.items():
        if not sites:
            continue
        n = random.randint(1, min(2, len(sites)))
        selected.extend(random.sample(sites, n))
    random.shuffle(selected)
    target = random.randint(SITES_PER_RUN_MIN, SITES_PER_RUN_MAX)
    return selected[:target]


def _follow_internal_link(driver, base_url: str) -> bool:
    try:
        domain = base_url.split("/")[2]
        links = driver.find_elements(By.CSS_SELECTOR, "a[href]")
        internal = []
        for link in links:
            try:
                href = link.get_attribute("href") or ""
                if domain in href and href != driver.current_url and link.is_displayed():
                    internal.append(link)
            except Exception:
                continue

        if not internal:
            return False

        target = random.choice(internal[:20])
        log.info("[COOKIE] following internal link on %s", domain)
        bezier_move(driver, target)
        precise_sleep(random.uniform(0.3, 0.8))
        target.click()
        precise_sleep(random.uniform(1.5, 3.5))
        return True

    except Exception as exc:
        log.debug("[COOKIE] internal link follow failed: %s", exc)
        return False


def _visit_site(driver, url: str) -> bool:
    try:
        log.info("[COOKIE] visiting %s", url)
        navigate_to(driver, url)

        landed = driver.current_url
        if not landed or landed in ("about:blank", "chrome://new-tab-page/"):
            log.warning("[COOKIE] %s failed to load - skipping", url)
            return False

        title = (driver.title or "").lower()
        if any(w in title for w in ("captcha", "verify", "robot", "blocked")):
            log.warning("[COOKIE] %s returned challenge page - skipping", url)
            return False

        dwell = random.uniform(DWELL_MIN, DWELL_MAX)
        log.info("[COOKIE] dwelling %.0fs on %s", dwell, url)

        scroll_time = dwell * random.uniform(0.5, 0.75)
        stochastic_scroll(driver, total_seconds=scroll_time)

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
        log.warning("[COOKIE] %s failed: %s", url, exc)
        return False


def run_cookie_session(driver) -> dict:
    sites = _select_sites_for_run()
    log.info("[COOKIE] session starting - %d sites selected", len(sites))
    log.info("[COOKIE] sites: %s", [s.split("/")[2] for s in sites])

    results = {"visited": 0, "skipped": 0, "sites": []}

    for i, url in enumerate(sites):
        success = _visit_site(driver, url)
        if success:
            results["visited"] += 1
            results["sites"].append(url)
        else:
            results["skipped"] += 1

        if i < len(sites) - 1:
            gap = random.uniform(BETWEEN_SITES_MIN, BETWEEN_SITES_MAX)
            log.info("[COOKIE] pausing %.0fs before next site", gap)
            precise_sleep(gap)

    log.info(
        "[COOKIE] session complete - visited=%d skipped=%d",
        results["visited"],
        results["skipped"],
    )
    return results


def cookie_robot(profile_id: str, runs: int = 1) -> None:
    driver = None
    launched = False

    try:
        log.info("[COOKIE ROBOT] profile=%s runs=%d", profile_id[:12], runs)
        info = start_profile(profile_id)
        launched = True
        driver = connect_selenium(info["webSocketDebuggerUrl"])
        driver.set_page_load_timeout(25)
        init_cursor_pos(driver)

        for run_idx in range(runs):
            if runs > 1:
                log.info("[COOKIE ROBOT] run %d/%d", run_idx + 1, runs)
            run_cookie_session(driver)

            if run_idx < runs - 1:
                gap = random.uniform(120, 300)
                log.info("[COOKIE ROBOT] %.0f min break before next run", gap / 60)
                precise_sleep(gap)

    except (TimeoutException, WebDriverException, RuntimeError) as exc:
        log.error("[COOKIE ROBOT] error on %s: %s", profile_id[:12], exc)

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        if launched:
            stop_profile(profile_id)


def cookie_robot_attached(debugger_address: str, profile_id: str = "manual", runs: int = 1) -> None:
    driver = None
    address = debugger_address.replace("ws://", "").split("/")[0]
    ws_url = f"ws://{address}"

    try:
        driver = connect_selenium(ws_url)
        driver.set_page_load_timeout(25)
        init_cursor_pos(driver)
        log.info("[COOKIE ROBOT] attached address=%s runs=%d", address, runs)

        for run_idx in range(runs):
            if runs > 1:
                log.info("[COOKIE ROBOT] run %d/%d", run_idx + 1, runs)
            run_cookie_session(driver)

            if run_idx < runs - 1:
                gap = random.uniform(120, 300)
                log.info("[COOKIE ROBOT] %.0f min break before next run", gap / 60)
                precise_sleep(gap)

    except (TimeoutException, WebDriverException, RuntimeError) as exc:
        log.error("[COOKIE ROBOT] error on attached %s: %s", profile_id[:12], exc)

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cookie_robot",
        description="Pre-warm Chrome profiles with cross-domain cookies.",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--profile", metavar="PROFILE_ID", help="ID of the configured Chrome profile.")
    mode.add_argument("--attach", metavar="HOST:PORT", help="CDP address of an already-open browser.")
    mode.add_argument("--all-profiles", action="store_true", help="Run on all COOKIE_PROFILE_IDS.")
    p.add_argument("--runs", type=int, default=1, metavar="N", help="Number of sessions per profile.")
    p.add_argument("--label", metavar="NAME", default=None, help="Label for logs in attach mode.")
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
        cookie_robot_attached(args.attach, profile_id=label, runs=args.runs)
    elif args.all_profiles:
        if not COOKIE_PROFILE_IDS:
            log.error("COOKIE_PROFILE_IDS is empty.")
            return
        log.info("[COOKIE ROBOT] running all %d profiles", len(COOKIE_PROFILE_IDS))
        for i, pid in enumerate(COOKIE_PROFILE_IDS):
            log.info("[COOKIE ROBOT] profile %d/%d: %s", i + 1, len(COOKIE_PROFILE_IDS), pid[:12])
            cookie_robot(pid, runs=args.runs)
            if i < len(COOKIE_PROFILE_IDS) - 1:
                gap = random.uniform(60, 180)
                log.info("[COOKIE ROBOT] %.0f min before next profile", gap / 60)
                precise_sleep(gap)
    else:
        cookie_robot(args.profile, runs=args.runs)

    log.info("[COOKIE ROBOT] done.")


if __name__ == "__main__":
    main()

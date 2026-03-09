"""
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
import textwrap
import argparse
from datetime import datetime, timedelta
from selenium.common.exceptions import WebDriverException, TimeoutException
from config import (
    PROFILE_IDS, TARGET_SOCIAL_URL,
    ACTIVE_HOURS_RANGE, INACTIVE_DAY_PROB,
    BUFFER_MIN_MIN, BUFFER_MAX_MIN,
    BUFFER_LONG_PROB, BUFFER_LONG_MIN, BUFFER_LONG_MAX,
)
from utils import log, cleanup_screenshots, precise_sleep
from api import get_running_browsers, _resolve_attached_address, start_profile, stop_profile
from browser import connect_selenium
from preflight import run_preflight
from scroll import navigate_to
from actions import check_login_status
from session import (
    warm_profile, warm_profile_attached,
    run_social_session, _sample_session_duration_sec
)
from daemon import daemon_main
from diagnostics import run_test_actions
from mouse import init_cursor_pos

cleanup_screenshots()

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

    p.add_argument(
        "--daemon",
        action="store_true",
        help=(
            "Run as a persistent 24/7 daemon.  Each profile is scheduled "
            "independently with 1\u20133 sessions per day and a 10%% chance of "
            "day-off.  The process runs until SIGINT/SIGTERM."
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

    # Build per-action weight overrides dict ,  only keys explicitly passed by
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
    #  DAEMON MODE  —  persistent 24/7 scheduler
    # ---------------------------------------------------------------- #
    if getattr(args, 'daemon', False):
        if args.attach or args.attach_profile:
            log.error("--daemon cannot be combined with --attach / --attach-profile")
            return
        if args.test_actions:
            log.error("--daemon cannot be combined with --test-actions")
            return
        daemon_main(weights=weights or None)
        return

    # ---------------------------------------------------------------- #
    #  ATTACH MODE  ,  reuse already-open browser, no daily open consumed
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
    #  NORMAL MODE  ,  open every profile via the API
    # ---------------------------------------------------------------- #

    # --test-actions in normal mode: skip inactive-day / time-of-day guards,
    # open the first profile via the API, run the diagnostic, then close it.
    if args.test_actions:
        pid = PROFILE_IDS[0] if PROFILE_IDS else None
        if not pid:
            log.error("No profiles configured in PROFILE_IDS ,  cannot run test-actions.")
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

    # Inactive-day simulation ,  models days when a real user never opens Threads.
    if random.random() < INACTIVE_DAY_PROB:
        log.info(
            "Simulating inactive day (%.0f%% probability) ,  no profiles run today.",
            INACTIVE_DAY_PROB * 100,
        )
        return

    # Time-of-day scheduling guard — wait until active hours instead of exiting.
    while True:
        _now = datetime.now()
        _now_hour = _now.hour
        if ACTIVE_HOURS_RANGE[0] <= _now_hour <= ACTIVE_HOURS_RANGE[1]:
            break  # within active window — proceed normally
        # Calculate seconds until the next active-window start.
        _start_hour = ACTIVE_HOURS_RANGE[0]
        _next_active = _now.replace(hour=_start_hour, minute=0, second=0, microsecond=0)
        if _next_active <= _now:
            # Already past today's window start time (i.e. we're after end hour),
            # so target tomorrow's start.
            _next_active += timedelta(days=1)
        _wait_sec = (_next_active - _now).total_seconds()
        log.info(
            "Outside active hours (%02d:00\u2013%02d:00 local) — sleeping %.0f min until %s.",
            ACTIVE_HOURS_RANGE[0], ACTIVE_HOURS_RANGE[1],
            _wait_sec / 60, _next_active.strftime("%H:%M"),
        )
        time.sleep(min(_wait_sec, 60))  # re-check every minute to handle clock drift

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
                log.info("Profile failed before reaching Threads ,  skipping inter-profile buffer.")

    log.info("=" * 60)
    log.info("All profiles warmed. Done.")


if __name__ == "__main__":
    main()
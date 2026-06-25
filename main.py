"""
Requirements:
    pip install selenium requests webdriver-manager pyautogui pyperclip Pillow

Setup:
    1. Install and launch the NstBrowser desktop app.
    2. Configure the API key and profile IDs in the Flask UI or .env.
    3. Run: python main.py
"""

import argparse
import random
import textwrap
import time
from datetime import datetime, timedelta

from selenium.common.exceptions import TimeoutException, WebDriverException

from actions import check_login_status
from api import get_running_browsers, _resolve_attached_address, start_profile, stop_profile
from browser import connect_selenium
from config import (
    ACTIVE_HOURS_RANGE,
    BUFFER_LONG_MAX,
    BUFFER_LONG_MIN,
    BUFFER_LONG_PROB,
    BUFFER_MAX_MIN,
    BUFFER_MIN_MIN,
    INACTIVE_DAY_PROB,
    PROFILE_IDS,
    TARGET_SOCIAL_URL,
)
from daemon import daemon_main
from diagnostics import run_test_actions
from mouse import init_cursor_pos
from reporting import append_run, encode_json, iso_now, make_run_id
from scroll import navigate_to
from session import warm_profile, warm_profile_attached
from utils import cleanup_screenshots, log, precise_sleep

cleanup_screenshots()

_WEIGHT_ARG_TO_KWARG = {
    "like": "w_like",
    "notify": "w_notify",
    "profile": "w_profile",
    "read_post": "w_read",
    "comment": "w_comment",
    "follow": "w_follow",
    "scroll": "w_top",
    "search": "w_search",
    "post": "w_post",
}


def _weight_value(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("weight must be a number from 0.0 to 1.0") from exc
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError("weight must be between 0.0 and 1.0")
    return value


def _build_weight_kwargs(args: argparse.Namespace) -> dict:
    weights = {}
    for arg_name, kwarg_name in _WEIGHT_ARG_TO_KWARG.items():
        value = getattr(args, arg_name)
        if value is not None:
            weights[kwarg_name] = value
    return weights


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="profile_operations",
        description="Authorized creator and business profile operations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            MODES
            -----
            Normal (no flags)
              Opens every profile in PROFILE_IDS via the NstBrowser API, runs a
              configured profile-operations session for each, then closes them.
            """
        ),
    )
    p.add_argument(
        "--profile-id",
        metavar="PROFILE_ID",
        default=None,
        help=(
            "UUID of the profile to run. If the profile is already open in "
            "NstBrowser, attaches to it automatically. If not, launches it via the API."
        ),
    )
    p.add_argument(
        "--label",
        metavar="NAME",
        default=None,
        help="Human-readable label for logs when using an attached profile.",
    )
    p.add_argument(
        "--no-preflight",
        action="store_true",
        help="Skip the Wikipedia pre-flight.",
    )
    p.add_argument(
        "--close",
        action="store_true",
        help="Quit an attached browser after the session. Default: leave it open.",
    )

    wg = p.add_argument_group(
        "action weights",
        "Override individual session action weights. Use 0.0-1.0; omit a flag "
        "to keep the built-in default.",
    )
    wg.add_argument("--like", metavar="WEIGHT", type=_weight_value, default=None)
    wg.add_argument("--notify", metavar="WEIGHT", type=_weight_value, default=None)
    wg.add_argument("--profile", metavar="WEIGHT", type=_weight_value, default=None)
    wg.add_argument("--read-post", metavar="WEIGHT", type=_weight_value, default=None, dest="read_post")
    wg.add_argument("--comment", metavar="WEIGHT", type=_weight_value, default=None)
    wg.add_argument("--follow", metavar="WEIGHT", type=_weight_value, default=None)
    wg.add_argument("--scroll", metavar="WEIGHT", type=_weight_value, default=None)
    wg.add_argument("--search", metavar="WEIGHT", type=_weight_value, default=None)
    wg.add_argument("--post", metavar="WEIGHT", type=_weight_value, default=None)

    p.add_argument(
        "--test-actions",
        nargs="?",
        const="__all__",
        default=None,
        metavar="ACTION",
        help=(
            "Run diagnostic actions. Without a value, executes every action once. "
            "With an action name, runs only that action."
        ),
    )
    p.add_argument(
        "--daemon",
        action="store_true",
        help="Run as a persistent daemon until SIGINT/SIGTERM.",
    )
    return p


def _run_test_actions(args: argparse.Namespace, profile_ids_to_run: list[str], run_id: str) -> None:
    pid = profile_ids_to_run[0] if profile_ids_to_run else None
    if not pid:
        log.error("No profiles configured -- cannot run test-actions.")
        return

    log.info("--test-actions: testing profile %s", pid)
    driver = None
    launched = False
    try:
        info = start_profile(pid)
        launched = True
        driver = connect_selenium(info["webSocketDebuggerUrl"])
        driver.set_page_load_timeout(30)
        init_cursor_pos(driver)
        log.info("Navigating to %s", TARGET_SOCIAL_URL)
        navigate_to(driver, TARGET_SOCIAL_URL)
        precise_sleep(random.uniform(2, 5))
        if not check_login_status(driver):
            log.error("Profile '%s' appears logged out -- cannot run test-actions.", pid)
            return
        action_filter = args.test_actions if args.test_actions != "__all__" else None
        run_test_actions(driver, profile_id=pid, filter_action=action_filter, run_id=run_id)
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


def _wait_for_active_hours() -> None:
    while True:
        now = datetime.now()
        if ACTIVE_HOURS_RANGE[0] <= now.hour <= ACTIVE_HOURS_RANGE[1]:
            return
        start_hour = ACTIVE_HOURS_RANGE[0]
        next_active = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        if next_active <= now:
            next_active += timedelta(days=1)
        wait_sec = (next_active - now).total_seconds()
        log.info(
            "Outside active hours (%02d:00-%02d:00 local) -- sleeping %.0f min until %s.",
            ACTIVE_HOURS_RANGE[0],
            ACTIVE_HOURS_RANGE[1],
            wait_sec / 60,
            next_active.strftime("%H:%M"),
        )
        time.sleep(min(wait_sec, 60))


def main() -> None:
    args = _build_parser().parse_args()
    weights = _build_weight_kwargs(args)
    process_type = "daemon" if args.daemon else "test_actions" if args.test_actions else "session"
    run_id = make_run_id(process_type)
    run_started_at = iso_now()
    run_status = "completed"

    log.info("=" * 60)
    log.info("Profile Operations (API v2) -- %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("Run ID: %s", run_id)
    if weights:
        log.info("Action weight overrides: %s", weights)

    profile_ids_to_run = [args.profile_id] if args.profile_id else list(PROFILE_IDS)

    try:
        if args.daemon:
            if args.test_actions:
                log.error("--daemon cannot be combined with --test-actions")
                run_status = "failed"
                return
            daemon_main(weights=weights, skip_preflight=args.no_preflight, run_id=run_id)
            return

        if args.test_actions:
            _run_test_actions(args, profile_ids_to_run, run_id)
            return

        if random.random() < INACTIVE_DAY_PROB:
            log.info(
                "Simulating inactive day (%.0f%% probability) -- no profiles run today.",
                INACTIVE_DAY_PROB * 100,
            )
            run_status = "skipped"
            return

        _wait_for_active_hours()

        log.info("Target: %s  |  Profiles: %d", TARGET_SOCIAL_URL, len(profile_ids_to_run))
        profile_order = profile_ids_to_run.copy()
        random.shuffle(profile_order)
        log.info("Execution order: %s", profile_order)

        running_ids = set()
        try:
            running_browsers = get_running_browsers()
            running_ids = {b.get("id") or b.get("profileId") for b in running_browsers}
            if running_browsers:
                log.info("Already running at startup: %s", [b.get("profileId") for b in running_browsers])
        except Exception as exc:
            log.warning("[MAIN]  could not query running browsers: %s", exc)

        for idx, pid in enumerate(profile_order):
            log.info("-" * 60)
            log.info("[%d/%d] Starting: %s", idx + 1, len(profile_order), pid)

            if pid in running_ids:
                log.info("[MAIN]  %s is already open -- attaching", pid[:12])
                try:
                    address = _resolve_attached_address(pid)
                    ran_session = warm_profile_attached(
                        debugger_address=address,
                        profile_id=pid,
                        skip_preflight=args.no_preflight,
                        close_after=args.close,
                        weights=weights,
                        run_id=run_id,
                    )
                except Exception as exc:
                    log.error("[MAIN]  attach failed for %s: %s", pid[:12], exc)
                    ran_session = False
            else:
                ran_session = warm_profile(
                    pid,
                    skip_preflight=args.no_preflight,
                    weights=weights,
                    run_id=run_id,
                )

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
                    log.info("Profile failed -- skipping inter-profile buffer.")

        log.info("=" * 60)
        log.info("All profile operations completed.")
    except Exception:
        run_status = "failed"
        raise
    finally:
        if not args.daemon:
            try:
                append_run({
                    "run_id": run_id,
                    "process_type": process_type,
                    "started_at": run_started_at,
                    "ended_at": iso_now(),
                    "status": run_status,
                    "profiles_requested": ";".join(profile_ids_to_run),
                    "target_url": TARGET_SOCIAL_URL,
                    "skip_preflight": args.no_preflight,
                    "weight_overrides_json": encode_json(weights),
                })
            except Exception as exc:
                log.warning("[REPORT]  run CSV write failed: %s", exc)


if __name__ == "__main__":
    main()

import time
import logging
import random
import unittest.mock
import posting
from datetime import date, datetime, timedelta
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from config import PROFILE_IDS, TARGET_SOCIAL_URL
from utils import log, precise_sleep
from mouse import init_cursor_pos, debug_cursor_state
from scroll import stochastic_scroll, navigate_to
from actions import (
    passive_action, active_action, read_post_action,
    comment_on_post, check_notifications_action,
    visit_search_action, return_to_top_action,
    view_profile_from_feed, follow_from_feed,
    click_home_button,
)
from reporting import append_diagnostic, iso_now, make_run_id
from session import _get_typing_dna, _get_ctx
from posting import create_post
from pools import get_profile_content
from state import _can_post_now, _record_post

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


def _record_diagnostic_row(row: dict, tlog: logging.Logger) -> None:
    try:
        append_diagnostic(row)
    except Exception as exc:
        tlog.warning("[REPORT]  diagnostic CSV write failed: %s", exc)

def run_test_actions(
    driver,
    profile_id: str | None = None,
    filter_action: str | None = None,
    run_id: str | None = None,
) -> None:
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

    run_id = run_id or make_run_id("diagnostic")
    tlog = _setup_test_logger()
    if not profile_id or profile_id == "test":
        profile_id = PROFILE_IDS[0] if PROFILE_IDS else "test"
        tlog.info("Using configured profile for diagnostics: %s", profile_id)

    # Load the per-profile pacing configuration used by publishing diagnostics.
    ctx = _get_ctx()
    ctx.active_typing_dna = _get_typing_dna(profile_id)
    profile_content = get_profile_content(profile_id)
    ctx.profile_content_loaded = True
    ctx.profile_content_id = profile_id
    ctx.profile_approved_reply_pool = profile_content["approved_replies"]
    ctx.profile_approved_caption_pool = profile_content["approved_captions"]
    ctx.profile_approved_media_pool = profile_content["approved_media"]
    ctx.profile_search_topic_pool = profile_content["search_topics"]

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
        fake_first_seen = (date.today() - timedelta(days=8)).isoformat()

        # Patch _can_post_now to always return True
        def _always_allow(profile_id, state):
            return True

        # Patch _record_post to do nothing
        def _noop_record(pid, state):
            tlog.info("[TEST]  _record_post SKIPPED — test post not recorded")

        with unittest.mock.patch.object(posting, '_can_post_now', _always_allow), \
            unittest.mock.patch.object(posting, '_record_post', _noop_record):
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
            tlog.error("BROWSER UNRESPONSIVE ,  aborting test run at action %d/%d (%s)", idx, total, name)
            # Record remaining actions as ERROR
            for j in range(idx, total + 1):
                remaining_name = actions[j - 1][0] if j <= total else "?"
                row = {
                    "index": j, "name": remaining_name,
                    "status": "ERROR", "duration_ms": 0,
                    "note": "BROWSER UNRESPONSIVE ,  aborted",
                }
                results.append(row)
                _record_diagnostic_row({
                    "run_id": run_id,
                    "profile_id": profile_id,
                    "action": remaining_name,
                    "started_at": iso_now(),
                    "ended_at": iso_now(),
                    "duration_ms": 0,
                    "status": "ERROR",
                    "note": row["note"],
                    "health_ok": False,
                    "cursor_drift": "",
                }, tlog)
            break

        header = f"[TEST {idx}/{total}] {name}"
        tlog.info("")
        tlog.info("-" * 60)
        tlog.info("%s", header)
        tlog.info("-" * 60)

        diag_started_at = iso_now()
        t0 = time.perf_counter()
        status = "PASS"
        note = ""
        return_value = None
        cursor_drift = False
        health_ok = ""

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
            cursor_drift = True
            cursor_drift_actions.append(name)
            tlog.warning("[CURSOR DRIFT]  cursor at (0,0) after action %s", name)

        # ── DOM health check ──────────────────────────────────────────────
        if _browser_is_alive(driver):
            healthy = _dom_health_check(driver, tlog)
            health_ok = healthy
            if not healthy:
                tlog.warning("[HEALTH FAIL]  after action %s ,  continuing to next action", name)
        else:
            health_ok = False
            tlog.error("[HEALTH FAIL]  browser unresponsive after action %s", name)

        _record_diagnostic_row({
            "run_id": run_id,
            "profile_id": profile_id,
            "action": name,
            "started_at": diag_started_at,
            "ended_at": iso_now(),
            "duration_ms": round(elapsed_ms),
            "status": status,
            "note": note[:240],
            "health_ok": health_ok,
            "cursor_drift": cursor_drift,
        }, tlog)

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
    # Also push to the main profile-operations log for the audit trail.
    log.info(report)

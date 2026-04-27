import os
import time
import logging
import logging.handlers
import contextvars
import dataclasses
import threading
from selenium.webdriver.common.by import By
from config import SCREENSHOT_DIR, LOG_FILE, MOUSE_LOG_FILE, _SCRIPT_DIR
# ── DEBUG LOGGING: session metrics accumulator ───────────────────────────────
# Reset by run_social_session() at the start of every session.
# Default values kept here for reference only; live state lives in

logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("selenium.webdriver.remote.remote_connection").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

# UTF-8 safe logging ,  prevents cp1252 crash on Windows terminals
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
# Main log file captures everything down to DEBUG; console stays at INFO.
_file_h.setLevel(logging.DEBUG)
_stream_h.setLevel(logging.INFO)
logging.basicConfig(level=logging.DEBUG, handlers=[_file_h, _stream_h])
# Suppress noisy HTTP debug lines from urllib3 / selenium remote connection


# Dedicated mouse-movement logger ,  writes to its own file at DEBUG level.
# Arc summaries are always written; per-step positions only when MOUSE_TRACE=True.
_mouse_fh = logging.FileHandler(MOUSE_LOG_FILE, encoding="utf-8")
_mouse_fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
_mlog = logging.getLogger("mouse")
_mlog.setLevel(logging.DEBUG)
_mlog.addHandler(_mouse_fh)
_mlog.propagate = False  # keep mouse events out of the main log

# ── DEBUG LOGGING: audit logger ,  writes into the same log file as `log` ─────
# All [TIMING], [ELEMENT], [MOUSE ARC], [CLICK], [CURSOR MOVE] etc. go to
# nstbrowser_warmer.log at DEBUG level.  The console handler is not attached
# so verbose debug lines don't clutter the terminal.
_dlog = logging.getLogger("audit")
_dlog.setLevel(logging.DEBUG)
_dlog.addHandler(_file_h)     # same file handler as main log
_dlog.propagate = False       # prevent double-printing via root logger
# ─────────────────────────────────────────────────────────────────────────────#

# ── Per-profile log routing via contextvars ──────────────────────────────────
# When a session sets _active_profile_id, the custom filter duplicates records
# to the corresponding per-profile RotatingFileHandler stored in _profile_log_handlers.
_active_profile_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_active_profile_id", default=""
)
_profile_log_handlers: dict[str, logging.Handler] = {}
_PROFILE_LOGS_DIR = os.path.join(_SCRIPT_DIR, "logs")


class _ProfileLogFilter(logging.Filter):
    """Duplicate matching log records to the active profile's RotatingFileHandler."""

    def filter(self, record: logging.LogRecord) -> bool:
        pid = _active_profile_id.get("")
        if pid and pid in _profile_log_handlers:
            _profile_log_handlers[pid].handle(record)
        return True  # always pass through to the global handler


def _ensure_profile_logger(profile_id: str) -> None:
    """Create (once) a RotatingFileHandler for *profile_id* in logs/."""
    if profile_id in _profile_log_handlers or not profile_id:
        return
    os.makedirs(_PROFILE_LOGS_DIR, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(_PROFILE_LOGS_DIR, f"{profile_id}.log"),
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_fmt)
    _profile_log_handlers[profile_id] = fh

# Attach the filter to the root logger so *all* records are evaluated.
# The filter does not block records — it only duplicates them to the
# per-profile handler when the contextvar is set.
logging.getLogger().addFilter(_ProfileLogFilter())
# ─────────────────────────────────────────────────────────────────────────────#
# utils.py
def cleanup_screenshots(max_age_days: int = 7) -> None:
    """Remove error screenshots older than max_age_days at process start."""
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    try:
        for name in os.listdir(SCREENSHOT_DIR):
            path = os.path.join(SCREENSHOT_DIR, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    except OSError:
        pass
    if removed:
        log.info("[STARTUP]  screenshot cleanup: removed %d files older than %d days",
                 removed, max_age_days)
# SessionContext (see _get_ctx() below).
_SESSION_METRICS_DEFAULTS: dict = {
    "actions_dispatched": 0,
    "likes":    0,
    "comments": 0,
    "follows":  0,
    "posts":    0,
    "passive":  0,
    "reads":    0,
    "profile_visits": 0,
    "searches": 0,
    "last_action": None,          # used for consecutive-action RISK WARN
    "consecutive_same": 0,
}
# ─────────────────────────────────────────────────────────────────────────────#

# ── Per-session mutable state: thread-local SessionContext ───────────────────
# All state that was previously scattered across module-level globals is now
# encapsulated here.  threading.local() ensures that concurrent sessions
# running in separate threads each have their own independent copy.
@dataclasses.dataclass
class SessionContext:
    """Isolated mutable state for a single social session."""
    cursor_pos: list          = dataclasses.field(default_factory=lambda: [0, 0])
    last_bezier_end_ts: float = 0.0
    cdp_consecutive_failures: int = 0
    session_followed: set     = dataclasses.field(default_factory=set)
    session_metrics: dict     = dataclasses.field(
        default_factory=lambda: dict(_SESSION_METRICS_DEFAULTS)
    )
    active_typing_dna: dict   = dataclasses.field(default_factory=dict)
    # Per-profile isolated content pools — populated by run_social_session().
    # Empty list means "not yet assigned"; callers fall back to the global pool.
    profile_comment_pool: list = dataclasses.field(default_factory=list)
    profile_caption_pool: list = dataclasses.field(default_factory=list)
    profile_search_topic_pool: list = dataclasses.field(default_factory=list)

_session_local = threading.local()

def _get_ctx() -> SessionContext:
    """Return the SessionContext for the current thread, creating one if needed."""
    ctx = getattr(_session_local, 'ctx', None)
    if ctx is None:
        _session_local.ctx = SessionContext()
    return _session_local.ctx
# ─────────────────────────────────────────────────────────────────────────────#


# ── DEBUG LOGGING: timing check helper ───────────────────────────────────────
def _timing_check(context: str, sampled_s: float,
                  expected_min_s: float, expected_max_s: float) -> float:
    """Log a timing sample to nstbrowser_warmer.log; emit WARN if outside expected range.

    Uses a z-score approximation: if sampled_s is more than 2 SD outside the
    expected range (treating the range as ±2 SD from the midpoint), emit WARN.
    Returns sampled_s unchanged so callers can inline the call.
    """
    mid  = (expected_min_s + expected_max_s) / 2.0
    sd   = (expected_max_s - expected_min_s) / 4.0  # range ≈ 4 SD
    within = expected_min_s <= sampled_s <= expected_max_s
    z_lo = (expected_min_s - sampled_s) / sd if sd > 0 else 0.0
    z_hi = (sampled_s - expected_max_s) / sd if sd > 0 else 0.0
    z_score = max(0.0, z_lo, z_hi)
    msg = (
        f"[TIMING]  context={context}  sampled={sampled_s*1000:.1f}ms"
        f"  expected_range={expected_min_s*1000:.0f}-{expected_max_s*1000:.0f}ms"
        f"  within_bounds={within}  z_score={z_score:.2f}"
    )
    if z_score > 2.0:
        _dlog.warning("[TIMING WARN]  z=%.2f  %s", z_score, msg)
    else:
        _dlog.debug(msg)
    return sampled_s


# ── DEBUG LOGGING: element interaction auditor ───────────────────────────────
def _log_element_interaction(driver, element, action: str) -> None:
    """Query element geometry + visibility via JS; emit [ELEMENT] log line.

    All execute_script calls are guarded so a JS error never crashes automation.
    Routes to nstbrowser_warmer.log (DEBUG) on success; emits [ELEMENT WARN] to the
    main log if element is invisible or off-viewport.
    """
    try:
        info = driver.execute_script("""
            var el = arguments[0];
            try {
                var r  = el.getBoundingClientRect();
                var cs = window.getComputedStyle(el);
                var vw = window.innerWidth;
                var vh = window.innerHeight;
                var inVp = r.width > 0 && r.height > 0
                           && r.right > 0 && r.bottom > 0
                           && r.left < vw && r.top < vh;
                var visible = cs.display     !== 'none'
                           && cs.visibility  !== 'hidden'
                           && parseFloat(cs.opacity || '1') > 0
                           && r.width  > 0 && r.height > 0;
                return {
                    tag:        el.tagName ? el.tagName.toLowerCase() : '?',
                    role:       el.getAttribute('role') || '',
                    aria_label: el.getAttribute('aria-label') || '',
                    visible:    visible,
                    in_viewport: inVp,
                    rect: [Math.round(r.left), Math.round(r.top),
                           Math.round(r.width), Math.round(r.height)]
                };
            } catch(e) {
                return {error: e.toString()};
            }
        """, element)
        if not info or "error" in info:
            _dlog.debug("[ELEMENT]  action=%s  query_error=%s", action,
                        info.get("error") if info else "null")
            return
        base_msg = (
            f"[ELEMENT]  action={action}  tag={info['tag']}"
            f"  role={info['role'] or 'n/a'}  aria_label={info['aria_label'] or 'n/a'!r}"
            f"  visible={info['visible']}  in_viewport={info['in_viewport']}"
            f"  rect={tuple(info['rect'])}"
        )
        if not info["visible"] or not info["in_viewport"]:
            _dlog.warning("[ELEMENT WARN]  %s", base_msg)
            log.warning("[ELEMENT WARN]  action=%s  tag=%s  visible=%s  in_viewport=%s",
                        action, info["tag"], info["visible"], info["in_viewport"])
        else:
            _dlog.debug(base_msg)
    except Exception as exc:
        _dlog.debug("[ELEMENT]  action=%s  exception=%s", action, exc)
# ─────────────────────────────────────────────────────────────────────────────#


# ── DEBUG LOGGING: element tag helper ────────────────────────────────────────
def _safe_tag(element) -> str:
    """Return element.tag_name without raising on stale references."""
    try:
        return element.tag_name
    except Exception:
        return "?"


# ── DEBUG LOGGING: page-state snapshot ───────────────────────────────────────
def _log_page_state(driver, context: str) -> None:
    """Emit a snapshot of the current browser state to the main INFO log.

    Reports URL, page title, scrollY, scroll percentage, and visible feed-item
    count so a reader of the log can reconstruct exactly what was on screen at
    every action boundary without opening the browser.
    """
    try:
        url   = driver.current_url
        title = driver.title
        scroll_y = driver.execute_script("return window.scrollY")
        scroll_pct = driver.execute_script(
            "var h=document.body.scrollHeight-window.innerHeight;"
            "return h>0?Math.round(window.scrollY/h*100):0;"
        )
        articles = len(driver.find_elements(
            By.CSS_SELECTOR, "article, div[data-pressable-container='true']"
        ))
        log.debug(
            "[PAGE STATE]  context=%s  url=%s  title=%r"
            "  scrollY=%dpx  scroll_pct=%d%%  feed_items=%d",
            context, url[:80], title[:40], scroll_y, scroll_pct, articles,
        )
    except Exception as exc:
        log.debug("[PAGE STATE]  context=%s  error=%s", context, exc)

def precise_sleep(seconds: float) -> None:
    """High-precision sleep: kernel sleep for bulk, busy-wait for the tail.

    On Windows with timeBeginPeriod(1), kernel sleep resolution is ~1 ms.
    The last 2 ms are consumed by a busy-wait loop on perf_counter to
    eliminate jitter from the kernel scheduler.
    """
    if seconds <= 0:
        return
    if seconds <= 0.002:
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            pass
        return
    end = time.perf_counter() + seconds
    kernel_sleep = seconds - 0.002
    if kernel_sleep > 0:
        time.sleep(kernel_sleep)
    while time.perf_counter() < end:
        pass

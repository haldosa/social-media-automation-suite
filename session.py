import time
import random
import math
import hashlib
import os
from datetime import datetime, date
from selenium.common.exceptions import WebDriverException, TimeoutException
from config import (
    TARGET_SOCIAL_URL,
    SESSION_LOGNORMAL_MU, SESSION_LOGNORMAL_SIGMA,
    SESSION_CLAMP_MIN, SESSION_CLAMP_MAX,
    SCREENSHOT_DIR,
)
from utils import log, precise_sleep, _get_ctx, _session_local, SessionContext, _dlog
from pools import get_profile_content
from browser import _get_typing_dna
from mouse import bezier_move_to_coords, CDPConnectionDead, init_cursor_pos
from scroll import navigate_to
from actions import (
    passive_action, active_action, read_post_action,
    comment_on_post, check_notifications_action,
    visit_search_action, return_to_top_action,
    view_profile_from_feed, follow_from_feed,
    check_login_status,
)
from posting import post_action
from reporting import append_action, append_session, iso_now, make_run_id, make_session_id
from state import _load_post_state, _save_post_state,_post_state_locked, _ensure_profile_in_state, _draw_passive_phase_sec
from api import start_profile, stop_profile
from browser import connect_selenium
from preflight import run_preflight
# ================================================================== #
#  MARKOV CHAIN ACTION DISPATCH ENGINE
# ================================================================== #
#
# Replaces the flat i.i.d. random dispatch with a first-order Markov
# chain where P(next_action | current_action) encodes real behavioral
# autocorrelation patterns:
#
#   â€¢ After reading â†’ scroll (30%) or like (25%), rarely search (5%)
#   â€¢ After liking  â†’ scroll (45%), read (20%), rarely like again (8%)
#   â€¢ After comment â†’ forced passive pause (55%), scroll down (12%)
#   â€¢ After notify  â†’ profile visit (15%) or scroll (40%)
#   â€¢ After posting â†’ passive scroll (60%), never immediate re-post
#
# Context modifiers layer on top of the base transition matrix:
#   â€¢ Session phase: early=passive, mid=active peak, late=wind-down
#   â€¢ Cumulative fatigue: engagement probability decays per action count
#   â€¢ Consecutive suppression: geometric penalty on same-action repeats
#   â€¢ Account maturity: young accounts heavily favour passive actions
#
# The transition matrix can evolve per-profile over time ,  a new account's
# matrix heavily favours passive, while a mature account allows full range.
# ================================================================== #

_MARKOV_STATES = [
    "passive", "active", "notify", "profile_view",
    "read_post", "comment", "follow", "return_top",
    "search", "post",
]

# Base transition matrix: P(next | current).
# Columns: passive  active  notify  profile  read  comment  follow  top  search  post
# Each row sums to ~1.0.
_BASE_TRANSITION_MATRIX = {
    "passive":      [0.25, 0.24, 0.04, 0.09, 0.15, 0.05, 0.04, 0.05, 0.06, 0.03],
    "active":       [0.45, 0.08, 0.03, 0.06, 0.20, 0.05, 0.03, 0.04, 0.04, 0.02],
    "notify":       [0.40, 0.10, 0.02, 0.15, 0.15, 0.03, 0.04, 0.03, 0.06, 0.02],
    "profile_view": [0.45, 0.15, 0.03, 0.04, 0.15, 0.04, 0.03, 0.04, 0.05, 0.02],
    "read_post":    [0.30, 0.25, 0.02, 0.06, 0.10, 0.12, 0.04, 0.04, 0.05, 0.02],
    "comment":      [0.55, 0.08, 0.04, 0.05, 0.12, 0.02, 0.03, 0.04, 0.05, 0.02],
    "follow":       [0.45, 0.12, 0.04, 0.08, 0.15, 0.03, 0.02, 0.04, 0.05, 0.02],
    "return_top":   [0.40, 0.18, 0.04, 0.06, 0.15, 0.04, 0.03, 0.02, 0.06, 0.02],
    "search":       [0.45, 0.12, 0.03, 0.08, 0.15, 0.04, 0.03, 0.03, 0.05, 0.02],
    "post":         [0.60, 0.05, 0.04, 0.08, 0.10, 0.02, 0.02, 0.04, 0.04, 0.01],
}


def _apply_session_phase_modifier(probs: list, elapsed_frac: float) -> list:
    """Shift transition probabilities based on session phase.

    elapsed_frac  0.0 = session start, 1.0 = session end.
      Early  (0-25%):  boost passive, suppress active engagement.
      Mid    (25-75%): slight boost to active actions (peak engagement).
      Late   (75-100%): wind down ,  boost passive, suppress active.
    """
    modified = probs[:]
    n = len(modified)
    if elapsed_frac < 0.25:
        boost = 0.15 * (1.0 - elapsed_frac / 0.25)
        modified[0] += boost
        active_sum = sum(modified[1:]) or 1.0
        for i in range(1, n):
            modified[i] *= max(0.0, 1.0 - boost / active_sum)
    elif elapsed_frac > 0.75:
        wind = (elapsed_frac - 0.75) / 0.25
        boost = 0.20 * wind
        modified[0] += boost
        modified[9] *= 0.1        # almost never post near session end
        active_sum = sum(modified[1:]) or 1.0
        for i in range(1, n):
            modified[i] *= max(0.0, 1.0 - boost / active_sum)
    else:
        mid_boost = 0.05
        modified[0] -= mid_boost
        modified[1] += mid_boost * 0.4    # active (like)
        modified[4] += mid_boost * 0.3    # read_post
        modified[5] += mid_boost * 0.2    # comment
        modified[3] += mid_boost * 0.1    # profile_view
    return modified


def _apply_fatigue_modifier(probs: list, metrics: dict) -> list:
    """Diminish engagement actions that have been performed many times.

    Uses exponential decay: P *= exp(-count / decay_constant).
    """
    modified = probs[:]
    fatigue_map = {
        1: ("likes",    8.0),          # active
        5: ("comments", 4.0),          # comment
        6: ("follows",  5.0),          # follow
        9: ("posts",    2.0),          # post
        3: ("profile_visits", 6.0),    # profile_view
        8: ("searches", 5.0),          # search
    }
    for idx, (key, decay) in fatigue_map.items():
        cnt = metrics.get(key, 0)
        if cnt > 0:
            modified[idx] *= math.exp(-cnt / decay)
    return modified


def _apply_consecutive_suppression(probs: list, current_state: str,
                                    consecutive_count: int) -> list:
    """Geometric penalty on repeating the same action: P *= 0.4^count."""
    if consecutive_count <= 0:
        return probs
    try:
        idx = _MARKOV_STATES.index(current_state)
    except ValueError:
        return probs
    modified = probs[:]
    modified[idx] *= 0.4 ** consecutive_count
    return modified


def _normalize_probs(probs: list) -> list:
    """Normalize probabilities to sum to 1.0."""
    total = sum(probs)
    if total <= 0:
        return [1.0 / len(probs)] * len(probs)
    return [p / total for p in probs]


#  Per-profile pool isolation 



def _get_profile_transition_matrix(profile_id: str) -> dict:
    """Load or generate a stable per-profile perturbed Markov transition matrix.

    Each profile receives a stable profile-specific variation derived by applying a
    deterministic Â±15 % noise vector (seeded from the MD5 of the profile ID)
    to every row of _BASE_TRANSITION_MATRIX.  The result is persisted in
    post_state.json so the same profile always exhibits the same statistical
    pattern across runs, providing stable configuration without drift.

    Falls back to _BASE_TRANSITION_MATRIX for anonymous / manual sessions.

    Design notes
    ------------
    - Â±15 % per-weight perturbation keeps each profile clearly within the
      reasonable interaction envelope while making cross-profile Markov
      settings analytically distinguishable.
    - Seeding from the profile ID (not os.urandom) ensures determinism: the
      matrix is regenerated identically if post_state.json is wiped.
    - All rows are re-normalised after perturbation so they still sum to 1.0.
    """
    if not profile_id or profile_id in ("manual", ""):
        return _BASE_TRANSITION_MATRIX

    with _post_state_locked():
        state = _load_post_state()
        _ensure_profile_in_state(profile_id, state)
        profile = state.get(profile_id, {})
        stored = profile.get("transition_matrix")
        # Validate: must have all expected state keys and correct row lengths
        if (
            stored
            and isinstance(stored, dict)
            and set(stored.keys()) == set(_BASE_TRANSITION_MATRIX.keys())
            and all(
                isinstance(stored[k], list)
                and len(stored[k]) == len(_BASE_TRANSITION_MATRIX[k])
                for k in stored
            )
        ):
            return stored

        # Generate a new perturbed matrix seeded from the profile ID
        profile_seed = int(hashlib.md5(profile_id.encode()).hexdigest()[:8], 16)
        rng = random.Random(profile_seed)

        perturbed: dict = {}
        for state_name, base_row in _BASE_TRANSITION_MATRIX.items():
            # Scale each weight by a factor in [0.85, 1.15] â€” same seed â†’ same factors
            noisy_row = [max(0.0, w * rng.uniform(0.85, 1.15)) for w in base_row]
            total = sum(noisy_row) or 1.0
            perturbed[state_name] = [p / total for p in noisy_row]

        state[profile_id]["transition_matrix"] = perturbed
        _save_post_state(state)
        log.info(
            "[ MARKOV ]  generated per-profile transition matrix for %s  seed=%d",
            profile_id[:12], profile_seed,
        )
        return perturbed


# â”€

def _markov_sample_next_action(
    current_state: str,
    session_elapsed_frac: float,
    metrics: dict,
    consecutive_same: int,
    account_days_old: int = 15,
    transition_matrix: dict | None = None,
) -> str:
    """Sample the next action from the Markov chain with context modifiers.

    Parameters
    ----------
    current_state         : what the session just did
    session_elapsed_frac  : 0.0 â†’ 1.0 how far through the session
    metrics               : _session_metrics accumulator
    consecutive_same      : how many times current_state repeated in a row
    account_days_old      : for account-maturity adjustment
    transition_matrix     : per-profile perturbed matrix; falls back to
                            _BASE_TRANSITION_MATRIX when None
    """
    _matrix = transition_matrix if transition_matrix is not None else _BASE_TRANSITION_MATRIX
    base = list(_matrix.get(current_state, _matrix["passive"]))

    # Account maturity: young accounts heavily favour passive
    if account_days_old < 7:
        base[0] += 0.30
        for i in range(1, len(base)):
            base[i] *= 0.30
    elif account_days_old < 14:
        for i in [5, 6, 9]:           # comment, follow, post
            base[i] *= 0.70

    # Layer modifiers
    probs = _apply_session_phase_modifier(base, session_elapsed_frac)
    probs = _apply_fatigue_modifier(probs, metrics)
    probs = _apply_consecutive_suppression(probs, current_state, consecutive_same)
    probs = _normalize_probs(probs)

    # Sample
    r = random.random()
    cumulative = 0.0
    for i, p in enumerate(probs):
        cumulative += p
        if r < cumulative:
            return _MARKOV_STATES[i]
    return _MARKOV_STATES[0]


def _sample_session_duration_sec() -> float:
    """Sample session length from a smooth log-normal distribution.

    Eliminates the old bimodal uniform draw (6-32 / 40-70 min gap) that
    created a repetitive timing pattern ,  real social-media sessions follow
    a right-skewed continuous curve.
    """
    minutes = random.lognormvariate(SESSION_LOGNORMAL_MU, SESSION_LOGNORMAL_SIGMA)
    minutes = max(SESSION_CLAMP_MIN, min(minutes, SESSION_CLAMP_MAX))
    return minutes * 60.0


def _distraction_pause(driver) -> None:
    """Simulate a brief multitasking distraction mid-session.

    Real users don't maintain unbroken focus for an entire session ,  they
    check another tab, glance at their phone, reply to a message, etc.
    This produces a visible pause (no scroll, no click) of 8â€“45 s that
    breaks the otherwise metronomic action cadence.

    ~12 % of session ticks trigger a distraction (called from the main loop).
    """
    pause_sec = random.uniform(8.0, 45.0)
    log.info("[ DISTRACTION ]  pausing %.0fs (simulated tab-switch / phone check)", pause_sec)

    # Occasionally move the cursor to a neutral spot first ,  user's hand
    # drifts as attention shifts away from the feed.
    if random.random() < 0.4:
        try:
            vw = driver.execute_script("return window.innerWidth")
            vh = driver.execute_script("return window.innerHeight")
            drift_x = random.randint(int(vw * 0.05), int(vw * 0.95))
            drift_y = random.randint(int(vh * 0.30), int(vh * 0.80))
            bezier_move_to_coords(driver, drift_x, drift_y, tag="distraction-drift")
        except Exception:
            pass

    precise_sleep(pause_sec)


def _safe_current_url(driver) -> str:
    try:
        return str(driver.current_url)[:240]
    except Exception:
        return "unknown"


def _metric_delta_note(before: dict, after: dict) -> str:
    keys = (
        "actions_dispatched",
        "likes",
        "comments",
        "follows",
        "posts",
        "passive",
        "reads",
        "profile_visits",
        "searches",
    )
    deltas = []
    for key in keys:
        delta = after.get(key, 0) - before.get(key, 0)
        if delta:
            deltas.append(f"{key}+{delta}")
    return "; ".join(deltas)


def _write_session_summary(
    *,
    run_id: str,
    session_id: str,
    profile_id: str,
    mode: str,
    started_at: str,
    session_start_ts: float,
    planned_duration_sec: float | None,
    status: str,
    account_days: int | None = None,
    warnings_count: int = 0,
    errors_count: int = 0,
    end_url: str = "",
) -> None:
    metrics = dict(_get_ctx().session_metrics)
    append_session({
        "run_id": run_id,
        "session_id": session_id,
        "profile_id": profile_id,
        "mode": mode,
        "started_at": started_at,
        "ended_at": iso_now(),
        "planned_duration_sec": planned_duration_sec,
        "actual_duration_sec": max(0.0, time.time() - session_start_ts),
        "status": status,
        "account_days": account_days,
        "actions_dispatched": metrics.get("actions_dispatched", 0),
        "likes": metrics.get("likes", 0),
        "comments": metrics.get("comments", 0),
        "follows": metrics.get("follows", 0),
        "posts": metrics.get("posts", 0),
        "passive": metrics.get("passive", 0),
        "reads": metrics.get("reads", 0),
        "profile_visits": metrics.get("profile_visits", 0),
        "searches": metrics.get("searches", 0),
        "warnings_count": warnings_count,
        "errors_count": errors_count,
        "end_url": end_url[:240],
    })


def run_social_session(
    driver,
    session_seconds: float,
    w_like=None,
    w_notify: float = 0.03,
    w_profile: float = 0.06,
    w_read: float = 0.08,
    w_comment: float = 0.05,
    w_follow: float = 0.03,
    w_top: float = 0.03,
    w_search: float = 0.06,
    w_post: float = 0.02,
    profile_id: str = "",
    run_id: str | None = None,
    session_id: str | None = None,
    mode: str = "session",
    required_post_kind: str | None = None,
) -> None:
    """
    Session loop driven by a first-order Markov chain.

    Each action is sampled from a transition matrix conditioned on the
    previous action, with layered context modifiers:
      - Session phase (early=passive, mid=active, late=wind-down)
      - Cumulative fatigue (engagement probability decays with count)
      - Consecutive suppression (geometric penalty on same-action repeats)
      - Account maturity (young accounts favour passive actions)

    The w_* weight parameters are kept for CLI backward compatibility
    but no longer directly control dispatch.  They are used to scale
    the base transition matrix when explicitly overridden by the user.
    """
    #  Reset all per-session mutable state in this thread's context 
    # Replacing the SessionContext object atomically resets cursor_pos,
    # cdp_consecutive_failures, session_followed, session_metrics, and
    # active_typing_dna in one step ,  safe for concurrent profiles running in
    # separate threads because each has its own threading.local slot.
    _session_local.ctx = SessionContext()
    _session_local.ctx.active_typing_dna = _get_typing_dna(profile_id)
    #  Assign per-profile isolated content pools â”€
    # Use exact profile configuration; no automatic sharding is applied.
    _profile_content = get_profile_content(profile_id)
    _session_local.ctx.profile_content_loaded = True
    _session_local.ctx.profile_content_id = profile_id
    _session_local.ctx.profile_approved_reply_pool = _profile_content["approved_replies"]
    _session_local.ctx.profile_approved_caption_pool = _profile_content["approved_captions"]
    _session_local.ctx.profile_approved_media_pool = _profile_content["approved_media"]
    _session_local.ctx.profile_search_topic_pool = _profile_content["search_topics"]
    log.info(
        "[ CONTENT POLICY ]  profile=%s  source=%s  approved_replies=%d  approved_captions=%d  approved_media=%d  search_topics=%d",
        profile_id[:12],
        _profile_content["source"],
        len(_session_local.ctx.profile_approved_reply_pool),
        len(_session_local.ctx.profile_approved_caption_pool),
        len(_session_local.ctx.profile_approved_media_pool),
        len(_session_local.ctx.profile_search_topic_pool),
    )
    # â”€

    #  Break cross-profile RNG correlation â”€
    # The global random module uses a single Mersenne Twister.  Without
    # reseeding, sequential profiles produce statistically correlated
    # random sequences (same PRNG state continues).  Reseed with 32
    # bytes from the OS CSPRNG so each session is independent.
    random.seed(os.urandom(32))

    # Draw a fresh passive-phase duration for this specific session.
    # Previously a module-level constant shared across all profiles.
    _session_passive_phase_sec = _draw_passive_phase_sec()
    log.debug("[ SESSION ]  passive phase drawn: %.1f min", _session_passive_phase_sec / 60)
    run_id = run_id or make_run_id("adhoc")
    session_id = session_id or make_session_id(profile_id)
    session_started_at = iso_now()
    session_start_ts = time.time()
    deadline    = session_start_ts + session_seconds
    count       = 0
    active_done = False
    warnings_count = 0
    errors_count = 0
    session_status = "completed"
    required_post_done = False

    # Resolve account age for maturity modifier
    _account_days = 15   # default: mature
    try:
        _post_state = _load_post_state()
        _ensure_profile_in_state(profile_id, _post_state)
        if profile_id in _post_state:
            _first = _post_state[profile_id].get("first_seen", "")
            if _first:
                _account_days = (date.today() - date.fromisoformat(_first)).days
    except Exception:
        pass

    #  Load or generate the stable per-profile perturbed transition matrix 
    # Stored in post_state.json; generated once per profile from a seed derived
    # from the profile ID so the same profile always has the same configuration.
    _profile_transition_matrix = _get_profile_transition_matrix(profile_id)
    # â”€

    # Current Markov state ,  start with passive (user just opened the feed)
    current_state = "passive"

    # CLI weight override: if user explicitly passed weights, scale the
    # base transition probabilities so the Markov chain respects them.
    _user_weights = {}
    if w_like    is not None: _user_weights["active"]       = w_like
    if w_notify  != 0.03:    _user_weights["notify"]        = w_notify
    if w_profile != 0.06:    _user_weights["profile_view"]  = w_profile
    if w_read    != 0.08:    _user_weights["read_post"]     = w_read
    if w_comment != 0.05:    _user_weights["comment"]       = w_comment
    if w_follow  != 0.03:    _user_weights["follow"]        = w_follow
    if w_top     != 0.03:    _user_weights["return_top"]    = w_top
    if w_search  != 0.06:    _user_weights["search"]        = w_search
    if w_post    != 0.02:    _user_weights["post"]          = w_post

    # If user overrides are present, patch the profile's local matrix copy
    # so the Markov chain honours them while preserving transition structure.
    # Note: _profile_transition_matrix is a local dict (copy), not the global
    # _BASE_TRANSITION_MATRIX, so this adjustment is session-scoped only.
    if _user_weights:
        for state_key in list(_profile_transition_matrix.keys()):
            row = list(_profile_transition_matrix[state_key])
            for action_name, desired_w in _user_weights.items():
                try:
                    idx = _MARKOV_STATES.index(action_name)
                    row[idx] = desired_w
                except ValueError:
                    pass
            total = sum(row)
            if total > 0:
                _profile_transition_matrix[state_key] = [p / total for p in row]

    log.info(
        "Session Markov chain  |  account_age=%d days  |  user_overrides=%s",
        _account_days, _user_weights or "none",
    )

    #  STATE SNAPSHOT: session start 
    try:
        _snap_url = driver.current_url
        _snap_vp  = driver.execute_script("return [window.innerWidth, window.innerHeight]")
    except Exception:
        _snap_url, _snap_vp = "unknown", [-1, -1]
    log.debug(
        "[STATE SNAPSHOT]  event=session_start  profile=%s  session_sec=%.0f"
        "  account_days=%d  cursor_pos=(%d,%d)  viewport=(%dx%d)  page_url=%s",
        profile_id, session_seconds, _account_days,
        _get_ctx().cursor_pos[0], _get_ctx().cursor_pos[1], _snap_vp[0], _snap_vp[1], _snap_url[:80],
    )
    # 

    # Action dispatch map ,  maps Markov state names to callables.
    def _dispatch(action: str, forced_post_kind: str | None = None):
        nonlocal active_done
        if action == "passive":
            passive_action(driver)
            return True
        elif action == "active":
            active_action(driver)
            active_done = True
            return True
        elif action == "notify":
            return check_notifications_action(driver)
        elif action == "profile_view":
            result = view_profile_from_feed(driver)
            _get_ctx().session_metrics["profile_visits"] += 1
            return result
        elif action == "read_post":
            return read_post_action(driver)
        elif action == "comment":
            return comment_on_post(driver)
        elif action == "follow":
            return follow_from_feed(driver)
        elif action == "return_top":
            return return_to_top_action(driver)
        elif action == "search":
            result = visit_search_action(driver)
            _get_ctx().session_metrics["searches"] += 1
            return result
        elif action == "post":
            passive_elapsed = time.time() - session_start_ts
            if forced_post_kind or passive_elapsed >= _session_passive_phase_sec:
                return post_action(driver, profile_id, post_kind=forced_post_kind or "auto")
            else:
                wait_min = (_session_passive_phase_sec - passive_elapsed) / 60
                log.info(
                    "[ POST ]  passive phase not complete (%.1f min remaining) "
                    "-- deferring post to scroll", wait_min,
                )
                passive_action(driver)
                return {"status": "skipped", "note": "passive_phase_not_complete"}
        else:
            passive_action(driver)
            return True

    required_post_after_sec = min(_session_passive_phase_sec, max(60.0, session_seconds * 0.25))

    while time.time() < deadline:
        time_left = deadline - time.time()
        elapsed_frac = min(1.0, (time.time() - session_start_ts) / max(1, session_seconds))
        forced_post_kind = None

        # Fix #31: move the active_done guarantee to the middle 25-75% of the
        # session with a 10% per-tick probability, instead of a deterministic
        # forced like in the final 60 s.  The old last-minute pattern created
        # a repeatable "like then exit" pattern visible across sessions.
        if (
            required_post_kind
            and not required_post_done
            and (time.time() - session_start_ts) >= required_post_after_sec
        ):
            forced_post_kind = required_post_kind
            selected_action = "post"
            log.info(
                "[APPROVED PUBLISHING] required daily %s post due; dispatching post action",
                forced_post_kind,
            )
        elif not active_done and 0.25 <= elapsed_frac <= 0.75 and random.random() < 0.10:
            log.info("[ SESSION ]  mid-session active guarantee triggered (elapsed_frac=%.2f)",
                     elapsed_frac)
            selected_action = "active"
        else:
            # Sample from the Markov chain
            selected_action = _markov_sample_next_action(
                current_state=current_state,
                session_elapsed_frac=elapsed_frac,
                metrics=_get_ctx().session_metrics,
                consecutive_same=_get_ctx().session_metrics["consecutive_same"],
                account_days_old=_account_days,
                transition_matrix=_profile_transition_matrix,
            )

        action_started_at = iso_now()
        action_t0 = time.perf_counter()
        url_before = _safe_current_url(driver)
        metrics_before = dict(_get_ctx().session_metrics)
        action_status = "success"
        action_note = ""
        action_error = None
        try:
            dispatch_result = _dispatch(selected_action, forced_post_kind=forced_post_kind)
            if isinstance(dispatch_result, dict):
                action_status = dispatch_result.get("status", "success")
                action_note = dispatch_result.get("note", "")
            elif dispatch_result is False:
                action_status = "failed"
                action_note = "returned False"
            if selected_action == "active":
                active_done = True
            if forced_post_kind and dispatch_result is True:
                required_post_done = True
                action_note = (
                    f"{action_note}; required_{forced_post_kind}_post_completed"
                    if action_note else f"required_{forced_post_kind}_post_completed"
                )
        except Exception as exc:
            errors_count += 1
            session_status = "failed"
            action_status = "error"
            action_note = f"{type(exc).__name__}: {exc}"[:240]
            action_error = exc

        # Update Markov state
        current_state = selected_action

        #  DEBUG LOGGING: [SESSION TICK] + consecutive-action tracking â”€
        _sess_elapsed = time.time() - session_start_ts
        if _get_ctx().session_metrics["last_action"] == selected_action:
            _get_ctx().session_metrics["consecutive_same"] += 1
        else:
            _get_ctx().session_metrics["consecutive_same"] = 0
        _get_ctx().session_metrics["last_action"] = selected_action
        metric_note = _metric_delta_note(metrics_before, _get_ctx().session_metrics)
        if metric_note:
            action_note = f"{action_note}; {metric_note}" if action_note else metric_note
        try:
            append_action({
                "run_id": run_id,
                "session_id": session_id,
                "profile_id": profile_id,
                "sequence": count + 1,
                "action": selected_action,
                "started_at": action_started_at,
                "ended_at": iso_now(),
                "duration_sec": time.perf_counter() - action_t0,
                "status": action_status,
                "note": action_note[:240],
                "elapsed_session_sec": _sess_elapsed,
                "elapsed_frac": elapsed_frac,
                "consecutive_same": _get_ctx().session_metrics["consecutive_same"],
                "account_days": _account_days,
                "url_before": url_before,
                "url_after": _safe_current_url(driver),
            })
        except Exception as report_exc:
            log.warning("[REPORT]  action CSV write failed: %s", report_exc)
        if action_error is not None:
            raise action_error
        log.info(
            "[SESSION TICK]  iteration=%d  markov_state=%s  selected=%s"
            "  session_elapsed=%.0fs  elapsed_frac=%.2f"
            "  active_done=%s  actions=%d  consecutive_same=%d"
            "  account_days=%d",
            count + 1, current_state, selected_action,
            _sess_elapsed, elapsed_frac,
            active_done, _get_ctx().session_metrics["actions_dispatched"],
            _get_ctx().session_metrics["consecutive_same"], _account_days,
        )
        if _get_ctx().session_metrics["consecutive_same"] >= 2:
            warnings_count += 1
            _dlog.warning(
                "[RISK WARN]  consecutive_same=%d  action=%s -- "
                "Markov suppression should reduce this",
                _get_ctx().session_metrics["consecutive_same"] + 1, selected_action,
            )
        # â”€

        count += 1

        #  Distraction / multitasking injection â”€
        # ~12 % of ticks: pause as if the user switched tabs or checked
        # their phone.  Skipped in the first 2 min (user is still engaged)
        # and the last 1 min (session is winding down).
        _elapsed_s = time.time() - session_start_ts
        if (_elapsed_s > 120
                and (deadline - time.time()) > 60
                and random.random() < 0.12):
            _distraction_pause(driver)
        else:
            # Fix #8: log-normal inter-action gap ,  median 1.5 s, Ïƒ=0.6 on the
            # log scale.  Produces a right-skewed distribution that matches
            # observed operator pacing between browsing actions better
            # than a flat uniform(1,3) which a classifier can trivially identify.
            precise_sleep(max(0.5, min(15.0, random.lognormvariate(math.log(1.5), 0.6))))

    if required_post_kind and not required_post_done:
        warnings_count += 1
        log.warning(
            "[APPROVED PUBLISHING] required daily %s post was not completed in this session",
            required_post_kind,
        )

    #  POST-SESSION DIAGNOSTICS â”€
    if _get_ctx().session_metrics["passive"] == 0:
        warnings_count += 1
        _dlog.warning(
            "[RISK WARN]  session ended with 0 passive actions "
            "-- engagement-only session pattern"
        )
    try:
        _end_url = driver.current_url
    except Exception:
        _end_url = "unknown"
    log.info(
        "[STATE SNAPSHOT]  event=session_end  profile=%s  session_sec=%.0f"
        "  account_days=%d  cursor_pos=(%d,%d)"
        "  session_followed=%d  actions_dispatched=%d"
        "  likes=%d  comments=%d  follows=%d  posts=%d  passive=%d  reads=%d"
        "  profile_visits=%d  searches=%d  page_url=%s",
        profile_id, session_seconds, _account_days,
        _get_ctx().cursor_pos[0], _get_ctx().cursor_pos[1],
        len(_get_ctx().session_followed), _get_ctx().session_metrics["actions_dispatched"],
        _get_ctx().session_metrics["likes"], _get_ctx().session_metrics["comments"],
        _get_ctx().session_metrics["follows"], _get_ctx().session_metrics["posts"],
        _get_ctx().session_metrics["passive"], _get_ctx().session_metrics["reads"],
        _get_ctx().session_metrics["profile_visits"], _get_ctx().session_metrics["searches"],
        _end_url[:80],
    )
    # 

    try:
        _write_session_summary(
            run_id=run_id,
            session_id=session_id,
            profile_id=profile_id,
            mode=mode,
            started_at=session_started_at,
            session_start_ts=session_start_ts,
            planned_duration_sec=session_seconds,
            status=session_status,
            account_days=_account_days,
            warnings_count=warnings_count,
            errors_count=errors_count,
            end_url=_end_url,
        )
    except Exception as report_exc:
        log.warning("[REPORT]  session CSV write failed: %s", report_exc)

    log.info("Session complete. Total actions: %d", count)


# ================================================================== #
#  SINGLE PROFILE OPERATIONS ORCHESTRATOR
# ================================================================== #

def warm_profile(
    profile_id: str,
    skip_preflight: bool = False,
    weights: dict | None = None,
    run_id: str | None = None,
    required_post_kind: str | None = None,
) -> bool:
    driver   = None
    launched = False
    ran_session = False
    session_recorded = False
    run_id = run_id or make_run_id("session")
    session_id = make_session_id(profile_id)
    session_started_at = iso_now()
    session_start_ts = time.time()
    session_sec = None
    _session_local.ctx = SessionContext()
    try:
        info   = start_profile(profile_id)
        ws_url = info["webSocketDebuggerUrl"]
        launched = True

        driver = connect_selenium(ws_url)
        driver.set_page_load_timeout(30)
        init_cursor_pos(driver)

        if skip_preflight:
            log.info("Preflight skipped (--no-preflight).")
        else:
            run_preflight(driver)

        log.info("Navigating to %s", TARGET_SOCIAL_URL)
        navigate_to(driver, TARGET_SOCIAL_URL)
        precise_sleep(random.uniform(2, 5))

        if not check_login_status(driver):
            log.error("Profile %s appears logged out â€” skipping.", profile_id)
            _write_session_summary(
                run_id=run_id,
                session_id=session_id,
                profile_id=profile_id,
                mode="session",
                started_at=session_started_at,
                session_start_ts=session_start_ts,
                planned_duration_sec=session_sec,
                status="logged_out",
                errors_count=1,
                end_url=_safe_current_url(driver),
            )
            session_recorded = True
            return False

        ran_session = True
        session_sec = _sample_session_duration_sec()
        log.info("Session: %.1f min  |  profile: %s", session_sec / 60, profile_id)

        run_social_session(
            driver,
            session_sec,
            profile_id=profile_id,
            run_id=run_id,
            session_id=session_id,
            mode="session",
            required_post_kind=required_post_kind,
            **(weights or {}),
        )
        session_recorded = True
    except (TimeoutException, RuntimeError, WebDriverException,
            CDPConnectionDead) as exc:
        log.error("Error on profile %s: %s", profile_id, exc)
        if not session_recorded:
            try:
                _write_session_summary(
                    run_id=run_id,
                    session_id=session_id,
                    profile_id=profile_id,
                    mode="session",
                    started_at=session_started_at,
                    session_start_ts=session_start_ts,
                    planned_duration_sec=session_sec,
                    status="failed",
                    errors_count=1,
                    end_url=_safe_current_url(driver) if driver else "",
                )
                session_recorded = True
            except Exception as report_exc:
                log.warning("[REPORT]  failed-session CSV write failed: %s", report_exc)
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
                if random.random() < 0.40:
                    time.sleep(random.uniform(2.0, 7.0))
                driver.quit()
            except Exception:
                pass
        if launched:
            stop_profile(profile_id)

    return ran_session

def warm_profile_attached(
    debugger_address: str,
    profile_id: str = "manual",
    skip_preflight: bool = False,
    close_after: bool = False,
    weights: dict | None = None,
    run_id: str | None = None,
) -> bool:
    """
    Run a profile-operations session on a browser already open in NstBrowser.

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
        established browsing state from an earlier authorized run).
    close_after : bool
        Quit the browser after the session.  Default: leave it open.
    """
    driver  = None
    address = debugger_address.replace("ws://", "").split("/")[0]
    ws_url  = f"ws://{address}"
    ran_session = False
    session_recorded = False
    run_id = run_id or make_run_id("attached")
    session_id = make_session_id(profile_id)
    session_started_at = iso_now()
    session_start_ts = time.time()
    session_sec = None
    _session_local.ctx = SessionContext()

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
        precise_sleep(random.uniform(2, 5))

        if not check_login_status(driver):
            log.error("Profile '%s' appears logged out -- skipping session.",
                      profile_id)
            _write_session_summary(
                run_id=run_id,
                session_id=session_id,
                profile_id=profile_id,
                mode="attached",
                started_at=session_started_at,
                session_start_ts=session_start_ts,
                planned_duration_sec=session_sec,
                status="logged_out",
                errors_count=1,
                end_url=_safe_current_url(driver),
            )
            session_recorded = True
            return False

        session_sec = _sample_session_duration_sec()
        log.info("Session: %.1f min  |  profile: %s", session_sec / 60, profile_id)
        ran_session = True
        run_social_session(
            driver,
            session_sec,
            profile_id=profile_id,
            run_id=run_id,
            session_id=session_id,
            mode="attached",
            **(weights or {}),
        )
        session_recorded = True

    except (TimeoutException, RuntimeError, WebDriverException) as exc:
        log.error("Error on attached profile '%s': %s", profile_id, exc)
        if not session_recorded:
            try:
                _write_session_summary(
                    run_id=run_id,
                    session_id=session_id,
                    profile_id=profile_id,
                    mode="attached",
                    started_at=session_started_at,
                    session_start_ts=session_start_ts,
                    planned_duration_sec=session_sec,
                    status="failed",
                    errors_count=1,
                    end_url=_safe_current_url(driver) if driver else "",
                )
                session_recorded = True
            except Exception as report_exc:
                log.warning("[REPORT]  failed attached-session CSV write failed: %s", report_exc)
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
    return ran_session

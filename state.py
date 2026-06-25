import os
import json
import time
import threading
import contextlib
import sys
import random
from datetime import datetime, date
from config import (
    POST_STATE_FILE,
)
from utils import log

# ================================================================== #
#  POSTING ENGINE
# ================================================================== #
# Creates original posts from profile-approved captions and optional media.
# A persistent state file (POST_STATE_FILE) tracks per-
# profile daily counts and account age to enforce a progressive ramp-up:
#   Days  1– 5 : 0 posts/day  (account establishing credibility)
#   Days  6–10 : 1 post/day
#   Days 11–14 : 2 posts/day
#   Day  15+   : 3 posts/day
# A hard 2-hour minimum gap (POST_MIN_INTERVAL_SEC) between posts is
# enforced on top of the daily quota.
# ================================================================== #

_POST_STATE_TLOCK = threading.Lock()

@contextlib.contextmanager
def _post_state_locked():
    """Acquire exclusive access to POST_STATE_FILE for a load-modify-save cycle.

    Combines an in-process threading.Lock with an OS-level file lock on
    ``POST_STATE_FILE + ".lock"`` so that concurrent threads *and* concurrent
    processes (parallel profile runs) are both serialised.

    Windows uses ``msvcrt.locking`` (LK_LOCK = blocking exclusive byte-range
    lock). POSIX uses ``fcntl.flock(LOCK_EX)``.

    Usage::

        with _post_state_locked():
            state = _load_post_state()
            # ... mutate state ...
            _save_post_state(state)
    """
    lock_path = POST_STATE_FILE + ".lock"
    with _POST_STATE_TLOCK:
        lf = open(lock_path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt as _msvcrt
                # LK_LOCK retries every 1 s for up to 10 s then raises OSError.
                lf.seek(0)
                _msvcrt.locking(lf.fileno(), _msvcrt.LK_LOCK, 1)
            else:
                import fcntl as _fcntl
                _fcntl.flock(lf.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                if sys.platform == "win32":
                    import msvcrt as _msvcrt
                    lf.seek(0)
                    _msvcrt.locking(lf.fileno(), _msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl as _fcntl
                    _fcntl.flock(lf.fileno(), _fcntl.LOCK_UN)
        finally:
            lf.close()


def _load_post_state() -> dict:
    """Read posting state from POST_STATE_FILE.

    Must be called from within a ``_post_state_locked()`` context whenever the
    caller intends to mutate and save.  Safe to call outside the lock for
    read-only queries where a slightly stale snapshot is acceptable.
    """
    if not os.path.exists(POST_STATE_FILE) and os.path.exists(POST_STATE_FILE + ".bak"):
        try:
            os.replace(POST_STATE_FILE + ".bak", POST_STATE_FILE)
        except OSError:
            pass

    if os.path.exists(POST_STATE_FILE):
        try:
            with open(POST_STATE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("post_state load failed (%s) ,  starting fresh", exc)
    return {}


def _save_post_state(state: dict) -> None:
    """Atomically persist *state* to POST_STATE_FILE.

    Writes to a sibling ``.tmp`` file, fsyncs to flush kernel buffers, then
    calls ``os.replace()`` which is atomic on both Windows (Vista+) and POSIX.
    A crash before ``os.replace()`` leaves the original file intact; a crash
    after leaves the complete new file.  Either way the state is never empty.

    Must be called from within a ``_post_state_locked()`` context.
    """
    tmp_path = POST_STATE_FILE + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(POST_STATE_FILE):
            try:
                os.replace(POST_STATE_FILE, POST_STATE_FILE + ".bak")
            except OSError:
                pass
        os.replace(tmp_path, POST_STATE_FILE)
    except OSError as exc:
        log.warning("post_state save failed: %s", exc)


def _post_daily_quota(days_old: int) -> int:
    """Max posts per day for an account days_old days old (0-indexed)."""
    if days_old < 5:   return 0   # days 1–5:  no posts
    if days_old < 10:  return 1   # days 6–10: 1/day
    if days_old < 14:  return 2   # days 11–14: 2/day
    return 3                       # day 15+: 3/day


def _ensure_profile_in_state(profile_id: str, state: dict) -> None:
    """Register profile's first-seen date if not already recorded."""
    if profile_id and profile_id not in state:
        today = date.today().isoformat()
        state[profile_id] = {
            "first_seen":    today,
            "daily_counts":  {},
            "last_post_ts":  0.0,
            "next_post_ts":  0.0,   # Poisson-sampled; 0 = no restriction yet
        }
        _save_post_state(state)
        log.info("[ POST ]  registered account start date: %s  (day 1 of ramp-up)", today)


def _next_post_delay_sec(days_old: int) -> float:
    """
    Sample the next inter-post gap using an exponential distribution
    (continuous Poisson inter-arrival model).  The mean gap and floor/ceiling
    are tuned per ramp-up phase so natural clustering *and* silence emerge:

      Days  6–10  mean=36h  floor=8h  ceil=84h  (mostly one post every 1-2 days)
      Days 11–14  mean=24h  floor=6h  ceil=52h  (1-2 per day or a skip day)
      Day  15+    mean=18h  floor=4h  ceil=40h  (tighter cadence, still varies)

    exponential(1/mean) produces a right-skewed distribution: most gaps cluster
    near the mean while the long tail models multi-day silences organically.
    """
    if days_old < 10:
        mean_h, floor_h, ceil_h = 36.0, 8.0, 84.0
    elif days_old < 14:
        mean_h, floor_h, ceil_h = 24.0, 6.0, 52.0
    else:
        mean_h, floor_h, ceil_h = 18.0, 4.0, 40.0
    gap_h = max(floor_h, min(random.expovariate(1.0 / mean_h), ceil_h))
    return gap_h * 3600.0

# Minimum elapsed session time (seconds) before a post action is allowed.
# Ensures the bot scrolls/reads for a meaningful passive phase first.
def _draw_passive_phase_sec() -> float:
    """Draw a per-session passive-phase minimum (seconds).

    Re-drawn every session so the same profile doesn't always start
    posting at the same elapsed time.  Module-level constant was shared
    across all profiles in a multi-profile invocation.
    """
    return random.uniform(5 * 60, 10 * 60)   # 5–10 min, per-session


# Soft floor between any two posts per profile.
# Instead of a fixed 4-hour hard floor (which eliminates natural post-
# clustering behavior), we use a Gaussian-sampled floor that averages
# ~2 hours but can go as low as 45 min or as high as 4 hours.
# This preserves the organic "two posts within an hour" pattern that real
# users sometimes exhibit while still preventing machine-gun posting.
def _sample_post_min_gap_sec() -> float:
    """Sample a soft minimum inter-post gap (seconds).

    Distribution: Gaussian(mean=2h, sigma=40min), clamped to [45min, 4h].
    The result is different each time so clusters can emerge naturally.
    """
    gap_h = random.gauss(2.0, 0.67)   # mean=2h, sigma=40min
    gap_h = max(0.75, min(gap_h, 4.0))  # clamp [45min, 4h]
    return gap_h * 3600.0

def _can_post_now(profile_id: str, state: dict) -> bool:
    """Return True if this profile is allowed to post right now."""
    if not profile_id or profile_id in ("manual", ""):
        log.debug("_can_post_now: no meaningful profile_id ,  post suppressed")
        return False

    _ensure_profile_in_state(profile_id, state)
    entry = state[profile_id]
    today = date.today().isoformat()

    # Poisson-sampled next-post gate (plus absolute floor)
    now = time.time()
    next_ts = entry.get("next_post_ts", 0.0)
    if now < next_ts:
        wait_min = (next_ts - now) / 60
        # ── DEBUG LOGGING: [POST GATE] blocked by Poisson ────────────────────
        days_old_pg = (date.fromisoformat(today) - date.fromisoformat(entry["first_seen"])).days
        log.info(
            "[POST GATE]  profile=%s  days_old=%d  quota=%d  today_count=%d"
            "  last_post_ago=%.1fh  next_post_in=%.1fh  poisson_gate=block"
            "  result=blocked_cooldown",
            profile_id, days_old_pg, _post_daily_quota(days_old_pg),
            entry.get("daily_counts", {}).get(today, 0),
            (now - entry.get("last_post_ts", now)) / 3600,
            wait_min / 60,
        )
        # ────────────────────────────────────────────────────────────────────
        log.info("[ POST ]  skipping ,  next post allowed in %.0f min (Poisson gate)", wait_min)
        return False
    # Always enforce hard floor as well
    elapsed = now - entry.get("last_post_ts", 0.0)
    _soft_floor = _sample_post_min_gap_sec()
    if elapsed < _soft_floor:
        days_old_pg2 = (date.fromisoformat(today) - date.fromisoformat(entry["first_seen"])).days
        # ── DEBUG LOGGING: [POST GATE] hard floor ─────────────────────────────
        log.info(
            "[POST GATE]  profile=%s  days_old=%d  quota=%d  today_count=%d"
            "  last_post_ago=%.1fh  next_post_in=%.1fh  poisson_gate=pass"
            "  result=blocked_cooldown",
            profile_id, days_old_pg2, _post_daily_quota(days_old_pg2),
            entry.get("daily_counts", {}).get(today, 0),
            elapsed / 3600,
            max(0, (entry.get("next_post_ts", 0) - now)) / 3600,
        )
        # ────────────────────────────────────────────────────────────────────
        log.info(
            "[ POST ]  skipping ,  %.0f min since last post (hard floor)",
            elapsed / 60,
        )
        return False

    # Account age ramp-up
    days_old = (
        date.fromisoformat(today) - date.fromisoformat(entry["first_seen"])
    ).days
    quota = _post_daily_quota(days_old)
    if quota == 0:
        # ── DEBUG LOGGING: [POST GATE] ramp-up block ───────────────────────────
        log.info(
            "[POST GATE]  profile=%s  days_old=%d  quota=0  today_count=%d"
            "  last_post_ago=%.1fh  next_post_in=n/a  poisson_gate=pass"
            "  result=blocked_rampup",
            profile_id, days_old,
            entry.get("daily_counts", {}).get(today, 0),
            (now - entry.get("last_post_ts", now)) / 3600,
        )
        # ────────────────────────────────────────────────────────────────────
        log.info(
            "[ POST ]  skipping ,  account age %d day(s), quota=0 during ramp-up",
            days_old,
        )
        return False

    # Daily cap
    today_count = entry.get("daily_counts", {}).get(today, 0)
    if today_count >= quota:
        # ── DEBUG LOGGING: [POST GATE] daily quota exhausted ──────────────────────
        log.info(
            "[POST GATE]  profile=%s  days_old=%d  quota=%d  today_count=%d"
            "  last_post_ago=%.1fh  next_post_in=%.1fh  poisson_gate=pass"
            "  result=blocked_quota",
            profile_id, days_old, quota, today_count,
            (now - entry.get("last_post_ts", now)) / 3600,
            max(0, (entry.get("next_post_ts", 0) - now)) / 3600,
        )
        # ────────────────────────────────────────────────────────────────────
        log.info(
            "[ POST ]  skipping ,  daily quota %d reached (%d posted today)",
            quota, today_count,
        )
        return False

    # ── DEBUG LOGGING: [POST GATE] allowed ────────────────────────────────────
    log.info(
        "[POST GATE]  profile=%s  days_old=%d  quota=%d  today_count=%d"
        "  last_post_ago=%.1fh  next_post_in=%.1fh  poisson_gate=pass"
        "  result=allowed",
        profile_id, days_old, quota, today_count,
        (now - entry.get("last_post_ts", now)) / 3600,
        max(0, (entry.get("next_post_ts", 0) - now)) / 3600,
    )
    # ────────────────────────────────────────────────────────────────────
    return True


def _record_post(profile_id: str, state: dict) -> None:
    """Increment daily count, update last-post timestamp, and sample next-post gate."""
    today = date.today().isoformat()
    _ensure_profile_in_state(profile_id, state)
    now = time.time()
    state[profile_id]["last_post_ts"] = now

    # Sample next-post allowed time from an exponential distribution.
    days_old = (
        date.fromisoformat(today) - date.fromisoformat(state[profile_id]["first_seen"])
    ).days
    gap_sec = _next_post_delay_sec(days_old)
    state[profile_id]["next_post_ts"] = now + gap_sec
    log.info(
        "[ POST ]  next post allowed in %.1f h (Poisson gap=%.1fh)",
        gap_sec / 3600, gap_sec / 3600,
    )

    daily = state[profile_id].setdefault("daily_counts", {})
    daily[today] = daily.get(today, 0) + 1
    _save_post_state(state)
    log.info(
        "[ POST ]  state updated  |  profile=%s  today=%d  first_seen=%s",
        profile_id, daily[today], state[profile_id]["first_seen"],
    )


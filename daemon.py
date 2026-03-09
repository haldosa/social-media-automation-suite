import time
import signal
import heapq
import random
import threading
import json
import os
import math
from datetime import datetime, timedelta, date
from config import (
    ACTIVE_HOURS_RANGE,
    HEARTBEAT_FILE, _HEARTBEAT_INTERVAL_SEC,
    PROFILE_IDS, _SCRIPT_DIR,
)
from utils import log, _ensure_profile_logger, _active_profile_id
from state import _load_post_state, _save_post_state
from session import warm_profile
from api import get_running_browsers
from state import _post_state_locked, _ensure_profile_in_state

# ================================================================== #
#  24/7 DAEMON SCHEDULER
# ================================================================== #
#
# When launched with ``--daemon``, the script runs as a persistent
# process with an independent per-profile priority queue.  Each profile
# has its own ``next_run_ts`` stored in post_state.json so the schedule
# survives restarts.
#
# Session count per day:  1 (50%), 2 (35%), 3 (15%)
# Day-off probability:    10% per profile per calendar day
# Inter-session gap:      log-normal ~5 h, [2 h, 10 h]
# Next-day gap:           log-normal ~23 h, [18 h, 32 h]
# ================================================================== #

_daemon_shutdown = threading.Event()             # set by signal handler
_daemon_start_ts: float = 0.0                    # set once in daemon_main()


# ── Signal handling ──────────────────────────────────────────────────────────
def _install_daemon_signals() -> None:
    """Register SIGINT/SIGTERM handlers that set the shutdown event."""
    def _handler(signum, frame):
        name = signal.Signals(signum).name
        log.info("[ DAEMON ]  received %s — will exit after current session", name)
        _daemon_shutdown.set()

    signal.signal(signal.SIGINT,  _handler)
    signal.signal(signal.SIGTERM, _handler)


# ── Day-off / session-target helpers ─────────────────────────────────────────

def _is_day_off(profile_id: str, state: dict, today_iso: str) -> bool:
    """Return True if *profile_id* has a day off on *today_iso*.

    The decision is drawn once (10 % probability) and persisted so it
    survives process restarts within the same calendar day.
    """
    entry = state.get(profile_id, {})
    stored = entry.get("day_off_date", "")
    if stored == today_iso:
        return True

    # If a different (older) date is stored, draw fresh for today.
    # Draw the decision using a profile-seeded RNG so a restart within
    # the same day doesn't re-roll.
    seed_str = f"{profile_id}:{today_iso}:day_off"
    is_off = random.Random(seed_str).random() < 0.0
    if is_off:
        state.setdefault(profile_id, {})["day_off_date"] = today_iso
    else:
        # Clear stale date if present
        if "day_off_date" in entry and entry["day_off_date"] != today_iso:
            entry.pop("day_off_date", None)
    return is_off


def _get_daily_session_target(profile_id: str, state: dict, today_iso: str) -> int:
    """Return the target session count for *profile_id* on *today_iso*.

    Distribution: 1 (25 %), 2 (50 %), 3 (25 %).
    Drawn once and persisted; survives restarts.
    """
    entry = state.get(profile_id, {})
    targets: dict = entry.get("daily_session_target", {})
    if today_iso in targets:
        return targets[today_iso]

    seed_str = f"{profile_id}:{today_iso}:session_target"
    rng = random.Random(seed_str)
    roll = rng.random()
    if roll < 0.25:
        target = 3
    elif roll < 0.75:
        target = 2
    else:
        target = 1

    targets[today_iso] = target
    state.setdefault(profile_id, {})["daily_session_target"] = targets
    return target


def _get_daily_session_count(profile_id: str, state: dict, today_iso: str) -> int:
    """Return how many sessions have already run today for *profile_id*."""
    entry = state.get(profile_id, {})
    counts: dict = entry.get("daily_session_counts", {})
    return counts.get(today_iso, 0)


def _increment_daily_session_count(profile_id: str, state: dict, today_iso: str) -> None:
    """Increment today's session count for *profile_id*."""
    entry = state.setdefault(profile_id, {})
    counts = entry.setdefault("daily_session_counts", {})
    counts[today_iso] = counts.get(today_iso, 0) + 1


def _sample_inter_session_gap_sec() -> float:
    """Log-normal gap between same-day sessions: ~5 h, clamped [2 h, 10 h]."""
    hours = random.lognormvariate(math.log(5.0), 0.5)
    hours = max(2.0, min(hours, 10.0))
    return hours * 3600.0


def _sample_next_day_gap_sec() -> float:
    """Log-normal gap to the next day's first session: ~23 h, clamped [18 h, 32 h]."""
    hours = random.lognormvariate(math.log(23.0), 0.4)
    hours = max(18.0, min(hours, 32.0))
    return hours * 3600.0


def _clamp_to_active_hours(ts: float) -> float:
    """If *ts* falls outside ACTIVE_HOURS_RANGE, advance it to the start of the next window."""
    lo, hi = ACTIVE_HOURS_RANGE
    if lo == 0 and hi == 23:
        return ts  # guard disabled
    dt = datetime.fromtimestamp(ts)
    if lo <= dt.hour <= hi:
        return ts
    # Advance to the next lo:00 boundary
    if dt.hour > hi:
        # Past tonight's window → next day at lo:00
        next_day = dt.date() + timedelta(days=1)
    else:
        # Before today's window → today at lo:00
        next_day = dt.date()
    target_dt = datetime.combine(next_day, datetime.min.time().replace(hour=lo))
    # Add 0–30 min random jitter so profiles don't all start on the hour boundary
    jitter = random.uniform(0, 30 * 60)
    return target_dt.timestamp() + jitter


def _schedule_next_run(profile_id: str, after_ts: float, state: dict) -> float:
    """Compute and persist the next_run_ts for *profile_id*, returning it.

    Logic
    -----
    1. Check if today still has remaining sessions (count < target).
    2. If yes → inter-session gap from *after_ts*.
    3. If no  → next-day gap from *after_ts*, then re-evaluate day-off on
       that target day and potentially push further.

    All timestamps are clamped to ACTIVE_HOURS_RANGE before persisting.
    """
    today_iso = date.today().isoformat()
    target = _get_daily_session_target(profile_id, state, today_iso)
    done   = _get_daily_session_count(profile_id, state, today_iso)

    if done < target:
        # More sessions remaining today
        next_ts = after_ts + _sample_inter_session_gap_sec()
    else:
        # Done for today — schedule for tomorrow
        next_ts = after_ts + _sample_next_day_gap_sec()

    next_ts = _clamp_to_active_hours(next_ts)

    # Persist
    state.setdefault(profile_id, {})["next_run_ts"] = next_ts
    return next_ts


def _prune_old_daily_keys(state: dict) -> None:
    """Remove daily_session_counts / daily_session_target entries older than 7 days."""
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    for pid in list(state.keys()):
        if pid.startswith("_"):
            continue
        entry = state.get(pid, {})
        for key in ("daily_session_counts", "daily_session_target"):
            bucket = entry.get(key)
            if isinstance(bucket, dict):
                stale = [d for d in bucket if d < cutoff]
                for d in stale:
                    del bucket[d]


# ── Heartbeat system ────────────────────────────────────────────────────────

def _heartbeat_writer(heap: list) -> None:
    """Write heartbeat.json every 5 minutes until _daemon_shutdown is set.

    Uses a threading.Timer chain instead of a background thread to avoid
    leaving stale threads on shutdown.
    """
    if _daemon_shutdown.is_set():
        return
    try:
        next_profile = ""
        next_run_iso = ""
        if heap:
            _ts, _pid = heap[0]
            next_profile = _pid
            next_run_iso = datetime.utcfromtimestamp(_ts).strftime("%Y-%m-%dT%H:%M:%SZ")

        payload = {
            "last_heartbeat": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "next_profile": next_profile,
            "next_run": next_run_iso,
            "uptime_seconds": round(time.time() - _daemon_start_ts),
        }
        tmp = HEARTBEAT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, HEARTBEAT_FILE)
    except Exception as exc:
        log.debug("[ HEARTBEAT ]  write failed: %s", exc)

    # Schedule next tick
    t = threading.Timer(_HEARTBEAT_INTERVAL_SEC, _heartbeat_writer, args=(heap,))
    t.daemon = True
    t.start()


# ── Daemon main function ────────────────────────────────────────────────────

def daemon_main(weights: dict | None = None) -> None:
    """Persistent 24/7 scheduler with per-profile independent scheduling.

    Never returns unless SIGINT/SIGTERM is received.  Each profile is
    managed independently via a min-heap priority queue keyed by
    next_run_ts.  All state survives restarts via post_state.json.
    """
    global _daemon_start_ts
    _daemon_start_ts = time.time()
    _pid_file = os.path.join(_SCRIPT_DIR, "daemon.pid")
    with open(_pid_file, "w") as f:
        f.write(str(os.getpid()))
    log.info("[ DAEMON ]  PID %d written to %s", os.getpid(), _pid_file)

    _install_daemon_signals()

    log.info("=" * 60)
    log.info("[ DAEMON ]  starting 24/7 scheduler  |  profiles=%d  |  pid=%d",
             len(PROFILE_IDS), os.getpid())

    # ── Initialise per-profile loggers ───────────────────────────────────
    for pid in PROFILE_IDS:
        _ensure_profile_logger(pid)

    # ── Build initial priority queue from post_state.json ────────────────
    heap: list[tuple[float, str]] = []
    with _post_state_locked():
        state = _load_post_state()
        _prune_old_daily_keys(state)

        now = time.time()
        for i, pid in enumerate(PROFILE_IDS):
            _ensure_profile_in_state(pid, state)
            stored_ts = state[pid].get("next_run_ts", 0.0)
            if stored_ts and stored_ts > now:
                ts = stored_ts
                log.info("[ DAEMON ]  %s  scheduled from state: %s",
                         pid[:12], datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"))
            else:
                # First launch or stale — stagger 0–90 min from now
                stagger = random.uniform(0, 90 * 60)
                ts = _clamp_to_active_hours(now + stagger)
                state[pid]["next_run_ts"] = ts
                log.info("[ DAEMON ]  %s  initial schedule: %s  (stagger=%.1f min)",
                         pid[:12], datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),
                         stagger / 60)
            heapq.heappush(heap, (ts, pid))
        _save_post_state(state)

    # ── Start heartbeat writer ───────────────────────────────────────────
    _heartbeat_writer(heap)

    log.info("[ DAEMON ]  scheduler running — Ctrl+C to stop after current session")
    log.info("-" * 60)

    # ── Main scheduler loop ───────────────────────────────────────────────
    while not _daemon_shutdown.is_set():
        if not heap:
            log.error("[ DAEMON ]  heap is empty — no profiles to schedule")
            break

        next_ts, profile_id = heapq.heappop(heap)

        # Sleep until the scheduled time (interruptible by _daemon_shutdown)
        wait_sec = next_ts - time.time()
        if wait_sec > 0:
            log.info(
                "[ DAEMON ]  sleeping %.1f min until %s for %s",
                wait_sec / 60,
                datetime.fromtimestamp(next_ts).strftime("%H:%M"),
                profile_id[:12],
            )
            # Use Event.wait() so SIGINT/SIGTERM can break the sleep
            if _daemon_shutdown.wait(timeout=wait_sec):
                # Shutdown requested while sleeping — put the entry back and exit
                heapq.heappush(heap, (next_ts, profile_id))
                break

        if _daemon_shutdown.is_set():
            heapq.heappush(heap, (next_ts, profile_id))
            break

        today_iso = date.today().isoformat()

        # ── Day-off / session-count gating ───────────────────────────────
        with _post_state_locked():
            state = _load_post_state()
            _ensure_profile_in_state(profile_id, state)

            if _is_day_off(profile_id, state, today_iso):
                log.info("[ DAEMON ]  %s  day-off today — skipping to tomorrow",
                         profile_id[:12])
                # Schedule for tomorrow
                next_ts = _clamp_to_active_hours(
                    time.time() + _sample_next_day_gap_sec()
                )
                state[profile_id]["next_run_ts"] = next_ts
                _save_post_state(state)
                heapq.heappush(heap, (next_ts, profile_id))
                continue

            target = _get_daily_session_target(profile_id, state, today_iso)
            done   = _get_daily_session_count(profile_id, state, today_iso)
            _save_post_state(state)

        if done >= target:
            # All sessions for today already complete — schedule for tomorrow
            log.info(
                "[ DAEMON ]  %s  daily target reached (%d/%d) — scheduling tomorrow",
                profile_id[:12], done, target,
            )
            with _post_state_locked():
                state = _load_post_state()
                next_ts = _schedule_next_run(profile_id, time.time(), state)
                _save_post_state(state)
            heapq.heappush(heap, (next_ts, profile_id))
            continue

        # ── Active-hours guard ───────────────────────────────────────────
        now_hour = datetime.now().hour
        if not (ACTIVE_HOURS_RANGE[0] <= now_hour <= ACTIVE_HOURS_RANGE[1]):
            delayed_ts = _clamp_to_active_hours(time.time())
            log.info(
                "[ DAEMON ]  %s  outside active hours (%02d:00–%02d:00) — delaying to %s",
                profile_id[:12], ACTIVE_HOURS_RANGE[0], ACTIVE_HOURS_RANGE[1],
                datetime.fromtimestamp(delayed_ts).strftime("%H:%M"),
            )
            with _post_state_locked():
                state = _load_post_state()
                state.setdefault(profile_id, {})["next_run_ts"] = delayed_ts
                _save_post_state(state)
            heapq.heappush(heap, (delayed_ts, profile_id))
            continue

        # ── Run the session ──────────────────────────────────────────────
        log.info(
            "[ DAEMON ]  %s  starting session %d/%d for %s",
            profile_id[:12], done + 1, target, today_iso,
        )

        # Set the contextvar so all log records from this session are
        # duplicated to the per-profile log file.
        _profile_token = _active_profile_id.set(profile_id)
        _ensure_profile_logger(profile_id)

        try:
            ran_session = warm_profile(profile_id, weights=weights)
        except Exception as exc:
            log.error("[ DAEMON ]  %s  uncaught exception: %s", profile_id[:12], exc)
            ran_session = False
        finally:
            _active_profile_id.reset(_profile_token)

        # ── Post-session scheduling ──────────────────────────────────────
        session_end = time.time()
        with _post_state_locked():
            state = _load_post_state()
            _ensure_profile_in_state(profile_id, state)
            if ran_session:
                _increment_daily_session_count(profile_id, state, today_iso)
            next_ts = _schedule_next_run(profile_id, session_end, state)
            _save_post_state(state)

        heapq.heappush(heap, (next_ts, profile_id))
        log.info(
            "[ DAEMON ]  %s  next session scheduled: %s",
            profile_id[:12], datetime.fromtimestamp(next_ts).strftime("%Y-%m-%d %H:%M"),
        )
        log.info("-" * 60)

    # ── Graceful shutdown ────────────────────────────────────────────────
    # Persist final schedule so next startup picks up where we left off
    with _post_state_locked():
        state = _load_post_state()
        for ts, pid in heap:
            state.setdefault(pid, {})["next_run_ts"] = ts
        _save_post_state(state)

    log.info("[ DAEMON ]  shutdown complete — schedule saved")
    log.info("=" * 60)

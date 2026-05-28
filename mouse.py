import math
import time
import random
from selenium.webdriver.common.actions.pointer_input import PointerInput
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import WebDriverException
from config import MOUSE_TRACE, DEBUG_CURSOR_OVERLAY
from utils import log, precise_sleep, _get_ctx, _dlog, _safe_tag, _timing_check, _mlog,_log_element_interaction

# ================================================================== #
#  INTERACTION PRIMITIVES
# ================================================================== #

# Bigram pairs that are naturally slow for most touch-typists ,  awkward
# hand transitions that produce longer inter-key intervals in corpus data.
# Fix #13: expanded from 10 to 50 bigrams.  The original 10 were all rare;
# common high-frequency English bigrams also require slower transitions and
# their absence means any corpus classifier will fail to reproduce the right
# n-gram timing distribution across real captions.
_SLOW_BIGRAMS = {
    # Original rare/awkward bigrams
    'qu', 'wr', 'xc', 'zx', 'bv', 'vb', 'pq', 'yw', 'wq', 'xz',
    # Common English bigrams with cross-hand or stretch transitions
    'th', 'he', 'in', 'er', 'an', 're', 'on', 'en', 'at', 'es',
    'ed', 'or', 'ti', 'hi', 'as', 'to', 'ou', 'ha', 'it', 'nd',
    'st', 'ng', 'nt', 'is', 'le', 'al', 'ar', 'se', 'te', 've',
    # Additional awkward index-to-pinky / same-hand stretch pairs
    'br', 'cr', 'dr', 'fr', 'gr', 'pr', 'tr', 'bl', 'cl', 'pl',
    'ct', 'ft', 'lt', 'pt', 'ny', 'ly', 'my', 'ry', 'ty', 'gy',
}

# Typo injection was intentionally removed from the thesis-facing workflow.
# The suite keeps per-profile pacing but types approved text as written.

#  CDP keystroke dispatch 
# Replaces Selenium element.send_keys() to avoid StaleElementReferenceException
# when React/Lexical re-renders the contenteditable <div> mid-typing.
# Also produces isTrusted:true keyboard events.

# Fix #13: mapping from printable ASCII char â†’ (code, windowsVirtualKeyCode, modifiers).
# `code` is the KeyboardEvent.code (physical key); `modifiers` bit 3 = Shift.
# Absence of `code` causes KeyboardEvent.code to read as "" in JS ,  repetitive.
_ASCII_KEY_INFO: dict[str, tuple[str, int, int]] = {
    # Lowercase letters ,  location 0, no shift
    **{c: (f"Key{c.upper()}", ord(c.upper()), 0) for c in "abcdefghijklmnopqrstuvwxyz"},
    # Uppercase letters ,  same physical key, shift modifier (bit 3 = 8)
    **{c: (f"Key{c}", ord(c), 8) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    # Digits ,  no shift
    "0": ("Digit0", 48, 0), "1": ("Digit1", 49, 0), "2": ("Digit2", 50, 0),
    "3": ("Digit3", 51, 0), "4": ("Digit4", 52, 0), "5": ("Digit5", 53, 0),
    "6": ("Digit6", 54, 0), "7": ("Digit7", 55, 0), "8": ("Digit8", 56, 0),
    "9": ("Digit9", 57, 0),
    # Space
    " ": ("Space", 32, 0),
    # Punctuation ,  unshifted
    "`": ("Backquote", 192, 0), "-": ("Minus",     189, 0), "=": ("Equal",       187, 0),
    "[": ("BracketLeft", 219, 0), "]": ("BracketRight", 221, 0), "\\": ("Backslash", 220, 0),
    ";": ("Semicolon", 186, 0), "'": ("Quote",   222, 0), ",": ("Comma",  188, 0),
    ".": ("Period",    190, 0), "/": ("Slash",   191, 0),
    # Punctuation ,  shifted variants (+8 modifiers)
    "~": ("Backquote", 192, 8), "_": ("Minus",     189, 8), "+": ("Equal",       187, 8),
    "{": ("BracketLeft", 219, 8), "}": ("BracketRight", 221, 8), "|": ("Backslash", 220, 8),
    ":": ("Semicolon", 186, 8), '"': ("Quote",   222, 8), "<": ("Comma",  188, 8),
    ">": ("Period",    190, 8), "?": ("Slash",   191, 8),
    "!": ("Digit1", 49, 8), "@": ("Digit2", 50, 8), "#": ("Digit3", 51, 8),
    "$": ("Digit4", 52, 8), "%": ("Digit5", 53, 8), "^": ("Digit6", 54, 8),
    "&": ("Digit7", 55, 8), "*": ("Digit8", 56, 8), "(": ("Digit9", 57, 8),
    ")": ("Digit0", 48, 8),
}


def _cdp_type_key(driver, char: str) -> None:
    """Type a single character via CDP Input.dispatchKeyEvent / insertText.

    Fix 1.2 ,  Full three-event keyboard sequence for printable ASCII:
      1. keyDown  ,  fires DOM 'keydown'; no text insertion yet (text="")
      2. char     ,  fires DOM 'keypress' AND inserts the character (text=char)
      3. keyUp    ,  fires DOM 'keyup'

    This exactly mirrors the CDP event sequence that Chrome records for
    physical hardware key presses.  The original two-event (keyDown+keyUp)
    sequence omitted 'keypress', which is repetitive by behavioral analytics
    that profile the full keyboard event chain (keydown â†’ keypress â†’
    beforeinput â†’ input â†’ keyup).

    Note: the 'char' CDP type is deprecated in the spec but still the only
    way to generate a trusted DOM 'keypress' event via CDP.  It matches what
    Chrome DevTools itself records when you reproduce typing via the Recorder.

    Non-ASCII (emoji, accented chars) use Input.insertText which fires
    beforeinput â†’ input ,  matching real IME / emoji-picker behaviour.
    """
    if len(char) == 1 and 32 <= ord(char) < 127:
        info = _ASCII_KEY_INFO.get(char)
        if info:
            code, vk, mods = info
        else:
            code, vk, mods = f"Key{char.upper()}", ord(char.upper()), 0
        # Shared fields for all three events
        base: dict = {
            "key":                  char,
            "code":                 code,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "location":             0,
            "modifiers":            mods,
        }
        # 1. keydown ,  no text so the browser doesn't insert twice
        driver.execute_cdp_cmd("Input.dispatchKeyEvent",
                                {**base, "type": "keyDown", "text": ""})
        # 2. keypress (char type) ,  fires 'keypress' DOM event + inserts char
        driver.execute_cdp_cmd("Input.dispatchKeyEvent",
                                {**base, "type": "char",    "text": char})
        # 3. keyup
        driver.execute_cdp_cmd("Input.dispatchKeyEvent",
                                {**base, "type": "keyUp",   "text": ""})
    else:
        driver.execute_cdp_cmd("Input.insertText", {"text": char})


def _cdp_backspace(driver) -> None:
    """Press Backspace via CDP Input.dispatchKeyEvent."""
    driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
        "type": "keyDown",
        "key": "Backspace",
        "code": "Backspace",
        "windowsVirtualKeyCode": 8,
        "nativeVirtualKeyCode": 8,
    })
    driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
        "type": "keyUp",
        "key": "Backspace",
        "code": "Backspace",
        "windowsVirtualKeyCode": 8,
        "nativeVirtualKeyCode": 8,
    })


def human_type(element, text: str, driver=None, typing_dna: dict = None) -> None:
    """
    Type text with a configurable per-profile keystroke timing model.

    When typing_dna is None, falls back to the session-level typing profile set
    by run_social_session(), or default mid-range parameters if neither exists.
    The current thesis-facing configuration disables automatic typo injection.
    """
    # -- DEBUG LOGGING: typing audit ----------------------------------------
    _type_t0 = time.perf_counter()
    log.info("[TYPE]  chars=%d  preview=%r  element_tag=%s",
             len(text), text[:30], _safe_tag(element))
    _dlog.debug("[TYPE START]  full_text=%r  chars=%d  dna=%s",
                text, len(text), "custom" if typing_dna else "session")
    # -----------------------------------------------------------------------
    if driver is not None:
        # CDP click at wherever the bezier arc landed -- no centre-snap.
        _cdp_click(driver)
    else:
        element.click()   # fallback when driver is unavailable
    precise_sleep(random.uniform(0.08, 0.25))   # focus-settle after click

    # Resolve typing profile (session-level or defaults)
    dna        = typing_dna or _get_ctx().active_typing_dna or {}
    _mu        = dna.get("base_mu",            math.log(0.08))
    _sigma     = dna.get("base_sigma",         0.40)
    _burst_min = dna.get("burst_min",          3)
    _burst_max = dna.get("burst_max",          7)
    _sp_lo     = dna.get("space_pause_lo",     0.05)
    _sp_hi     = dna.get("space_pause_hi",     0.18)
    _pp_lo     = dna.get("punct_pause_lo",     0.20)
    _pp_hi     = dna.get("punct_pause_hi",     0.60)
    _bg_lo     = dna.get("burst_gap_lo",       0.06)
    _bg_hi     = dna.get("burst_gap_hi",       0.20)
    _hes_prob  = dna.get("hesitation_prob",    0.04)
    _hes_lo    = dna.get("hesitation_lo",      0.30)
    _hes_hi    = dna.get("hesitation_hi",      0.80)
    _bp_lo     = dna.get("bigram_penalty_lo",  1.4)
    _bp_hi     = dna.get("bigram_penalty_hi",  2.0)
    _fatigue   = dna.get("fatigue_drift",      0.005)

    typed_sequence = [{"char": c} for c in text]

    prev        = ''
    word_len    = 0
    burst_rem   = random.randint(_burst_min, _burst_max)
    chars_typed = 0

    for action in typed_sequence:
        char = action["char"]
        is_backspace = action.get("backspace", False)

        if is_backspace:
            # Backspace timing: faster than normal keystrokes, short
            # reaction-driven delay.
            base = random.lognormvariate(math.log(0.05), 0.30)
            base = max(0.03, min(base, 0.15))
            if driver is not None:
                _cdp_backspace(driver)
            else:
                element.send_keys(Keys.BACKSPACE)
            precise_sleep(base)
            continue

        # Log-normal base with per-profile parameters + fatigue drift.
        fatigue_mult = 1.0 + _fatigue * (chars_typed / 100.0)
        base = random.lognormvariate(_mu, _sigma) * fatigue_mult
        base = max(0.04, min(base, 0.60))

        # Slow bigram penalty
        if (prev + char).lower() in _SLOW_BIGRAMS:
            base *= random.uniform(_bp_lo, _bp_hi)

        # Word boundary
        if char == ' ':
            base += random.uniform(_sp_lo, _sp_hi)
            word_len = 0
        else:
            word_len += 1

        # Post-sentence punctuation re-reading pause
        if prev in '.!?':
            base += random.uniform(_pp_lo, _pp_hi)

        # Rare mid-word hesitation
        if word_len > 4 and random.random() < _hes_prob:
            base += random.uniform(_hes_lo, _hes_hi)

        # Burst gap: extra pause at end of each burst
        burst_rem -= 1
        if burst_rem <= 0:
            base += random.uniform(_bg_lo, _bg_hi)
            burst_rem = random.randint(_burst_min, _burst_max)

        # -- DEBUG LOGGING: keystroke timing audit --------------------------
        _timing_check("human_type_key", base, 0.040, 0.600)
        if base < 0.030:
            _dlog.warning(
                "[RISK WARN]  keystroke interval %.1fms < 30ms floor"
                " -- unnatural speed", base * 1000)
        elif base > 0.700:
            _dlog.warning(
                "[RISK WARN]  keystroke interval %.1fms > 700ms ceiling"
                " -- outside corpus range", base * 1000)
        # -------------------------------------------------------------------

        if driver is not None:
            _cdp_type_key(driver, char)
        else:
            element.send_keys(char)
        precise_sleep(base)
        prev = char
        chars_typed += 1

    # -- DEBUG LOGGING: type complete ---------------------------------------
    _n_typos = sum(1 for a in typed_sequence if a.get("backspace"))
    log.info("[TYPE END]  chars=%d  duration=%.1fs  corrections=%d",
             len(text), time.perf_counter() - _type_t0, _n_typos)
    # -----------------------------------------------------------------------

# Followed/visited profile tracking is now in SessionContext.session_followed.
# See _get_ctx().


# ------------------------------------------------------------------ #
#  DEBUG CURSOR OVERLAY
# ------------------------------------------------------------------ #
# Injects a visible red dot + coordinate label into the live browser page.
# Responds to real DOM mousemove events fired by Seleniumâ€™s ActionChains,
# so it follows every bezier step in real time.
# Injected via execute_script after each page load ,  safe, no profile risk.

_CURSOR_OVERLAY_JS = """
(function () {
    var ID  = '__cursor_debug_dot';
    var LID = '__cursor_debug_lbl';
    // Remove stale instances from previous injection on same page
    var old = document.getElementById(ID);  if (old) old.remove();
    var olL = document.getElementById(LID); if (olL) olL.remove();

    var dot = document.createElement('div');
    dot.id  = ID;
    dot.style.cssText = [
        'position:fixed', 'top:0', 'left:0',
        'width:14px', 'height:14px',
        'background:rgba(255,40,40,0.88)',
        'border:2px solid #fff',
        'border-radius:50%',
        'pointer-events:none',
        'z-index:2147483647',
        'transform:translate(-50%,-50%)',
        'box-shadow:0 0 5px rgba(0,0,0,0.6)',
    ].join(';');

    var lbl = document.createElement('div');
    lbl.id  = LID;
    lbl.style.cssText = [
        'position:fixed', 'top:0', 'left:0',
        'background:rgba(0,0,0,0.72)',
        'color:#0ff',
        'font:bold 10px/1 monospace',
        'padding:2px 5px',
        'border-radius:3px',
        'pointer-events:none',
        'z-index:2147483647',
        'white-space:nowrap',
    ].join(';');

    // Append to <html> not <body> so React reconciler cannot wipe the nodes.
    document.documentElement.appendChild(dot);
    document.documentElement.appendChild(lbl);

    document.addEventListener('mousemove', function (e) {
        var x = e.clientX, y = e.clientY;
        dot.style.left = x + 'px';
        dot.style.top  = y + 'px';
        lbl.style.left = (x + 14) + 'px';
        lbl.style.top  = (y -  8) + 'px';
        lbl.textContent = x + ', ' + y;
    }, true);

    var found = document.getElementById(ID);
    return {
        appended: found !== null,
        bodyChildCount: document.body ? document.body.children.length : -1,
        cspMeta: (document.querySelector('meta[http-equiv="Content-Security-Policy"]') || {}).content || 'none'
    };
})();
"""

def inject_cursor_overlay(driver) -> None:
    """Inject the visual cursor overlay into the current page.
    Cursor placement is now managed by navigate_to / navigate_history;
    this function only handles the visual debug dot."""
    if not DEBUG_CURSOR_OVERLAY:
        return
    try:
        # Brief settle so SPA hydration finishes before we inject.
        time.sleep(0.4)
        result = driver.execute_script(_CURSOR_OVERLAY_JS)
        if result:
            log.info("Overlay inject: appended=%s  bodyChildren=%s  csp=%s",
                     result.get('appended'), result.get('bodyChildCount'),
                     (result.get('cspMeta') or 'none')[:120])
    except WebDriverException as exc:
        log.debug("Cursor overlay injection failed: %s", exc)

# ------------------------------------------------------------------ #
#  SHARED BÃ‰ZIER PATH ENGINE
# ------------------------------------------------------------------ #
# Fix #27: The JS batch-dispatch approach (_BEZIER_DISPATCH_JS) was removed.
# MouseEvent() created inside execute_script() is always isTrusted:false , 
# a property that cannot be overridden by user-land JS and is explicitly
# checked by Meta's platform-integrity pipeline via trusted-events heuristics.
# CDP Input.dispatchMouseEvent generates isTrusted:true events with the full
# pointermove â†’ mousemove chain identical to physical hardware input, at the
# cost of ~1â€“2 ms per-step round-trip over localhost.  The per-step overhead
# is subtracted from the subsequent sleep (fix #29) so arc timing accuracy
# is unaffected.

def debug_cursor_state(driver, label: str = "") -> None:
    """Log both the Python-tracked position and the overlay's actual DOM position.

    Compares _cursor_pos (Python state) with the visual debug dot's style.left/top
    to detect drift between the two cursor systems.
    """
    try:
        dom_pos = driver.execute_script("""
            var dot = document.getElementById('__cursor_debug_dot');
            if (!dot) return {x: -1, y: -1, exists: false};
            return {
                x: parseInt(dot.style.left) || 0,
                y: parseInt(dot.style.top)  || 0,
                exists: true
            };
        """)
        log.info("CURSOR SYNC CHECK [%s]  python=(%d,%d)  dom=(%d,%d)  overlay_exists=%s",
                 label,
                 _get_ctx().cursor_pos[0], _get_ctx().cursor_pos[1],
                 dom_pos['x'], dom_pos['y'],
                 dom_pos['exists'])
        if dom_pos['exists']:
            drift = math.hypot(dom_pos['x'] - _get_ctx().cursor_pos[0],
                               dom_pos['y'] - _get_ctx().cursor_pos[1])
            if drift > 15:
                log.warning("CURSOR DRIFT  %.1fpx between Python state and overlay DOM", drift)
    except Exception as e:
        log.debug("debug_cursor_state failed: %s", e)

def _fire_bezier_arc(
    driver,
    x0: int, y0: int, x1: int, y1: int,
    vw: int, vh: int,
    *,
    exact_end: bool = False,
) -> tuple:
    """
    Build a randomised quadratic BÃ©zier path from (x0,y0) to (x1,y1),
    dispatch it step-by-step via CDP Input.dispatchMouseEvent (isTrusted:true),
    and subtract each CDP round-trip from the subsequent sleep so arc timing
    matches the Fitts's Law model precisely.  Returns (points, delays) for
    any post-arc work by the caller.

    Why CDP per-step dispatch instead of a single JS batch call (fix #27):
      JS-constructed MouseEvent()/PointerEvent() objects produced inside
      execute_script() always have isTrusted=false (Web spec Â§4.2.2).  This
      property is read-only and cannot be overridden by user-land JS.  Meta's
      platform-integrity layer explicitly checks isTrusted for pointer events
      during engagement actions.  CDP Input.dispatchMouseEvent injects events
      at the browser input pipeline level, so they arrive as isTrusted:true
      with the complete pointermove â†’ mousemove event sequence that real
      hardware produces.  The ~1â€“2 ms per-step round-trip overhead over
      localhost is subtracted from each inter-step sleep (fix #29), keeping
      the arc's velocity profile faithful to the Fitts's Law model.

    Control-point strategy
    ----------------------
    The cp is always offset perpendicular to the travel direction so the
    curve has genuine curvature even on vertical or horizontal arcs.
    â€¢ 25 % of arcs use a large lateral deviation (more visible sweep).
    â€¢ 35 % let the cp bulge outside the viewport bbox ,  real paths often
      arc beyond the straight-line trajectory on diagonal moves.
    â€¢ The sign is flipped when the chosen direction would be eaten by the
      viewport edge, guaranteeing a real perpendicular offset.

    Tremor model
    ------------
    Two-factor: velocity Ã— distance.
    â€¢ velocity bell (sin Ï€Â·t): tremor is low at mid-arc (fast movement)
      and rises at endpoints.  The approach phase (t > 0.80) adds extra
      corrective wobble, matching Fitts's Law biomechanics.
    â€¢ dist_scale: short arcs get proportionally less absolute tremor.
    The last step is not tremored:
      exact_end=True  â†’ forced to exactly (x1, y1) (coord-based arcs).
      exact_end=False â†’ bare BÃ©zier point used (bezier_move, where the
                        Phase 2 ActionChains snap corrects the position).
    """
    _arc_dist = math.hypot(x1 - x0, y1 - y0)
    _mid_x    = (x0 + x1) / 2.0
    _mid_y    = (y0 + y1) / 2.0
    _perp_x   = -(y1 - y0) / _arc_dist
    _perp_y   =  (x1 - x0) / _arc_dist
    min_cp_offset = max(20, int(_arc_dist * 0.15))
    if random.random() < 0.25:
        # 25 % excursion: large lateral deviation for a visible curve
        lateral = random.randint(30, 80) * random.choice([-1, 1])
    else:
        lateral = random.randint(
            min_cp_offset,
            max(min_cp_offset + 10, int(_arc_dist * 0.25)),
        ) * random.choice([-1, 1])
    # Flip sign if chosen lateral direction gets eaten by viewport clamp.
    _cp_x_trial   = int(_mid_x + _perp_x * lateral)
    _cp_y_trial   = int(_mid_y + _perp_y * lateral)
    _cp_x_clamped = max(0, min(_cp_x_trial, int(vw)))
    _cp_y_clamped = max(0, min(_cp_y_trial, int(vh)))
    if abs(_cp_x_clamped - int(_mid_x)) + abs(_cp_y_clamped - int(_mid_y)) < min_cp_offset:
        lateral = -lateral
    # 35 % of arcs: let the cp bulge outside the viewport bounding box.
    if random.random() < 0.35:
        extra = random.uniform(0.20, 0.40) * _arc_dist * random.choice([-1, 1])
        cp = (int(_mid_x + _perp_x * (lateral + extra)),
              int(_mid_y + _perp_y * (lateral + extra)))
    else:
        cp = (
            max(0, min(int(_mid_x + _perp_x * lateral), int(vw))),
            max(0, min(int(_mid_y + _perp_y * lateral), int(vh))),
        )
    # Fitts's Law: total arc duration based on distance and target size.
    total_ms   = _fitts_duration_ms(_arc_dist, 40.0)
    steps      = max(20, min(90, int(_arc_dist / 3.5)))  # ~3.5 px/step; clamp 20-90
    step_ms    = total_ms / steps   # derived from Fitts duration, not fixed
    # Physiological tremor: 2-3 narrow-band sinusoids at 8-12 Hz.
    _tremor_components = _make_tremor_components(random.randint(2, 3))
    points     = []
    delays     = []
    prev       = (x0, y0)
    dist_scale = max(0.30, min(_arc_dist / 500.0, 1.0))
    drift_x    = 0.0
    drift_y    = 0.0
    # Corrective sub-movement for long arcs (>200 px):
    # At t ~ 0.75 a small positional correction creates the velocity
    # "notch" characteristic of real Fitts-paradigm pointing movements.
    _has_sub   = _arc_dist > 200 and random.random() < 0.70
    _sub_t     = random.uniform(0.70, 0.82) if _has_sub else 2.0
    _sub_ox    = random.gauss(0, max(2.0, _arc_dist * 0.008))
    _sub_oy    = random.gauss(0, max(2.0, _arc_dist * 0.006))
    _sub_done  = False
    _arc_t0    = time.perf_counter()
    for i in range(1, steps + 1):
        t_raw  = i / steps
        t      = _min_jerk_basis(t_raw)
        nx, ny = _bezier_point((x0, y0), cp, (x1, y1), t)
        # Corrective sub-movement nudge
        if not _sub_done and t_raw >= _sub_t:
            nx = int(nx + _sub_ox)
            ny = int(ny + _sub_oy)
            _sub_done = True
        if i < steps:
            # Physiological tremor: narrow-band sinusoids (8-12 Hz)
            # replacing previous Gaussian white-noise model.
            elapsed = time.perf_counter() - _arc_t0
            bleed_steps    = max(1, min(3, steps // 4))
            steps_from_end = steps - i
            bleed_factor   = (
                steps_from_end / (bleed_steps + 1)
                if steps_from_end <= bleed_steps else 1.0
            )
            # Velocity-dependent amplitude: tremor strongest at endpoints
            # (low velocity), weakest mid-arc (peak velocity).
            vel_norm   = _min_jerk_velocity(t_raw) / 1.88
            vel_factor = 1.0 - vel_norm * 0.55
            approach   = max(0.0, (t_raw - 0.80) / 0.20) if t_raw > 0.80 else 0.0
            tremor_amp = (0.9 * vel_factor + approach * 1.0) * dist_scale * bleed_factor
            tremor_x = sum(
                a * tremor_amp * math.sin(2.0 * math.pi * f * elapsed + p)
                for f, a, p in _tremor_components)
            tremor_y = sum(
                a * tremor_amp * 0.75 * math.sin(2.0 * math.pi * f * elapsed + p + 1.2)
                for f, a, p in _tremor_components)
            nx = int(nx + tremor_x)
            ny = int(ny + tremor_y)
            # Low-frequency drift: correlated wrist/arm oscillation.
            drift_x = drift_x * 0.88 + random.gauss(0, 0.55 * dist_scale * bleed_factor)
            drift_y = drift_y * 0.88 + random.gauss(0, 0.40 * dist_scale * bleed_factor)
            drift_cap = max(1.0, 4.0 * dist_scale)
            drift_x = max(-drift_cap, min(drift_x, drift_cap))
            drift_y = max(-drift_cap, min(drift_y, drift_cap))
            nx = max(0, min(int(nx + drift_x), int(vw) - 1))
            ny = max(0, min(int(ny + drift_y), int(vh) - 1))
        elif exact_end:
            nx, ny = x1, y1   # force exact landing for coord-based arcs
        dx, dy = nx - prev[0], ny - prev[1]
        points.append([nx, ny, dx, dy])
        prev = (nx, ny)
        # Per-step delay from minimum-jerk velocity profile.
        vel_n = _min_jerk_velocity(t_raw) / 1.88
        d_ms  = step_ms * (1.4 - vel_n * 0.7) + random.gauss(0, 1.8)
        delays.append(max(8.0, d_ms))
    # Cumulative step-fire times for STEP log annotation.
    cum_ms     = 0.0
    step_times = []
    for d in delays:
        step_times.append(cum_ms)
        cum_ms += d
    _mlog.debug(
        "ARC  from=(%d,%d)  cp=(%d,%d)  to=(%d,%d)  steps=%d  ms/step=%.1f  dur=%.0fms",
        x0, y0, cp[0], cp[1], x1, y1, steps, step_ms, cum_ms,
    )
    #  DEBUG LOGGING: MOUSE ARC structured audit â”€
    _cp_offset = int(math.hypot(cp[0] - _mid_x, cp[1] - _mid_y))
    _dlog.debug(
        "[MOUSE ARC]  from=(%d,%d)  to=(%d,%d)  arc_dist=%.0fpx"
        "  steps=%d  duration_ms=%.0f  cp_offset=%dpx  step_ms=%.1f  exact_end=%s",
        x0, y0, x1, y1, _arc_dist, steps, cum_ms, _cp_offset, step_ms, exact_end,
    )
    if _arc_dist > 300 and cum_ms < 150:
        _dlog.warning(
            "[RISK WARN]  unnatural arc speed  dist=%.0fpx  duration=%.0fms"
            " ,  human minimum ~150ms for 300px+",
            _arc_dist, cum_ms,
        )
    _timing_check("bezier_arc", cum_ms / 1000.0,
                  max(0.10, _arc_dist / 4000.0), max(1.0, _arc_dist / 500.0))
    # â”€
    #  DEBUG LOGGING: update arc-completion timestamp for _cdp_click RISK WARN 
    _get_ctx().last_bezier_end_ts = time.perf_counter()
    # â”€
    if MOUSE_TRACE:
        for i, ((nx, ny, dx, dy), t_ms) in enumerate(zip(points, step_times), 1):
            _mlog.debug("STEP  i=%02d  t=+%.0fms  pos=(%d,%d)  delta=(%+d,%+d)",
                        i, t_ms, nx, ny, dx, dy)
    # Dispatch via CDP Input.dispatchMouseEvent ,  produces isTrusted:true
    # events with the full pointermove â†’ mousemove chain that real hardware
    # input generates.  JS-constructed MouseEvent() via execute_script() would
    # always be isTrusted:false (Web spec Â§4.2.2) ,  a read-only flag checked by
    # Meta's platform-integrity layer for pointer events during engagement actions.
    for pt, d_ms in zip(points, delays):
        # Fix #6/#29: measure CDP round-trip time and subtract it from the
        # inter-step sleep so the actual inter-step interval matches the
        # biomechanical model instead of inflating it by the ~1â€“2 ms localhost
        # RTT on every step (~25% slowdown across a full arc without this).
        _step_t0 = time.perf_counter()
        try:
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": pt[0],
                "y": pt[1],
                # Fix #5: pointer fields required for a fully-spec-compliant
                # PointerEvent; absent fields default to undefined in Chrome's
                # input pipeline, which is repetitive via performance.getEntries.
                "pointerType": "mouse",
                "pressure": 0.0,
                "tiltX": 0,
                "tiltY": 0,
                "twist": 0,
            })
            _cdp_record_success()
        except WebDriverException as exc:
            _cdp_record_failure("bezier_arc_step", exc)
        precise_sleep(max(0.0, d_ms / 1000.0 - (time.perf_counter() - _step_t0)))
    return points, delays


def _cdp_click(driver, x: int = None, y: int = None) -> None:
    """Dispatch a trusted click via CDP Input.dispatchMouseEvent.

    If x, y are omitted, clicks at the current _cursor_pos.
    Produces mousePressed + mouseReleased with a realistic inter-event
    gap drawn from a operator-like distribution.
    """
    cx = x if x is not None else _get_ctx().cursor_pos[0]
    cy = y if y is not None else _get_ctx().cursor_pos[1]
    #  DEBUG LOGGING: every click â”€
    log.debug("[CLICK]  pos=(%d,%d)  source=%s", cx, cy,
             "explicit" if x is not None else "cursor_pos")
    # 
    driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
        "type": "mousePressed",
        "x": cx, "y": cy,
        "button": "left",
        "clickCount": 1,
        "pointerType": "mouse",
        "pressure": 0.5,
        "tiltX": 0,
        "tiltY": 0,
        "twist": 0,
    })
    precise_sleep(random.uniform(0.04, 0.11))
    driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
        "type": "mouseReleased",
        "x": cx, "y": cy,
        "button": "left",
        "clickCount": 1,
        "pointerType": "mouse",
        "pressure": 0.0,
        "tiltX": 0,
        "tiltY": 0,
        "twist": 0,
    })
    #  DEBUG LOGGING: RISK WARN ,  click within 50ms of bezier completion 
    try:
        gap_ms = (time.perf_counter() - _get_ctx().last_bezier_end_ts) * 1000
        if 0 < gap_ms < 50:
            _dlog.warning(
                "[RISK WARN]  cdp_click fired %.1fms after bezier arc end"
                " ,  unnaturally fast (threshold 50ms)  pos=(%d,%d)",
                gap_ms, cx, cy,
            )
        else:
            _dlog.debug("[CLICK]  pos=(%d,%d)  gap_from_arc=%.1fms", cx, cy, gap_ms)
    except Exception:
        pass
    # 

def _cdp_click_element(driver, element) -> None:
    """CDP click on a specific element ,  single event pair, no retry.

    Fix 6.4: the old two-attempt strategy (cursor_pos first, then bbox-centre
    fallback) fired mousePressed+mouseReleased *twice* when the primary missed.
    A mousePressed/Released pair that produces no click event followed
    immediately by one that does is a unwanted automation signal.

    New strategy: read the element's bounding rect BEFORE any mouse events,
    then fire exactly ONE mousePressed/mouseReleased pair:
      â€¢ Normal path ,  cursor_pos is already inside the element bounds
                      (expected after every bezier_move): click there.
      â€¢ Snap path   ,  cursor_pos is outside the element (page reflux / stale
                      pos): silently update cursor to element centre first,
                      then click there.  Still one event pair, no failed attempt.

    Raises WebDriverException if the single CDP attempt fails.  A missed click
    is always preferable to isTrusted:false or a double-fire artefact.
    """
    rect = driver.execute_script(
        "var r = arguments[0].getBoundingClientRect();"
        "return {l: r.left, t: r.top, r: r.right, b: r.bottom,"
        "        cx: Math.round(r.left + r.width  / 2),"
        "        cy: Math.round(r.top  + r.height / 2)};",
        element,
    )
    cur_x, cur_y = _get_ctx().cursor_pos
    if rect["l"] <= cur_x <= rect["r"] and rect["t"] <= cur_y <= rect["b"]:
        # Normal path: bezier arc already landed on the element.
        log.debug("[CLICK]  cdp_click_element  on-element  pos=(%d,%d)", cur_x, cur_y)
        _cdp_click(driver)
    else:
        # Cursor is outside element ,  snap to centre before the first (and
        # only) mouse event so there is no failed-attempt artefact.
        cx, cy = int(rect["cx"]), int(rect["cy"])
        log.debug(
            "[CLICK]  cdp_click_element  snapped  centre=(%d,%d)  cursor_was=(%d,%d)",
            cx, cy, cur_x, cur_y,
        )
        _set_cursor(cx, cy, "cdp-element-snap")
        _cdp_click(driver, cx, cy)

def init_cursor_pos(driver) -> None:
    """
    Silently set _cursor_pos to a random position within the current viewport.

    No DOM event is dispatched ,  a single-step jump from (0,0) to a random
    coordinate is a unwanted automation signal.  The first real cursor event the
    page sees will be the drift arc from _navigate_and_settle or the first
    bezier_move call, both of which start from this seeded position.
    """
    try:
        vw = driver.execute_script("return window.innerWidth")
        vh = driver.execute_script("return window.innerHeight")
        x = random.randint(int(vw * 0.10), int(vw * 0.90))
        y = random.randint(int(vh * 0.15), int(vh * 0.85))
        _mlog.debug("INIT  vp=(%dx%d)", vw, vh)
        _set_cursor(x, y, "init")
    except WebDriverException as exc:
        log.debug("init_cursor_pos failed: %s", exc)

def bezier_move(driver, target_element) -> None:
    """
    Move the mouse to target_element along a randomised quadratic Bezier curve
    at ~60 fps using CDP Input.dispatchMouseEvent for trusted events.

    All Bezier points are pre-computed in Python and dispatched via CDP,
    producing isTrusted:true mouseMoved events with the full
    pointermove â†’ mousemove chain.

    Cursor continuity: _cursor_pos is used as the start point and updated
    after each call so every arc begins from where the cursor last rested.
    """
    #  DEBUG LOGGING: element interaction audit 
    try:
        _log_element_interaction(driver, target_element, "hover")
    except Exception:
        pass
    # â”€
    try:
        vw   = driver.execute_script("return window.innerWidth")
        vh   = driver.execute_script("return window.innerHeight")
        rect = driver.execute_script(
            "var r=arguments[0].getBoundingClientRect();"
            "return {x:r.left+r.width/2, y:r.top+r.height/2,"
            "        w:r.width, h:r.height};",
            target_element,
        )
        x1 = int(rect["x"])
        y1 = int(rect["y"])
        # Aim offset: humans don't land on the geometric centre.
        # Sigma scales with element size; clamped to Â±35 % of dimension.
        _ew = max(1, int(rect["w"]))
        _eh = max(1, int(rect["h"]))
        off_dx = int(max(-_ew * 0.35, min(random.gauss(0, max(2.0, _ew * 0.12)), _ew * 0.35)))
        off_dy = int(max(-_eh * 0.35, min(random.gauss(0, max(2.0, _eh * 0.12)), _eh * 0.35)))
        # Start from last known position, clamped to current viewport.
        x0 = max(0, min(_get_ctx().cursor_pos[0], int(vw)))
        y0 = max(0, min(_get_ctx().cursor_pos[1], int(vh)))
        # Proximity guard: cursor already within 25 px ,  treat as hovering.
        if math.hypot(x1 - x0, y1 - y0) < 25:
            _cdp_x = int(rect["x"]) + off_dx
            _cdp_y = int(rect["y"]) + off_dy
            try:
                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mouseMoved",
                    "x": _cdp_x,
                    "y": _cdp_y,
                    "pointerType": "mouse",
                    "pressure": 0.0,
                    "tiltX": 0,
                    "tiltY": 0,
                    "twist": 0,
                })
                _cdp_record_success()
            except WebDriverException as exc:
                _cdp_record_failure("bezier_dwell", exc)
            _set_cursor(_cdp_x, _cdp_y, "hover-dwell")
            _mlog.debug("DWELL  cursor within 25px of target  dist=%.1fpx",
                        math.hypot(x1 - x0, y1 - y0))
            debug_cursor_state(driver, "bezier-dwell")
            return
        # Off-viewport correction: scroll element into view then re-query.
        if x1 < 0 or y1 < 0 or x1 > int(vw) or y1 > int(vh):
            driver.execute_script(
                "arguments[0].scrollIntoView({behavior:'instant', block:'center'});",
                target_element,
            )
            precise_sleep(random.uniform(0.3, 0.6))
            rect = driver.execute_script(
                "var r=arguments[0].getBoundingClientRect();"
                "return {x:r.left+r.width/2, y:r.top+r.height/2, w:r.width, h:r.height};",
                target_element,
            )
            x1 = int(rect["x"])
            y1 = int(rect["y"])
            if x1 < 0 or y1 < 0 or x1 > int(vw) or y1 > int(vh):
                _mlog.debug("SKIP  target off-screen after scroll  pos=(%d,%d)", x1, y1)
                return
        # Arc destination incorporates aim offset so JS animation and the
        # Phase 2 ActionChains snap land at the same position.
        arc_x, arc_y = x1 + off_dx, y1 + off_dy
        points, _ = _fire_bezier_arc(driver, x0, y0, arc_x, arc_y, vw, vh)
        # Diagnostic: warn if last synthetic point is far from the snap target.
        snap_x = int(rect["x"]) + off_dx
        snap_y = int(rect["y"]) + off_dy
        if points:
            last_syn_x, last_syn_y = points[-1][0], points[-1][1]
            snap_gap = math.hypot(snap_x - last_syn_x, snap_y - last_syn_y)
            if snap_gap > 10:
                _mlog.warning(
                    "SNAP GAP  last_synthetic=(%d,%d)  snap_target=(%d,%d)  gap=%.1fpx",
                    last_syn_x, last_syn_y, snap_x, snap_y, snap_gap,
                )
            #  DEBUG LOGGING: MOUSE SNAP structured audit 
            _dlog.debug(
                "[MOUSE SNAP]  python_pos=(%d,%d)  snap_target=(%d,%d)  drift=%.1fpx",
                last_syn_x, last_syn_y, snap_x, snap_y, snap_gap,
            )
            if snap_gap > 15:
                _dlog.warning(
                    "[MOUSE SNAP]  WARN drift=%.1fpx > 15px threshold"
                    "  python=(%d,%d)  target=(%d,%d)",
                    snap_gap, last_syn_x, last_syn_y, snap_x, snap_y,
                )
            # 
        # CDP dispatch already produced trusted events at the exact
        # endpoint ,  no Phase 2 ActionChains snap needed.
        _set_cursor(snap_x, snap_y, "elem-hover")
        debug_cursor_state(driver, "bezier-snap")

    except CDPConnectionDead:
        raise   # circuit breaker ,  propagate immediately
    except WebDriverException as exc:
        log.debug("bezier_move failed: %s", exc)

def bezier_move_to_coords(driver, x1: int, y1: int, tag: str = "arc-end") -> None:
    """
    Animate the cursor from _cursor_pos to explicit viewport coordinates
    (x1, y1) along a randomised quadratic Bezier arc at ~60 fps.

    Unlike bezier_move(), no DOM element is required.  Used for:
      â€¢ parking the cursor at y=0 before page navigation  ("nav-park")
      â€¢ idle cursor drift onto content after page load     ("idle-settle")
      â€¢ cursor wanders during reading pauses               ("reading-wander")
      â€¢ hand-shift nudges between scroll chunks            ("scroll-drift")
      â€¢ pre-aim drifts toward a UI region                  ("nav-hover")

    CDP mouseMoved dispatch via _fire_bezier_arc() with exact_end=True
    so the arc lands exactly on the target coordinate.  CDP produces
    trusted events ,  no Phase 2 ActionBuilder snap needed.
    """
    try:
        vw = driver.execute_script("return window.innerWidth")
        vh = driver.execute_script("return window.innerHeight")
        x0 = max(0, min(_get_ctx().cursor_pos[0], int(vw) - 1))
        y0 = max(0, min(_get_ctx().cursor_pos[1], int(vh) - 1))
        x1 = max(0, min(x1, int(vw) - 1))
        y1 = max(0, min(y1, int(vh) - 1))
        if x0 == x1 and y0 == y1:
            return
        #  DEBUG LOGGING 
        _dlog.debug("[CURSOR MOVE]  tag=%s  from=(%d,%d)  to=(%d,%d)  dist=%.0fpx",
                    tag, x0, y0, x1, y1, math.hypot(x1 - x0, y1 - y0))
        # 
        _fire_bezier_arc(driver, x0, y0, x1, y1, vw, vh, exact_end=True)
        _set_cursor(x1, y1, tag)
        debug_cursor_state(driver, f"bezier-coords/{tag}")
    except CDPConnectionDead:
        raise   # circuit breaker ,  propagate immediately
    except WebDriverException as exc:
        log.debug("bezier_move_to_coords failed: %s", exc)

def _bezier_point(p0, p1, p2, t):
    """Quadratic Bezier interpolation between three 2-D control points."""
    x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
    y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
    return int(x), int(y)


# _ease_in_out_sine() REMOVED ,  symmetric sine ease was a profile-level
# tell repetitive by trajectory classifiers.  Replaced by _min_jerk_basis()
# which produces the asymmetric velocity profile of real arm movements.


def _min_jerk_basis(t: float) -> float:
    """Minimum-jerk position basis: 10t^3 - 15t^4 + 6t^5  (Flash & Hogan 1985).

    Produces an asymmetric velocity profile peaking at t ~ 0.47 -- faster
    acceleration and slower deceleration -- matching real arm-movement
    kinematics.  Replaces the symmetric _ease_in_out_sine() which was a
    repetitive timing pattern repetitive by trajectory classifiers.
    """
    t2 = t * t
    t3 = t2 * t
    return t3 * (10.0 - 15.0 * t + 6.0 * t2)


def _min_jerk_velocity(t: float) -> float:
    """Normalised minimum-jerk speed: 30t^2 - 60t^3 + 30t^4.

    Derivative of _min_jerk_basis().  Peak value is ~1.875 at t ~ 0.5.
    """
    t2 = t * t
    return 30.0 * t2 - 60.0 * t2 * t + 30.0 * t2 * t2


def _fitts_duration_ms(distance: float, target_width: float = 40.0) -> float:
    """Fitts's Law movement time:  T = a + b * log2(D / W + 1).

    Parameters from motor-control literature with +/-15 % jitter so the
    duration is plausible but never deterministic.
        a ~ 150 ms  (reaction + initiation overhead)
        b ~ 120 ms  (information-processing rate)
    """
    a = 150.0 * random.uniform(0.85, 1.15)
    b = 120.0 * random.uniform(0.85, 1.15)
    id_bits = math.log2(max(1.0, distance) / max(1.0, target_width) + 1.0)
    return max(180.0, a + b * id_bits)


def _make_tremor_components(n: int = 3) -> list:
    """Generate physiological-tremor sinusoid parameters at 8-12 Hz.

    Human hand tremor is narrow-band (8-12 Hz) -- not white Gaussian noise.
    Returns list of (freq_hz, amplitude, phase) tuples.
    """
    return [
        (random.uniform(8.0, 12.0), random.uniform(0.3, 1.5),
         random.uniform(0.0, 2.0 * math.pi))
        for _ in range(n)
    ]


# Cursor pos, last bezier timestamp, and CDP failure count are now tracked
# per-thread in SessionContext (see _get_ctx()).  Access them via
# _get_ctx().cursor_pos / .last_bezier_end_ts / .cdp_consecutive_failures.
_CDP_FAILURE_THRESHOLD: int = 5


class CDPConnectionDead(WebDriverException):
    """Raised when the CDP circuit breaker trips."""
    pass


def _cdp_record_success() -> None:
    """Reset the consecutive-failure counter on a successful CDP call."""
    _get_ctx().cdp_consecutive_failures = 0


def _cdp_record_failure(context: str, exc: Exception) -> None:
    """Record a CDP failure and trip the circuit breaker if threshold exceeded."""
    ctx = _get_ctx()
    ctx.cdp_consecutive_failures += 1
    log.warning(
        "[CDP CIRCUIT]  failure %d/%d  context=%s  error=%s",
        ctx.cdp_consecutive_failures, _CDP_FAILURE_THRESHOLD, context, exc,
    )
    if ctx.cdp_consecutive_failures >= _CDP_FAILURE_THRESHOLD:
        log.error(
            "[CDP CIRCUIT]  TRIPPED ,  %d consecutive failures.  "
            "Browser connection presumed dead.", ctx.cdp_consecutive_failures,
        )
        raise CDPConnectionDead(
            f"CDP circuit breaker tripped after {ctx.cdp_consecutive_failures} "
            f"consecutive failures (last context: {context})"
        ) from exc


def _set_cursor(x: int, y: int, tag: str = "") -> None:
    """
    Update _cursor_pos and emit a compact one-line position log to BOTH the
    dedicated mouse-movement file (_mlog) AND the main console (log.info),
    so every cursor coordinate change is visible in the live run output.

    Low-level arc detail (ARC / STEP lines) continues to go only to _mlog.
    This function covers the final settled position after each move.
    """
    _get_ctx().cursor_pos[0], _get_ctx().cursor_pos[1] = x, y
    label = f"  [{tag}]" if tag else ""
    _mlog.debug("CURSOR  (%d, %d)%s", x, y, label)




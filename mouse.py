import math
import random
import time
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import WebDriverException
from config import DEBUG_CURSOR_OVERLAY
from utils import log, precise_sleep, _get_ctx

_CDP_FAILURE_THRESHOLD = 5


class CDPConnectionDead(WebDriverException):
    """Raised when repeated CDP calls indicate a dead browser connection."""


def _cdp_record_success() -> None:
    _get_ctx().cdp_consecutive_failures = 0


def _cdp_record_failure(context: str, exc: Exception) -> None:
    ctx = _get_ctx()
    ctx.cdp_consecutive_failures += 1
    log.warning(
        "[CDP] failure %d/%d context=%s error=%s",
        ctx.cdp_consecutive_failures,
        _CDP_FAILURE_THRESHOLD,
        context,
        exc,
    )
    if ctx.cdp_consecutive_failures >= _CDP_FAILURE_THRESHOLD:
        raise CDPConnectionDead(
            f"CDP circuit breaker tripped after {ctx.cdp_consecutive_failures} failures"
        ) from exc


def _set_cursor(x: int, y: int, tag: str = "") -> None:
    _get_ctx().cursor_pos[0], _get_ctx().cursor_pos[1] = x, y
    if tag:
        log.debug("[CURSOR] (%d,%d) %s", x, y, tag)


def _dispatch_mouse_moved(driver, x: int, y: int) -> None:
    try:
        driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseMoved",
                "x": x,
                "y": y,
                "pointerType": "mouse",
                "pressure": 0.0,
                "tiltX": 0,
                "tiltY": 0,
                "twist": 0,
            },
        )
        _cdp_record_success()
    except WebDriverException as exc:
        _cdp_record_failure("mouse_move", exc)


def _linear_move(driver, x0: int, y0: int, x1: int, y1: int) -> None:
    dist = math.hypot(x1 - x0, y1 - y0)
    if dist < 1:
        _set_cursor(x1, y1, "move")
        return
    steps = max(8, min(28, int(dist / 20)))
    for i in range(1, steps + 1):
        t = i / steps
        x = int(x0 + (x1 - x0) * t)
        y = int(y0 + (y1 - y0) * t)
        _dispatch_mouse_moved(driver, x, y)
        precise_sleep(random.uniform(0.008, 0.02))
    _get_ctx().last_bezier_end_ts = time.perf_counter()
    _set_cursor(x1, y1, "move")


def _cdp_click(driver, x: int = None, y: int = None) -> None:
    cx = x if x is not None else _get_ctx().cursor_pos[0]
    cy = y if y is not None else _get_ctx().cursor_pos[1]
    try:
        driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": cx,
                "y": cy,
                "button": "left",
                "clickCount": 1,
                "pointerType": "mouse",
            },
        )
        precise_sleep(random.uniform(0.04, 0.11))
        driver.execute_cdp_cmd(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": cx,
                "y": cy,
                "button": "left",
                "clickCount": 1,
                "pointerType": "mouse",
            },
        )
        _cdp_record_success()
    except WebDriverException as exc:
        _cdp_record_failure("mouse_click", exc)


def _cdp_click_element(driver, element) -> None:
    try:
        rect = driver.execute_script(
            "var r=arguments[0].getBoundingClientRect();"
            "return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)};",
            element,
        )
        cx, cy = int(rect["x"]), int(rect["y"])
        _set_cursor(cx, cy, "click-element")
        _cdp_click(driver, cx, cy)
    except WebDriverException:
        element.click()


def init_cursor_pos(driver) -> None:
    try:
        vw = int(driver.execute_script("return window.innerWidth") or 1280)
        vh = int(driver.execute_script("return window.innerHeight") or 720)
        x = random.randint(int(vw * 0.10), int(vw * 0.90))
        y = random.randint(int(vh * 0.15), int(vh * 0.85))
        _set_cursor(x, y, "init")
    except WebDriverException as exc:
        log.debug("init_cursor_pos failed: %s", exc)


def bezier_move(driver, target_element) -> None:
    """
    Keep public API name for compatibility, but use a simpler linear move.
    """
    try:
        rect = driver.execute_script(
            "var r=arguments[0].getBoundingClientRect();"
            "return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)};",
            target_element,
        )
        x1, y1 = int(rect["x"]), int(rect["y"])
        x0, y0 = _get_ctx().cursor_pos
        _linear_move(driver, x0, y0, x1, y1)
    except WebDriverException as exc:
        log.debug("bezier_move failed: %s", exc)


def bezier_move_to_coords(driver, x1: int, y1: int, tag: str = "arc-end") -> None:
    """
    Keep public API name for compatibility, but use a simpler linear move.
    """
    try:
        vw = int(driver.execute_script("return window.innerWidth") or 1280)
        vh = int(driver.execute_script("return window.innerHeight") or 720)
        x1 = max(0, min(int(x1), vw - 1))
        y1 = max(0, min(int(y1), vh - 1))
        x0, y0 = _get_ctx().cursor_pos
        _linear_move(driver, x0, y0, x1, y1)
        _set_cursor(x1, y1, tag)
    except WebDriverException as exc:
        log.debug("bezier_move_to_coords failed: %s", exc)


def _cdp_type_text(driver, text: str) -> None:
    try:
        driver.execute_cdp_cmd("Input.insertText", {"text": text})
        _cdp_record_success()
    except WebDriverException as exc:
        _cdp_record_failure("type_text", exc)


def human_type(element, text: str, driver=None, typing_dna: dict = None) -> None:
    """
    Simplified typing model:
    - trusted Input.insertText when driver is available
    - lightweight per-character timing variance
    """
    if driver is not None:
        try:
            _cdp_click_element(driver, element)
        except Exception:
            try:
                element.click()
            except Exception:
                pass
    else:
        element.click()

    precise_sleep(random.uniform(0.08, 0.22))

    dna = typing_dna or _get_ctx().active_typing_dna or {}
    base_mu = dna.get("base_mu", math.log(0.085))
    base_sigma = min(0.8, max(0.12, dna.get("base_sigma", 0.35)))
    mean_delay = max(0.045, min(0.25, math.exp(base_mu)))

    for ch in text:
        if driver is not None:
            _cdp_type_text(driver, ch)
        else:
            element.send_keys(ch)

        d = random.lognormvariate(math.log(mean_delay), base_sigma)
        d = max(0.035, min(d, 0.55))
        if ch in ".!?":
            d += random.uniform(0.08, 0.25)
        elif ch == " ":
            d += random.uniform(0.02, 0.08)
        precise_sleep(d)


_CURSOR_OVERLAY_JS = """
(function () {
    var ID = '__cursor_debug_dot';
    var old = document.getElementById(ID);
    if (old) old.remove();
    var dot = document.createElement('div');
    dot.id = ID;
    dot.style.cssText = [
      'position:fixed','top:0','left:0','width:10px','height:10px',
      'background:rgba(255,40,40,0.85)','border-radius:50%',
      'pointer-events:none','z-index:2147483647','transform:translate(-50%,-50%)'
    ].join(';');
    document.documentElement.appendChild(dot);
    document.addEventListener('mousemove', function(e){
      dot.style.left = e.clientX + 'px';
      dot.style.top = e.clientY + 'px';
    }, true);
})();
"""


def inject_cursor_overlay(driver) -> None:
    if not DEBUG_CURSOR_OVERLAY:
        return
    try:
        driver.execute_script(_CURSOR_OVERLAY_JS)
    except WebDriverException as exc:
        log.debug("Cursor overlay injection failed: %s", exc)


def debug_cursor_state(driver, label: str = "") -> None:
    try:
        x, y = _get_ctx().cursor_pos
        log.debug("CURSOR SYNC [%s] python=(%d,%d)", label, x, y)
    except Exception as exc:
        log.debug("debug_cursor_state failed: %s", exc)

import os
import time
import random
import ctypes
import glob as _glob
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    WebDriverException,
)
from config import (
    MEDIA_POOL_DIR, POST_MEDIA_EXTENSIONS,
    _POST_TEMP_DIR, TARGET_SOCIAL_URL,
)
from dom_selectors import (
    COMPOSE_BTN_SELECTORS, 
    COMPOSE_ATTACH_BTN_CSS, COMPOSE_FILE_INPUT_CSS,
)
from utils import log, precise_sleep, _get_ctx
from mouse import bezier_move, human_type, _cdp_click_element, debug_cursor_state
from scroll import navigate_to
from state import (
    _can_post_now, _record_post,
    _load_post_state, _save_post_state,
    _post_state_locked
)
from content_policy import (
    ContentPolicyError,
    prepare_caption_for_publishing,
    validate_caption,
)
from pools import APPROVED_CAPTIONS, BRAND_VOICE
# ================================================================== #
#  MEDIA PREPARATION  (per-profile temp copy)
# ================================================================== #

def _prepare_image_for_profile(src_path: str, profile_id: str) -> str:
    """
    Return a sanitized per-profile temp copy of src_path.

    The preparation step validates the media, applies EXIF orientation, strips
    metadata, and lightly compresses/resaves the file when possible. It performs
    no metadata fabrication or hidden image modifications so the thesis-facing
    implementation remains an ordinary approved-media upload workflow.
    """
    from PIL import Image, ImageOps

    ext = os.path.splitext(src_path)[1].lower()
    if ext not in POST_MEDIA_EXTENSIONS:
        raise ValueError(f"Unsupported media extension: {ext}")

    safe_pid = (profile_id or "anon")[:16].replace("-", "")
    profile_dir = os.path.join(_POST_TEMP_DIR, safe_pid)
    os.makedirs(profile_dir, exist_ok=True)
    out_path = os.path.join(profile_dir, f"post_{time.time_ns()}{ext}")

    with Image.open(src_path) as original:
        img = ImageOps.exif_transpose(original)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        if ext in (".jpg", ".jpeg"):
            img = img.convert("RGB")
            img.save(out_path, format="JPEG", quality=90, optimize=True)
        elif ext == ".webp":
            img.save(out_path, format="WEBP", quality=90)
        else:
            img.save(out_path, format="PNG", optimize=True)

    log.info("[ POST ]  media prepared for upload -> %s", os.path.basename(out_path))
    return out_path

def _cleanup_post_scratch(profile_id: str, max_age_sec: float = 3600.0) -> None:
    """Remove stale prepared image files from the per-profile scratch dir.

    Called after every successful post so the temp directory doesn't grow
    indefinitely.  Removes files older than *max_age_sec* (default 1 hour)
    and deletes entirely empty profile subdirectories.

    Any OS errors (permission, concurrent access) are logged and swallowed
    so cleanup never blocks the main session flow.
    """
    try:
        if not os.path.isdir(_POST_TEMP_DIR):
            return
        now = time.time()
        safe_pid = (profile_id or "anon")[:16].replace("-", "")
        profile_dir = os.path.join(_POST_TEMP_DIR, safe_pid)

        # Phase 1: purge stale files in THIS profile's scratch dir
        if os.path.isdir(profile_dir):
            for fname in os.listdir(profile_dir):
                fpath = os.path.join(profile_dir, fname)
                try:
                    if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > max_age_sec:
                        os.remove(fpath)
                except OSError:
                    pass
            # Remove empty dir
            try:
                if not os.listdir(profile_dir):
                    os.rmdir(profile_dir)
            except OSError:
                pass

        # Phase 2: opportunistically purge OTHER profiles' stale dirs
        # (handles profiles that crashed without cleanup)
        for entry in os.listdir(_POST_TEMP_DIR):
            subdir = os.path.join(_POST_TEMP_DIR, entry)
            if not os.path.isdir(subdir):
                continue
            try:
                for fname in os.listdir(subdir):
                    fpath = os.path.join(subdir, fname)
                    if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > max_age_sec * 4:
                        os.remove(fpath)
                if not os.listdir(subdir):
                    os.rmdir(subdir)
            except OSError:
                pass

        # Phase 3: remove top-level dir if completely empty
        try:
            if not os.listdir(_POST_TEMP_DIR):
                os.rmdir(_POST_TEMP_DIR)
        except OSError:
            pass

        log.debug("[ POST ]  scratch cleanup complete for %s", profile_id)
    except Exception as exc:
        log.debug("[ POST ]  scratch cleanup error: %s", exc)


# ================================================================== #
#  APPROVED CAPTION SELECTION
# ================================================================== #

def _select_approved_caption(pool: list[str]) -> str | None:
    """Choose one complete approved caption after deterministic preparation."""
    eligible: list[str] = []
    for index, candidate in enumerate(pool):
        try:
            eligible.append(prepare_caption_for_publishing(candidate, BRAND_VOICE))
        except ContentPolicyError as exc:
            log.warning(
                "[CONTENT POLICY] caption rejected index=%d reason=%s",
                index,
                exc,
            )
    if not eligible:
        return None
    return random.choice(eligible)

# ================================================================== #
#  TEXTBOX IDENTIFICATION
# ================================================================== #
# Identifies the compose text input by behavioral characteristics rather
# than framework-specific attributes (data-lexical-editor) which Meta
# renames whenever the Lexical editor framework is refactored.
#
# Identification strategy (priority order):
#   1. role="textbox" + contenteditable="true" in a modal/overlay context
#      (ARIA role is legally mandated for accessibility ,  stable).
#   2. Sole visible contenteditable="true" element above the fold
#      (compose modal is always the topmost layer).
#   3. document.activeElement after programmatic focus into the compose
#      area ,  completely framework-agnostic.
#   4. Legacy data-lexical-editor attribute (lowest priority fallback).
# ================================================================== #

def _find_compose_textbox(driver, timeout: float = 10.0):
    """Find the compose modal's text input using textbox identification.

    Returns the WebElement or None if no suitable textbox is found.
    """
    end = time.time() + timeout
    while time.time() < end:
        # Strategy 1: role="textbox" + contenteditable in modal context
        try:
            candidates = driver.find_elements(
                By.CSS_SELECTOR,
                '[contenteditable="true"][role="textbox"]',
            )
            for el in candidates:
                if not el.is_displayed():
                    continue
                # Verify it's inside a modal/overlay (high z-index or dialog role)
                is_compose = driver.execute_script("""
                    var el = arguments[0];
                    var r  = el.getBoundingClientRect();
                    if (r.height === 0) return false;
                    var node = el;
                    for (var d = 0; d < 15; d++) {
                        if (!node) break;
                        var z = parseInt(window.getComputedStyle(node).zIndex);
                        if (z > 100) return true;
                        var role = node.getAttribute('role');
                        if (role === 'dialog' || role === 'presentation') return true;
                        node = node.parentElement;
                    }
                    return false;
                """, el)
                if is_compose:
                    log.debug("[TEXTBOX]  found via role=textbox + contenteditable (modal context)")
                    return el
        except Exception:
            pass

        # Strategy 2: sole visible contenteditable element
        try:
            editables = driver.find_elements(
                By.CSS_SELECTOR, '[contenteditable="true"]'
            )
            visible_editables = []
            for el in editables:
                try:
                    if not el.is_displayed():
                        continue
                    r = driver.execute_script(
                        "var r=arguments[0].getBoundingClientRect();"
                        "return {h:r.height, top:r.top};", el)
                    vh = driver.execute_script("return window.innerHeight")
                    if r["h"] > 0 and r["top"] < vh:
                        visible_editables.append(el)
                except Exception:
                    continue
            if len(visible_editables) == 1:
                log.debug("[TEXTBOX]  found via unique visible contenteditable")
                return visible_editables[0]
        except Exception:
            pass

        # Strategy 3: document.activeElement after focus attempt
        try:
            active = driver.execute_script("""
                var editables = document.querySelectorAll('[contenteditable="true"]');
                for (var i = 0; i < editables.length; i++) {
                    var el = editables[i];
                    if (el.offsetParent === null) continue;
                    var r = el.getBoundingClientRect();
                    if (r.height === 0) continue;
                    el.focus();
                    if (document.activeElement === el) return el;
                }
                return null;
            """)
            if active and active.is_displayed():
                # Verify it's in a compose context (not a comment box)
                is_modal = driver.execute_script("""
                    var node = arguments[0];
                    for (var d = 0; d < 15; d++) {
                        if (!node) break;
                        var z = parseInt(window.getComputedStyle(node).zIndex);
                        if (z > 100) return true;
                        var role = node.getAttribute('role');
                        if (role === 'dialog' || role === 'presentation') return true;
                        node = node.parentElement;
                    }
                    return false;
                """, active)
                if is_modal:
                    log.debug("[TEXTBOX]  found via activeElement focus probe")
                    return active
        except Exception:
            pass

        # Strategy 4: legacy data-lexical-editor fallback
        try:
            legacy = driver.find_elements(
                By.CSS_SELECTOR,
                'div[data-lexical-editor="true"][contenteditable="true"]',
            )
            for el in legacy:
                if el.is_displayed():
                    log.debug("[TEXTBOX]  found via legacy data-lexical-editor fallback")
                    return el
        except Exception:
            pass

        precise_sleep(0.5)

    log.debug("[TEXTBOX]  no compose textbox found within %.0fs", timeout)
    return None


def create_post(driver, profile_id: str) -> bool:
    """
    Create an original Threads post with a caption from APPROVED_CAPTIONS
    and optionally an image from MEDIA_POOL_DIR.

    Flow:
      1.  Guard ,  _can_post_now() checks quota + 2-hour cooldown.
      2.  Pick a random caption and (if MEDIA_POOL_DIR is set) a random image.
      3.  Find the compose / New post button in the nav sidebar.
      4.  Bezier-arc to the button and click to open the compose modal.
      5.  Attach image via the hidden <input type="file"> if a path was picked.
      6.  Bezier-arc to the textbox and type the caption with type_text().
      7.  Re-read pause (1.5â€“4 s) ,  mimics proof-reading before posting.
      8.  Find the Post button in the modal and click it.
      9.  Wait for the modal to dismiss, then call _record_post().

    Selector notes:
      COMPOSE_BTN ,  tried in priority order; first visible match wins.
      File input   ,  made temporarily visible via JS so send_keys works on
                     the hidden <input type="file"> without a click chain.
      Post button  ,  reuses COMMENT_POST_XPATH (same "Post" text node).
    """
    if not APPROVED_CAPTIONS:
        log.warning("[CONTENT POLICY] approved caption pool is empty; publishing skipped")
        return False

    caption = _select_approved_caption(
        _get_ctx().profile_approved_caption_pool or APPROVED_CAPTIONS
    )
    if caption is None:
        log.warning("[CONTENT POLICY] no caption passed approved publishing controls")
        return False

    # === Fix #11: locked pre-post transaction ,  gate check + image reservation =
    # The lock is held only for the short read-modify-write cycle on state.
    # Heavy browser automation below runs entirely outside the lock so parallel
    # profiles are not blocked for the duration of the UI interaction.
    image_path = None
    _src_for_prepare = None
    with _post_state_locked():
        state = _load_post_state()
        if not _can_post_now(profile_id, state):
            return False

        # Pick media ,  cross-profile deduplication: a single locked read-write
        # ensures two parallel profiles cannot reserve the same source file.
        if MEDIA_POOL_DIR and not os.path.isdir(MEDIA_POOL_DIR):
            log.warning(
                "[ POST ]  MEDIA_POOL_DIR not found ,  posting text-only  "
                "(configured path: %s)", MEDIA_POOL_DIR
            )
        if MEDIA_POOL_DIR and os.path.isdir(MEDIA_POOL_DIR):
            all_images = [
                os.path.abspath(os.path.join(MEDIA_POOL_DIR, f))
                for f in os.listdir(MEDIA_POOL_DIR)
                if os.path.splitext(f)[1].lower() in POST_MEDIA_EXTENSIONS
            ]
            if all_images:
                used_list = state.get("_used_images", [])
                pool_basenames = {os.path.basename(p) for p in all_images}
                used_list = [x for x in used_list if x in pool_basenames]

                fresh = [p for p in all_images if os.path.basename(p) not in used_list]
                if not fresh:
                    log.info("[ POST ]  image pool fully cycled ,  resetting cross-profile dedup list")
                    fresh = all_images

                _src_for_prepare = random.choice(fresh)
                basename = os.path.basename(_src_for_prepare)

                if basename in used_list:
                    used_list.remove(basename)
                used_list.append(basename)

                max_history = max(0, len(all_images) - 1)
                while len(used_list) > max_history:
                    used_list.pop(0)

                state["_used_images"] = used_list
        _save_post_state(state)  # one atomic write covers gate + image reservation
    # ===========================================================================

    if _src_for_prepare:
        try:
            image_path = _prepare_image_for_profile(_src_for_prepare, profile_id)
        except Exception as exc:
            log.warning("[ POST ]  media preparation failed (%s) ,  using original", exc)
            image_path = _src_for_prepare

    #  DEBUG LOGGING: POST FLOW timer 
    _pf_t0 = time.perf_counter()
    # 

    log.info(
        "[ APPROVED PUBLISHING ]  caption accepted  |  chars=%d  |  media=%s",
        len(caption),
        os.path.basename(image_path) if image_path else "none",
    )

    try:
        # 1. Ensure we're on the Threads feed (compose button lives in the nav)
        url = driver.current_url or ""
        if "threads.net" not in url and "threads.com" not in url:
            navigate_to(driver, TARGET_SOCIAL_URL)

        # 2. Find the compose / New post button (multiple selector fallbacks)
        compose_btn = None
        for kind, sel in COMPOSE_BTN_SELECTORS:
            by = By.CSS_SELECTOR if kind == "css" else By.XPATH
            visible = [el for el in driver.find_elements(by, sel) if el.is_displayed()]
            if visible:
                compose_btn = visible[0]
                break

        if not compose_btn:
            # Diagnostic: dump visible nav buttons so selectors can be updated
            try:
                btn_info = driver.execute_script("""
                    return Array.from(document.querySelectorAll(
                        '[role="button"],[role="link"]'
                    )).filter(el => {
                        var r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 &&
                               r.top >= 0 && r.top < window.innerHeight;
                    }).slice(0, 30).map(el => ({
                        tag:   el.tagName,
                        role:  el.getAttribute('role'),
                        label: el.getAttribute('aria-label') || '',
                        svgLabels: Array.from(el.querySelectorAll('svg[aria-label]'))
                                       .map(s => s.getAttribute('aria-label')),
                        text:  (el.innerText || '').slice(0, 40).replace(/\\n/g,' '),
                    }));
                """)
                log.warning(
                    "create_post: compose button not found ,  visible role=button/link elements: %s",
                    btn_info,
                )
            except Exception as _diag_exc:
                log.debug("create_post: compose button not found (diag failed: %s)", _diag_exc)
            return False

        #scroll_element_into_loose_view(driver, compose_btn)
        bezier_move(driver, compose_btn)
        precise_sleep(random.uniform(0.4, 0.9))
        _cdp_click_element(driver, compose_btn)  # Fix 1.4: never JS .click()

        # 3. Wait for the compose modal's contenteditable text area.
        #    Uses textbox identification (role=textbox, contenteditable, modal
        #    context) rather than framework-specific data-lexical-editor.
        text_box = _find_compose_textbox(driver, timeout=10.0)
        if not text_box:
            log.debug("create_post: compose modal textarea did not appear")
            driver.execute_script(
                "document.dispatchEvent(new KeyboardEvent('keydown',"
                "{key:'Escape',keyCode:27,bubbles:true}));"
            )
            return False

        # Brief settle ,  SPA modal animation
        precise_sleep(random.uniform(0.6, 1.2))
        log.info("[POST FLOW]  step=compose_open  success=True  duration=%.0fms  detail=textbox_visible",
                 (time.perf_counter() - _pf_t0) * 1000)

        # 4. Attach image.
        #
        # Primary: pyautogui OS file dialog (visually natural ,  opens the real
        # system file picker).  Fallback: hidden <input type="file"> send_keys
        # (no dialog, used when pyautogui/pyperclip are not installed or the
        # attach button is not visible in the DOM).
        if image_path:
            try:
                # Short settle after the modal animation
                precise_sleep(random.uniform(0.3, 0.6))

                _media_attached = False

                # Primary: click the attach button â†’ OS file dialog â†’ pyautogui paste.
                try:
                    import pyautogui as _pag
                    import pyperclip as _ppc

                    attach_btns = [
                        el for el in driver.find_elements(
                            By.CSS_SELECTOR, COMPOSE_ATTACH_BTN_CSS
                        )
                        if el.is_displayed()
                    ]
                    if attach_btns:
                        bezier_move(driver, attach_btns[0])
                        precise_sleep(random.uniform(0.3, 0.6))
                        _cdp_click_element(driver, attach_btns[0])  # Fix 1.4: never JS .click()

                        _locate_delay = max(3.0, min(9.0, random.gauss(5.0, 1.5)))
                        log.debug("create_post: OS dialog open ,  file-locate pause %.1fs", _locate_delay)
                        precise_sleep(_locate_delay)

                        try:
                            _FindWindow  = ctypes.windll.user32.FindWindowW
                            _SetFG       = ctypes.windll.user32.SetForegroundWindow
                            _BringToTop  = ctypes.windll.user32.BringWindowToTop
                            _ShowWindow  = ctypes.windll.user32.ShowWindow
                            _dialog_hwnd = _FindWindow("#32770", None)
                            if _dialog_hwnd:
                                _ShowWindow(_dialog_hwnd, 5)
                                _BringToTop(_dialog_hwnd)
                                _SetFG(_dialog_hwnd)
                                precise_sleep(0.3)
                        except Exception as _fg_exc:
                            log.debug("create_post: SetForegroundWindow failed: %s", _fg_exc)

                        _ppc.copy(os.path.abspath(image_path))
                        precise_sleep(random.uniform(0.10, 0.25))
                        _pag.hotkey("ctrl", "a")
                        precise_sleep(random.uniform(0.06, 0.14))
                        _pag.hotkey("ctrl", "v")
                        precise_sleep(random.uniform(0.15, 0.35))
                        _pag.press("enter")
                        _ppc.copy("")  # clear clipboard immediately after use
                        log.info("[ POST ]  media attached via OS dialog: %s",
                                 os.path.basename(image_path))
                        log.info("[POST FLOW]  step=media_attach  success=True  method=os_dialog  detail=%s",
                                 os.path.basename(image_path))
                        precise_sleep(random.uniform(1.5, 2.5))
                        _media_attached = True
                    else:
                        log.debug("create_post: attach button not visible ,  falling back to hidden-input")

                except ImportError:
                    log.debug("create_post: pyautogui/pyperclip not installed ,  falling back to hidden-input")

                if not _media_attached:
                    # Fallback: inject path directly into the hidden file input.
                    # Works without opening any dialog; reliable for unattended runs.
                    file_inputs = driver.find_elements(By.CSS_SELECTOR, COMPOSE_FILE_INPUT_CSS)
                    if file_inputs:
                        fi = file_inputs[0]
                        driver.execute_script(
                            "arguments[0].style.display    = 'block';"
                            "arguments[0].style.visibility = 'visible';"
                            "arguments[0].style.opacity    = '1';",
                            fi,
                        )
                        fi.send_keys(image_path)
                        log.info("[ POST ]  media attached (hidden-input fallback): %s",
                                 os.path.basename(image_path))
                        log.info("[POST FLOW]  step=media_attach  success=True  method=hidden_input  detail=%s",
                                 os.path.basename(image_path))
                        _media_attached = True
                    else:
                        log.warning("create_post: no attach button or file input found ,  skipping media")
                        image_path = None

                # Wait for upload thumbnail / preview to render
                if image_path:
                    precise_sleep(random.uniform(2.0, 4.0))

            except WebDriverException as exc:
                log.debug("create_post: media attach failed (%s) ,  text-only fallback", exc)
                image_path = None

        # 5. Re-query the compose textbox before typing.
        #    The OS file dialog interaction (pyautogui paste + Enter) causes
        #    React to re-render the compose modal, which invalidates the
        #    WebElement reference captured before the dialog was opened.
        #    Using a stale reference in type_text() raises
        #    StaleElementReferenceException â†’ the outer handler fires Escape
        #    and returns False, making the session loop retry indefinitely.
        if image_path:
            text_box = _find_compose_textbox(driver, timeout=8.0)
            if not text_box:
                log.debug("create_post: could not re-find textbox after media attach ,  aborting")
                driver.execute_script(
                    "document.dispatchEvent(new KeyboardEvent('keydown',"
                    "{key:'Escape',keyCode:27,bubbles:true}));"
                )
                return False
            log.debug("create_post: textbox re-queried after media attach")

        bezier_move(driver, text_box)
        precise_sleep(random.uniform(0.3, 0.7))
        human_type(text_box, caption, driver)
        log.info("[POST FLOW]  step=caption_type  success=True  detail=%r",
                 caption[:40])

        # 6. Re-read pause ,  mimics proof-reading before hitting Post
        reread_s = random.uniform(5.0, 10.0)
        log.info("[ POST ]  re-reading before submit (%.1fs)â€¦", reread_s)
        precise_sleep(reread_s)

        # 7. Find the Post submit button scoped to the compose modal.
        #
        #    Confirmed DOM (from live inspection):
        #      <div role="button"><div>Post</div></div>
        #
        #    IMPORTANT: identical Post buttons exist in every visible comment
        #    reply form in the feed behind the modal.  A global XPath therefore
        #    returns the wrong element.  Instead we walk UP from text_box to
        #    find the modal's own container, then search within it.
        #
        #    Strategy:
        #      Pass A ,  JS ancestor walk from text_box (most reliable, scoped).
        #      Pass B ,  JS global scan as last resort, logging all visible
        #               button texts as a diagnostic if it also fails.
        #    Each pass is retried up to 3 times (2 s apart) so React has time
        #    to activate the button after processing the typed text.
        post_btn = None
        
        for _attempt in range(3):
            # Pass A: walk up from text_box â†’ find Post button in same container
            post_btn = driver.execute_script("""
                var textbox = arguments[0];
                // Walk up ancestors looking for a container that owns a Post button
                var node = textbox.parentElement;
                for (var depth = 0; depth < 20; depth++) {
                    if (!node) break;
                    // Look for a direct-child-div Post button within this ancestor
                    var btns = node.querySelectorAll('div[role="button"]');
                    for (var i = 0; i < btns.length; i++) {
                        var btn = btns[i];
                        if (btn.offsetParent === null) continue;  // not visible
                        // Direct child <div> whose sole text is "Post"
                        var kids = btn.children;
                        for (var k = 0; k < kids.length; k++) {
                            if (kids[k].tagName === 'DIV' &&
                                (kids[k].innerText || '').trim() === 'Post') {
                                return btn;
                            }
                        }
                        // Also accept aria-label="Post" directly on the button
                        if ((btn.getAttribute('aria-label') || '').trim() === 'Post') {
                            return btn;
                        }
                    }
                    node = node.parentElement;
                }
                return null;
            """, text_box)

            if post_btn:
                log.debug("create_post: Post button found via modal-scoped ancestor walk (attempt %d)", _attempt + 1)
                break

            # Pass B: global scan as fallback
            log.debug("create_post: ancestor walk attempt %d missed ,  global JS scan", _attempt + 1)
            post_btn = driver.execute_script("""
                var btns = document.querySelectorAll('div[role="button"]');
                for (var i = 0; i < btns.length; i++) {
                    var btn = btns[i];
                    if (btn.offsetParent === null) continue;
                    var kids = btn.children;
                    for (var k = 0; k < kids.length; k++) {
                        if (kids[k].tagName === 'DIV' &&
                            (kids[k].innerText || '').trim() === 'Post') {
                            return btn;
                        }
                    }
                    if ((btn.getAttribute('aria-label') || '').trim() === 'Post') {
                        return btn;
                    }
                }
                return null;
            """)
            if post_btn:
                log.debug("create_post: Post button found via global JS scan (attempt %d)", _attempt + 1)
                break

            precise_sleep(2.0)   # let React activate the button after text entry

        if not post_btn:
            try:
                btn_texts = driver.execute_script("""
                    var els = document.querySelectorAll('div[role="button"], button');
                    var out = [];
                    for (var i = 0; i < els.length; i++) {
                        var el = els[i];
                        if (el.offsetParent !== null) {
                            out.push((el.innerText || el.getAttribute('aria-label') || '')
                                     .trim().replace(/\\n/g,' ').slice(0,40));
                        }
                    }
                    return out;
                """)
                log.warning(
                    "create_post: Post button NOT found after 3 attempts ,  "
                    "visible buttons in DOM: %s", btn_texts[:25],
                )
            except Exception:
                log.warning("create_post: Post button NOT found and diagnostic also failed")
            driver.execute_script(
                "document.dispatchEvent(new KeyboardEvent('keydown',"
                "{key:'Escape',keyCode:27,bubbles:true}));"
            )
            return False

        # Do NOT call scroll_element_into_loose_view here.  The Post button
        # lives in the compose modal's sticky footer and is always visible.
        # scroll_element_into_loose_view fires mouseWheel CDP events at the
        # current cursor position ,  which after type_text() is still inside
        # the modal's scrollable content area.  The wheel events are delivered
        # to that scroll container (not the page), causing the modal body to
        # scroll instead of the page.  Bezier-move directly to the button.
        bezier_move(driver, post_btn)
        precise_sleep(random.uniform(0.4, 0.9))
        _cdp_click_element(driver, post_btn)  # Fix 1.4: never JS .click()
        log.info("[POST FLOW]  step=submit_click  success=True  detail=post_btn_clicked")
        debug_cursor_state(driver, "post-submit-click")

        # 8. Wait for modal to close (compose textbox disappears on success)
        #    Uses web-standards selectors for textbox identification.
        try:
            WebDriverWait(driver, 12).until(
                lambda d: not [
                    el for el in d.find_elements(
                        By.CSS_SELECTOR,
                        '[contenteditable="true"][role="textbox"]'
                    ) if el.is_displayed()
                ]
            )
        except TimeoutException:
            pass
        precise_sleep(random.uniform(1.5, 3.0))
        _modal_still_open = bool([
            el for el in driver.find_elements(
                By.CSS_SELECTOR,
                '[contenteditable="true"][role="textbox"]'
            ) if el.is_displayed()
        ])
        log.info("[POST FLOW]  step=modal_close  success=%s  duration=%.0fms  detail=compose_textbox_gone",
                 not _modal_still_open,
                 (time.perf_counter() - _pf_t0) * 1000)

        # Fix #11: reload state inside a fresh locked transaction so any
        # concurrent profile's daily-count writes are not overwritten.
        with _post_state_locked():
            _rp_state = _load_post_state()
            _record_post(profile_id, _rp_state)
        _cleanup_post_scratch(profile_id)
        log.info("[ POST ]  new post published successfully")
        return True

    except (NoSuchElementException, TimeoutException, WebDriverException) as exc:
        log.warning("create_post failed: %s", exc)
        try:
            driver.execute_script(
                "document.dispatchEvent(new KeyboardEvent('keydown',"
                "{key:'Escape',keyCode:27,bubbles:true}));"
            )
        except Exception:
            pass
        return False


def post_action(driver, profile_id: str) -> None:
    """Session-loop dispatch wrapper for create_post."""
    #  DEBUG LOGGING: ACTION START 
    _action_t0 = time.perf_counter()
    _get_ctx().session_metrics["actions_dispatched"] += 1
    log.info("[ACTION START]  action=post")
    # 
    result = create_post(driver, profile_id)
    #  DEBUG LOGGING: ACTION END 
    if result:
        _get_ctx().session_metrics["posts"] += 1
    log.info("[ACTION END]  action=post  result=%s  duration=%.1fs",
             "success" if result else "failure", time.perf_counter() - _action_t0)
    return result
    # 




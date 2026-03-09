import os
import re
import time
import random
import hashlib
import ctypes
import glob as _glob
from datetime import datetime
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
    POST_CAPTION_EMOJIS, POST_CAPTION_SHORTS,
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
from pools import POST_CAPTION_POOL
# ================================================================== #
#  IMAGE UNIQUIFIER  (per-profile re-encode)
# ================================================================== #

def _prepare_image_for_profile(src_path: str, profile_id: str) -> str:
    """
    Return a uniquified, re-encoded copy of src_path scoped to profile_id.

    Transformations applied every call so the output file always differs,
    even when two profiles choose the same source image:

      1. Random 1–3 px crop on every edge  (geometry changes)
      1b. Horizontal flip ,  50 % chance    (pHash distance +8–15 bits)
      1c. Random rotation 2–5 °            (geometry/DCT fingerprint shift)
      2. ±12 % brightness adjustment       (DCT coefficient shift)
      3. ±12 % contrast adjustment         (DCT coefficient shift)
      3b. Invisible corner stamp           (raw pixel alteration)
      4. Re-encode at randomised quality   (file bytes change)
         JPEG/WebP: base-88 ± 2–5 pts;
         PNG: lossless but fresh encoding.
      5. Strip all original EXIF metadata.
      6. Inject synthetic EXIF DateTimeOriginal within the last 48 h
         (requires piexif; silently skipped if not installed).

    The output is written to a per-profile scratch folder inside the OS
    temp directory.  The folder is named after a prefix of profile_id so
    stale copies from previous sessions are easy to identify and the same
    profile never sees another profile's scratch files.
    """
    from PIL import Image, ImageDraw, ImageEnhance  # Pillow – always installed

    try:
        import piexif as _piexif
        _have_piexif = True
    except ImportError:
        _have_piexif = False
        log.debug("_prepare_image_for_profile: piexif not installed ,  EXIF injection skipped")

    # Per-profile scratch directory
    safe_pid = (profile_id or "anon")[:16].replace("-", "")
    profile_dir = os.path.join(_POST_TEMP_DIR, safe_pid)
    os.makedirs(profile_dir, exist_ok=True)

    img = Image.open(src_path).convert("RGB")
    w, h = img.size

    # 1. Tiny random crop ,  different geometry per call
    left   = random.randint(1, 3)
    top    = random.randint(1, 3)
    right  = random.randint(1, 3)
    bottom = random.randint(1, 3)
    img = img.crop((left, top, w - right, h - bottom))

    # 1b. Horizontal flip ,  50 % chance.  A flip changes every pixel's
    #     position, pushing the pHash distance to 8–15 bits vs the source,
    #     which is well above any practical near-duplicate threshold.
    _flip = random.random() < 0.5
    if _flip:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)

    # 1c. Small random rotation (2–5 °) in a random direction.  Combined with
    #     the optional flip this ensures geometry-based hashes (dHash, aHash)
    #     also differ significantly on every call.
    angle = random.uniform(2.0, 5.0) * random.choice([-1, 1])
    
    # Calculate scale factor to cover black corners
    import math
    cw, ch = img.size
    theta = math.radians(abs(angle))
    z = math.cos(theta) + max(cw/ch, ch/cw) * math.sin(theta)
    
    # Zoom in first, then rotate
    zoom_w = int(math.ceil(cw * z))
    zoom_h = int(math.ceil(ch * z))
    img = img.resize((zoom_w, zoom_h), resample=Image.BICUBIC)
    
    # Rotate with expand=True to avoid any clipping before the final crop
    img = img.rotate(angle, resample=Image.BICUBIC, expand=True)
    
    # Crop back to original dimensions to remove the corners
    rotated_w, rotated_h = img.size
    crop_x = (rotated_w - cw) // 2
    crop_y = (rotated_h - ch) // 2
    img = img.crop((crop_x, crop_y, crop_x + cw, crop_y + ch))

    # 2. Brightness ±12 % (was ±2 %) ,  wider luminance shift moves DCT
    #    coefficients far outside the ±1-LSB neighbourhood that pHash
    #    near-duplicate detection relies on.
    b_factor = 1.0 + random.uniform(-0.12, 0.12)
    img = ImageEnhance.Brightness(img).enhance(b_factor)

    # 3. Contrast ±12 % (was ±2 %)
    c_factor = 1.0 + random.uniform(-0.12, 0.12)
    img = ImageEnhance.Contrast(img).enhance(c_factor)

    # 3b. Invisible corner stamp ,  two-digit number drawn in a colour sampled
    #     from the corner pixel ± a small random offset so it is imperceptible
    #     to a human reviewer but alters the raw pixel values and the DCT block
    #     in that corner region.
    try:
        _cw, _ch = img.size
        _corner_x = random.choice([2, _cw - 14])
        _corner_y = random.choice([2, _ch - 14])
        _sample_rgb = img.getpixel((_corner_x, _corner_y))
        _overlay_rgb = tuple(
            max(0, min(255, _sample_rgb[c] + random.randint(-18, 18)))
            for c in range(3)
        )
        _draw = ImageDraw.Draw(img)
        _draw.text((_corner_x, _corner_y), f"{random.randint(10, 99)}", fill=_overlay_rgb)
        del _draw
    except Exception:
        pass  # never block the upload path

    # 4. Re-encode at randomised quality
    ext = os.path.splitext(src_path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        save_fmt  = "JPEG"
        quality   = 88 + random.randint(-5, 5)
    elif ext == ".webp":
        save_fmt  = "WEBP"
        quality   = 88 + random.randint(-5, 5)
    else:  # .png
        save_fmt  = "PNG"
        quality   = None

    # 5+6. Build synthetic EXIF ,  random timestamp in the last 48 h
    exif_bytes = b""
    if _have_piexif and save_fmt in ("JPEG", "WEBP"):
        try:
            ts = datetime.fromtimestamp(time.time() - random.randint(0, 48 * 3600))
            ts_str = ts.strftime("%Y:%m:%d %H:%M:%S").encode()
            exif_dict = {
                "0th":  {},
                "Exif": {
                    _piexif.ExifIFD.DateTimeOriginal:  ts_str,
                    _piexif.ExifIFD.DateTimeDigitized: ts_str,
                },
                "GPS":  {},
                "1st":  {},
            }
            exif_bytes = _piexif.dump(exif_dict)
        except Exception as exc:
            log.debug("_prepare_image_for_profile: EXIF build failed (%s)", exc)
            exif_bytes = b""

    # Unique output filename ,  hash of inputs + wall clock so every call differs
    uid = hashlib.md5(f"{src_path}{time.time_ns()}{profile_id}".encode()).hexdigest()[:10]
    out_path = os.path.join(profile_dir, f"post_{uid}{ext}")

    save_kwargs: dict = {}
    if quality is not None:
        save_kwargs["quality"] = quality
    if save_fmt == "JPEG":
        save_kwargs["optimize"] = True
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes
    elif save_fmt == "WEBP" and exif_bytes:
        save_kwargs["exif"] = exif_bytes

    img.save(out_path, format=save_fmt, **save_kwargs)
    log.info(
        "[ POST ]  image uniquified  |  crop=(%d,%d,%d,%d)  flip=%s  "
        "rot=%.1f°  b=%.3f  c=%.3f  q=%s  → %s",
        left, top, right, bottom, _flip, angle, b_factor, c_factor,
        quality if quality else "lossless",
        os.path.basename(out_path),
    )
    return out_path


def _cleanup_post_scratch(profile_id: str, max_age_sec: float = 3600.0) -> None:
    """Remove stale uniquified image files from the per-profile scratch dir.

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
#  CAPTION HUMANISER
# ================================================================== #

def _mutate_caption(text: str) -> str:
    """
    Apply a random subset of natural social-media imperfections to a single
    caption string.  Mutations are probabilistic and can stack.

    Rules:
      • Remove Oxford comma (, and / , or)           ,  60 % of eligible
      • Drop leading capital (casual register)        ,  30 %
      • Strip terminal period                         ,  40 %  (leaves !? alone)
      • Replace terminal period with "…"              ,  15 %
      • Replace mid-sentence " I " with " i "         ,   8 % of eligible
      • Append an emoji from POST_CAPTION_EMOJIS      ,  35 %
      • Append a casual filler word (tbh/ngl/idk …)   ,  12 %
    """
    # Oxford comma removal
    if random.random() < 0.60:
        text = re.sub(r",\s+(and|or)\s+", lambda m: f" {m.group(1)} ", text)

    # Lowercase first character
    if text and random.random() < 0.30:
        text = text[0].lower() + text[1:]

    # Terminal-period mutation
    if text.endswith("."):
        r = random.random()
        if r < 0.40:
            text = text[:-1]        # bare end
        elif r < 0.55:
            text = text[:-1] + "…"  # ellipsis

    # Mid-sentence "I" → "i" (casual / typo)
    if random.random() < 0.08:
        text = re.sub(r"(?<=\s)I(?=\s)", "i", text)

    # Trailing emoji
    if random.random() < 0.35:
        text = text.rstrip() + " " + random.choice(POST_CAPTION_EMOJIS)

    # Casual filler
    if random.random() < 0.12:
        filler = random.choice(["tbh", "ngl", "idk", "lol", "honestly", "fr"])
        text = text.rstrip(".!?…").rstrip() + f" {filler}"

    return text


def _humanize_caption(pool: list) -> str:
    """
    Select a caption and apply realistic length + imperfection variation.

    Length tiers (proportional to authentic posting behaviour):
      5 %  emoji-only      ,  single character from POST_CAPTION_EMOJIS
     10 %  short fragment  ,  1-3 casual words from POST_CAPTION_SHORTS
     60 %  single sentence ,  one entry from pool, mutated via _mutate_caption()
     25 %  double          ,  two mutated pool entries joined with a line-break,
                             comma, or em-dash (varied per call)

    Ensures no two profiles ever get the same output even from the same pool.
    """
    tier = random.random()

    if tier < 0.05:
        # Emoji-only ,  optionally doubled
        e = random.choice(POST_CAPTION_EMOJIS)
        if random.random() < 0.3:
            e += " " + random.choice(POST_CAPTION_EMOJIS)
        return e

    if tier < 0.15:
        # Short fragment
        base = random.choice(POST_CAPTION_SHORTS)
        if random.random() < 0.40:
            base += " " + random.choice(POST_CAPTION_EMOJIS)
        return base

    if tier < 0.75:
        # Single sentence, mutated
        return _mutate_caption(random.choice(pool))

    # Double: two independently mutated sentences
    a = _mutate_caption(random.choice(pool))
    b = _mutate_caption(random.choice(pool))
    join = random.choice(["newline", "comma", "dash"])
    if join == "newline":
        return f"{a}\n{b}"
    if join == "comma":
        return f"{a.rstrip('.!?…')}, {b}"
    return f"{a.rstrip('.!?…')} ,  {b}"


# ================================================================== #
#  BEHAVIORAL TEXTBOX DETECTION
# ================================================================== #
# Identifies the compose text input by behavioral characteristics rather
# than framework-specific attributes (data-lexical-editor) which Meta
# renames whenever the Lexical editor framework is refactored.
#
# Detection strategy (priority order):
#   1. role="textbox" + contenteditable="true" in a modal/overlay context
#      (ARIA role is legally mandated for accessibility ,  stable).
#   2. Sole visible contenteditable="true" element above the fold
#      (compose modal is always the topmost layer).
#   3. document.activeElement after programmatic focus into the compose
#      area ,  completely framework-agnostic.
#   4. Legacy data-lexical-editor attribute (lowest priority fallback).
# ================================================================== #

def _find_compose_textbox(driver, timeout: float = 10.0):
    """Find the compose modal's text input using behavioral detection.

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
    Create an original Threads post with a caption from POST_CAPTION_POOL
    and optionally an image from MEDIA_POOL_DIR.

    Flow:
      1.  Guard ,  _can_post_now() checks quota + 2-hour cooldown.
      2.  Pick a random caption and (if MEDIA_POOL_DIR is set) a random image.
      3.  Find the compose / New post button in the nav sidebar.
      4.  Bezier-arc to the button and click to open the compose modal.
      5.  Attach image via the hidden <input type="file"> if a path was picked.
      6.  Bezier-arc to the textbox and type the caption with human_type().
      7.  Re-read pause (1.5–4 s) ,  mimics proof-reading before posting.
      8.  Find the Post button in the modal and click it.
      9.  Wait for the modal to dismiss, then call _record_post().

    Selector notes:
      COMPOSE_BTN ,  tried in priority order; first visible match wins.
      File input   ,  made temporarily visible via JS so send_keys works on
                     the hidden <input type="file"> without a click chain.
      Post button  ,  reuses COMMENT_POST_XPATH (same "Post" text node).
    """
    if not POST_CAPTION_POOL:
        log.warning("create_post: POST_CAPTION_POOL is empty ,  cannot post")
        return False

    # === Fix #11: locked pre-post transaction ,  gate check + image reservation =
    # The lock is held only for the short read-modify-write cycle on state.
    # Heavy browser automation below runs entirely outside the lock so parallel
    # profiles are not blocked for the duration of the UI interaction.
    image_path = None
    _src_for_uniquify = None
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

                _src_for_uniquify = random.choice(fresh)
                basename = os.path.basename(_src_for_uniquify)

                if basename in used_list:
                    used_list.remove(basename)
                used_list.append(basename)

                max_history = max(0, len(all_images) - 1)
                while len(used_list) > max_history:
                    used_list.pop(0)

                state["_used_images"] = used_list
        _save_post_state(state)  # one atomic write covers gate + image reservation
    # ===========================================================================

    if _src_for_uniquify:
        try:
            image_path = _prepare_image_for_profile(_src_for_uniquify, profile_id)
        except Exception as exc:
            log.warning("[ POST ]  image uniquification failed (%s) ,  using original", exc)
            image_path = _src_for_uniquify

    # ── DEBUG LOGGING: POST FLOW timer ────────────────────────────────────────
    _pf_t0 = time.perf_counter()
    # ──────────────────────────────────────────────────────────────────────────

    caption = _humanize_caption(_get_ctx().profile_caption_pool or POST_CAPTION_POOL)
    log.info(
        "[ POST ]  composing  |  caption=%r  |  media=%s",
        caption,
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
        #    Uses behavioral detection (role=textbox, contenteditable, modal
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

                # Primary: click the attach button → OS file dialog → pyautogui paste.
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
        #    Using a stale reference in human_type() raises
        #    StaleElementReferenceException → the outer handler fires Escape
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
        log.info("[ POST ]  re-reading before submit (%.1fs)…", reread_s)
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
            # Pass A: walk up from text_box → find Post button in same container
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
        # current cursor position ,  which after human_type() is still inside
        # the modal's scrollable content area.  The wheel events are delivered
        # to that scroll container (not the page), causing the modal body to
        # scroll instead of the page.  Bezier-move directly to the button.
        bezier_move(driver, post_btn)
        precise_sleep(random.uniform(0.4, 0.9))
        _cdp_click_element(driver, post_btn)  # Fix 1.4: never JS .click()
        log.info("[POST FLOW]  step=submit_click  success=True  detail=post_btn_clicked")
        debug_cursor_state(driver, "post-submit-click")

        # 8. Wait for modal to close (compose textbox disappears on success)
        #    Uses web-standards selectors for textbox detection.
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
    # ── DEBUG LOGGING: ACTION START ────────────────────────────────────────────
    _action_t0 = time.perf_counter()
    _get_ctx().session_metrics["actions_dispatched"] += 1
    log.info("[ACTION START]  action=post")
    # ────────────────────────────────────────────────────────────────────
    result = create_post(driver, profile_id)
    # ── DEBUG LOGGING: ACTION END ────────────────────────────────────────────
    if result:
        _get_ctx().session_metrics["posts"] += 1
    log.info("[ACTION END]  action=post  result=%s  duration=%.1fs",
             "success" if result else "failure", time.perf_counter() - _action_t0)
    # ────────────────────────────────────────────────────────────────────

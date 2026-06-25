import time
import random
import json
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    WebDriverException,
)
from config import (
    TARGET_SOCIAL_URL,
    ELEMENT_CONFIDENCE_THRESHOLD,
    CHALLENGE_URL_PATHS,
    CHALLENGE_DOM_SELECTORS,
)
from dom_selectors import (
    FEED_PROFILE_LINK, FOLLOW_BTN_XPATH,
    REPLY_BTN_CSS, COMMENT_BOX_CSS,
    COMMENT_POST_XPATH, SEARCH_INPUT_CSS,
    _KNOWN_UNLIKE_LABELS, 
    _JS_MULTI_SIGNAL_LIKE, _JS_MULTI_SIGNAL_REPLY,
)
from utils import log, precise_sleep, _log_page_state, _get_ctx
from mouse import (
    bezier_move, bezier_move_to_coords,
    _cdp_click, _cdp_click_element,
    human_type, debug_cursor_state,
    CDPConnectionDead, inject_cursor_overlay, init_cursor_pos,
)
from scroll import stochastic_scroll, navigate_to, navigate_history, smooth_scroll_chunk, _close_media_overlay
from content_policy import ContentPolicyError, prepare_reply_for_publishing, validate_reply
from pools import APPROVED_REPLIES, BRAND_VOICE, SEARCH_TOPIC_POOL
'''
def _score_post_relevance(post_text: str) -> str:
    """Returns 'primary', 'secondary', 'negative', or 'neutral'."""
    text_lower = post_text.lower()
    
    if any(kw in text_lower for kw in NICHE_KEYWORDS["negative"]):
        return "negative"
    if any(kw in text_lower for kw in NICHE_KEYWORDS["primary"]):
        return "primary"
    if any(kw in text_lower for kw in NICHE_KEYWORDS["secondary"]):
        return "secondary"
    return "neutral"


def _should_engage_with_post(post_text: str) -> bool:
    """Decide whether to engage based on topical relevance."""
    relevance = _score_post_relevance(post_text)
    
    if relevance == "negative":
        return False
    if relevance == "primary":
        return random.random() < NICHE_ENGAGEMENT_PROB
    if relevance == "secondary":
        return random.random() < (NICHE_ENGAGEMENT_PROB * 0.5)
    # neutral
    return random.random() < OFFTOPIC_ENGAGEMENT_PROB

def _score_post_relevance(post_text: str) -> str:
    """Returns 'primary', 'secondary', 'negative', or 'neutral'."""
    text_lower = post_text.lower()
    
    if any(kw in text_lower for kw in NICHE_KEYWORDS["negative"]):
        return "negative"
    if any(kw in text_lower for kw in NICHE_KEYWORDS["primary"]):
        return "primary"
    if any(kw in text_lower for kw in NICHE_KEYWORDS["secondary"]):
        return "secondary"
    return "neutral"


def _should_engage_with_post(post_text: str) -> bool:
    """Decide whether to engage based on topical relevance."""
    relevance = _score_post_relevance(post_text)
    
    if relevance == "negative":
        return False
    if relevance == "primary":
        return random.random() < NICHE_ENGAGEMENT_PROB
    if relevance == "secondary":
        return random.random() < (NICHE_ENGAGEMENT_PROB * 0.5)
    # neutral
    return random.random() < OFFTOPIC_ENGAGEMENT_PROB

def _get_post_text(driver, element) -> str:
    """Extract visible text content from a post element."""
    try:
        post = driver.execute_script("""
            var el = arguments[0];
            var container = el.closest('article') ||
                            el.closest('[data-pressable-container]');
            if (!container) return null;
            
            // Exclude username spans which have translate="no"
            // Post body spans have dir="auto" but NO translate attribute
            var spans = container.querySelectorAll('span[dir="auto"]');
            for (var i = 0; i < spans.length; i++) {
                var span = spans[i];
                if (span.getAttribute('translate') === 'no') continue;
                var text = span.innerText.trim();
                if (text.length > 15) return text;
            }
            return null;
        """, element)

        return (post or "").strip()

    except Exception as exc:
        log.debug("[GET_POST_TEXT]  failed: %s", exc)
        return "" 
'''
def _find_unliked_buttons_fallback(driver) -> list:
    """Legacy XPath/CSS fallback for like buttons ,  used when JS scoring fails."""
    results = []
    viewport_h = driver.execute_script("return window.innerHeight")

    for xp in [
        "//div[@role='button'][.//*[local-name()='svg'][@aria-label='Like']]",
    ]:
        try:
            for el in driver.find_elements(By.XPATH, xp):
                try:
                    r = driver.execute_script(
                        "var r=arguments[0].getBoundingClientRect();"
                        "return {top:r.top,height:r.height};", el)
                    if r["height"] > 0 and -50 <= r["top"] <= viewport_h + 50:
                        if el.is_displayed():
                            pressed = el.get_attribute("aria-pressed")
                            label   = (el.get_attribute("aria-label") or "").lower()
                            if pressed != "true" and label not in _KNOWN_UNLIKE_LABELS:
                                results.append(el)
                except Exception:
                    continue
        except (NoSuchElementException, WebDriverException):
            continue
        if results:
            log.info("[FALLBACK]  XPath: %d unliked like button(s)", len(results))
            break

    if not results:
        for sel in ["div[role='button']:has(svg[aria-label='Like'])"]:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        r = driver.execute_script(
                            "var r=arguments[0].getBoundingClientRect();"
                            "return {top:r.top,height:r.height};", el)
                        if r["height"] > 0 and -50 <= r["top"] <= viewport_h + 50:
                            if el.is_displayed():
                                pressed = el.get_attribute("aria-pressed")
                                label   = (el.get_attribute("aria-label") or "").lower()
                                if pressed != "true" and label not in _KNOWN_UNLIKE_LABELS:
                                    results.append(el)
                    except Exception:
                        continue
            except (NoSuchElementException, WebDriverException):
                continue
            if results:
                log.info("[FALLBACK]  CSS: %d unliked like button(s)", len(results))
                break

    if not results:
        log.info("No likeable posts visible in viewport (fallback)")
    return results


def _find_unliked_buttons(driver) -> list:
    """
    Return all clickable like-button wrapper divs that are visible in the
    current viewport and have NOT been liked yet.

    Uses multi-signal composite scoring:
      1. Structural position in the action bar
      2. SVG path geometry (heart fingerprint)
      3. Fill state (transparent = un-liked)
      4. Sibling context (3-5 icon buttons)
      5. ARIA labels (low-weight fallback)

    Elements scoring below ELEMENT_CONFIDENCE_THRESHOLD are discarded.
    Falls back to legacy XPath/CSS selectors if JS scoring fails.
    """
    results = []
    try:
        raw = driver.execute_script(
            _JS_MULTI_SIGNAL_LIKE, ELEMENT_CONFIDENCE_THRESHOLD
        )

        if not raw:
            log.info("[MULTI-SIGNAL]  no like-button candidates found in viewport")
            _log_selector_failure(driver, "like")
            # Try legacy fallback
            return _find_unliked_buttons_fallback(driver)

        for item in raw:
            el    = item[0]
            score = item[1]
            pos   = item[2]
            sibs  = item[3]
            label = item[4] if len(item) > 4 else ""
            try:
                if not el.is_displayed():
                    continue
                results.append(el)
                log.debug(
                    "[MULTI-SIGNAL]  like candidate  score=%.2f  pos=%d/%d  aria=%r",
                    score, pos, sibs, label,
                )
            except Exception:
                continue

        if results:
            log.info(
                "[MULTI-SIGNAL]  %d unliked like button(s) found (best_score=%.2f)",
                len(results), raw[0][1] if raw else 0,
            )
        else:
            log.info("[MULTI-SIGNAL]  candidates found but none displayed")
            _log_selector_failure(driver, "like")

    except WebDriverException as exc:
        log.debug("[MULTI-SIGNAL]  like-button scan error: %s", exc)
        results = _find_unliked_buttons_fallback(driver)

    return results


def _find_reply_buttons(driver) -> list:
    """
    Return visible reply buttons using multi-signal structural scoring.

    The reply button is identified as the 2nd button in the action bar
    with speech-bubble SVG geometry.  Falls back to the legacy CSS
    selector (REPLY_BTN_CSS) if JS scoring fails.
    """
    results = []
    try:
        raw = driver.execute_script(
            _JS_MULTI_SIGNAL_REPLY, ELEMENT_CONFIDENCE_THRESHOLD
        )
        if raw:
            for item in raw:
                el = item[0]
                try:
                    if el.is_displayed():
                        r = driver.execute_script(
                            "var r=arguments[0].getBoundingClientRect();"
                            "return {top:r.top,h:r.height};", el)
                        vh = driver.execute_script("return window.innerHeight")
                        if r["h"] > 0 and 0 <= r["top"] <= vh:
                            results.append(el)
                except Exception:
                    continue
        if results:
            log.info("[MULTI-SIGNAL]  %d reply button(s) found", len(results))
            return results
    except WebDriverException:
        pass

    # Legacy fallback
    for btn in driver.find_elements(By.CSS_SELECTOR, REPLY_BTN_CSS):
        try:
            r = driver.execute_script(
                "var r=arguments[0].getBoundingClientRect();"
                "return {top:r.top,h:r.height};", btn)
            vh = driver.execute_script("return window.innerHeight")
            if r["h"] > 0 and 0 <= r["top"] <= vh and btn.is_displayed():
                results.append(btn)
        except Exception:
            continue
    if results:
        log.info("[FALLBACK]  %d reply button(s) found via CSS", len(results))
    return results


def scroll_element_into_loose_view(driver, element) -> None:
    """
    Scroll the page until the element is loosely visible ,  somewhere in
    the viewport, not mathematically centered.  Mimics a human scrolling
    until they can see what they're looking for and stopping.

    Each scroll chunk is routed through smooth_scroll_chunk so it inherits
    the same sine ease-in/ease-out velocity curve used during stochastic
    browsing ,  slow start, peak in the middle, deceleration to stop.
    Step sizes and tick rates are randomised per chunk so consecutive
    scroll events vary the way real scroll-wheel flicks do.
    """
    for _ in range(12):  # max attempts before giving up
        rect = driver.execute_script(
            "var r=arguments[0].getBoundingClientRect();"
            "return {top:r.top, bottom:r.bottom, height:r.height};",
            element,
        )
        vh = driver.execute_script("return window.innerHeight")

        # Element is comfortably visible ,  not within 15 % of either edge
        margin = vh * 0.15
        if margin < rect["top"] and rect["bottom"] < (vh - margin):
            break

        if rect["top"] < margin:
            # Element above viewport (or too close to top) ,  scroll up
            step = -random.randint(80, 220)
        else:
            # Element below viewport (or too close to bottom) ,  scroll down
            step = random.randint(80, 220)

        # Use smooth_scroll_chunk so each scroll chunk gets the same sine
        # ease-in/ease-out physics as normal browsing ,  not a raw instant jump.
        smooth_scroll_chunk(
            driver, step,
            step_px=random.randint(4, 8),
            tick_ms=random.randint(13, 20),
        )
        # Brief inter-chunk pause ,  hand rests between flicks
        precise_sleep(random.uniform(0.08, 0.25))

    # Imprecise final pause ,  not a fixed sleep
    precise_sleep(random.uniform(0.3, 0.7))


def _attempt_like(driver, element) -> bool:
    """
    Perform one like action with human-like timing:
      1. Scroll post into view
      2. Pause ,  as if reading the post before liking
      3. Bezier mouse curve to the like button
      4. Hover pause (hand settling)
      5. Click (Selenium first, JS fallback on intercept)
      6. Post-click pause (watching the heart animation)
    Returns True on success.
    """
    '''
    post_text = _get_post_text(driver, element)
    log.info("[LIKE]  post_text extracted: %r", post_text[:80] if post_text else "EMPTY")
    
    if post_text and not _should_engage_with_post(post_text):
        log.debug("[LIKE]  skipping off-topic post")
        return False
    '''
    try:
        log.info("[ LIKE ]  scrolling post into view + clicking like")
        scroll_element_into_loose_view(driver, element)

        # Content-aware guard: skip posts with no visible engagement signal.
        # Real users predominantly like content that already has replies, likes,
        # or reposts.  Liking zero-engagement posts in bulk is a bot pattern.
        # 70 % skip rate when no digit is visible ,  leaves a small chance so
        # the bot can occasionally like emerging posts as a real user would.
        try:
            has_signal = driver.execute_script("""
                var btn  = arguments[0];
                var post = btn.closest('article') ||
                           btn.closest('[data-pressable-container]');
                if (!post) return true;
                return /\\d/.test(post.innerText || '');
            """, element)
            if not has_signal and random.random() < 0.70:
                log.debug("[ LIKE ]  skipping zero-engagement post (content-aware guard)")
                return False
        except Exception:
            pass

        # Reading pause before liking ,  humans read before they react
        precise_sleep(random.uniform(0.8, 2.5))

        bezier_move(driver, element)
        precise_sleep(random.uniform(0.2, 0.6))   # hand settling on the button

        _cdp_click_element(driver, element)  # Fix 1.4: never JS .click()
        debug_cursor_state(driver, "like-click")

        # Watch the heart animation
        precise_sleep(random.uniform(0.8, 2.0))
        return True

    except (NoSuchElementException, WebDriverException) as exc:
        log.debug("Like attempt failed: %s", exc)
        return False


def check_login_status(driver) -> bool:
    """
    Return True if the current Threads page shows a logged-in feed.

    Detects:
    - Logged-out state via /login redirect.
    - Challenge / verification screens via specific URL paths and structural
      DOM elements ,  avoiding false positives from user-generated content.
      A challenge page can look logged-in (no /login in URL, feed elements
      absent) so explicit challenge detection is required.
    """
    try:
        url = driver.current_url.lower()

        # Definite logged-out
        if any(s in url for s in ("/login", "/accounts/login")):
            log.warning("[LOGIN]  status=logged_out  reason=login_redirect  url=%s", url[:80])
            return False

        # URL-based challenge detection ,  specific paths only
        if any(s in url for s in CHALLENGE_URL_PATHS):
            log.warning("[LOGIN]  status=challenge  reason=challenge_url  url=%s", url[:80])
            return False

        # DOM-based challenge detection ,  structural elements only
        for sel in CHALLENGE_DOM_SELECTORS:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    log.warning("[LOGIN]  status=challenge  reason=dom_element  selector=%s", sel)
                    return False
            except NoSuchElementException:
                continue

# Feed present — but verify not showing login wall
        articles = driver.find_elements(
            By.CSS_SELECTOR,
            "article, div[data-pressable-container='true']",
        )
        if articles:
            # Check for login wall — logged-out users see public feed + login panel
            try:
                login_wall = driver.find_element(
                    By.CSS_SELECTOR, 
                    "a[href*='/login']"

                )
                if login_wall.is_displayed():
                    log.warning("[LOGIN]  status=logged_out  reason=login_wall_present  url=%s",
                                url[:80])
                    return False
            except NoSuchElementException:
                pass

            log.info("[LOGIN]  status=logged_in  feed_items=%d  url=%s",
                    len(articles), url[:80])
            return True
    except CDPConnectionDead:
        raise   # circuit breaker ,  propagate immediately
    except WebDriverException as exc:
        log.debug("check_login_status failed: %s", exc)
    return False

# ================================================================== #
#  ENGAGEMENT VARIETY ACTIONS
# ================================================================== #

def _is_visually_visible(driver, el) -> bool:
    """
    Return True only if el is genuinely visible to the user.

    Guards against honeypot elements that pass is_displayed() but are
    invisible via CSS tricks ,  opacity:0, visibility:hidden, zero size,
    or off-screen positioning (left:-9999px).
    """
    try:
        return driver.execute_script(
            """
            var s   = window.getComputedStyle(arguments[0]);
            var r   = arguments[0].getBoundingClientRect();
            return s.display     !== 'none'
                && s.visibility  !== 'hidden'
                && s.opacity     !== '0'
                && parseFloat(s.opacity) > 0
                && parseInt(s.width)  > 0
                && parseInt(s.height) > 0
                && r.width  > 0
                && r.height > 0
                && r.right  > 0
                && r.bottom > 0;
            """,
            el,
        )
    except Exception:
        return False

def _get_own_profile_href(driver) -> str:
    """
    Return the href of the logged-in user's own nav-bar profile link,
    or '' on failure.

    The nav icon is the only a[href^="/@"] that wraps an
    svg[aria-label="Profile"] ,  all post-author links use text/avatars.
    Caching it before each candidate scan prevents the bot from navigating
    to its own account.
    """
    try:
        # Avoid CSS :has() ,  walk up from the Profile SVG to its <a> ancestor
        el = driver.execute_script("""
            var svgs = document.querySelectorAll('svg[aria-label="Profile"]');
            for (var i = 0; i < svgs.length; i++) {
                var node = svgs[i].parentElement;
                for (var d = 0; d < 6; d++) {
                    if (!node) break;
                    var href = node.getAttribute('href') || '';
                    if (node.tagName === 'A' && href.startsWith('/@')) return node;
                    node = node.parentElement;
                }
            }
            return null;
        """)
        if el is None:
            return ""
        return (el.get_attribute("href") or "").rstrip("/")
    except Exception:
        return ""

def view_profile_from_feed(driver) -> bool:
    """
    Click a random post-author username link in the feed to visit their
    profile, scroll it, optionally follow, then navigate back.

    Has a 15 % probabilistic gate for calling follow_from_profile_page().
    """
    _action_t0 = time.perf_counter()
    log.info("[ACTION START]  action=profile_view")
    try:
        own_href = _get_own_profile_href(driver)
        candidates = []
        for el in driver.find_elements(By.CSS_SELECTOR, FEED_PROFILE_LINK):
            try:
                if not el.is_displayed():
                    continue
                # Skip timestamp links (same selector but contain <time>)
                if driver.execute_script(
                    "return arguments[0].querySelector('time') !== null;", el
                ):
                    continue
                href = el.get_attribute("href") or ""
                if "/post/" in href or "/t/" in href:
                    continue
                if own_href and href.rstrip("/") == own_href:
                    continue
                if href.rstrip("/") in _get_ctx().session_followed:
                    continue
                # Viewport filter ,  only keep elements currently on-screen
                rect = driver.execute_script(
                    "var r=arguments[0].getBoundingClientRect();"
                    "return {y:r.top, h:r.height};",
                    el,
                )
                vh_c = driver.execute_script("return window.innerHeight")
                if rect["h"] == 0 or rect["y"] < 0 or rect["y"] > vh_c:
                    continue
                candidates.append(el)
            except Exception:
                continue

        if not candidates:
            log.info("[ACTION SKIP]  action=profile_view  reason=no_feed_profile_links")
            return False

        # Try up to 3 candidates in case one goes stale between scan and click
        random.shuffle(candidates)
        for attempt, target in enumerate(candidates[:3]):
            profile_url = target.get_attribute("href")
            if not profile_url:
                continue

            # Re-validate ,  element may have scrolled off-screen since the candidate
            # list was built (page could have loaded more content / user scroll).
            _rect = driver.execute_script(
                "var r=arguments[0].getBoundingClientRect();"
                "return {y: r.top, h: r.height};",
                target,
            )
            _vh = driver.execute_script("return window.innerHeight")
            if _rect["h"] == 0 or _rect["y"] < 0 or _rect["y"] > _vh:
                log.debug("Profile link scrolled off-screen since scan ,  trying next candidate (%d)", attempt + 1)
                continue

            # Found a valid on-screen candidate ,  proceed with it
            break
        else:
            log.debug("Profile link scrolled off-screen since scan ,  all candidates exhausted")
            return False

        # Scroll the link loosely into view before moving the cursor to it.
        scroll_element_into_loose_view(driver, target)

        _get_ctx().session_followed.add(profile_url.rstrip("/"))
        log.info("[PROFILE VIEW]  candidates=%d  target=%s",
                 len(candidates[:15]), profile_url[:60])
        bezier_move(driver, target)
        precise_sleep(random.uniform(0.5, 1.5))

        # Click the profile link and wait for navigation
        _cdp_click_element(driver, target)
        precise_sleep(random.uniform(1.5, 3.0))

        # Verify we actually navigated to the profile
        current = driver.current_url
        if "threads" not in current or current.rstrip("/") == TARGET_SOCIAL_URL.rstrip("/"):
            log.debug("[PROFILE VIEW]  navigation did not occur — returning to feed")
            _safe_return_to_feed(driver, "profile_view_no_nav")
            return False

        log.info("[PROFILE VIEW]  landed on %s", current[:60])

        # Scroll the profile
        stochastic_scroll(driver, total_seconds=random.uniform(8, 20))

        # Follow gate — 15% probabilistic
        if random.random() < 0.15:
            follow_from_profile_page(driver)

        # Navigate back to feed
        navigate_history(driver, "back")
        precise_sleep(random.uniform(1.0, 2.5))

    except (TimeoutException, WebDriverException) as exc:
        log.warning("[ACTION END]  action=profile_view  result=failure  error=%s", exc)
        try:
            _safe_return_to_feed(driver, "profile_view_err")
        except Exception:
            pass
        return False

def follow_from_feed(driver) -> bool:
    """
    Follow a user directly from the feed via the hover-card that Threads
    renders when the cursor rests over a post-author username.

    Flow:
      1. Find a visible feed profile link and scroll it into view.
      2. Bezier-arc to the username (hover only ,  no click).
      3. Wait up to 2 s for a text-based Follow button to appear in the
         hover card (it is absent before the hover fires).
      4. Bezier-arc to that Follow button and click it.
      5. Move the cursor back to a neutral mid-feed position so the hover
         card dismisses naturally and scrolling can continue.
    """
    _action_t0 = time.perf_counter()
    log.info("[ACTION START]  action=follow_feed")
    try:
        # ── 1. Collect visible, non-timestamp feed profile links ──────────────
        own_href = _get_own_profile_href(driver)
        candidates = []
        for el in driver.find_elements(By.CSS_SELECTOR, FEED_PROFILE_LINK):
            try:
                if not el.is_displayed():
                    continue
                if driver.execute_script(
                    "return arguments[0].querySelector('time') !== null;", el
                ):
                    continue
                href = el.get_attribute("href") or ""
                if "/post/" in href or "/t/" in href:
                    continue
                if own_href and href.rstrip("/") == own_href:
                    continue
                if href.rstrip("/") in _get_ctx().session_followed:
                    continue
                # Avatar links contain <img>; username links contain only text.
                # We want textual username links ,  avatar hover triggers the
                # quick-follow SVG, not the text-based hover-card Follow button.
                if driver.execute_script(
                    "return arguments[0].querySelector('img') !== null;", el
                ):
                    continue
                rect = driver.execute_script(
                    "var r=arguments[0].getBoundingClientRect();"
                    "return {y:r.top, h:r.height};",
                    el,
                )
                vh_c = driver.execute_script("return window.innerHeight")
                if rect["h"] == 0 or rect["y"] < 0 or rect["y"] > vh_c:
                    continue
                candidates.append(el)
            except Exception:
                continue

        if not candidates:
            log.info("[ACTION SKIP]  action=follow_feed  reason=no_visible_feed_profile_links")
            return False

        log.info("[FOLLOW FEED]  candidates=%d", len(candidates))
        username_el = random.choice(candidates[:10])

        # ── 2. Scroll username into view, then hover (no click) ───────────────
        scroll_element_into_loose_view(driver, username_el)

        # Snapshot of text-based Follow buttons already in DOM before hover
        pre_follow_ids = set(
            el.id for el in driver.find_elements(By.XPATH, FOLLOW_BTN_XPATH)
        )

        bezier_move(driver, username_el)          # hover ,  ActionChains fires mouseenter

        # ── 3. Wait for the hover card's Follow button to appear ──────────────
        follow_btn = None
        try:
            def _new_follow_btn(d):
                for el in d.find_elements(By.XPATH, FOLLOW_BTN_XPATH):
                    if el.id not in pre_follow_ids and el.is_displayed():
                        return el
                return None

            follow_btn = WebDriverWait(driver, 2).until(_new_follow_btn)
        except TimeoutException:
            log.debug("follow_from_feed: hover card Follow button did not appear")
            # Drift cursor far from the hover card, then scroll to guarantee dismissal
            try:
                vw_e = driver.execute_script("return window.innerWidth")
                vh_e = driver.execute_script("return window.innerHeight")
                bezier_move_to_coords(
                    driver,
                    random.randint(int(vw_e * 0.20), int(vw_e * 0.80)),
                    random.randint(int(vh_e * 0.45), int(vh_e * 0.75)),
                    tag="idle-settle",
                )
                precise_sleep(random.uniform(0.2, 0.4))
                # Small scroll to force any lingering hover card off the screen
                smooth_scroll_chunk(driver, random.randint(60, 130), step_px=5, tick_ms=16)
            except Exception:
                pass
            return False

        # ── 4. Arc to the Follow button and click ─────────────────────────────
        precise_sleep(random.uniform(0.3, 0.7))      # eye settling on the card
        bezier_move(driver, follow_btn)
        precise_sleep(random.uniform(0.3, 0.8))
        _cdp_click(driver)
        debug_cursor_state(driver, "follow-feed-click")
        precise_sleep(random.uniform(0.8, 1.5))
        log.info("follow_from_feed: follow clicked via hover card")
        _get_ctx().session_followed.add((username_el.get_attribute("href") or "").rstrip("/"))

        # ── DEBUG LOGGING: ACTION END (success) ──────────────────────────────────
        _get_ctx().session_metrics["follows"] += 1
        _get_ctx().session_metrics["actions_dispatched"] += 1
        log.info("[ACTION END]  action=follow_feed  result=success")
        # ────────────────────────────────────────────────────────────────────

        # ── 5. Drift cursor to mid-feed + scroll to guarantee card dismissal ──
        try:
            vw_e = driver.execute_script("return window.innerWidth")
            vh_e = driver.execute_script("return window.innerHeight")
            bezier_move_to_coords(
                driver,
                random.randint(int(vw_e * 0.20), int(vw_e * 0.80)),
                random.randint(int(vh_e * 0.45), int(vh_e * 0.75)),
                tag="idle-settle",
            )
            precise_sleep(random.uniform(0.2, 0.4))
            # Scroll down slightly ,  moves the hovered username off-screen so
            # the hover card closes even if cursor proximity keeps it open.
            smooth_scroll_chunk(driver, random.randint(80, 160), step_px=5, tick_ms=16)
        except Exception:
            pass

        return True

    except (NoSuchElementException, WebDriverException) as exc:
        log.warning("[ACTION END]  action=follow_feed  result=failure  error=%s", exc)
        return False

def follow_from_profile_page(driver) -> bool:
    """
    Click the Follow button on a loaded profile page.
    Only call this when already on a profile URL (/@username).

    Flow:
      1. Smooth scroll to top (follow button lives in the profile header).
      2. Drift cursor to the header region before committing to the button.
      3. Deliberate deciding pause.
      4. Bezier arc to follow button + click.
      5. Post-follow drift toward the mid-feed so navigate_history() has a
         realistic arc length when it parks the cursor at y=0.
    """
    log.info("[ FOLLOW ]  attempting follow from profile page")
    try:
        if "/@" not in driver.current_url:
            log.debug("Not on a profile page ,  skipping follow")
            return False

        # 1. Smooth scroll to top ,  the follow button is in the profile header.
        #    Use smooth_scroll_chunk in small upward steps so it looks like a
        #    human scrolling back up after reading, rather than instant jump.
        try:
            current_scroll = driver.execute_script("return window.scrollY")
            if current_scroll > 50:
                # Scroll up in one smooth chunk
                smooth_scroll_chunk(driver, -current_scroll, step_px=8, tick_ms=14)
                precise_sleep(random.uniform(0.4, 0.9))
        except WebDriverException:
            pass

        # 2. Wait for the follow button to appear in the now-visible header.
        btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, FOLLOW_BTN_XPATH))
        )
        if not _is_visually_visible(driver, btn):
            return False

        # Scroll it loosely into view (handles any residual offset)
        scroll_element_into_loose_view(driver, btn)

        # 3. Drift cursor into the header area before aiming at the button , 
        #    simulates the eye landing on the profile header after scrolling up.
        try:
            vw_f = driver.execute_script("return window.innerWidth")
            vh_f = driver.execute_script("return window.innerHeight")
            pre_x = random.randint(int(vw_f * 0.10), int(vw_f * 0.60))
            pre_y = random.randint(int(vh_f * 0.10), int(vh_f * 0.30))
            bezier_move_to_coords(driver, pre_x, pre_y, tag="nav-hover")
        except WebDriverException:
            pass

        # 4. Deliberate deciding pause + bezier arc to button + click.
        precise_sleep(random.uniform(2.0, 5.0))
        bezier_move(driver, btn)
        precise_sleep(random.uniform(0.3, 0.8))
        _cdp_click(driver)
        debug_cursor_state(driver, "follow-profile-click")
        precise_sleep(random.uniform(0.8, 1.5))

        try:
            WebDriverWait(driver, 5).until(
                lambda d: len(d.find_elements(
                    By.XPATH,
                    '//div[@role="button" and (.//div[normalize-space(text())="Following"]'
                    ' or .//div[normalize-space(text())="Requested"])]',
                )) > 0
            )
            log.info("Follow confirmed on profile page")
        except TimeoutException:
            log.debug("Follow state change not confirmed ,  may still have worked")

        # 5. Post-follow drift toward mid-feed so the upcoming navigate_history()
        #    park arc has a realistic length rather than a near-zero hop from y≈0.
        try:
            vw_f = driver.execute_script("return window.innerWidth")
            vh_f = driver.execute_script("return window.innerHeight")
            drift_x = random.randint(int(vw_f * 0.20), int(vw_f * 0.80))
            drift_y = random.randint(int(vh_f * 0.40), int(vh_f * 0.70))
            bezier_move_to_coords(driver, drift_x, drift_y, tag="idle-settle")
        except WebDriverException:
            pass

        return True

    except (TimeoutException, NoSuchElementException, WebDriverException) as exc:
        log.debug("Follow from profile failed: %s", exc)
        return False

def _find_nav_btn_by_label(driver, aria_label: str):
    """
    Find a nav bar anchor/button by its SVG aria-label using a JS DOM walk.
    Avoids CSS :has() which has unreliable support in ChromeDriver.
    Returns the clickable <a> or role="button" ancestor element, or None.
    """
    return driver.execute_script("""
        var svgs = document.querySelectorAll('svg[aria-label="' + arguments[0] + '"]');
        for (var i = 0; i < svgs.length; i++) {
            var svg = svgs[i];
            var rect = svg.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            var el = svg.parentElement;
            for (var d = 0; d < 6; d++) {
                if (!el) break;
                if (el.tagName === 'A' || el.getAttribute('role') === 'button') {
                    return el;
                }
                el = el.parentElement;
            }
        }
        return null;
    """, aria_label)

def _click_nav_btn(driver, aria_label: str, label: str) -> bool:
    """
    Generic helper: find a nav bar icon by its SVG aria-label, bezier-move
    to it, and click it.
    aria_label  ,  the SVG aria-label value (e.g. "Home", "Search", "Notifications").
    label       ,  human-readable name used only for log messages.
    Returns True on success, False if not found / not clickable.
    """
    try:
        btn = _find_nav_btn_by_label(driver, aria_label)
        if not btn:
            log.debug("_click_nav_btn: '%s' not found", label)
            return False
        WebDriverWait(driver, 4).until(lambda d: btn.is_displayed())
        bezier_move(driver, btn)
        precise_sleep(random.uniform(0.3, 0.7))
        _cdp_click(driver)
        debug_cursor_state(driver, f"nav-btn/{label}")
        precise_sleep(random.uniform(0.8, 1.8))   # SPA transition settle
        log.debug("_click_nav_btn: clicked '%s'", label)
        return True
    except WebDriverException as exc:
        log.debug("_click_nav_btn: WebDriverException on '%s': %s", label, exc)
        return False

def click_home_button(driver) -> bool:
    """
    Click the Home or Threads-logo nav button to return to the feed.
    Tries "Home" SVG label first (compact nav), then "Threads" (sidebar logo).
    Returns True on success, False if neither is found.
    """
    # "Home" label used by compact/mobile nav; "Threads" by the sidebar logo
    for aria_label in ("Home", "Threads"):
        if _click_nav_btn(driver, aria_label, aria_label):
            return True
    log.debug("click_home_button: no home button found")
    return False

def _safe_return_to_feed(driver, context: str = "") -> None:
    """
    Return to the Threads feed using a 3-step fallback hierarchy:

      1. click_home_button()          ,  SPA nav click, no page load (best)
      2. navigate_history("back")     ,  browser back, avoids star-topology
      3. navigate_to(TARGET_SOCIAL_URL) ,  hard navigate, last resort only

    Using hard navigate as the *first* fallback creates a star-shaped
    navigation graph (all action paths converge at the feed root) which is
    a detectable bot pattern.  Steps 1–2 preserve the SPA history so the
    back-stack remains varied and realistic.
    """
    tag = f"[{context}]  " if context else ""
    if click_home_button(driver):
        return
    log.debug("%shome button not found ,  trying navigate_history(back)", tag)
    try:
        navigate_history(driver, "back")
        WebDriverWait(driver, 6).until(
            lambda d: "threads.net" in d.current_url or "threads.com" in d.current_url
        )
        return
    except Exception:
        pass
    log.debug("%sback navigation failed ,  hard navigate (last resort)", tag)
    navigate_to(driver, TARGET_SOCIAL_URL)

def check_notifications_action(driver) -> None:
    """
    Click the Notifications nav button and dwell.
    30 % of visits tap a notification item and dwell 2-5 s before returning
    (fix #36) ,  scroll-only visits every time is a mildly suspicious pattern.
    """
    log.info("Checking notifications via nav button")
    try:
        if not _click_nav_btn(driver, "Notifications", "Notifications"):
            log.debug("Notifications button not found ,  skipping")
            return
        precise_sleep(random.uniform(2.0, 5.0))
        stochastic_scroll(driver, total_seconds=random.uniform(5, 15))

        # Fix #36: 30 % of visits tap a notification item.
        # Notification items are <a> links inside the notifications list;
        # we target visible ones that contain a time element (activity items)
        # and avoid the generic "All" / "Verified" filter tabs.
        if random.random() < 0.30:
            try:
                notif_items = [
                    el for el in driver.find_elements(
                        By.CSS_SELECTOR,
                        'a[href*="@"][role="link"], a[href*="/t/"][role="link"]'
                    )
                    if el.is_displayed()
                ]
                if notif_items:
                    target = random.choice(notif_items[:8])
                    scroll_element_into_loose_view(driver, target)
                    bezier_move(driver, target)
                    precise_sleep(random.uniform(0.4, 0.9))
                    _cdp_click_element(driver, target)  # Fix 1.4: never JS .click()
                    log.info("[ NOTIFY ]  tapped notification item")
                    precise_sleep(random.uniform(2.0, 5.0))
                    # Go back to notifications page before returning to feed
                    navigate_history(driver, "back")
                    precise_sleep(random.uniform(0.8, 1.8))
            except (NoSuchElementException, WebDriverException) as _ne:
                log.debug("[ NOTIFY ]  item tap failed: %s", _ne)

        # Return to feed via 3-step hierarchy (home → back → hard-navigate)
        _safe_return_to_feed(driver, "notifications")
        precise_sleep(random.uniform(1.0, 2.5))
    except (TimeoutException, WebDriverException) as exc:
        log.debug("Notification check failed: %s", exc)

def return_to_top_action(driver) -> None:
    """Click the Home / Threads-logo nav button to scroll back to the top of the feed."""
    _safe_return_to_feed(driver, "return_top")

def visit_search_action(driver) -> None:
    """
    Click the Search nav icon.  70 % of the time type a short generic query
    from SEARCH_TOPIC_POOL and scroll the results for 5–15 s.  The remaining
    30 % only dwell (scanning trending topics without committing to a query).

    Opening search and immediately leaving without any interaction is a
    statistically rare pattern that looks automated; adding real query typing
    and result scrolling brings the action in line with observed user behaviour.
    """
    log.info("Visiting search page via nav button")
    try:
        if not _click_nav_btn(driver, "Search", "Search"):
            log.debug("Search button not found ,  skipping")
            return

        # Wait for the search input to be present and visible before proceeding.
        # The fixed sleep is replaced with an explicit element wait so the bot
        # never proceeds on a partially-loaded page regardless of network speed.
        try:
            search_input = WebDriverWait(driver, 12).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, SEARCH_INPUT_CSS))
            )
        except TimeoutException:
            log.debug("visit_search_action: search input did not appear after nav click ,  dwell fallback")
            precise_sleep(random.uniform(3.0, 8.0))
            _safe_return_to_feed(driver, "search")
            return

        # Brief human pause after the page settles — user orients before acting.
        precise_sleep(random.uniform(0.8, 2.0))

        if random.random() < 0.70:
            # ── Type a query and scroll results ──────────────────────────────
            try:
                ctx = _get_ctx()
                _active_search_pool = (
                    ctx.profile_search_topic_pool
                    if ctx.profile_content_loaded else SEARCH_TOPIC_POOL
                )
                if not _active_search_pool:
                    log.info("[ SEARCH ]  skipped query; no approved search topics configured")
                    _safe_return_to_feed(driver, "search")
                    return
                query = random.choice(_active_search_pool)
                bezier_move(driver, search_input)
                precise_sleep(random.uniform(0.3, 0.8))
                _cdp_click_element(driver, search_input)  # Fix 1.6: CDP, not native Selenium click
                precise_sleep(random.uniform(0.2, 0.5))
                human_type(search_input, query, driver)
                precise_sleep(random.uniform(0.4, 0.9))
                search_input.send_keys(Keys.RETURN)
                log.info("[ SEARCH ]  typed query=%r", query)

                # Wait for at least one result item to appear before scrolling.
                # Falls back to a fixed pause if results use a non-standard structure.
                _SEARCH_RESULT_CSS = (
                    "article, "
                    "div[data-pressable-container='true'], "
                    "div[role='article']"
                )
                try:
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, _SEARCH_RESULT_CSS))
                    )
                    # Small settle pause after first result renders
                    precise_sleep(random.uniform(0.5, 1.2))
                except TimeoutException:
                    # Results may use a layout we don't recognise — wait a fixed
                    # amount so the page still has time to finish loading.
                    log.debug("[ SEARCH ]  result selector timed out ,  fixed settle fallback")
                    precise_sleep(random.uniform(2.0, 3.5))

                stochastic_scroll(driver, total_seconds=random.uniform(5.0, 15.0))
            except (NoSuchElementException, WebDriverException) as _se:
                # Input interaction failed ,  fall back to plain dwell.
                log.debug("visit_search_action: input interaction failed (%s) ,  dwell fallback", _se)
                precise_sleep(random.uniform(3.0, 8.0))
        else:
            # ── Dwell only ,  scanning trending content ────────────────────────
            precise_sleep(random.uniform(3.0, 8.0))

        # Return to feed via 3-step hierarchy (home → back → hard-navigate)
        _safe_return_to_feed(driver, "search")
        precise_sleep(random.uniform(1.0, 2.0))
    except (TimeoutException, WebDriverException) as exc:
        log.debug("visit_search_action failed: %s", exc)


# ================================================================== #
#  PASSIVE / ACTIVE ACTIONS  +  SESSION LOOP
# ================================================================== #

def passive_action(driver) -> None:
    """
    Passive action: scroll, with occasional browser back/forward
    to break the perfectly linear navigation graph.

    Feed guard: if the current URL is not the Threads feed homepage
    (e.g. an unintended click during a prior action landed elsewhere),
    the home nav button is used to return before scrolling starts.
    A hard navigate_to fallback fires if the button is not found.
    """
    # ── Feed URL guard ────────────────────────────────────────────────
    _FEED_ROOTS = ("https://www.threads.com/", "https://www.threads.net/")
    try:
        current = driver.current_url

        # ── Media-viewer overlay: close with the X button, then re-check URL ─
        if _close_media_overlay(driver):
            current = driver.current_url   # refresh after close

        on_feed = any(current.rstrip("/") + "/" == root or current == root
                      for root in _FEED_ROOTS)
        if not on_feed:
            log.info(
                "[ACTION SKIP]  action=passive  reason=off_feed  recovered=True  url=%s",
                current[:80],
            )
            # 3-step fallback: home button → navigate_history(back) → hard navigate
            _safe_return_to_feed(driver, "passive_feed_guard")
            precise_sleep(random.uniform(1.2, 2.5))  # settle after nav
    except WebDriverException as exc:
        log.debug("[ PASSIVE ]  URL guard error: %s", exc)
    # ─────────────────────────────────────────────────────────────────

    scroll_time = random.uniform(15, 45)
    # ── DEBUG LOGGING: ACTION START ────────────────────────────────────────────
    _action_t0 = time.perf_counter()
    _get_ctx().session_metrics["actions_dispatched"] += 1
    log.info("[ACTION START]  action=passive")
    # ────────────────────────────────────────────────────────────────────
    log.info("[ PASSIVE ]  scroll %.0fs", scroll_time)
    stochastic_scroll(driver, total_seconds=scroll_time)

    # Pause after scrolling stops ,  user finishes reading the post
    precise_sleep(random.uniform(1.0, 3.0))
    # ── DEBUG LOGGING: ACTION END ────────────────────────────────────────────
    _get_ctx().session_metrics["passive"] += 1
    _log_page_state(driver, "passive_end")
    log.info("[ACTION END]  action=passive  result=success  duration=%.1fs",
             time.perf_counter() - _action_t0)
    # ────────────────────────────────────────────────────────────────────


def active_action(driver) -> None:
    """
    Active action ,  likes only.

    Improvements over the original:
    - URL-guarded: only runs on threads.net to avoid scanning the login page.
    - Stochastic pre-scroll: 50 % short, 30 % medium, 20 % none at all.
    - Variable like count: 0 (skip, 15%), 1 (50%), 2 (25%), 3 (10%).
    """
    current_url = driver.current_url
    # Accept both threads.net and threads.com ,  the browser redirects .net → .com
    on_threads = "threads.net" in current_url or "threads.com" in current_url
    if not on_threads:
        log.info(
            "[ACTION SKIP]  action=active  reason=not_on_threads  url=%s",
            current_url[:60],
        )
        stochastic_scroll(driver, total_seconds=random.uniform(15, 30))
        return

    # ── DEBUG LOGGING: ACTION START ────────────────────────────────────────────
    _action_t0 = time.perf_counter()
    _get_ctx().session_metrics["actions_dispatched"] += 1
    _log_page_state(driver, "active_start")
    log.info("[ACTION START]  action=active  url=%s", current_url[:60])
    # ────────────────────────────────────────────────────────────────────
    liked = 0
    try:
        # Stochastic pre-scroll ,  50% short, 30% medium, 20% skip entirely
        pre_roll = random.random()
        if pre_roll < 0.50:
            smooth_scroll_chunk(driver, random.randint(150, 400), step_px=5)
            precise_sleep(random.uniform(1.0, 3.0))
        elif pre_roll < 0.80:
            smooth_scroll_chunk(driver, random.randint(400, 800), step_px=6)
            precise_sleep(random.uniform(2.0, 4.0))
        else:
            # No pre-scroll ,  cursor is already resting on feed content
            precise_sleep(random.uniform(0.5, 1.5))

        candidates = _find_unliked_buttons(driver)
        log.info("[ACTIVE]  unliked_buttons_found=%d", len(candidates))
        if not candidates:
            log.info("[ACTION SKIP]  action=active  reason=no_likeable_posts"
                     "  fallback=passive_scroll")
            stochastic_scroll(driver, total_seconds=random.uniform(15, 30))
            return

        # Weighted like count: 75% 1-like, 25% 2-likes
        like_roll = random.random()
        if like_roll < 0.75:
            n_targets = 1
        else:
            n_targets = 2

        n_targets = min(n_targets, len(candidates))
        targets   = random.sample(candidates, n_targets)

        for btn in targets:
            if _attempt_like(driver, btn):
                liked += 1
                if liked < len(targets):
                    # Pause between likes ,  user glances at feed between hearts
                    precise_sleep(random.uniform(2.0, 5.0))

        # After liking, scroll slightly to load fresh content
        if liked > 0:
            precise_sleep(random.uniform(0.5, 1.5))
            smooth_scroll_chunk(driver, random.randint(250, 500), step_px=6)
            precise_sleep(random.uniform(1.0, 2.0))

    except (NoSuchElementException, WebDriverException) as exc:
        log.warning("[ACTIVE]  error: %s", exc)

    # ── DEBUG LOGGING: ACTION END ────────────────────────────────────────────
    _get_ctx().session_metrics["likes"] += liked
    _log_page_state(driver, "active_end")
    log.info("[ACTION END]  action=active  result=success  likes=%d  duration=%.1fs",
             liked, time.perf_counter() - _action_t0)
    # ────────────────────────────────────────────────────────────────────


# ================================================================== #
#  READ-POST ACTION
# ================================================================== #

def read_post_action(driver) -> bool:
    """
    Click into a thread post to read the full reply chain, dwell naturally,
    then navigate back to the feed.

    This models the common behaviour of a user tapping a post to read the
    comments ,  a signal that strongly differentiates real users from bots
    that only scroll-and-like without ever opening individual threads.

    Flow:
      1. Find visible post links (href contains /post/ or /t/).
      2. Scroll the chosen link into loose view.
      3. Bezier-arc to the link and click.
      4. Dwell 5–18 s with stochastic scrolling through the reply chain.
      5. Navigate back to the feed.
    """
    try:
        current_url = driver.current_url
        if "threads.net" not in current_url and "threads.com" not in current_url:
            log.info("[ACTION SKIP]  action=read_post  reason=not_on_threads  url=%s",
                     current_url[:60])
            return False
        # ── DEBUG LOGGING: ACTION START ─────────────────────────────────────────
        _action_t0 = time.perf_counter()
        _get_ctx().session_metrics["actions_dispatched"] += 1
        log.info("[ACTION START]  action=read_post")
        # ────────────────────────────────────────────────────────────────────

        # Collect visible post links
        links = driver.find_elements(
            By.CSS_SELECTOR,
            'a[href*="/post/"], a[href*="/t/"]',
        )
        visible = []
        for lnk in links:
            try:
                if lnk.is_displayed():
                    r = driver.execute_script(
                        "var r=arguments[0].getBoundingClientRect();"
                        "return {top:r.top, h:r.height};",
                        lnk,
                    )
                    vh = driver.execute_script("return window.innerHeight")
                    if r["h"] > 0 and 0 <= r["top"] <= vh:
                        visible.append(lnk)
            except Exception:
                continue

        log.info("[READ POST]  visible_post_links=%d", len(visible))
        if not visible:
            log.info("[ACTION SKIP]  action=read_post  reason=no_visible_post_links")
            return False

        target = random.choice(visible[:10])
        scroll_element_into_loose_view(driver, target)

        # Deliberate hover pause ,  user deciding to click
        bezier_move(driver, target)
        precise_sleep(random.uniform(0.4, 1.2))

        _cdp_click(driver)
        debug_cursor_state(driver, "read-post-click")
        try:
            WebDriverWait(driver, 15).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException:
            pass
        inject_cursor_overlay(driver)
        init_cursor_pos(driver)

        dwell = random.uniform(5.0, 18.0)
        log.info("[ READ POST ]  reading thread for %.0fs", dwell)
        precise_sleep(random.uniform(1.0, 2.5))  # initial read of the post itself
        stochastic_scroll(driver, total_seconds=dwell)

        # Return via Home nav button ,  clicking the logo is more natural than
        # the browser back button when finishing reading a thread.
        # Falls back to navigate_history only if the nav icon is not found.
        if not click_home_button(driver):
            log.debug("read_post_action: home button not found ,  back fallback")
            navigate_history(driver, "back")
        precise_sleep(random.uniform(1.0, 2.5))
        log.info("[ READ POST ]  returned to feed")
        # ── DEBUG LOGGING: ACTION END (success) ──────────────────────────────────
        _get_ctx().session_metrics["reads"] += 1
        log.info("[ACTION END]  action=read_post  result=success  duration=%.1fs",
                 time.perf_counter() - _action_t0)
        # ────────────────────────────────────────────────────────────────────
        return True

    except (NoSuchElementException, TimeoutException, WebDriverException) as exc:
        log.warning("[ACTION END]  action=read_post  result=failure  error=%s", exc)
        try:
            if driver.current_url != current_url:
                if not click_home_button(driver):
                    navigate_history(driver, "back")
        except Exception:
            pass
        return False


# ================================================================== #
#  COMMENT ACTION
# ================================================================== #

def comment_on_post(driver) -> bool:
    """
    Publish a pre-approved, business-safe reply to a visible post in the feed.

    Flow:
      1. Find visible Reply buttons in the current viewport.
      2. Pick one at random; scroll it loosely into view.
      3. Read-pause ,  user finishes reading the post before replying.
      4. Bezier-arc to the Reply button and click.
      5. Wait up to 8 s for the comment text field to appear.
      6. Bezier-arc to the text field and type one complete reply from the
         approved reply pool.
      7. Re-reading pause ,  user proofreads before posting.
      8. Find the Post button closest to the text field (avoids matching the
         global “New post” compose button); bezier-arc and click.
      9. Post-click pause ,  watching the reply appear.
    Returns True on success.
    """
    try:
        current_url = driver.current_url
        on_threads  = "threads.net" in current_url or "threads.com" in current_url
        if not on_threads:
            log.info("[ACTION SKIP]  action=comment  reason=not_on_threads  url=%s",
                     current_url[:60])
            return False

        ctx = _get_ctx()
        approved_pool = (
            ctx.profile_approved_reply_pool
            if ctx.profile_content_loaded else APPROVED_REPLIES
        )
        eligible_replies: list[str] = []
        for index, candidate in enumerate(approved_pool):
            try:
                eligible_replies.append(
                    prepare_reply_for_publishing(candidate, BRAND_VOICE)
                )
            except ContentPolicyError as exc:
                log.warning(
                    "[REPLY CONTROLS] reply rejected index=%d reason=%s",
                    index,
                    exc,
                )
        if not eligible_replies:
            log.warning("[REPLY CONTROLS] no approved business-safe reply available")
            return False
        reply = random.choice(eligible_replies)
        # ── DEBUG LOGGING: ACTION START ─────────────────────────────────────────
        _action_t0 = time.perf_counter()
        _get_ctx().session_metrics["actions_dispatched"] += 1
        log.info("[ACTION START]  action=comment")
        # ────────────────────────────────────────────────────────────────────

        # 1. Collect visible Reply buttons using multi-signal scoring
        visible = _find_reply_buttons(driver)

        log.info("[COMMENT]  visible_reply_buttons=%d", len(visible))
        if not visible:
            log.info("[ACTION SKIP]  action=comment  reason=no_visible_reply_buttons")
            return False

        target_btn = random.choice(visible[:8])
        scroll_element_into_loose_view(driver, target_btn)

        # 2. Read-pause ,  user reads the post before deciding to reply
        precise_sleep(random.uniform(2.5, 6.0))

        # 3. Bezier-arc to Reply and click
        bezier_move(driver, target_btn)
        precise_sleep(random.uniform(0.3, 0.7))
        _cdp_click_element(driver, target_btn)  # Fix 1.4: never JS .click()
        debug_cursor_state(driver, "comment-reply-click")

        # 4. Wait for the comment box to appear
        try:
            comment_box = WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, COMMENT_BOX_CSS))
            )
        except TimeoutException:
            log.debug("comment_on_post: comment box did not appear after Reply click")
            return False

        precise_sleep(random.uniform(0.5, 1.2))   # settle before moving to box

        # 5. Bezier-arc to text field, then type
        bezier_move(driver, comment_box)
        precise_sleep(random.uniform(0.3, 0.6))
        log.info("[REPLY CONTROLS] approved reply selected chars=%d", len(reply))
        human_type(comment_box, reply, driver)

        # 6. Re-reading pause
        precise_sleep(random.uniform(1.2, 3.0))

        # 7. Find the Post button.
        #    Multiple matches can exist (one per visible reply form).
        #    We pick the one whose vertical midpoint is closest to the comment
        #    box ,  this reliably targets the active reply form’s submit button
        #    without misidentifying the global compose button.
        try:
            box_mid = driver.execute_script(
                "var r=arguments[0].getBoundingClientRect();"
                "return r.top + r.height / 2;",
                comment_box,
            )
            post_btns = driver.find_elements(By.XPATH, COMMENT_POST_XPATH)
            post_btns = [b for b in post_btns if b.is_displayed()]
            if not post_btns:
                raise NoSuchElementException("Post button not found")
            # Sort by distance from the comment box midpoint
            def _dist(b):
                try:
                    r = driver.execute_script(
                        "var r=arguments[0].getBoundingClientRect();"
                        "return r.top + r.height / 2;",
                        b,
                    )
                    return abs(r - box_mid)
                except Exception:
                    return 9999
            post_btn = min(post_btns, key=_dist)
        except (NoSuchElementException, WebDriverException):
            log.debug("comment_on_post: Post button not found ,  aborting")
            return False

        #scroll_element_into_loose_view(driver, post_btn)
        bezier_move(driver, post_btn)
        precise_sleep(random.uniform(0.3, 0.6))
        _cdp_click_element(driver, post_btn)  # Fix 1.4: never JS .click()
        debug_cursor_state(driver, "comment-post-click")

        # 8. Post-click pause ,  watching the reply appear
        precise_sleep(random.uniform(1.5, 3.5))
        log.info("[REPLY CONTROLS] approved reply published successfully")
        # ── DEBUG LOGGING: ACTION END (success) ──────────────────────────────────
        _get_ctx().session_metrics["comments"] += 1
        log.info("[ACTION END]  action=comment  result=success  duration=%.1fs",
                 time.perf_counter() - _action_t0)
        # ────────────────────────────────────────────────────────────────────
        return True

    except (NoSuchElementException, TimeoutException, WebDriverException) as exc:
        log.warning("[ACTION END]  action=comment  result=failure  error=%s", exc)
        return False

# ─────────────────────────────────────────────────────────────────────────────#
def _log_selector_failure(driver, element_type: str) -> None:
    """Log a DOM snapshot for offline selector maintenance when scoring fails."""
    try:
        snapshot = driver.execute_script("""
            var posts = document.querySelectorAll(
                'article, [data-pressable-container="true"]'
            );
            var out = [];
            for (var i = 0; i < Math.min(posts.length, 3); i++) {
                var p = posts[i];
                var btns = p.querySelectorAll('div[role="button"]');
                var info = [];
                for (var b = 0; b < Math.min(btns.length, 8); b++) {
                    var svg = btns[b].querySelector('svg');
                    info.push({
                        aria: btns[b].getAttribute('aria-label') || '',
                        svg_aria: svg ? (svg.getAttribute('aria-label') || '') : '',
                        pressed: btns[b].getAttribute('aria-pressed') || '',
                        visible: btns[b].offsetParent !== null,
                    });
                }
                out.push({ btn_count: info.length, buttons: info });
            }
            return out;
        """)
        log.warning(
            "[SELF-HEAL]  element=%s  score_failure  dom_snapshot=%s",
            element_type, json.dumps(snapshot)[:500],
        )
    except Exception:
        log.warning(
            "[SELF-HEAL]  element=%s  score_failure  snapshot_unavailable",
            element_type,
        )

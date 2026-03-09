# ── Selector constants ─────────────────────────────────────────────────────── #
# Profile link in post header ,  href="/@username"
FEED_PROFILE_LINK  = 'a[href^="/@"][role="link"]'
# Small + icon follow button (SVG aria-label="Follow")
QUICK_FOLLOW_BTN   = 'div[role="button"]:has(svg[aria-label="Follow"])'
# XPath for text-based Follow button (feed inline, profile page, cards)
FOLLOW_BTN_XPATH   = '//div[@role="button" and .//div[normalize-space(text())="Follow"]]'
# X button on suggested cards
DISMISS_CARD_BTN   = 'div[role="button"]:has(svg[aria-label="Close"])'
# Reply (comment) button in each post’s action bar
REPLY_BTN_CSS      = 'div[role="button"]:has(svg[aria-label="Reply"])'
# Contenteditable reply box that appears after clicking Reply
COMMENT_BOX_CSS    = 'div[contenteditable="true"][role="textbox"]'
# Post button that submits the comment (XPath ,  scoped to role=button wrapping text “Post”)
COMMENT_POST_XPATH = '//div[@role="button" and .//div[normalize-space(text())="Post"]]'
# Hidden file-upload input inside the compose modal
COMPOSE_FILE_INPUT_CSS = 'input[type="file"][accept]'
# Compose / New-post button in the nav sidebar (aria-label="Post")
COMPOSE_BTN_SELECTORS = [
    # aria-label="New post" ,  current Threads desktop nav (2025+)
    ("css",   '[aria-label="New post"]'),
    ("css",   '[aria-label="New Post"]'),
    # aria-label on the SVG itself ,  older builds
    ("css",   'div[role="button"]:has(svg[aria-label="Post"])'),
    ("css",   'a[role="link"]:has(svg[aria-label="Post"])'),
    ("css",   'div[role="button"][aria-label="Post"]'),
    # aria-label variants seen across locales / A/B tests
    ("css",   '[aria-label="Create"]'),
    ("css",   '[aria-label="Compose"]'),
    ("xpath", '//div[@role="button" and contains(translate(@aria-label,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"new post")]'),
    ("xpath", '//*[@role="button" and .//*[local-name()="svg"][@aria-label="Post"]]'),
    ("xpath", '//*[@role="button" and .//*[local-name()="svg"][@aria-label="New post"]]'),
]
# Compose modal textbox (new-post box, not the comment/reply box)
COMPOSE_TEXTBOX_CSS = 'div[data-lexical-editor="true"][contenteditable="true"]'
# "Attach media" button inside the compose modal
COMPOSE_ATTACH_BTN_CSS = 'div[role="button"]:has(svg[aria-label="Attach media"])'
# Search text input on the Threads search page
SEARCH_INPUT_CSS = (
    'input[type="search"],'
    'input[placeholder*="earch"],'
    'input[role="searchbox"]'
)

# Known aria-label values across locales ,  LOW-WEIGHT signal only.
_KNOWN_LIKE_LABELS = frozenset({
    "like", "love", "heart", "me gusta", "j'aime", "gefällt mir",
    "いいね", "curtir", "좋아요", "赞", "mi piace", "thích",
})
_KNOWN_UNLIKE_LABELS = frozenset({
    "unlike", "unlove", "no me gusta", "je n'aime plus",
    "gefällt mir nicht mehr", "いいね取消", "descurtir",
    "좋아요 취소", "取消赞", "non mi piace più",
})
_KNOWN_REPLY_LABELS = frozenset({
    "reply", "comment", "respond", "responder", "répondre",
    "antworten", "返信", "comentar", "댓글", "评论", "rispondi",
})
# ─────────────────────────────────────────────────────────────────────────────#

# ================================================================== #
#  MULTI-SIGNAL LIKE ENGINE
# ================================================================== #
#
# Element identification uses composite scoring across multiple signals
# instead of relying solely on fragile aria-label selectors:
#   1. Structural position in the action bar (like = 1st button)
#   2. SVG path geometry (heart = many Bezier curves, ~square viewBox)
#   3. Fill state (transparent = un-liked, currentColor = liked)
#   4. Sibling context (3-5 icon-button siblings = action bar)
#   5. ARIA labels (low-weight fallback covering known locales)
#
# Self-healing: when no candidate passes ELEMENT_CONFIDENCE_THRESHOLD
# a DOM snapshot is logged for offline selector maintenance.  Legacy
# XPath/CSS selectors serve as a fallback during transitions.
# ================================================================== #

# JavaScript: multi-signal like-button scorer.
# Returns [[WebElement, score, positionIdx, siblingCount, ariaLabel], ...].
# Selenium deserialises returned DOM nodes as WebElements.
_JS_MULTI_SIGNAL_LIKE = r"""
(function(threshold) {
    var vp = window.innerHeight;
    var results = [];
    var posts = document.querySelectorAll(
        'article, [data-pressable-container="true"]'
    );

    for (var p = 0; p < posts.length; p++) {
        var post = posts[p];
        var pr   = post.getBoundingClientRect();
        if (pr.bottom < -100 || pr.top > vp + 100 || pr.height === 0) continue;

        /* -- Find the action bar structurally --
           The action bar is a container whose direct children include
           3-6 role="button" elements each wrapping an SVG icon.
           We pick the one lowest (largest relative-Y) in the post. */
        var allDivs  = post.querySelectorAll('div');
        var barBtns  = [];
        var bestRelY = -Infinity;

        for (var d = 0; d < allDivs.length; d++) {
            var ctr  = allDivs[d];
            var btns = [];
            for (var c = 0; c < ctr.children.length; c++) {
                var ch  = ctr.children[c];
                var rb  = (ch.getAttribute && ch.getAttribute('role') === 'button')
                          ? ch
                          : (ch.querySelector ? ch.querySelector('[role="button"]') : null);
                if (rb && rb.querySelector('svg')) btns.push(rb);
            }
            if (btns.length < 3 || btns.length > 6) continue;
            var cr   = ctr.getBoundingClientRect();
            var relY = cr.top - pr.top;
            if (relY > bestRelY) { bestRelY = relY; barBtns = btns; }
        }
        if (!barBtns.length) continue;

        /* -- Score each button in the action bar -- */
        for (var i = 0; i < barBtns.length; i++) {
            var btn = barBtns[i];
            var svg = btn.querySelector('svg');
            if (!svg) continue;
            var br = btn.getBoundingClientRect();
            if (br.height === 0 || br.width === 0) continue;

            var s = 0.0;

            /* Signal 1 - Position: like is almost always first */
            if      (i === 0) s += 0.25;
            else if (i === 1) s += 0.08;
            else              s -= 0.15;

            /* Signal 2 - SVG path geometry: heart = many Bezier curves */
            var paths = svg.querySelectorAll('path');
            for (var pp = 0; pp < paths.length; pp++) {
                var dd     = paths[pp].getAttribute('d') || '';
                var curves = (dd.match(/[CcQqSsAa]/g) || []).length;
                if (curves >= 4) { s += 0.15; break; }
            }

            /* Signal 3 - ViewBox aspect ratio: hearts are roughly square */
            var vb = (svg.getAttribute('viewBox') || '').split(/[\s,]+/);
            if (vb.length === 4) {
                var rat = parseFloat(vb[2]) / Math.max(1, parseFloat(vb[3]));
                if (rat > 0.8 && rat < 1.3) s += 0.10;
            }

            /* Signal 4 - Fill state: transparent = NOT yet liked */
            var sty  = svg.getAttribute('style') || '';
            var fill = svg.getAttribute('fill')  || '';
            var isTransparent = (sty.indexOf('transparent') > -1 ||
                                 fill === 'transparent' || fill === 'none');
            var isFilled      = (sty.indexOf('currentColor') > -1 ||
                                 fill === 'currentColor');
            if (isTransparent) s += 0.15;
            if (isFilled)      s -= 0.50;

            /* Signal 5 - ARIA label (low weight fallback) */
            var lbl = (svg.getAttribute('aria-label') ||
                       btn.getAttribute('aria-label') || '').toLowerCase();
            var likeL   = ['like','love','heart','me gusta',"j'aime",
                           'curtir','gefällt mir','\u3044\u3044\u306d','\uC88B\uC544\uC694',
                           '\u8D5E','mi piace','thích'];
            var unlikeL = ['unlike','unlove','no me gusta','descurtir',
                           "je n'aime plus",'\u3044\u3044\u306d\u53d6\u6d88',
                           '\uC88B\uC544\uC694 \uCDE8\uC18C','\u53d6\u6d88\u8d5e'];
            for (var kl = 0; kl < likeL.length;   kl++) {
                if (lbl === likeL[kl])   { s += 0.12; break; }
            }
            for (var ku = 0; ku < unlikeL.length; ku++) {
                if (lbl === unlikeL[ku]) { s -= 0.60; break; }
            }

            /* Signal 6 - aria-pressed */
            if (btn.getAttribute('aria-pressed') === 'true') s -= 0.40;

            /* Signal 7 - Sibling context: 3-5 icon-button siblings */
            if (barBtns.length >= 3 && barBtns.length <= 5) s += 0.08;

            if (s >= threshold) {
                results.push([btn, s, i, barBtns.length, lbl || '']);
            }
        }
    }

    results.sort(function(a, b) { return b[1] - a[1]; });
    return results;
})(arguments[0]);
"""

# JavaScript: multi-signal reply-button scorer.
# Reply is typically the 2nd button in the action bar; its SVG has a
# speech-bubble shape (mix of curves and lines, moderate total commands).
_JS_MULTI_SIGNAL_REPLY = r"""
(function(threshold) {
    var vp = window.innerHeight;
    var results = [];
    var posts = document.querySelectorAll(
        'article, [data-pressable-container="true"]'
    );

    for (var p = 0; p < posts.length; p++) {
        var post = posts[p];
        var pr   = post.getBoundingClientRect();
        if (pr.bottom < -100 || pr.top > vp + 100 || pr.height === 0) continue;

        var allDivs  = post.querySelectorAll('div');
        var barBtns  = [];
        var bestRelY = -Infinity;

        for (var d = 0; d < allDivs.length; d++) {
            var ctr  = allDivs[d];
            var btns = [];
            for (var c = 0; c < ctr.children.length; c++) {
                var ch  = ctr.children[c];
                var rb  = (ch.getAttribute && ch.getAttribute('role') === 'button')
                          ? ch
                          : (ch.querySelector ? ch.querySelector('[role="button"]') : null);
                if (rb && rb.querySelector('svg')) btns.push(rb);
            }
            if (btns.length < 3 || btns.length > 6) continue;
            var cr   = ctr.getBoundingClientRect();
            var relY = cr.top - pr.top;
            if (relY > bestRelY) { bestRelY = relY; barBtns = btns; }
        }
        if (!barBtns.length) continue;

        for (var i = 0; i < barBtns.length; i++) {
            var btn = barBtns[i];
            var svg = btn.querySelector('svg');
            if (!svg) continue;
            var br = btn.getBoundingClientRect();
            if (br.height === 0) continue;

            var s = 0.0;

            /* Position: reply is typically second (index 1) */
            if      (i === 1) s += 0.30;
            else if (i === 0) s -= 0.10;
            else if (i === 2) s += 0.05;
            else              s -= 0.15;

            /* SVG geometry: speech bubbles have moderate curve+line mix */
            var paths = svg.querySelectorAll('path');
            for (var pp = 0; pp < paths.length; pp++) {
                var dd     = paths[pp].getAttribute('d') || '';
                var curves = (dd.match(/[CcQqSsAa]/g) || []).length;
                var lines  = (dd.match(/[LlHhVv]/g) || []).length;
                if (curves >= 2 && curves <= 8 && (curves + lines) >= 3) {
                    s += 0.15; break;
                }
            }

            /* ViewBox aspect: speech bubbles are often roughly square-ish */
            var vb = (svg.getAttribute('viewBox') || '').split(/[\s,]+/);
            if (vb.length === 4) {
                var rat = parseFloat(vb[2]) / Math.max(1, parseFloat(vb[3]));
                if (rat > 0.85 && rat < 1.4) s += 0.08;
            }

            /* ARIA label (low weight) */
            var lbl = (svg.getAttribute('aria-label') ||
                       btn.getAttribute('aria-label') || '').toLowerCase();
            var replyL = ['reply','comment','respond','responder','répondre',
                          'antworten','\u8FD4\u4FE1','comentar','\uB313\uAE00',
                          '\u8BC4\u8BBA','rispondi'];
            for (var k = 0; k < replyL.length; k++) {
                if (lbl === replyL[k]) { s += 0.15; break; }
            }

            /* Sibling context */
            if (barBtns.length >= 3 && barBtns.length <= 5) s += 0.08;

            if (s >= threshold) results.push([btn, s, i]);
        }
    }

    results.sort(function(a, b) { return b[1] - a[1]; });
    return results;
})(arguments[0]);
"""

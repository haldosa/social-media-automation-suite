import os
import re
import random
import tempfile
import hashlib
import math
import shutil
import glob as _glob
import requests
import sys
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException
from utils import log
from state import _post_state_locked, _load_post_state, _save_post_state, _ensure_profile_in_state
# ================================================================== #
#  CHROMEDRIVER $cdc_ VARIABLE DEFENCE
# ================================================================== #
# ChromeDriver injects $cdc_asdjflasutopfhvcZLmcfl_ (or similarly named)
# properties into every document context.  Meta's JS can enumerate
# document properties and detect these.  Multi-layered defence:
#   Layer 1 ,  Pre-page JS mask (Page.addScriptToEvaluateOnNewDocument)
#   Layer 2 ,  Binary-patch ChromeDriver (build-time, cached)
#   Layer 3 ,  Runtime verification

_CDC_MASK_JS = """
(function() {
    const re = /\\$[a-z]dc_/;
    const names = Object.getOwnPropertyNames(document);
    for (const p of names) {
        if (re.test(p)) {
            delete document[p];
            Object.defineProperty(document, p, {
                get: function() { return undefined; },
                configurable: false,
            });
        }
    }
})();
"""


def _patch_chromedriver_binary(path: str) -> str:
    """Binary-patch ChromeDriver to replace $cdc_ variable name.

    Copies the binary to a stable per-original temp path before patching so
    the original executable (potentially locked by the OS or used by other
    runs) is never modified in-place.  The copy path is keyed to the original
    path via its MD5 digest so the same original always maps to the same copy.
    A .patched marker on the copy path makes the operation idempotent.
    """
    _tmp_root = os.path.join(tempfile.gettempdir(), "nstbrowser_cd_patch")
    os.makedirs(_tmp_root, exist_ok=True)
    name_hash = hashlib.md5(os.path.abspath(path).encode()).hexdigest()[:12]
    suffix = ".exe" if sys.platform == "win32" else ""
    copy_path = os.path.join(_tmp_root, f"chromedriver_{name_hash}{suffix}")
    patched_marker = copy_path + ".patched"
    if os.path.exists(patched_marker) and os.path.exists(copy_path):
        return copy_path
    try:
        shutil.copy2(path, copy_path)
        with open(copy_path, "rb") as f:
            data = f.read()
        pattern = re.compile(rb'\$cdc_[a-zA-Z0-9]{22}_')
        matches = list(pattern.finditer(data))
        if not matches:
            log.info("ChromeDriver binary: no $cdc_ pattern found (already clean or new version)")
            open(patched_marker, "w").close()
            return copy_path
        for m in reversed(matches):
            replacement = b'$xxx_' + b'a' * (len(m.group()) - 5)
            data = data[:m.start()] + replacement + data[m.end():]
        with open(copy_path, "wb") as f:
            f.write(data)
        open(patched_marker, "w").close()
        log.info("ChromeDriver binary patched: %d $cdc_ occurrence(s) replaced → %s",
                 len(matches), copy_path)
    except PermissionError:
        log.warning("ChromeDriver binary patch failed: permission denied ,  using original")
        return path
    except Exception as exc:
        log.warning("ChromeDriver binary patch failed: %s ,  using original", exc)
        return path
    return copy_path

# ================================================================== #
#  CHROMEDRIVER RESOLUTION
# ================================================================== #

def _get_browser_major_version(ws_url: str) -> int:
    """Query /json/version on the running browser to get the Chrome major version."""
    host_port = ws_url.replace("ws://", "").split("/")[0]
    try:
        resp = requests.get(f"http://{host_port}/json/version", timeout=5)
        m = re.search(r"/(\d+)", resp.json().get("Browser", ""))
        if m:
            return int(m.group(1))
    except Exception as exc:
        log.debug("Could not read browser version: %s", exc)
    return 0


def _get_chromedriver_path(major: int) -> str:
    """
    Resolve the correct chromedriver in priority order:
      1. NstBrowser's own bundled chromedriver (guaranteed version match)
      2. webdriver-manager auto-download
      3. System PATH fallback
    """
    # 1. Bundled
    search_roots = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\NstBrowser\resources\app\browser"),
        os.path.expandvars(r"%PROGRAMFILES%\NstBrowser\resources\app\browser"),
        os.path.expanduser("~/Library/Application Support/NstBrowser/browser"),
        "/Applications/NstBrowser.app/Contents/Resources/app/browser",
        os.path.expanduser("~/.config/NstBrowser/browser"),
        "/opt/NstBrowser/resources/app/browser",
    ]
    for root in search_roots:
        for pat in (os.path.join(root, "**", "chromedriver.exe"),
                    os.path.join(root, "**", "chromedriver")):
            hits = _glob.glob(pat, recursive=True)
            if hits:
                log.info("Using bundled chromedriver: %s", hits[0])
                return _patch_chromedriver_binary(hits[0])

    # 2. webdriver-manager
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        path = ChromeDriverManager(driver_version=str(major)).install()
        log.info("webdriver-manager chromedriver: %s", path)
        return _patch_chromedriver_binary(path)
    except ImportError:
        log.warning("webdriver-manager not installed ,  run: pip install webdriver-manager")
    except Exception as e:
        log.warning("webdriver-manager failed (%s) ,  falling back to PATH", e)

    # 3. PATH
    log.warning("Using system chromedriver from PATH (may fail if version mismatches).")
    return "chromedriver"


def connect_selenium(ws_debugger_url: str) -> webdriver.Chrome:
    """
    Attach Selenium to the already-running NstBrowser Orbita browser via CDP.

    When attaching via debuggerAddress the ONLY valid ChromeOption is
    debuggerAddress ,  all launch-time flags are rejected by ChromeDriver
    because the browser process is already running.
    """
    address = ws_debugger_url.replace("ws://", "").split("/")[0]
    log.info("Connecting Selenium -> debuggerAddress: %s", address)

    major = _get_browser_major_version(ws_debugger_url)
    log.info("Detected Orbita major version: %s", major or "unknown")

    options = webdriver.ChromeOptions()
    options.add_experimental_option("debuggerAddress", address)

    driver = webdriver.Chrome(
        service=Service(executable_path=_get_chromedriver_path(major)),
        options=options,
    )

    # NstBrowser's Orbita engine handles navigator.webdriver and fingerprint
    # spoofing at a lower level than CDP scripts.  Injecting our own
    # Page.addScriptToEvaluateOnNewDocument patch creates a detectable
    # inconsistency (the characteristic getOwnPropertyDescriptor side-effects
    # that Meta's bot detection explicitly checks for), so we omit it entirely
    # and trust the profile's built-in fingerprint configuration.
    # Fix 7.3: Network.enable is not needed (no Network.* CDP commands used
    # downstream) and keeping the domain active is a detectable CDP signal.

    # Fix #14: run Layer 3 (runtime verification) FIRST.
    # If the binary patch already removed all $cdc_ properties, skipping the
    # Page.addScriptToEvaluateOnNewDocument injection avoids creating the
    # non-configurable property-descriptor side-effects that Object.
    # getOwnPropertyDescriptor() can expose as a bot-detection signal.
    _cdc_found = []
    try:
        _cdc_found = driver.execute_script(
            "return Object.getOwnPropertyNames(document)"
            ".filter(function(p){return /\\$[a-z]dc_/.test(p)});"
        ) or []
    except WebDriverException:
        pass

    if _cdc_found:
        # Properties still present ,  fix the current context immediately
        # and register the mask for all subsequent page loads (Layer 1).
        log.warning("$cdc_ variables detected ,  applying runtime fix + pre-page mask: %s", _cdc_found)
        try:
            driver.execute_script(_CDC_MASK_JS)
        except WebDriverException:
            pass
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": _CDC_MASK_JS,
            })
        except WebDriverException as exc:
            log.debug("$cdc_ pre-page mask injection failed: %s", exc)
    else:
        # Binary patch succeeded ,  no $cdc_ properties exist.  Skipping the
        # addScriptToEvaluateOnNewDocument injection entirely prevents the
        # non-configurable getter side-effect that anti-bot probes look for.
        log.debug("$cdc_ mask skipped: runtime verification confirms no ChromeDriver variables")

    _verify_browser_fingerprint(driver)
    log.info("Selenium attached successfully.")
    return driver


def _verify_browser_fingerprint(driver) -> bool:
    """
    Post-connect fingerprint consistency check (Fix #28).

    Verifies three independent signals that NstBrowser's spoofing layer
    should have already configured.  Failures are WARNING-level so the
    session continues ,  the operator can review the log offline.

    Checks
    ------
    1. navigator.webdriver must be falsy ,  Orbita suppresses this at the
       engine level without CDP injection (which creates detectable
       non-configurable getter side-effects).

    2. WebGL unmasked renderer must be non-empty and must NOT be
       "Google SwiftShader" (= CPU software renderer, meaning no GPU
       identity spoofing is active and the raw GPU string is visible).

    3. Canvas toDataURL fingerprint must be stable (two identical samples
       within the same page context) ,  confirms the per-profile canvas
       noise seed is deterministic rather than randomised per-call
       (per-call randomisation is itself a detectable pattern).
    """
    all_ok = True

    # 1. navigator.webdriver ────────────────────────────────────────────────
    try:
        wd_val = driver.execute_script("return navigator.webdriver;")
        if wd_val:
            log.warning(
                "[FP VERIFY]  FAIL  navigator.webdriver=%s "
                ",  Orbita injection may not be active", wd_val
            )
            all_ok = False
        else:
            log.debug("[FP VERIFY]  OK    navigator.webdriver=false")
    except WebDriverException as exc:
        log.debug("[FP VERIFY]  navigator.webdriver check skipped: %s", exc)

    # 2. WebGL unmasked renderer ────────────────────────────────────────────
    try:
        gl_info = driver.execute_script("""
            try {
                var c = document.createElement('canvas');
                var gl = c.getContext('webgl') ||
                         c.getContext('experimental-webgl');
                if (!gl) return {renderer: '', vendor: ''};
                var ext = gl.getExtension('WEBGL_debug_renderer_info');
                if (!ext)  return {renderer: 'ext-unavailable', vendor: ''};
                return {
                    renderer: gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) || '',
                    vendor:   gl.getParameter(ext.UNMASKED_VENDOR_WEBGL)   || '',
                };
            } catch(e) { return {renderer: '', vendor: ''}; }
        """)
        renderer = (gl_info or {}).get("renderer", "")
        vendor   = (gl_info or {}).get("vendor",   "")
        log.info("[FP VERIFY]  WebGL  renderer=%r  vendor=%r", renderer, vendor)
        if not renderer or renderer == "ext-unavailable":
            log.warning(
                "[FP VERIFY]  WARN   WebGL UNMASKED_RENDERER unavailable "
                ",  WEBGL_debug_renderer_info extension may be blocked"
            )
        elif "SwiftShader" in renderer:
            log.warning(
                "[FP VERIFY]  FAIL   WebGL renderer is SwiftShader (CPU) "
                ",  GPU-level spoofing is inactive; real GPU string is exposed"
            )
            all_ok = False
    except WebDriverException as exc:
        log.debug("[FP VERIFY]  WebGL check skipped: %s", exc)

    # 3. Canvas fingerprint stability ───────────────────────────────────────
    _CANVAS_FP_JS = """
        try {
            var c = document.createElement('canvas');
            c.width = 220; c.height = 30;
            var ctx = c.getContext('2d');
            ctx.textBaseline = 'top';
            ctx.font = '14px \'Arial\'';
            ctx.fillStyle = '#f60';
            ctx.fillRect(125, 1, 62, 20);
            ctx.fillStyle = '#069';
            ctx.fillText('Cwm fjordbank glyphs vext quiz', 2, 15);
            ctx.fillStyle = 'rgba(102,204,0,0.7)';
            ctx.fillText('Cwm fjordbank glyphs vext quiz', 4, 17);
            return c.toDataURL();
        } catch(e) { return ''; }
    """
    try:
        sample_a = driver.execute_script(_CANVAS_FP_JS)
        sample_b = driver.execute_script(_CANVAS_FP_JS)
        if not sample_a:
            log.warning(
                "[FP VERIFY]  FAIL   canvas toDataURL returned empty string "
                ",  canvas API may be blocked or broken"
            )
            all_ok = False
        elif sample_a != sample_b:
            log.warning(
                "[FP VERIFY]  FAIL   canvas fingerprint differs between two calls "
                ",  per-call randomisation is itself a detectable bot signal"
            )
            all_ok = False
        else:
            fp_hash = hashlib.md5(sample_a.encode()).hexdigest()[:12]
            log.info("[FP VERIFY]  OK    canvas stable  hash=%s", fp_hash)
    except WebDriverException as exc:
        log.debug("[FP VERIFY]  canvas check skipped: %s", exc)

    if all_ok:
        log.info("[FP VERIFY]  all checks passed")
    else:
        log.warning(
            "[FP VERIFY]  one or more checks failed "
            ",  session continues but trust signals may be degraded"
        )
    return all_ok

def _generate_typing_dna() -> dict:
    """Generate a stable per-profile typing fingerprint ('typing DNA').

    Each person has idiosyncratic keystroke dynamics -- their personal
    mu/sigma for inter-key intervals, burst length range, bigram-specific
    penalties, punctuation pause personality, error propensity, and fatigue
    drift rate.  Sampled once and persisted in post_state.json so the same
    profile always types with the same rhythm.
    """
    burst_min = random.randint(2, 5)
    burst_max = burst_min + random.randint(2, 5)
    return {
        "base_mu":              random.uniform(math.log(0.065), math.log(0.110)),
        "base_sigma":           random.uniform(0.30, 0.55),
        "burst_min":            burst_min,
        "burst_max":            burst_max,
        "space_pause_lo":       random.uniform(0.03, 0.08),
        "space_pause_hi":       random.uniform(0.12, 0.22),
        "punct_pause_lo":       random.uniform(0.15, 0.30),
        "punct_pause_hi":       random.uniform(0.40, 0.70),
        "burst_gap_lo":         random.uniform(0.04, 0.08),
        "burst_gap_hi":         random.uniform(0.12, 0.25),
        "hesitation_prob":      random.uniform(0.02, 0.07),
        "hesitation_lo":        random.uniform(0.20, 0.40),
        "hesitation_hi":        random.uniform(0.60, 1.00),
        "bigram_penalty_lo":    random.uniform(1.2, 1.6),
        "bigram_penalty_hi":    random.uniform(1.6, 2.2),
        "error_rate":           random.uniform(0.01, 0.06),
        "correction_prob":      random.uniform(0.70, 0.95),
        "detection_delay_mean": random.uniform(0.5, 2.5),
        "fatigue_drift":        random.uniform(0.002, 0.012),
    }


def _get_typing_dna(profile_id: str) -> dict:
    """Load or generate the typing DNA for a profile.

    On first call for a profile, generates a new typing fingerprint and
    persists it in post_state.json.  Subsequent calls return the stored
    fingerprint so the same profile always types with the same rhythm.
    """
    if not profile_id:
        return _generate_typing_dna()

    with _post_state_locked():
        state = _load_post_state()
        _ensure_profile_in_state(profile_id, state)

        profile = state.get(profile_id, {})
        dna = profile.get("typing_dna")
        if dna and isinstance(dna, dict) and "base_mu" in dna:
            return dna

        dna = _generate_typing_dna()
        state[profile_id]["typing_dna"] = dna
        _save_post_state(state)
    log.info("[ TYPING DNA ]  generated fingerprint for %s  "
             "mu=%.3f sigma=%.2f err=%.1f%%",
             profile_id[:8], dna["base_mu"], dna["base_sigma"],
             dna["error_rate"] * 100)
    return dna

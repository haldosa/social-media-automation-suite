import os
import re
import random
import math
import glob as _glob
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from utils import log
from state import (
    _post_state_locked,
    _load_post_state,
    _save_post_state,
    _ensure_profile_in_state,
)


def _get_browser_major_version(ws_url: str) -> int:
    """Read Chrome major version from the debugger endpoint."""
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
    Resolve chromedriver with a simple deterministic order:
      1. NstBrowser bundled driver
      2. webdriver-manager
      3. PATH fallback
    """
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
                return hits[0]

    try:
        from webdriver_manager.chrome import ChromeDriverManager
        if major > 0:
            path = ChromeDriverManager(driver_version=str(major)).install()
        else:
            path = ChromeDriverManager().install()
        log.info("webdriver-manager chromedriver: %s", path)
        return path
    except ImportError:
        log.warning("webdriver-manager not installed; run: pip install webdriver-manager")
    except Exception as exc:
        log.warning("webdriver-manager failed (%s), falling back to PATH", exc)

    log.warning("Using system chromedriver from PATH (version mismatch possible).")
    return "chromedriver"


def connect_selenium(ws_debugger_url: str) -> webdriver.Chrome:
    """
    Attach Selenium to an already-running browser via debuggerAddress.

    This thesis version intentionally avoids stealth/evasion-specific runtime
    patching and keeps the connection layer minimal and auditable.
    """
    address = ws_debugger_url.replace("ws://", "").split("/")[0]
    log.info("Connecting Selenium -> debuggerAddress: %s", address)

    major = _get_browser_major_version(ws_debugger_url)
    log.info("Detected browser major version: %s", major or "unknown")

    options = webdriver.ChromeOptions()
    options.add_experimental_option("debuggerAddress", address)

    driver = webdriver.Chrome(
        service=Service(executable_path=_get_chromedriver_path(major)),
        options=options,
    )
    log.info("Selenium attached successfully.")
    return driver


def _generate_typing_dna() -> dict:
    """Generate a stable per-profile typing fingerprint."""
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
    """Load or create the typing DNA for a profile."""
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

    log.info(
        "[ TYPING DNA ]  generated fingerprint for %s  mu=%.3f sigma=%.2f err=%.1f%%",
        profile_id[:8], dna["base_mu"], dna["base_sigma"], dna["error_rate"] * 100,
    )
    return dna

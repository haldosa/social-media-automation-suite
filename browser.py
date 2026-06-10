import glob as _glob
import math
import os
import random
import re

import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service

from state import (
    _ensure_profile_in_state,
    _load_post_state,
    _post_state_locked,
    _save_post_state,
)
from utils import log


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
      1. NstBrowser's bundled chromedriver when present
      2. webdriver-manager auto-download
      3. System PATH fallback
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
        for pat in (
            os.path.join(root, "**", "chromedriver.exe"),
            os.path.join(root, "**", "chromedriver"),
        ):
            hits = _glob.glob(pat, recursive=True)
            if hits:
                log.info("Using bundled chromedriver: %s", hits[0])
                return hits[0]

    try:
        from webdriver_manager.chrome import ChromeDriverManager

        path = ChromeDriverManager(driver_version=str(major)).install()
        log.info("webdriver-manager chromedriver: %s", path)
        return path
    except ImportError:
        log.warning("webdriver-manager not installed; run: pip install webdriver-manager")
    except Exception as exc:
        log.warning("webdriver-manager failed (%s); falling back to PATH", exc)

    log.warning("Using system chromedriver from PATH (may fail if version mismatches).")
    return "chromedriver"


def connect_selenium(ws_debugger_url: str) -> webdriver.Chrome:
    """
    Attach Selenium to an already-running NstBrowser/Chrome profile.

    When attaching via debuggerAddress, the only launch option needed is the
    debugger address because the browser process is already running.
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


# ================================================================== #
#  TYPING PROFILE
# ================================================================== #


def _generate_typing_dna() -> dict:
    """Generate a stable per-profile typing profile.

    The function name is kept for compatibility with existing imports. New
    thesis-facing wording treats this as a configurable typing pace profile,
    not as a behavioral identity marker.
    """
    burst_min = random.randint(2, 5)
    burst_max = burst_min + random.randint(2, 5)
    return {
        "base_mu": random.uniform(math.log(0.065), math.log(0.110)),
        "base_sigma": random.uniform(0.30, 0.50),
        "burst_min": burst_min,
        "burst_max": burst_max,
        "space_pause_lo": random.uniform(0.03, 0.08),
        "space_pause_hi": random.uniform(0.12, 0.22),
        "punct_pause_lo": random.uniform(0.15, 0.30),
        "punct_pause_hi": random.uniform(0.35, 0.65),
        "burst_gap_lo": random.uniform(0.04, 0.08),
        "burst_gap_hi": random.uniform(0.12, 0.25),
        "hesitation_prob": random.uniform(0.01, 0.04),
        "hesitation_lo": random.uniform(0.20, 0.40),
        "hesitation_hi": random.uniform(0.50, 0.90),
        "bigram_penalty_lo": random.uniform(1.1, 1.4),
        "bigram_penalty_hi": random.uniform(1.4, 1.8),
        "error_rate": 0.0,
        "correction_prob": 0.0,
        "correction_delay_mean": 0.0,
        "fatigue_drift": random.uniform(0.001, 0.006),
    }


def _get_typing_dna(profile_id: str) -> dict:
    """Load or generate the typing profile for a profile.

    Existing persisted ``typing_dna`` keys are still accepted for compatibility.
    """
    if not profile_id:
        return _generate_typing_dna()

    with _post_state_locked():
        state = _load_post_state()
        _ensure_profile_in_state(profile_id, state)

        profile = state.get(profile_id, {})
        typing_profile = profile.get("typing_dna")
        if typing_profile and isinstance(typing_profile, dict) and "base_mu" in typing_profile:
            typing_profile["error_rate"] = 0.0
            typing_profile["correction_prob"] = 0.0
            typing_profile["correction_delay_mean"] = 0.0
            return typing_profile

        typing_profile = _generate_typing_dna()
        state[profile_id]["typing_dna"] = typing_profile
        _save_post_state(state)

    log.info(
        "[ TYPING PROFILE ]  generated pace profile for %s  mu=%.3f sigma=%.2f",
        profile_id[:8],
        typing_profile["base_mu"],
        typing_profile["base_sigma"],
    )
    return typing_profile


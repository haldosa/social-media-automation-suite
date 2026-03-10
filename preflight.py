import random
from config import (
    PREFLIGHT_SITES_MIN, PREFLIGHT_SITES_MAX,
    PREFLIGHT_DWELL_MIN, PREFLIGHT_DWELL_MAX,
)
from pools import PREFLIGHT_SITES_POOL
from utils import log
from scroll import stochastic_scroll, navigate_to
from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
)
# ================================================================== #
#  PRE-FLIGHT  (Wikipedia only)
# ================================================================== #

def run_preflight(driver) -> None:
    """
    Browse a random selection of pre-flight sites to seed a varied, natural
    browsing history before navigating to Threads.

    Each run samples 2-4 sites from PREFLIGHT_SITES_POOL so the history is
    never identical across sessions, reducing the pattern of a single
    Wikipedia visit that always precedes Threads activity.
    """
    k     = random.randint(PREFLIGHT_SITES_MIN, PREFLIGHT_SITES_MAX)
    sites = random.sample(PREFLIGHT_SITES_POOL, k=min(k, len(PREFLIGHT_SITES_POOL)))
    log.info("Pre-flight: visiting %d site(s): %s", len(sites), [s.split('/')[2] for s in sites])
    for site in sites:
        dwell = random.uniform(PREFLIGHT_DWELL_MIN, PREFLIGHT_DWELL_MAX)
        log.info("Pre-flight: %s  (%.0fs)", site, dwell)
        try:
            navigate_to(driver, site)
        except (TimeoutException, WebDriverException):
            log.warning("Pre-flight: %s timed out ,  skipping", site)
            continue
        stochastic_scroll(driver, total_seconds=dwell)


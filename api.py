import subprocess
import time
import requests
import os
from config import NSTBROWSER_BASE_URL, NSTBROWSER_API_KEY, _SCRIPT_DIR
from utils import log
# ================================================================== #
#  NSTBROWSER API v2
# ================================================================== #

def _headers() -> dict:
    return {"x-api-key": NSTBROWSER_API_KEY}


def start_profile(profile_id: str) -> dict:
    """
    POST /api/v2/browsers/{profileId}
    No body, no Content-Type ,  exactly as the official curl example shows.
    Returns data dict containing webSocketDebuggerUrl and port.
    """
    url  = f"{NSTBROWSER_BASE_URL}/browsers/{profile_id}"
    resp = requests.post(url, headers=_headers(), timeout=60)
    if not resp.ok:
        log.error("start_profile HTTP %s ,  %s", resp.status_code, resp.text[:300])
    resp.raise_for_status()
    body = resp.json()
    if body.get("err") is True or body.get("code") not in (0, 200):
        raise RuntimeError(f"NstBrowser start error for {profile_id}: {body}")
    data = body["data"]
    log.info("Profile %s launched  |  port=%s  |  ws=%s",
             profile_id, data.get("port"), data.get("webSocketDebuggerUrl"))
    return data


def stop_profile(profile_id: str) -> None:
    """DELETE /api/v2/browsers/{profileId}"""
    url = f"{NSTBROWSER_BASE_URL}/browsers/{profile_id}"
    try:
        resp = requests.delete(url, headers=_headers(), timeout=15)
        body = resp.json()
        if body.get("err") is False or body.get("code") in (0, 200):
            log.info("Profile %s closed cleanly.", profile_id)
        else:
            log.warning("Unexpected close response for %s: %s", profile_id, body)
    except Exception as exc:
        log.error("Error closing profile %s: %s", profile_id, exc)

def get_running_browsers() -> list:
    """GET /api/v2/browsers ,  lists all running browser instances."""
    url = f"{NSTBROWSER_BASE_URL}/browsers"
    try:
        resp = requests.get(url, headers=_headers(), timeout=10)
        resp.raise_for_status()
        return resp.json().get("data") or []
    except Exception as exc:
        log.debug("Could not fetch running browsers: %s", exc)
        return []

def _resolve_attached_address(profile_id: str) -> str:
    """
    Query ``GET /api/v2/browsers`` to find the CDP debug address for a
    profile that is currently open in NstBrowser.

    Returns ``host:port`` string or raises ``RuntimeError``.
    """
    running = get_running_browsers()
    if not running:
        raise RuntimeError(
            "No running browsers reported by GET /api/v2/browsers.\n"
            "Make sure the profile is open in NstBrowser first."
        )

    for b in running:
        pid = b.get("profileId") or b.get("profile_id") or b.get("id") or ""
        if pid != profile_id:
            continue

        # Try the ws URL first (most reliable)
        for ws_key in ("webSocketDebuggerUrl", "wsUrl", "ws", "wsDebugUrl"):
            ws = b.get(ws_key, "")
            if ws:
                return ws.replace("ws://", "").split("/")[0]

        # Fall back to bare port (try all known field names across API versions)
        port = (
            b.get("remoteDebuggingPort")
            or b.get("port")
            or b.get("debugPort")
            or b.get("remote_debugging_port")
        )
        if port:
            return f"127.0.0.1:{port}"

        # Profile matched but no address extractable
        raise RuntimeError(
            f"Profile '{profile_id}' is running but no debug address could be "
            "resolved from the API response.  "
            "Pass --attach HOST:PORT directly."
        )

    ids = [b.get("profileId") or b.get("id", "?") for b in running]
    raise RuntimeError(
        f"Profile '{profile_id}' was not found in the running browsers list.\n"
        f"Currently running: {ids}\n"
        "Open the profile in NstBrowser before using --attach-profile."
    )

# Common Chrome executable paths — first match wins
_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]


def _find_chrome_exe() -> str:
    """Return the first Chrome executable that exists on this machine."""
    import os
    for path in _CHROME_CANDIDATES:
        if os.path.isfile(path):
            return path
    raise RuntimeError(
        "Chrome executable not found. Install Chrome or set the path manually "
        "in _CHROME_CANDIDATES inside api.py."
    )


def get_chrome_ws_url(port: int = 9222) -> str:
    """
    Query Chrome's CDP endpoint and return the browser-level WebSocket URL.
    Chrome must already be running with --remote-debugging-port=<port>.
    """
    try:
        resp = requests.get(
            f"http://localhost:{port}/json/version", timeout=5
        )
        resp.raise_for_status()
        return resp.json()["webSocketDebuggerUrl"]
    except Exception as exc:
        raise RuntimeError(
            f"Could not reach Chrome CDP on port {port}. "
            f"Is Chrome running with --remote-debugging-port={port}? "
            f"Error: {exc}"
        )


_chrome_procs: dict[str, subprocess.Popen] = {}  # keyed by profile id


def start_chrome(profile: dict) -> dict:
    """
    Launch a plain Chrome instance for a demo profile.
    profile = {"id": "demo1", "port": 9222, "dir": "C:\\..."}
    """
    pid  = profile["id"]
    port = profile["port"]
    dir_ = profile["dir"]

    os.makedirs(dir_, exist_ok=True)
    exe = _find_chrome_exe()

    log.info("[DEMO]  launching Chrome  |  id=%s  |  port=%d", pid, port)

    proc = subprocess.Popen(
        [
            exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={dir_}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _chrome_procs[pid] = proc
    log.info("[DEMO]  Chrome launched  |  id=%s  |  pid=%d", pid, proc.pid)

    for _ in range(20):
        try:
            ws_url = get_chrome_ws_url(port)
            log.info("[DEMO]  CDP ready  |  id=%s  |  ws=%s", pid, ws_url)
            return {"webSocketDebuggerUrl": ws_url, "port": port, "id": pid}
        except RuntimeError:
            time.sleep(0.5)

    raise RuntimeError(f"Chrome CDP did not become available for {pid} on port {port}")


def stop_chrome(profile_id: str) -> None:
    """Terminate the Chrome instance for the given demo profile."""
    proc = _chrome_procs.get(profile_id)
    if proc and proc.poll() is None:
        log.info("[DEMO]  stopping Chrome  |  id=%s  |  pid=%d", profile_id, proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        del _chrome_procs[profile_id]
    else:
        log.debug("[DEMO]  stop_chrome called but no process found for %s", profile_id)
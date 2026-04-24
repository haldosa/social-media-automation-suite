import subprocess
import time
import requests
from config import CHROME_PROFILES
from utils import log
import os

# Common Chrome executable paths — first match wins
_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]


def _find_chrome_exe() -> str:
    """Return the first Chrome executable that exists on this machine."""
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


def _chrome_profile_by_id(profile_id: str) -> dict:
    for profile in CHROME_PROFILES:
        if profile.get("id") == profile_id:
            return profile
    raise RuntimeError(
        f"Unknown Chrome profile id '{profile_id}'. "
        f"Available ids: {[p['id'] for p in CHROME_PROFILES]}"
    )


def start_chrome(profile: dict) -> dict:
    """
    Launch a plain Chrome instance for a configured profile.
    profile = {"id": "profile1", "port": 9222, "dir": "C:\\..."}
    """
    pid = profile["id"]
    port = profile["port"]
    dir_ = profile["dir"]

    existing = _chrome_procs.get(pid)
    if existing and existing.poll() is None:
        ws_url = get_chrome_ws_url(port)
        return {"webSocketDebuggerUrl": ws_url, "port": port, "id": pid}

    os.makedirs(dir_, exist_ok=True)
    exe = _find_chrome_exe()

    log.info("Launching Chrome  |  id=%s  |  port=%d", pid, port)

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
    log.info("Chrome launched  |  id=%s  |  pid=%d", pid, proc.pid)

    for _ in range(20):
        try:
            ws_url = get_chrome_ws_url(port)
            log.info("CDP ready  |  id=%s  |  ws=%s", pid, ws_url)
            return {"webSocketDebuggerUrl": ws_url, "port": port, "id": pid}
        except RuntimeError:
            time.sleep(0.5)

    raise RuntimeError(f"Chrome CDP did not become available for {pid} on port {port}")


def stop_chrome(profile_id: str) -> None:
    """Terminate the Chrome instance for the given profile."""
    proc = _chrome_procs.get(profile_id)
    if proc and proc.poll() is None:
        log.info("Stopping Chrome  |  id=%s  |  pid=%d", profile_id, proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        del _chrome_procs[profile_id]
    else:
        log.debug("stop_chrome called but no process found for %s", profile_id)


def start_profile(profile_id: str) -> dict:
    """Compatibility wrapper: start configured Chrome profile by id."""
    profile = _chrome_profile_by_id(profile_id)
    return start_chrome(profile)


def stop_profile(profile_id: str) -> None:
    """Compatibility wrapper: stop configured Chrome profile by id."""
    stop_chrome(profile_id)


def get_running_browsers() -> list:
    """
    Return currently managed Chrome profile processes in a legacy-compatible
    shape expected by existing attach code.
    """
    out = []
    for profile in CHROME_PROFILES:
        pid = profile["id"]
        proc = _chrome_procs.get(pid)
        if not proc or proc.poll() is not None:
            continue
        port = profile["port"]
        try:
            ws_url = get_chrome_ws_url(port)
        except Exception:
            ws_url = ""
        out.append(
            {
                "id": pid,
                "profileId": pid,
                "port": port,
                "webSocketDebuggerUrl": ws_url,
            }
        )
    return out


def _resolve_attached_address(profile_id: str) -> str:
    profile = _chrome_profile_by_id(profile_id)
    return f"127.0.0.1:{profile['port']}"

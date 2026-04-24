"""
Threads Warmer — Flask Control Panel
Run with: python app.py
Access at: http://localhost:5000
"""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

# ── Path to your script directory ─────────────────────────────────────────────
# Change this to the absolute path of your script folder
SCRIPT_DIR = os.environ.get(
    "WARMER_SCRIPT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."),
)
PYTHON_EXE  = sys.executable
MAIN_SCRIPT = os.path.join(SCRIPT_DIR, "main.py")
COOKIE_SCRIPT = os.path.join(SCRIPT_DIR, "cookie_bot.py")
STATE_FILE  = os.path.join(SCRIPT_DIR, "post_state.json")
HEARTBEAT_FILE = os.path.join(SCRIPT_DIR, "heartbeat.json")
COOKIE_STATE_FILE = os.path.join(SCRIPT_DIR, "cookie_state.json")

import logging

flask_log_path = os.path.join(SCRIPT_DIR, "logs", "flask.log")
os.makedirs(os.path.dirname(flask_log_path), exist_ok=True)

flask_handler = logging.FileHandler(flask_log_path, encoding="utf-8")
flask_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(message)s"
))

# Log Flask app events
app.logger.addHandler(flask_handler)
app.logger.setLevel(logging.INFO)

# Also log process launches/stops
_app_log = logging.getLogger("warmer.flask")
_app_log.addHandler(flask_handler)
_app_log.setLevel(logging.INFO)

# ── Process registry ───────────────────────────────────────────────────────────
_procs: dict[str, subprocess.Popen] = {}
_proc_lock = threading.Lock()
_proc_start_times: dict[str, float] = {}


# Used by _launch() to route subprocess output
PROC_LOG_FILES = {
    "daemon":         os.path.join(SCRIPT_DIR, "logs", "warmer.log"),
    "session":        os.path.join(SCRIPT_DIR, "logs", "warmer.log"),
    "cookie_session": os.path.join(SCRIPT_DIR, "logs", "cookie.log"),
    "test":           os.path.join(SCRIPT_DIR, "logs", "warmer.log"),
}

# Used by /api/logs/stream/<log_name>
LOG_FILES = {
    "warmer": os.path.join(SCRIPT_DIR, "logs", "warmer.log"),
    "cookie": os.path.join(SCRIPT_DIR, "logs", "cookie.log"),
    "flask":  os.path.join(SCRIPT_DIR, "logs", "flask.log"),
}

def _launch(key: str, cmd: list[str]) -> dict:
    with _proc_lock:
        existing = _procs.get(key)
        if existing and existing.poll() is None:
            return {"ok": False, "msg": f"{key} is already running"}

        log_path = PROC_LOG_FILES.get(key, os.path.join(SCRIPT_DIR, "logs", f"{key}.log"))
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        log_file = open(log_path, "a", encoding="utf-8")

        proc = subprocess.Popen(
            cmd,
            cwd=SCRIPT_DIR,
            stdout=log_file,
            stderr=log_file,
        )
        _procs[key] = proc
        _proc_start_times[key] = time.time()
        return {"ok": True, "msg": f"{key} started (pid={proc.pid})"}
    

def _stop(key: str) -> dict:
    with _proc_lock:
        proc = _procs.get(key)
        if not proc or proc.poll() is not None:
            return {"ok": False, "msg": f"{key} is not running"}
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        _app_log.info("STOP    key=%s", key)
        return {"ok": True, "msg": f"{key} stopped"}


def _status(key: str) -> dict:
    with _proc_lock:
        proc = _procs.get(key)
        running = proc is not None and proc.poll() is None
        pid     = proc.pid if running else None
        start   = _proc_start_times.get(key)
        uptime  = None
        if running and start:
            elapsed = int(time.time() - start)
            h, r = divmod(elapsed, 3600)
            m    = r // 60
            uptime = f"{h}h {m:02d}m" if h else f"{m}m"
        return {"running": running, "pid": pid, "uptime": uptime}


# ── Routes: pages ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

# ── Routes: process control ───────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    return jsonify({
        "daemon":        _status("daemon"),
        "cookie_session":_status("cookie_session"),
        "session":       _status("session"),
        "test":          _status("test"),
        "timestamp":     datetime.now().strftime("%H:%M:%S"),
    })


@app.route("/api/daemon/start", methods=["POST"])
def daemon_start():
    cmd = [PYTHON_EXE, MAIN_SCRIPT, "--daemon"]
    return jsonify(_launch("daemon", cmd))


@app.route("/api/daemon/stop", methods=["POST"])
def daemon_stop():
    return jsonify(_stop("daemon"))


@app.route("/api/cookie/single/start", methods=["POST"])
def cookie_single_start():
    data = request.json or {}
    profile = data.get("profile_id", "")
    runs = data.get("runs")
    cmd = [PYTHON_EXE, COOKIE_SCRIPT]
    if runs:
        cmd += ["--runs", runs]
    if profile:
        cmd += ["--profile", profile]
    else:
        cmd += ["--all-profiles"]
    return jsonify(_launch("cookie_session", cmd))  # was "cookie_single"

@app.route("/api/cookie/single/stop", methods=["POST"])
def cookie_single_stop():
    return jsonify(_stop("cookie_session"))  # was "cookie_single"

@app.route("/api/session/start", methods=["POST"])
def session_start():
    data = request.json or {}
    profile = data.get("profile_id", "")
    cmd = [PYTHON_EXE, MAIN_SCRIPT]
    if profile:
        cmd += ["--profile-id", profile]
    if data.get("no_preflight"):
        cmd.append("--no-preflight")
    return jsonify(_launch("session", cmd))


@app.route("/api/test/start", methods=["POST"])
def test_start():
    data = request.json or {}
    action  = data.get("action", "like")
    profile = data.get("profile_id", "")
    cmd = [PYTHON_EXE, MAIN_SCRIPT, "--test-actions", action]
    if profile:
        cmd += ["--profile-id", profile]
    cmd.append("--no-preflight")
    return jsonify(_launch("test", cmd))


@app.route("/api/session/stop", methods=["POST"])
def session_stop():
    return jsonify(_stop("session"))


@app.route("/api/test/stop", methods=["POST"])
def test_stop():
    return jsonify(_stop("test"))


# ── Routes: state & heartbeat ─────────────────────────────────────────────────

@app.route("/api/heartbeat")
def heartbeat():
    try:
        with open(HEARTBEAT_FILE, "r") as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({})


@app.route("/api/state")
def state():
    try:
        with open(STATE_FILE, "r") as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({})


@app.route("/api/cookie_state")
def cookie_state():
    try:
        with open(COOKIE_STATE_FILE, "r") as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({})


# ── Routes: log streaming (SSE) ───────────────────────────────────────────────

import re
from collections import defaultdict

def _parse_log_lines(lines: list[str]) -> list[dict]:
    """Parse raw log lines into structured entries."""
    entries = []
    pattern = re.compile(
        r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)\s+(\w+)\s+(.+)$'
    )
    for line in lines:
        line = line.rstrip()
        if not line:
            continue
        m = pattern.match(line)
        if m:
            entries.append({
                "ts":    m.group(1),
                "level": m.group(2),
                "msg":   m.group(3),
                "raw":   line,
            })
        else:
            # Continuation line (stack trace etc) — attach to last entry
            if entries:
                entries[-1]["raw"] += "\n" + line
            else:
                entries.append({"ts": "", "level": "INFO", "msg": line, "raw": line})
    return entries


def _group_log_entries(entries: list[dict]) -> list[dict]:
    """
    Group log entries into sessions/processes based on delimiter lines.
    Returns list of groups, newest first.
    """
    groups = []
    current = None

    for entry in entries:
        msg = entry["msg"]

        # Detect session/process start boundaries
        if "=" * 20 in msg and current is not None:
            groups.append(current)
            current = None

        if current is None:
            # Start a new group
            label = "Session"
            if "DAEMON" in msg:
                label = "Daemon"
            elif "COOKIE" in msg:
                label = "Cookie Bot"
            elif "TEST" in msg or "test-actions" in msg:
                label = "Test Actions"
            elif "Threads Warmer" in msg:
                label = f"Session {entry['ts'][11:16]}"

            current = {
                "label":   label,
                "ts":      entry["ts"],
                "lines":   [],
                "has_error": False,
            }

        current["lines"].append(entry)
        if entry["level"] in ("ERROR", "WARNING"):
            current["has_error"] = True

    if current and current["lines"]:
        groups.append(current)

    # Newest first
    groups.reverse()
    return groups


@app.route("/api/logs/groups")
def log_groups():
    """Return grouped log entries as JSON for the dropdown UI."""
    try:
        with open(LOG_FILES["warmer"], "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-500:]  # last 500 lines
        entries = _parse_log_lines(lines)
        groups  = _group_log_entries(entries)
        # Trim each group to last 100 lines for payload size
        for g in groups:
            g["lines"] = [l["raw"] for l in g["lines"]][-100:]
        return jsonify({"groups": groups})
    except FileNotFoundError:
        return jsonify({"groups": []})
    except Exception as exc:
        return jsonify({"groups": [], "error": str(exc)})


@app.route("/api/logs/stream")
@app.route("/api/logs/stream/<log_name>")
def log_stream(log_name="warmer"):
    if log_name not in LOG_FILES:
        return "Not found", 404
    path = LOG_FILES[log_name]

    def generate():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                for line in lines[-80:]:
                    yield f"data: {line.rstrip()}\n\n"
        except FileNotFoundError:
            yield f"data: [{log_name}.log not found]\n\n"
        
        yield "data: __backfill_end__\n\n"  # signal end of backfill
        
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        yield f"data: {line.rstrip()}\n\n"
                    else:
                        time.sleep(0.4)
        except Exception:
            return
    
    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        from waitress import serve
        print("Starting on http://0.0.0.0:5000")
        serve(app, host="0.0.0.0", port=5000, threads=16)
    except ImportError:
        app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)

"""
NstBrowser Warmer — Flask Control Panel
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

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
except Exception:
    pass

app = Flask(__name__)

# ── Path to your script directory ─────────────────────────────────────────────
# Change this to the absolute path of your script folder
SCRIPT_DIR = os.environ.get(
    "WARMER_SCRIPT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."),
)
PYTHON_EXE  = sys.executable
MAIN_SCRIPT = os.path.join(SCRIPT_DIR, "main.py")
STATE_FILE  = os.path.join(SCRIPT_DIR, "post_state.json")
HEARTBEAT_FILE = os.path.join(SCRIPT_DIR, "heartbeat.json")
UI_CONFIG_FILE = os.path.join(SCRIPT_DIR, "warmer_ui_config.json")

ACTION_WEIGHT_FLAGS = {
    "like": "--like",
    "notify": "--notify",
    "profile": "--profile",
    "read_post": "--read-post",
    "comment": "--comment",
    "follow": "--follow",
    "scroll": "--scroll",
    "search": "--search",
    "post": "--post",
}

DEFAULT_UI_CONFIG = {
    "nstbrowser_api_key": "",
    "profile_ids": [],
    "target_social_url": "https://www.threads.net",
    "active_hours": [8, 23],
    "default_no_preflight": False,
    "action_weights": {key: None for key in ACTION_WEIGHT_FLAGS},
}

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
    "test":           os.path.join(SCRIPT_DIR, "logs", "warmer.log"),
}

# Used by /api/logs/stream/<log_name>
LOG_FILES = {
    "warmer": os.path.join(SCRIPT_DIR, "logs", "warmer.log"),
    "flask":  os.path.join(SCRIPT_DIR, "logs", "flask.log"),
}


def _deepcopy_default_config() -> dict:
    return json.loads(json.dumps(DEFAULT_UI_CONFIG))


def _load_ui_config_raw() -> dict:
    config = _deepcopy_default_config()
    config["nstbrowser_api_key"] = os.getenv("NSTBROWSER_API_KEY", "")
    env_profiles = os.getenv("PROFILE_IDS", "")
    if env_profiles:
        config["profile_ids"] = [p.strip() for p in env_profiles.split(",") if p.strip()]
    env_target = os.getenv("TARGET_SOCIAL_URL", "")
    if env_target:
        config["target_social_url"] = env_target
    env_hours = os.getenv("ACTIVE_HOURS_RANGE", "")
    if env_hours:
        parts = [p.strip() for p in env_hours.split(",")]
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            config["active_hours"] = [int(parts[0]), int(parts[1])]

    try:
        with open(UI_CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except FileNotFoundError:
        saved = {}
    except json.JSONDecodeError:
        saved = {}

    if isinstance(saved, dict):
        for key in ("nstbrowser_api_key", "profile_ids", "target_social_url",
                    "active_hours", "default_no_preflight"):
            if key in saved:
                config[key] = saved[key]
        weights = saved.get("action_weights")
        if isinstance(weights, dict):
            for key in ACTION_WEIGHT_FLAGS:
                if key in weights:
                    config["action_weights"][key] = weights[key]
    return config


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _public_config(config: dict) -> dict:
    public = {
        "profile_ids": config.get("profile_ids", []),
        "target_social_url": config.get("target_social_url", DEFAULT_UI_CONFIG["target_social_url"]),
        "active_hours": config.get("active_hours", [8, 23]),
        "default_no_preflight": bool(config.get("default_no_preflight", False)),
        "action_weights": {
            key: (config.get("action_weights") or {}).get(key)
            for key in ACTION_WEIGHT_FLAGS
        },
    }
    api_key = str(config.get("nstbrowser_api_key") or "")
    public["api_key_set"] = bool(api_key)
    public["api_key_masked"] = _mask_secret(api_key)
    return public


def _parse_profile_ids(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.replace("\n", ",").split(",") if v.strip()]
    return []


def _validate_ui_config(payload: dict, existing: dict) -> tuple[dict | None, str | None]:
    api_key = str(payload.get("nstbrowser_api_key") or "").strip()
    if not api_key:
        api_key = str(existing.get("nstbrowser_api_key") or "").strip()
    if not api_key:
        return None, "NstBrowser API key is required."

    profile_ids = _parse_profile_ids(payload.get("profile_ids"))
    if not profile_ids:
        return None, "At least one profile ID is required."

    target_url = str(payload.get("target_social_url") or "").strip()
    if not (target_url.startswith("http://") or target_url.startswith("https://")):
        return None, "Target URL must start with http:// or https://."

    hours = payload.get("active_hours")
    if not isinstance(hours, list) or len(hours) != 2:
        return None, "Active hours must contain start and end hours."
    try:
        start_hour, end_hour = int(hours[0]), int(hours[1])
    except (TypeError, ValueError):
        return None, "Active hours must be integers."
    if not (0 <= start_hour <= end_hour <= 23):
        return None, "Active hours must be within 0-23 and start must be <= end."

    raw_weights = payload.get("action_weights") or {}
    if not isinstance(raw_weights, dict):
        return None, "Action weights must be an object."
    weights = {}
    for key in ACTION_WEIGHT_FLAGS:
        raw = raw_weights.get(key)
        if raw in ("", None):
            weights[key] = None
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None, f"Weight '{key}' must be blank or a number."
        if not 0.0 <= value <= 1.0:
            return None, f"Weight '{key}' must be between 0.0 and 1.0."
        weights[key] = value

    config = {
        "nstbrowser_api_key": api_key,
        "profile_ids": profile_ids,
        "target_social_url": target_url,
        "active_hours": [start_hour, end_hour],
        "default_no_preflight": bool(payload.get("default_no_preflight", False)),
        "action_weights": weights,
    }
    return config, None


def _save_ui_config(config: dict) -> None:
    tmp = UI_CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, UI_CONFIG_FILE)


def _append_weight_flags(cmd: list[str], weights: dict | None = None) -> list[str]:
    weights = weights if weights is not None else _load_ui_config_raw().get("action_weights", {})
    for key, flag in ACTION_WEIGHT_FLAGS.items():
        value = (weights or {}).get(key)
        if value is not None:
            cmd += [flag, str(value)]
    return cmd


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

@app.route("/api/config", methods=["GET"])
def get_ui_config():
    return jsonify({"ok": True, "config": _public_config(_load_ui_config_raw())})


@app.route("/api/config", methods=["POST"])
def save_ui_config():
    existing = _load_ui_config_raw()
    config, error = _validate_ui_config(request.json or {}, existing)
    if error:
        return jsonify({"ok": False, "msg": error}), 400
    try:
        _save_ui_config(config)
    except Exception as exc:
        return jsonify({"ok": False, "msg": f"Could not save config: {exc}"}), 500
    return jsonify({"ok": True, "msg": "configuration saved", "config": _public_config(config)})

# ── Routes: process control ───────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    return jsonify({
        "daemon":        _status("daemon"),
        "session":       _status("session"),
        "test":          _status("test"),
        "timestamp":     datetime.now().strftime("%H:%M:%S"),
    })


@app.route("/api/daemon/start", methods=["POST"])
def daemon_start():
    config = _load_ui_config_raw()
    data = request.json or {}
    cmd = [PYTHON_EXE, MAIN_SCRIPT, "--daemon"]
    if data.get("no_preflight", config.get("default_no_preflight", False)):
        cmd.append("--no-preflight")
    _append_weight_flags(cmd, config.get("action_weights"))
    return jsonify(_launch("daemon", cmd))


@app.route("/api/daemon/stop", methods=["POST"])
def daemon_stop():
    return jsonify(_stop("daemon"))


@app.route("/api/session/start", methods=["POST"])
def session_start():
    config = _load_ui_config_raw()
    data = request.json or {}
    profile = data.get("profile_id", "")
    cmd = [PYTHON_EXE, MAIN_SCRIPT]
    if profile:
        cmd += ["--profile-id", profile]
    if data.get("no_preflight", config.get("default_no_preflight", False)):
        cmd.append("--no-preflight")
    _append_weight_flags(cmd, config.get("action_weights"))
    return jsonify(_launch("session", cmd))


@app.route("/api/test/start", methods=["POST"])
def test_start():
    data = request.json or {}
    action  = data.get("action", "like")
    profile = data.get("profile_id", "")
    cmd = [PYTHON_EXE, MAIN_SCRIPT, "--test-actions"]
    if action and action != "all":
        cmd.append(action)
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
            elif "TEST" in msg or "test-actions" in msg:
                label = "Test Actions"
            elif "NstBrowser Warmer" in msg:
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

import csv
import json
import os
import threading
import uuid
from datetime import datetime


_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.environ.get(
    "SESSION_REPORTS_DIR",
    os.environ.get("WARMER_REPORTS_DIR", os.path.join(_SCRIPT_DIR, "reports")),
)

REPORT_SCHEMAS = {
    "runs": [
        "run_id",
        "process_type",
        "started_at",
        "ended_at",
        "status",
        "profiles_requested",
        "target_url",
        "skip_preflight",
        "weight_overrides_json",
    ],
    "sessions": [
        "run_id",
        "session_id",
        "profile_id",
        "mode",
        "started_at",
        "ended_at",
        "planned_duration_sec",
        "actual_duration_sec",
        "status",
        "account_days",
        "actions_dispatched",
        "likes",
        "comments",
        "follows",
        "posts",
        "passive",
        "reads",
        "profile_visits",
        "searches",
        "warnings_count",
        "errors_count",
        "end_url",
    ],
    "actions": [
        "run_id",
        "session_id",
        "profile_id",
        "sequence",
        "action",
        "started_at",
        "ended_at",
        "duration_sec",
        "status",
        "note",
        "elapsed_session_sec",
        "elapsed_frac",
        "consecutive_same",
        "account_days",
        "url_before",
        "url_after",
    ],
    "diagnostics": [
        "run_id",
        "profile_id",
        "action",
        "started_at",
        "ended_at",
        "duration_ms",
        "status",
        "note",
        "health_ok",
        "cursor_drift",
    ],
}

_write_lock = threading.Lock()


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def make_run_id(process_type: str = "run") -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{process_type}-{stamp}-{uuid.uuid4().hex[:8]}"


def make_session_id(profile_id: str) -> str:
    safe_profile = (profile_id or "manual").replace(" ", "_")[:16]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{safe_profile}-{stamp}-{uuid.uuid4().hex[:6]}"


def encode_json(value) -> str:
    if value in (None, "", {}, []):
        return ""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def report_path(report_name: str) -> str:
    _validate_report_name(report_name)
    return os.path.join(REPORTS_DIR, f"{report_name}.csv")


def _validate_report_name(report_name: str) -> None:
    if report_name not in REPORT_SCHEMAS:
        raise ValueError(f"Unknown report: {report_name}")


def _coerce_cell(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def append_row(report_name: str, row: dict) -> None:
    _validate_report_name(report_name)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = report_path(report_name)
    fields = REPORT_SCHEMAS[report_name]
    normalized = {field: _coerce_cell(row.get(field, "")) for field in fields}
    with _write_lock:
        needs_header = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            if needs_header:
                writer.writeheader()
            writer.writerow(normalized)


def append_run(row: dict) -> None:
    append_row("runs", row)


def append_session(row: dict) -> None:
    append_row("sessions", row)


def append_action(row: dict) -> None:
    append_row("actions", row)


def append_diagnostic(row: dict) -> None:
    append_row("diagnostics", row)


def read_rows(report_name: str, limit: int = 100, filters: dict | None = None) -> list[dict]:
    path = report_path(report_name)
    if not os.path.exists(path):
        return []
    filters = {
        key: str(value)
        for key, value in (filters or {}).items()
        if value not in (None, "")
    }
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if filters:
        rows = [
            row for row in rows
            if all(str(row.get(key, "")) == value for key, value in filters.items())
        ]
    if limit and limit > 0:
        rows = rows[-limit:]
    rows.reverse()
    return rows

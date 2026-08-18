import json
import os
import threading
from typing import Optional

_LOCK = threading.Lock()
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DATA_PATH = os.path.join(_DATA_DIR, "users.json")
_ROSTER_PATH = os.path.join(_DATA_DIR, "roster.json")


def _ensure_file(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({}, f)


def _read_json(path: str) -> dict:
    _ensure_file(path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, data: dict) -> None:
    _ensure_file(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _read_all() -> dict:
    return _read_json(_DATA_PATH)


def _write_all(data: dict) -> None:
    _write_json(_DATA_PATH, data)


def get_user(user_id: str) -> Optional[dict]:
    with _LOCK:
        return _read_all().get(user_id)


def set_name(user_id: str, name: str) -> None:
    with _LOCK:
        data = _read_all()
        record = data.get(user_id, {})
        record["name"] = name
        data[user_id] = record
        _write_all(data)


def set_shifts(user_id: str, shifts: list[dict], off_dates: list[str]) -> None:
    with _LOCK:
        data = _read_all()
        record = data.get(user_id, {})
        record["shifts"] = sorted(shifts, key=lambda s: s["date"])
        record["off_dates"] = sorted(set(off_dates))
        data[user_id] = record
        _write_all(data)


def mark_shift_start_alert_sent(user_id: str, date_str: str) -> None:
    with _LOCK:
        data = _read_all()
        record = data.get(user_id, {})
        record["last_shift_start_alert_date"] = date_str
        data[user_id] = record
        _write_all(data)


def all_users() -> dict:
    with _LOCK:
        return _read_all()


def set_roster(roster: dict) -> None:
    with _LOCK:
        _write_json(_ROSTER_PATH, roster)


def get_roster() -> dict:
    with _LOCK:
        return _read_json(_ROSTER_PATH)

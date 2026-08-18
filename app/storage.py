import json
import os
import threading
from typing import Optional

_LOCK = threading.Lock()
_DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "users.json")


def _ensure_file() -> None:
    os.makedirs(os.path.dirname(_DATA_PATH), exist_ok=True)
    if not os.path.exists(_DATA_PATH):
        with open(_DATA_PATH, "w", encoding="utf-8") as f:
            json.dump({}, f)


def _read_all() -> dict:
    _ensure_file()
    with open(_DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_all(data: dict) -> None:
    _ensure_file()
    with open(_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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


def set_shifts(user_id: str, shift_dates: list[str]) -> None:
    with _LOCK:
        data = _read_all()
        record = data.get(user_id, {})
        record["shifts"] = sorted(set(shift_dates))
        data[user_id] = record
        _write_all(data)


def all_users() -> dict:
    with _LOCK:
        return _read_all()

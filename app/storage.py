import datetime as dt
import json
import os
import threading
from typing import Optional
from zoneinfo import ZoneInfo

_LOCK = threading.Lock()
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DATA_PATH = os.path.join(_DATA_DIR, "users.json")
_ROSTER_PATH = os.path.join(_DATA_DIR, "roster.json")
_JST = ZoneInfo("Asia/Tokyo")


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


def _today_str() -> str:
    return dt.datetime.now(_JST).strftime("%Y-%m-%d")


def _merge_shift_data(
    existing_shifts: list[dict], existing_off: list[str], new_shifts: list[dict], new_off: list[str]
) -> tuple[list[dict], list[str]]:
    """新しいシフト表に載っている日付だけを反映し、載っていない日付(まだ来ていない
    別の月の予定など)は既存データを保持する。今日より前の日付は自動的に取り除く。"""
    today = _today_str()

    shift_map = {s["date"]: s for s in existing_shifts}
    off_set = set(existing_off)

    new_shift_dates = {s["date"] for s in new_shifts}
    for s in new_shifts:
        shift_map[s["date"]] = s
    for d in new_off:
        off_set.add(d)
        shift_map.pop(d, None)
    off_set -= new_shift_dates

    shift_map = {d: s for d, s in shift_map.items() if d >= today}
    off_set = {d for d in off_set if d >= today}

    return sorted(shift_map.values(), key=lambda s: s["date"]), sorted(off_set)


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
        merged_shifts, merged_off = _merge_shift_data(
            record.get("shifts", []), record.get("off_dates", []), shifts, off_dates
        )
        record["shifts"] = merged_shifts
        record["off_dates"] = merged_off
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
    """名簿を更新する。新しいファイルに載っている日付だけを反映し、
    載っていない日付や、今回のファイルに登場しなかった人の既存データは保持する
    (今日より前の日付は自動的に取り除く)。"""
    with _LOCK:
        existing = _read_json(_ROSTER_PATH)
        merged: dict[str, dict] = {}

        for name in set(existing) | set(roster):
            old_record = existing.get(name, {})
            new_record = roster.get(name, {})
            merged_shifts, merged_off = _merge_shift_data(
                old_record.get("shifts", []),
                old_record.get("off_dates", []),
                new_record.get("shifts", []),
                new_record.get("off_dates", []),
            )
            if merged_shifts or merged_off:
                merged[name] = {"shifts": merged_shifts, "off_dates": merged_off}

        _write_json(_ROSTER_PATH, merged)


def get_roster() -> dict:
    with _LOCK:
        return _read_json(_ROSTER_PATH)


def prune_past_dates() -> None:
    """全利用者・名簿から、今日より前の日付を削除する。日付が変わるタイミングで
    専用のcronジョブから呼び出す想定(今日のデータは`>= today`の判定により保持される)。"""
    with _LOCK:
        data = _read_all()
        for record in data.values():
            merged_shifts, merged_off = _merge_shift_data(
                record.get("shifts", []), record.get("off_dates", []), [], []
            )
            record["shifts"] = merged_shifts
            record["off_dates"] = merged_off
        _write_all(data)

        roster = _read_json(_ROSTER_PATH)
        merged_roster: dict[str, dict] = {}
        for name, record in roster.items():
            merged_shifts, merged_off = _merge_shift_data(
                record.get("shifts", []), record.get("off_dates", []), [], []
            )
            if merged_shifts or merged_off:
                merged_roster[name] = {"shifts": merged_shifts, "off_dates": merged_off}
        _write_json(_ROSTER_PATH, merged_roster)

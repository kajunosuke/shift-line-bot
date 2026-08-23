import datetime as dt
import json
import os
import threading
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

_LOCK = threading.Lock()
_JST = ZoneInfo("Asia/Tokyo")

_REDIS_URL = os.environ["UPSTASH_REDIS_REST_URL"].rstrip("/")
_REDIS_TOKEN = os.environ["UPSTASH_REDIS_REST_TOKEN"]
_USERS_KEY = "shift_bot:users"
_ROSTER_KEY = "shift_bot:roster"


def _read_json(key: str) -> dict:
    response = httpx.get(
        f"{_REDIS_URL}/get/{key}",
        headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
        timeout=10,
    )
    response.raise_for_status()
    result = response.json().get("result")
    if not result:
        return {}
    return json.loads(result)


def _write_json(key: str, data: dict) -> None:
    response = httpx.post(
        f"{_REDIS_URL}/set/{key}",
        headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
        content=json.dumps(data, ensure_ascii=False),
        timeout=10,
    )
    response.raise_for_status()


def _read_all() -> dict:
    return _read_json(_USERS_KEY)


def _write_all(data: dict) -> None:
    _write_json(_USERS_KEY, data)


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
    (今日より前の日付は自動的に取り除く)。並び順は、既存の並びをそのまま維持し、
    新しく現れた人だけを(表に出てきた順で)末尾に追加する。"""
    with _LOCK:
        existing = _read_json(_ROSTER_KEY)
        merged: dict[str, dict] = {}

        ordered_names = list(existing.keys())
        for name in roster.keys():
            if name not in existing:
                ordered_names.append(name)

        for name in ordered_names:
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

        _write_json(_ROSTER_KEY, merged)


def get_roster() -> dict:
    with _LOCK:
        return _read_json(_ROSTER_KEY)


def update_roster_entry(name: str, shifts: list[dict], off_dates: list[str]) -> None:
    """名簿のうち1人分だけをマージ更新する(シフト変更コマンド用)。"""
    with _LOCK:
        roster = _read_json(_ROSTER_KEY)
        old_record = roster.get(name, {})
        merged_shifts, merged_off = _merge_shift_data(
            old_record.get("shifts", []), old_record.get("off_dates", []), shifts, off_dates
        )
        if merged_shifts or merged_off:
            roster[name] = {"shifts": merged_shifts, "off_dates": merged_off}
        elif name in roster:
            del roster[name]
        _write_json(_ROSTER_KEY, roster)


def set_pending_edit(user_id: str, state: Optional[dict]) -> None:
    with _LOCK:
        data = _read_all()
        record = data.get(user_id, {})
        if state is None:
            record.pop("pending_edit", None)
        else:
            record["pending_edit"] = state
        data[user_id] = record
        _write_all(data)


def get_pending_edit(user_id: str) -> Optional[dict]:
    with _LOCK:
        return (_read_all().get(user_id) or {}).get("pending_edit")


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

        roster = _read_json(_ROSTER_KEY)
        merged_roster: dict[str, dict] = {}
        for name, record in roster.items():
            merged_shifts, merged_off = _merge_shift_data(
                record.get("shifts", []), record.get("off_dates", []), [], []
            )
            if merged_shifts or merged_off:
                merged_roster[name] = {"shifts": merged_shifts, "off_dates": merged_off}
        _write_json(_ROSTER_KEY, merged_roster)

import datetime as dt
import re
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import openpyxl

_JST = ZoneInfo("Asia/Tokyo")

_OFF_MARKERS = {"", "○", "休", "-", "ー", "off", "OFF", "×", "公休", "希望休", "有給", "欠勤"}
_YEAR_MONTH_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")
_MAX_ROWS = 400
_MAX_HEADER_SEARCH_ROWS = 50
_MIN_STRING_CELLS = 3


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dt.datetime, dt.date)):
        return value.strftime("%Y-%m-%d")
    return str(value).strip()


def _looks_numeric(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", "", name)


def _names_match(table_name: str, user_name: str) -> bool:
    a = _normalize_name(table_name)
    b = _normalize_name(user_name)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _merge_range_at(ws, row: int, col: int):
    for merged_range in ws.merged_cells.ranges:
        if merged_range.min_row <= row <= merged_range.max_row and merged_range.min_col <= col <= merged_range.max_col:
            return merged_range
    return None


def _find_year_month(ws, today: dt.date) -> tuple[int, int]:
    max_row = min(ws.max_row or 0, 30)
    max_col = min(ws.max_column or 0, 20)
    for row in ws.iter_rows(min_row=1, max_row=max_row, max_col=max_col):
        for cell in row:
            match = _YEAR_MONTH_RE.search(_cell_text(cell.value))
            if match:
                return int(match.group(1)), int(match.group(2))
    return today.year, today.month


def _find_day_header(ws, year_month_hint: tuple[int, int]) -> dict[dt.date, int] | None:
    """月間シフト表のヘッダー行(日付が横に並ぶ行)を探す。
    セルが実際の日付(datetime)の場合と、1〜31の素の数値の場合の両方に対応する。"""
    max_row = min(ws.max_row or 0, _MAX_HEADER_SEARCH_ROWS)
    for row_idx in range(1, max_row + 1):
        cells = ws[row_idx]

        date_to_col: dict[dt.date, int] = {}
        prev_date = None
        date_ordered_ok = True
        for cell in cells:
            value = cell.value
            if isinstance(value, dt.datetime):
                date_value = value.date()
            elif isinstance(value, dt.date):
                date_value = value
            else:
                continue
            if prev_date is not None and (date_value - prev_date).days != 1:
                date_ordered_ok = False
            date_to_col[date_value] = cell.column
            prev_date = date_value
        if date_ordered_ok and len(date_to_col) >= 28:
            return date_to_col

        day_to_col: dict[int, int] = {}
        prev_day = 0
        day_ordered_ok = True
        for cell in cells:
            value = cell.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if float(value).is_integer() and 1 <= int(value) <= 31:
                day = int(value)
                if day <= prev_day:
                    day_ordered_ok = False
                day_to_col[day] = cell.column
                prev_day = day
        if day_ordered_ok and len(day_to_col) >= 28 and 1 in day_to_col:
            year, month = year_month_hint
            converted: dict[dt.date, int] = {}
            for day, col in day_to_col.items():
                try:
                    converted[dt.date(year, month, day)] = col
                except ValueError:
                    continue
            if converted:
                return converted

    return None


def _find_name_left_of(ws, row_idx: int, first_data_col: int) -> str | None:
    """データ列より左側から氏名を探す。結合セル(複数行にまたがる)のテキストを優先し、
    社員番号のような数値だけのセルは氏名の候補から除外する。"""
    merged_candidates: list[tuple[int, str]] = []
    plain_candidates: list[tuple[int, str]] = []
    seen_ranges = set()

    for col in range(first_data_col - 1, 0, -1):
        merged_range = _merge_range_at(ws, row_idx, col)
        if merged_range is not None:
            key = (merged_range.min_row, merged_range.min_col, merged_range.max_row, merged_range.max_col)
            if key in seen_ranges:
                continue
            seen_ranges.add(key)
            value = ws.cell(row=merged_range.min_row, column=merged_range.min_col).value
            text = _cell_text(value)
            if not text or _looks_numeric(text):
                continue
            height = merged_range.max_row - merged_range.min_row + 1
            if height >= 2:
                merged_candidates.append((col, text))
            else:
                plain_candidates.append((col, text))
        else:
            text = _cell_text(ws.cell(row=row_idx, column=col).value)
            if text and not _looks_numeric(text):
                plain_candidates.append((col, text))

    if merged_candidates:
        merged_candidates.sort(key=lambda c: -c[0])
        return merged_candidates[0][1]
    if plain_candidates:
        plain_candidates.sort(key=lambda c: -c[0])
        return plain_candidates[0][1]
    return None


def _extract_shifts_for_name(ws, date_to_col: dict[dt.date, int], user_name: str):
    """各行を調べ、日付列に文字(シフト記号)が並ぶ行だけを氏名の手がかりとして使う。
    出退勤時刻のように数値(時刻シリアル値)しか入らない行は自然に除外される。"""
    first_data_col = min(date_to_col.values())
    max_row = min(ws.max_row or 0, _MAX_ROWS)

    for row_idx in range(1, max_row + 1):
        string_cells = 0
        for col in date_to_col.values():
            value = ws.cell(row=row_idx, column=col).value
            if isinstance(value, str) and value.strip():
                string_cells += 1
        if string_cells < _MIN_STRING_CELLS:
            continue

        name = _find_name_left_of(ws, row_idx, first_data_col)
        if not name or not _names_match(name, user_name):
            continue

        shift_dates = set()
        for date_value, col in date_to_col.items():
            text = _cell_text(ws.cell(row=row_idx, column=col).value)
            if text and text not in _OFF_MARKERS:
                shift_dates.add(date_value)
        return name, shift_dates

    return None, set()


def extract_shift_dates_from_excel(file_bytes: bytes, user_name: str) -> dict:
    workbook = openpyxl.load_workbook(BytesIO(file_bytes), data_only=True)
    today = datetime.now(_JST).date()
    any_header_found = False

    for ws in workbook.worksheets:
        year_month_hint = _find_year_month(ws, today)
        date_to_col = _find_day_header(ws, year_month_hint)
        if not date_to_col:
            continue
        any_header_found = True

        matched_name, shift_dates = _extract_shifts_for_name(ws, date_to_col, user_name)
        if matched_name:
            dates = sorted(d.isoformat() for d in shift_dates)
            return {"shift_dates": dates, "matched_name_in_table": matched_name, "note": ""}

    if any_header_found:
        note = "日付が並んだ表は見つかりましたが、登録名と一致する行が見つかりませんでした。登録名がシフト表内の表記と一致しているか確認してください。"
    else:
        note = "対応している表の形式(日付が横に並んだヘッダー行を含む月間シフト表)が見つかりませんでした。"

    return {"shift_dates": [], "matched_name_in_table": None, "note": note}

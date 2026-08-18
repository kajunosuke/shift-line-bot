import datetime as dt
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import openpyxl

from app.llm_common import client, extract_json

_JST = ZoneInfo("Asia/Tokyo")

_MAX_ROWS = 300
_MAX_COLS = 80

_PROMPT_TEMPLATE = """\
これはExcelの勤務シフト表を、シートごとにタブ区切りのテキストとして書き出したものです。この表の中から、名前が「{name}」の人が出勤する日をすべて抽出してください。

ルール:
- 表の形式は月間カレンダー形式、名前×日付のマトリクス形式など様々な可能性があります。タブ区切りのセル配置から表の構造をよく観察してから判断してください。
- 「{name}」に完全一致または表記ゆれ(姓のみ、名のみ、ひらがな/カタカナ違いなど)で近い名前があれば、その人の行/列/セルを採用してください。
- 休み、公休、欠勤などを示すマークがあるセルは出勤日に含めないでください。
- 出勤日と判断できるのは、名前が明記されたシフト、勤務時間の記載、出勤を示す記号(◯など、表内の凡例に従う)があるセルです。
- 日付が年を含まない形式(例: 8/1、8月1日)の場合、基準日 {today} を参考に妥当な年を推測してください。
- 該当する日付が1つも見つからない場合は空配列を返してください。

--- シフト表データ ---
{table}
--- ここまで ---

出力は以下のJSON形式のみを返してください。説明文やコードブロックの記法は一切不要です。
{{"shift_dates": ["YYYY-MM-DD", "YYYY-MM-DD", ...], "matched_name_in_table": "表内で実際に一致した表記(見つからない場合はnull)", "note": "判断に迷った点があれば短く記載(なければ空文字)"}}
"""


def _format_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dt.datetime, dt.date)):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _sheet_to_text(ws) -> str:
    lines = []
    max_row = min(ws.max_row or 0, _MAX_ROWS)
    max_col = min(ws.max_column or 0, _MAX_COLS)
    for row in ws.iter_rows(min_row=1, max_row=max_row, max_col=max_col):
        cells = [_format_cell(cell.value) for cell in row]
        if any(c.strip() for c in cells):
            lines.append("\t".join(cells))
    return "\n".join(lines)


def _workbook_to_text(file_bytes: bytes) -> str:
    workbook = openpyxl.load_workbook(BytesIO(file_bytes), data_only=True)
    sheets_text = []
    for ws in workbook.worksheets:
        text = _sheet_to_text(ws)
        if text.strip():
            sheets_text.append(f"[シート: {ws.title}]\n{text}")
    return "\n\n".join(sheets_text)


def extract_shift_dates_from_excel(file_bytes: bytes, user_name: str) -> dict:
    today = datetime.now(_JST).strftime("%Y-%m-%d")
    table_text = _workbook_to_text(file_bytes)
    prompt = _PROMPT_TEMPLATE.format(name=user_name, today=today, table=table_text)

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    text = "".join(block.text for block in response.content if block.type == "text")
    return extract_json(text)

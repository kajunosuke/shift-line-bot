import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessageAction,
    MessagingApi,
    MessagingApiBlob,
    PushMessageRequest,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    TextMessage as LineTextMessage,
)
from linebot.v3.webhooks import (
    FileMessageContent,
    FollowEvent,
    ImageMessageContent,
    MessageEvent,
    TextMessageContent,
)

from app import storage
from app.excel_extract import extract_all_shifts_from_excel, extract_shift_dates_from_excel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shiftbot")

_JST = ZoneInfo("Asia/Tokyo")

CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
REMINDER_TRIGGER_TOKEN = os.environ["REMINDER_TRIGGER_TOKEN"]

_config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

app = FastAPI()

_ROLE_NAMES = {
    "和": "水物",
    "牛": "牛乳",
    "パ": "パン",
    "洋": "洋菓子・ヨーグルト",
    "ス": "スキャンチェック",
    "銘": "銘店",
    "中": "中番",
    "遅": "遅番",
    "研修": "研修",
    "飲": "飲料",
}


_ROLE_KEYS_BY_LENGTH = sorted(_ROLE_NAMES, key=len, reverse=True)

_ROLE_ORDER = ["ス", "洋", "パ", "牛", "飲", "和", "銘", "中", "遅", "研修"]


def _full_role_name(code: str) -> str:
    if code in _ROLE_NAMES:
        return _ROLE_NAMES[code]
    parts = []
    remaining = code
    while remaining:
        matched_key = next((k for k in _ROLE_KEYS_BY_LENGTH if remaining.startswith(k)), None)
        if matched_key is None:
            return code
        parts.append(_ROLE_NAMES[matched_key])
        remaining = remaining[len(matched_key):]
    if not parts:
        return code
    return "と".join(parts)


def _role_sort_key(role: str | None) -> int:
    """役割の並び順(ス→洋→パ→牛→飲→和→銘→中→遅→研修)でのソートキーを返す。
    組み合わせ記号(例:「ス洋」)は先頭に一致する役割の順位を使う。"""
    if not role:
        return len(_ROLE_ORDER)
    matched_key = next((k for k in _ROLE_KEYS_BY_LENGTH if role.startswith(k)), None)
    if matched_key is None or matched_key not in _ROLE_ORDER:
        return len(_ROLE_ORDER)
    return _ROLE_ORDER.index(matched_key)


_LABEL_TODAY_LIST = "今日出勤リスト"
_LABEL_AM_I_WORKING_TODAY = "今日の自分は出勤？"
_LABEL_TOMORROW_LIST = "明日出勤リスト"
_LABEL_AM_I_WORKING = "明日の自分は出勤？"


def _default_quick_reply() -> QuickReply:
    return QuickReply(
        items=[
            QuickReplyItem(action=MessageAction(label=_LABEL_TODAY_LIST, text=_LABEL_TODAY_LIST)),
            QuickReplyItem(action=MessageAction(label=_LABEL_AM_I_WORKING_TODAY, text=_LABEL_AM_I_WORKING_TODAY)),
            QuickReplyItem(action=MessageAction(label=_LABEL_TOMORROW_LIST, text=_LABEL_TOMORROW_LIST)),
            QuickReplyItem(action=MessageAction(label=_LABEL_AM_I_WORKING, text=_LABEL_AM_I_WORKING)),
        ]
    )


def _reply(reply_token: str, text: str, with_quick_reply: bool = True) -> None:
    quick_reply = _default_quick_reply() if with_quick_reply else None
    with ApiClient(_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[LineTextMessage(text=text, quick_reply=quick_reply)],
            )
        )


def _push(user_id: str, text: str) -> None:
    with ApiClient(_config) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(
                to=user_id,
                messages=[LineTextMessage(text=text)],
            )
        )


def _get_message_content(message_id: str) -> bytes:
    with ApiClient(_config) as api_client:
        return MessagingApiBlob(api_client).get_message_content(message_id)


def _format_shift_line(shift: dict) -> str:
    start = shift.get("start")
    end = shift.get("end")
    role = shift.get("role")
    line = f"・{shift['date']}"
    if start and end:
        line += f" {start}〜{end}"
    if role:
        line += f"(役割:{role})"
    return line


def _format_shift_details(shift: dict) -> str:
    """出勤・退勤時刻と役割(正式名称)を「(07:30〜19:00、役割:...)」の形式で返す。該当情報がなければ空文字。"""
    start = shift.get("start")
    end = shift.get("end")
    role = shift.get("role")
    details = []
    if start and end:
        details.append(f"{start}〜{end}")
    if role:
        full_role = _full_role_name(role)
        if shift.get("add_meiten"):
            full_role = f"銘店・{full_role}"
        details.append(f"役割:{full_role}")
    return f"({'、'.join(details)})" if details else ""


def _find_shift_for_date(shifts: list[dict], date_str: str) -> dict | None:
    return next((s for s in shifts if s.get("date") == date_str), None)


def _build_worker_list_message(date_str: str, roster: dict) -> str:
    rows = []
    for name, record in roster.items():
        match = _find_shift_for_date(record.get("shifts") or [], date_str)
        if match:
            rows.append((name, match))
    rows.sort(key=lambda r: _role_sort_key(r[1].get("role")))
    lines = [f"・{name}さん {_format_shift_details(match)}".rstrip() for name, match in rows]
    if lines:
        return f"{date_str}の出勤予定:\n" + "\n".join(lines)
    return f"{date_str}は出勤予定の人はいません。"


def _time_str_to_minutes(time_str: str | None) -> int | None:
    if not time_str:
        return None
    try:
        h, m = time_str.split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def _apply_extraction_result(event: MessageEvent, name: str, user_id: str, result: dict) -> None:
    shifts = result.get("shifts") or []
    off_dates = result.get("off_dates") or []
    storage.set_shifts(user_id, shifts, off_dates)

    if not shifts:
        note = result.get("note") or ""
        message = f"「{name}」さんの出勤日が見つかりませんでした。"
        if note:
            message += f"\n{note}"
        _reply(event.reply_token, message)
        return

    lines = "\n".join(_format_shift_line(s) for s in shifts)
    note = result.get("note") or ""
    message = f"「{name}」さんの出勤日を登録しました:\n{lines}"
    if note:
        message += f"\n\n(補足: {note})"
    message += "\n\n各出勤日の前日 13:00 にリマインドをお送りします。"
    _reply(event.reply_token, message)


@handler.add(FollowEvent)
def handle_follow(event: FollowEvent) -> None:
    _reply(
        event.reply_token,
        "友だち追加ありがとうございます!\n"
        "まず、シフト表に載っているあなたの表記(名前)を送ってください。\n"
        "例:「田中」「たなか」など、表と同じ表記でお願いします。\n"
        "その後、シフト表のExcelファイル(.xlsx)を送っていただくと出勤日を抽出します。",
    )


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event: MessageEvent) -> None:
    user_id = event.source.user_id
    text = event.message.text.strip()

    if text == _LABEL_TODAY_LIST:
        today = datetime.now(_JST).strftime("%Y-%m-%d")
        message = _build_worker_list_message(today, storage.get_roster())
        _reply(event.reply_token, message)
        return

    if text == _LABEL_AM_I_WORKING_TODAY:
        today = datetime.now(_JST).strftime("%Y-%m-%d")
        record = storage.get_user(user_id) or {}
        match = _find_shift_for_date(record.get("shifts") or [], today)
        if match:
            message = f"はい、今日({today})は出勤日です {_format_shift_details(match)}".rstrip()
        elif today in (record.get("off_dates") or []):
            message = f"今日({today})は休みです。"
        else:
            message = f"今日({today})の予定が登録されていません。"
        _reply(event.reply_token, message)
        return

    if text == _LABEL_TOMORROW_LIST:
        tomorrow = (datetime.now(_JST) + timedelta(days=1)).strftime("%Y-%m-%d")
        message = _build_worker_list_message(tomorrow, storage.get_roster())
        _reply(event.reply_token, message)
        return

    if text == _LABEL_AM_I_WORKING:
        tomorrow = (datetime.now(_JST) + timedelta(days=1)).strftime("%Y-%m-%d")
        record = storage.get_user(user_id) or {}
        match = _find_shift_for_date(record.get("shifts") or [], tomorrow)
        if match:
            message = f"はい、明日({tomorrow})は出勤日です {_format_shift_details(match)}".rstrip()
        elif tomorrow in (record.get("off_dates") or []):
            message = f"明日({tomorrow})は休みです。"
        else:
            message = f"明日({tomorrow})の予定が登録されていません。"
        _reply(event.reply_token, message)
        return

    if text.startswith("名前変更"):
        new_name = text.replace("名前変更", "", 1).strip()
        if not new_name:
            _reply(event.reply_token, "「名前変更 新しい名前」の形式で送ってください。")
            return
        storage.set_name(user_id, new_name)
        _reply(event.reply_token, f"登録名を「{new_name}」に変更しました。")
        return

    record = storage.get_user(user_id)
    if not record or not record.get("name"):
        storage.set_name(user_id, text)
        _reply(
            event.reply_token,
            f"「{text}」で登録しました。\n"
            "次に、シフト表のExcelファイル(.xlsx)を送ってください。あなたの出勤日を抽出します。\n"
            "(LINEでは「+」メニューの「ファイル」から送ってください)\n"
            "(名前を間違えた場合は「名前変更 正しい名前」と送ってください)",
        )
        return

    _reply(
        event.reply_token,
        f"現在の登録名は「{record['name']}」です。\n"
        "シフト表のExcelファイル(.xlsx)を送ると出勤日を抽出します。名前を変える場合は「名前変更 新しい名前」と送ってください。",
    )


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event: MessageEvent) -> None:
    _reply(
        event.reply_token,
        "写真からの読み取りには対応していません。シフト表のExcelファイル(.xlsx)を「+」メニューの「ファイル」から送ってください。",
    )


@handler.add(MessageEvent, message=FileMessageContent)
def handle_file(event: MessageEvent) -> None:
    user_id = event.source.user_id
    record = storage.get_user(user_id)

    if not record or not record.get("name"):
        _reply(
            event.reply_token,
            "先にあなたの名前を教えてください。シフト表に載っている表記をそのまま送ってください。",
        )
        return

    file_name = (event.message.file_name or "").lower()
    name = record["name"]

    if not file_name.endswith(".xlsx"):
        _reply(
            event.reply_token,
            "対応しているファイル形式は Excel (.xlsx) のみです。\n"
            "ファイルをExcelで開き、「名前を付けて保存」で「.xlsx」形式にしてから送ってください。",
        )
        return

    file_bytes = _get_message_content(event.message.id)
    try:
        result = extract_shift_dates_from_excel(file_bytes, name)
    except Exception:
        logger.exception("excel shift extraction failed")
        _reply(event.reply_token, "Excelファイルの解析に失敗しました。ファイルが壊れていないか確認して再度送ってください。")
        return

    logger.info("extraction result for %r: %r", name, result)

    try:
        roster = extract_all_shifts_from_excel(file_bytes)
        storage.set_roster(roster)
    except Exception:
        logger.exception("roster extraction failed")

    _apply_extraction_result(event, name, user_id, result)


@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return PlainTextResponse("OK")


@app.post("/internal/send-reminders")
async def send_reminders(request: Request):
    token = request.query_params.get("token")
    if token != REMINDER_TRIGGER_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    tomorrow = (datetime.now(_JST) + timedelta(days=1)).strftime("%Y-%m-%d")
    sent = []
    for user_id, record in storage.all_users().items():
        name = record.get("name", "")
        match = _find_shift_for_date(record.get("shifts") or [], tomorrow)
        if match:
            detail_part = _format_shift_details(match)
            _push(user_id, f"【リマインド】明日 {tomorrow} は出勤日です{detail_part}。{name}さん、忘れずに!")
            sent.append(user_id)
        elif tomorrow in (record.get("off_dates") or []):
            _push(user_id, f"【リマインド】明日 {tomorrow} は休みです。{name}さん、ゆっくり休んでください。")
            sent.append(user_id)

    return {"date_checked": tomorrow, "reminders_sent": len(sent)}


@app.post("/internal/send-shift-start-alerts")
async def send_shift_start_alerts(request: Request):
    token = request.query_params.get("token")
    if token != REMINDER_TRIGGER_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    now = datetime.now(_JST)
    today = now.strftime("%Y-%m-%d")
    now_minutes = now.hour * 60 + now.minute

    users = storage.all_users()
    message = None
    sent = []

    for user_id, record in users.items():
        if record.get("last_shift_start_alert_date") == today:
            continue
        match = _find_shift_for_date(record.get("shifts") or [], today)
        if not match:
            continue
        start_minutes = _time_str_to_minutes(match.get("start"))
        if start_minutes is None:
            continue
        if now_minutes < start_minutes - 10:
            continue

        if message is None:
            message = _build_worker_list_message(today, storage.get_roster())
        _push(user_id, f"まもなく出勤時刻です。\n{message}")
        storage.mark_shift_start_alert_sent(user_id, today)
        sent.append(user_id)

    return {"date_checked": today, "alerts_sent": len(sent)}


@app.get("/health")
async def health():
    return {"status": "ok"}

import logging
import os
import re
from datetime import date as dt_date
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    ConfirmTemplate,
    Configuration,
    DatetimePickerAction,
    MessageAction,
    MessagingApi,
    MessagingApiBlob,
    PushMessageRequest,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    TemplateMessage,
    TextMessage as LineTextMessage,
)
from linebot.v3.webhooks import (
    FileMessageContent,
    FollowEvent,
    ImageMessageContent,
    MessageEvent,
    PostbackEvent,
    TextMessageContent,
)

from app import storage
from app.cronjob_client import schedule_next_shift_alert
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


_WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


def _format_date_jp(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return f"{d.month}月{d.day}日({_WEEKDAY_JP[d.weekday()]})"


_LABEL_TODAY_LIST = "本日の出勤者"
_LABEL_AM_I_WORKING_TODAY = "今日は出勤？"
_LABEL_TOMORROW_LIST = "明日の出勤者"
_LABEL_AM_I_WORKING = "明日は出勤？"
_LABEL_THIS_MONTH = "今月の出勤"
_LABEL_THIS_MONTH_OFF = "今月の休日"
_LABEL_SHIFT_EDIT = "シフト変更"
_LABEL_CANCEL = "キャンセル"

_SHIFT_EDIT_DATE_RE = re.compile(r"^\d{1,2}$")
_SHIFT_EDIT_TIME_RE = re.compile(r"^\d{3,4}$")
_SHIFT_EDIT_DAY_POSTBACK_DATA = "shift_edit_day"


def _default_quick_reply() -> QuickReply:
    return QuickReply(
        items=[
            QuickReplyItem(action=MessageAction(label=_LABEL_TODAY_LIST, text=_LABEL_TODAY_LIST)),
            QuickReplyItem(action=MessageAction(label=_LABEL_TOMORROW_LIST, text=_LABEL_TOMORROW_LIST)),
            QuickReplyItem(action=MessageAction(label=_LABEL_AM_I_WORKING_TODAY, text=_LABEL_AM_I_WORKING_TODAY)),
            QuickReplyItem(action=MessageAction(label=_LABEL_AM_I_WORKING, text=_LABEL_AM_I_WORKING)),
            QuickReplyItem(action=MessageAction(label=_LABEL_THIS_MONTH, text=_LABEL_THIS_MONTH)),
            QuickReplyItem(action=MessageAction(label=_LABEL_THIS_MONTH_OFF, text=_LABEL_THIS_MONTH_OFF)),
            QuickReplyItem(action=MessageAction(label=_LABEL_SHIFT_EDIT, text=_LABEL_SHIFT_EDIT)),
        ]
    )


def _flow_quick_reply() -> QuickReply:
    return QuickReply(items=[QuickReplyItem(action=MessageAction(label=_LABEL_CANCEL, text=_LABEL_CANCEL))])


def _shift_edit_month_options() -> list[int]:
    """シフト変更で選べる月(当月・翌月)を返す。"""
    current_month = datetime.now(_JST).month
    next_month = current_month % 12 + 1
    return [current_month, next_month]


def _shift_edit_year_for_month(month: int) -> int:
    """指定した月が「当月より前」なら年をまたいでいる(例:12月→1月)とみなし、翌年を返す。"""
    today = datetime.now(_JST)
    return today.year + 1 if month < today.month else today.year


def _month_quick_reply() -> QuickReply:
    items = [
        QuickReplyItem(action=MessageAction(label=f"{m}月", text=str(m))) for m in _shift_edit_month_options()
    ]
    items.append(QuickReplyItem(action=MessageAction(label=_LABEL_CANCEL, text=_LABEL_CANCEL)))
    return QuickReply(items=items)


def _day_quick_reply(month: int) -> QuickReply:
    today = datetime.now(_JST).date()
    year = _shift_edit_year_for_month(month)
    next_month_first = dt_date(year + 1, 1, 1) if month == 12 else dt_date(year, month + 1, 1)
    last_day = (next_month_first - timedelta(days=1)).day
    # 当月を選んでいる場合は、今日より前の日を選べないようにする
    first_selectable = today if (year, month) == (today.year, today.month) else dt_date(year, month, 1)
    min_date = first_selectable.isoformat()
    max_date = f"{year:04d}-{month:02d}-{last_day:02d}"
    return QuickReply(
        items=[
            QuickReplyItem(
                action=DatetimePickerAction(
                    label="日付を選ぶ",
                    data=_SHIFT_EDIT_DAY_POSTBACK_DATA,
                    mode="date",
                    initial=min_date,
                    min=min_date,
                    max=max_date,
                )
            ),
            QuickReplyItem(action=MessageAction(label=_LABEL_CANCEL, text=_LABEL_CANCEL)),
        ]
    )


def _reply(
    reply_token: str, text: str, with_quick_reply: bool = True, quick_reply: QuickReply | None = None
) -> None:
    if quick_reply is None and with_quick_reply:
        quick_reply = _default_quick_reply()
    with ApiClient(_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[LineTextMessage(text=text, quick_reply=quick_reply)],
            )
        )


def _reply_confirm(reply_token: str, question: str) -> None:
    template = ConfirmTemplate(
        text=question,
        actions=[
            MessageAction(label="はい", text="はい"),
            MessageAction(label="いいえ", text="いいえ"),
        ],
    )
    with ApiClient(_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TemplateMessage(alt_text=question, template=template, quick_reply=_flow_quick_reply())],
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
    line = f"・{_format_date_jp(shift['date'])}"
    if start and end:
        line += f" {start}〜{end}"
    if role:
        line += f"(役割:{role})"
    return line


def _format_shift_time_only(shift: dict) -> str:
    start = shift.get("start")
    end = shift.get("end")
    line = f"・{_format_date_jp(shift['date'])}"
    if start and end:
        line += f" {start}〜{end}"
    return line


def _format_shift_details(shift: dict) -> str:
    """出勤・退勤時刻と役割(正式名称)を「(07:30〜19:00 役割:...)」の形式で返す。該当情報がなければ空文字。"""
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
    return f"({' '.join(details)})" if details else ""


def _find_shift_for_date(shifts: list[dict], date_str: str) -> dict | None:
    return next((s for s in shifts if s.get("date") == date_str), None)


def _build_worker_list_message(date_str: str, roster: dict) -> str:
    rows = []
    for name, record in roster.items():
        match = _find_shift_for_date(record.get("shifts") or [], date_str)
        if match:
            rows.append((name, match))
    rows.sort(key=lambda r: _role_sort_key(r[1].get("role")))
    lines = [f"・{name}さん {_format_shift_details(match).strip('()')}".rstrip() for name, match in rows]
    date_label = _format_date_jp(date_str)
    if lines:
        return f"{date_label}の出勤予定:\n" + "\n".join(lines)
    return f"{date_label}は出勤予定の人はいません。"


def _time_str_to_minutes(time_str: str | None) -> int | None:
    if not time_str:
        return None
    try:
        h, m = time_str.split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def _schedule_next_shift_alert_for_date(target_date: str) -> None:
    """指定した日付(YYYY-MM-DD)の登録者の中で最も早い出勤時刻を探し、
    その10分前にcron-job.org経由でsend-shift-start-alertsが1回だけ実行されるよう予約する。
    登録者が複数いる場合、正確な時刻に合わせられるのは最も早い1人分のみ。"""
    earliest_minutes = None
    for record in storage.all_users().values():
        match = _find_shift_for_date(record.get("shifts") or [], target_date)
        if not match:
            continue
        start_minutes = _time_str_to_minutes(match.get("start"))
        if start_minutes is None:
            continue
        if earliest_minutes is None or start_minutes < earliest_minutes:
            earliest_minutes = start_minutes

    if earliest_minutes is None:
        return

    target_day = datetime.strptime(target_date, "%Y-%m-%d").date()
    day_offset, minute_of_day = divmod(earliest_minutes - 10, 24 * 60)
    alarm_day = target_day + timedelta(days=day_offset)
    hour, minute = divmod(minute_of_day, 60)
    alarm_dt = datetime(alarm_day.year, alarm_day.month, alarm_day.day, hour, minute, tzinfo=_JST)
    schedule_next_shift_alert(alarm_dt)


def _find_user_id_by_name(name: str) -> str | None:
    for uid, record in storage.all_users().items():
        if record.get("name") == name:
            return uid
    return None


def _time_digits_to_hhmm(text: str) -> str | None:
    """"630"→"6:30"、"0630"→"06:30"、"1000"→"10:00" のように3〜4桁の数字を時刻に変換する。"""
    if not _SHIFT_EDIT_TIME_RE.match(text):
        return None
    if len(text) == 3:
        hour, minute = int(text[0]), int(text[1:3])
    else:
        hour, minute = int(text[0:2]), int(text[2:4])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _start_shift_edit(event: MessageEvent, user_id: str) -> None:
    roster = storage.get_roster()
    if not roster:
        _reply(event.reply_token, "名簿にまだ誰も登録されていません。先にシフト表のExcelファイルを送ってください。")
        return

    names = list(roster.keys())[:12]
    storage.set_pending_edit(user_id, {"step": "awaiting_target_name"})
    items = [QuickReplyItem(action=MessageAction(label=n[:20], text=n)) for n in names]
    items.append(QuickReplyItem(action=MessageAction(label=_LABEL_CANCEL, text=_LABEL_CANCEL)))
    _reply(event.reply_token, "誰のシフトを変更しますか?", quick_reply=QuickReply(items=items))


def _cancel_shift_edit(event: MessageEvent, user_id: str) -> None:
    storage.set_pending_edit(user_id, None)
    _reply(event.reply_token, "シフト変更をキャンセルしました。")


def _apply_shift_edit(user_id: str, state: dict, shifts: list[dict], off_dates: list[str]) -> None:
    name = state["target_name"]
    storage.update_roster_entry(name, shifts, off_dates)
    target_user_id = _find_user_id_by_name(name)
    if target_user_id:
        storage.set_shifts(target_user_id, shifts, off_dates)

    storage.set_pending_edit(user_id, None)

    try:
        tomorrow = (datetime.now(_JST) + timedelta(days=1)).strftime("%Y-%m-%d")
        _schedule_next_shift_alert_for_date(tomorrow)
    except Exception:
        logger.exception("failed to schedule next shift alert")


def _proceed_after_day_selected(event, user_id: str, state: dict, date_str: str) -> None:
    state["date"] = date_str
    state["step"] = "awaiting_confirm"
    storage.set_pending_edit(user_id, state)

    record = storage.get_roster().get(state["target_name"], {})
    current = _find_shift_for_date(record.get("shifts") or [], date_str)
    if current:
        current_desc = _format_shift_details(current).strip("()") or "出勤"
    elif date_str in (record.get("off_dates") or []):
        current_desc = "休日"
    else:
        current_desc = "登録なし"
    question = f"{state['target_name']}さんの{_format_date_jp(date_str)}のシフトは{current_desc}です。変更しますか?"
    _reply_confirm(event.reply_token, question)


def _handle_shift_edit_step(event: MessageEvent, user_id: str, text: str, state: dict) -> None:
    if text == _LABEL_CANCEL:
        _cancel_shift_edit(event, user_id)
        return

    step = state.get("step")

    if step == "awaiting_target_name":
        roster = storage.get_roster()
        if text not in roster:
            _reply(event.reply_token, "候補にない名前です。ボタンから選ぶか「キャンセル」と送ってください。")
            return
        state["target_name"] = text
        state["step"] = "awaiting_month"
        storage.set_pending_edit(user_id, state)
        _reply(event.reply_token, "何月ですか?", quick_reply=_month_quick_reply())
        return

    if step == "awaiting_month":
        if not _SHIFT_EDIT_DATE_RE.match(text) or int(text) not in _shift_edit_month_options():
            _reply(event.reply_token, "ボタンから月を選んでください。", quick_reply=_month_quick_reply())
            return
        month = int(text)
        state["month"] = month
        state["step"] = "awaiting_day"
        storage.set_pending_edit(user_id, state)
        _reply(event.reply_token, "日付を選んでください。", quick_reply=_day_quick_reply(month))
        return

    if step == "awaiting_day":
        # 日付は下のボタン(日付ピッカー)から選ぶ想定。テキストで来た場合は再度案内する。
        _reply(event.reply_token, "下のボタンから日付を選んでください。", quick_reply=_day_quick_reply(state["month"]))
        return

    if step == "awaiting_confirm":
        if text == "はい":
            state["step"] = "awaiting_off_choice"
            storage.set_pending_edit(user_id, state)
            _reply_confirm(event.reply_token, "休日ですか?")
        elif text == "いいえ":
            _cancel_shift_edit(event, user_id)
        else:
            _reply_confirm(event.reply_token, "「はい」か「いいえ」で答えてください。")
        return

    if step == "awaiting_off_choice":
        if text == "はい":
            date_str = state["date"]
            _apply_shift_edit(user_id, state, [], [date_str])
            _reply(event.reply_token, f"{state['target_name']}さんの{_format_date_jp(date_str)}を休日に変更しました。")
        elif text == "いいえ":
            state["step"] = "awaiting_start"
            storage.set_pending_edit(user_id, state)
            _reply(
                event.reply_token,
                "出勤時間を入力してください(例:630 → 6:30、1000 → 10:00)",
                quick_reply=_flow_quick_reply(),
            )
        else:
            _reply_confirm(event.reply_token, "「はい」か「いいえ」で答えてください。")
        return

    if step == "awaiting_start":
        hhmm = _time_digits_to_hhmm(text)
        if hhmm is None:
            _reply(event.reply_token, "3〜4桁の数字で送ってください(例:630、1000)。", quick_reply=_flow_quick_reply())
            return
        state["start"] = hhmm
        state["step"] = "awaiting_end"
        storage.set_pending_edit(user_id, state)
        _reply(event.reply_token, "退勤時間を入力してください(例:1800)", quick_reply=_flow_quick_reply())
        return

    if step == "awaiting_end":
        hhmm = _time_digits_to_hhmm(text)
        if hhmm is None:
            _reply(event.reply_token, "3〜4桁の数字で送ってください(例:1800)。", quick_reply=_flow_quick_reply())
            return
        state["end"] = hhmm
        state["step"] = "awaiting_role"
        storage.set_pending_edit(user_id, state)
        _reply(event.reply_token, "役割を入力してください(例:ス)", quick_reply=_flow_quick_reply())
        return

    if step == "awaiting_role":
        role = text.strip() or "遅"
        date_str = state["date"]
        shift_entry = {"date": date_str, "start": state["start"], "end": state["end"], "role": role, "add_meiten": False}
        _apply_shift_edit(user_id, state, [shift_entry], [])
        detail = _format_shift_details(shift_entry).strip("()")
        _reply(
            event.reply_token,
            f"{state['target_name']}さんの{_format_date_jp(date_str)}を出勤({detail})に変更しました。",
        )
        return

    storage.set_pending_edit(user_id, None)
    _reply(event.reply_token, "エラーが発生しました。もう一度「シフト変更」から始めてください。")


def _apply_extraction_result(event: MessageEvent, name: str, user_id: str, result: dict) -> None:
    shifts = result.get("shifts") or []
    off_dates = result.get("off_dates") or []
    storage.set_shifts(user_id, shifts, off_dates)

    note = result.get("note") or ""

    if not result.get("matched_name_in_table"):
        message = f"「{name}」さんの出勤日が見つかりませんでした。"
        if note:
            message += f"\n{note}"
        _reply(event.reply_token, message)
        return

    # 表示は保存後(過去日付を除いた・既存データとマージ済み)のデータを使う
    stored_shifts = (storage.get_user(user_id) or {}).get("shifts") or []
    if not stored_shifts:
        _reply(event.reply_token, f"「{name}」さんの出勤日が見つかりませんでした。")
        return

    lines = "\n".join(_format_shift_line(s) for s in stored_shifts)
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

    pending_edit = storage.get_pending_edit(user_id)
    if pending_edit is not None:
        _handle_shift_edit_step(event, user_id, text, pending_edit)
        return

    if text == _LABEL_SHIFT_EDIT:
        _start_shift_edit(event, user_id)
        return

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
            detail = _format_shift_details(match).strip("()")
            message = f"今日は出勤日です\n{detail}" if detail else "今日は出勤日です"
        elif today in (record.get("off_dates") or []):
            message = "今日は休みです。"
        else:
            message = "今日の予定が登録されていません。"
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
            detail = _format_shift_details(match).strip("()")
            message = f"明日は出勤日です\n{detail}" if detail else "明日は出勤日です"
        elif tomorrow in (record.get("off_dates") or []):
            message = "明日は休みです。"
        else:
            message = "明日の予定が登録されていません。"
        _reply(event.reply_token, message)
        return

    if text == _LABEL_THIS_MONTH:
        today = datetime.now(_JST)
        month_prefix = today.strftime("%Y-%m")
        record = storage.get_user(user_id) or {}
        month_shifts = sorted(
            (s for s in (record.get("shifts") or []) if s["date"].startswith(month_prefix)),
            key=lambda s: s["date"],
        )
        if month_shifts:
            lines = "\n".join(_format_shift_time_only(s) for s in month_shifts)
            message = f"{today.month}月の出勤日:\n{lines}"
        else:
            message = f"{today.month}月の出勤予定はありません。"
        _reply(event.reply_token, message)
        return

    if text == _LABEL_THIS_MONTH_OFF:
        today = datetime.now(_JST)
        month_prefix = today.strftime("%Y-%m")
        record = storage.get_user(user_id) or {}
        month_off_dates = sorted(d for d in (record.get("off_dates") or []) if d.startswith(month_prefix))
        if month_off_dates:
            lines = "\n".join(f"・{_format_date_jp(d)}" for d in month_off_dates)
            message = f"{today.month}月の休日:\n{lines}"
        else:
            message = f"{today.month}月の休日はありません。"
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


@handler.add(PostbackEvent)
def handle_postback(event: PostbackEvent) -> None:
    if event.postback.data != _SHIFT_EDIT_DAY_POSTBACK_DATA:
        return

    user_id = event.source.user_id
    state = storage.get_pending_edit(user_id)
    if not state or state.get("step") != "awaiting_day":
        return

    params = event.postback.params
    date_str = params.get("date") if isinstance(params, dict) else getattr(params, "date", None)
    if not date_str:
        _reply(event.reply_token, "日付の取得に失敗しました。もう一度お試しください。", quick_reply=_day_quick_reply(state["month"]))
        return

    picked = datetime.strptime(date_str, "%Y-%m-%d").date()
    if picked.month != state["month"] or picked < datetime.now(_JST).date():
        _reply(event.reply_token, "選べる範囲の日付を選んでください。", quick_reply=_day_quick_reply(state["month"]))
        return

    _proceed_after_day_selected(event, user_id, state, date_str)


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

    try:
        tomorrow = (datetime.now(_JST) + timedelta(days=1)).strftime("%Y-%m-%d")
        _schedule_next_shift_alert_for_date(tomorrow)
    except Exception:
        logger.exception("failed to schedule next shift alert")


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
    tomorrow_label = _format_date_jp(tomorrow)
    sent = []
    for user_id, record in storage.all_users().items():
        name = record.get("name", "")
        match = _find_shift_for_date(record.get("shifts") or [], tomorrow)
        if match:
            detail_part = _format_shift_details(match)
            _push(user_id, f"【リマインド】明日 {tomorrow_label} は出勤日です{detail_part}。{name}さん 忘れずに!")
            sent.append(user_id)
        elif tomorrow in (record.get("off_dates") or []):
            _push(user_id, f"【リマインド】明日 {tomorrow_label} は休みです。{name}さん ゆっくり休んでください。")
            sent.append(user_id)

    try:
        _schedule_next_shift_alert_for_date(tomorrow)
    except Exception:
        logger.exception("failed to schedule next shift alert")

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


@app.post("/internal/prune-past-dates")
async def prune_past_dates(request: Request):
    token = request.query_params.get("token")
    if token != REMINDER_TRIGGER_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    storage.prune_past_dates()
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}

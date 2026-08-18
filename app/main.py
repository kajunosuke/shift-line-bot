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
    MessagingApi,
    MessagingApiBlob,
    PushMessageRequest,
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
from app.excel_extract import extract_shift_dates_from_excel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shiftbot")

_JST = ZoneInfo("Asia/Tokyo")

CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
REMINDER_TRIGGER_TOKEN = os.environ["REMINDER_TRIGGER_TOKEN"]

_config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

app = FastAPI()


def _reply(reply_token: str, text: str) -> None:
    with ApiClient(_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[LineTextMessage(text=text)],
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


def _apply_extraction_result(event: MessageEvent, name: str, user_id: str, result: dict) -> None:
    shift_dates = sorted(set(result.get("shift_dates") or []))
    storage.set_shifts(user_id, shift_dates)

    if not shift_dates:
        note = result.get("note") or ""
        message = f"「{name}」さんの出勤日が見つかりませんでした。"
        if note:
            message += f"\n{note}"
        _reply(event.reply_token, message)
        return

    lines = "\n".join(f"・{d}" for d in shift_dates)
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
        shifts = record.get("shifts") or []
        if tomorrow in shifts:
            name = record.get("name", "")
            _push(user_id, f"【リマインド】明日 {tomorrow} は出勤日です。{name}さん、忘れずに!")
            sent.append(user_id)

    return {"date_checked": tomorrow, "reminders_sent": len(sent)}


@app.get("/health")
async def health():
    return {"status": "ok"}

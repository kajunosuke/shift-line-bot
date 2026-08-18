import base64
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

_JST = ZoneInfo("Asia/Tokyo")

_PROMPT_TEMPLATE = """\
これは勤務シフト表の写真です。この表の中から、名前が「{name}」の人が出勤する日をすべて抽出してください。

ルール:
- 表の形式は月間カレンダー形式、名前×日付のマトリクス形式など様々な可能性があります。表の構造をよく観察してから判断してください。
- 「{name}」に完全一致または表記ゆれ(姓のみ、名のみ、ひらがな/カタカナ違いなど)で近い名前があれば、その人の行/列/セルを採用してください。
- 休み、公休、欠勤などを示すマークがあるセルは出勤日に含めないでください。
- 出勤日と判断できるのは、名前が明記されたシフト、勤務時間の記載、出勤を示す記号(◯など、表内の凡例に従う)があるセルです。
- 表に年が明記されていない場合、基準日 {today} を参考に妥当な年・月を推測してください。
- 該当する日付が1つも見つからない場合は空配列を返してください。

出力は以下のJSON形式のみを返してください。説明文やコードブロックの記法は一切不要です。
{{"shift_dates": ["YYYY-MM-DD", "YYYY-MM-DD", ...], "matched_name_in_table": "表内で実際に一致した表記(見つからない場合はnull)", "note": "判断に迷った点があれば短く記載(なければ空文字)"}}
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"Claudeの応答からJSONを抽出できませんでした: {text!r}")
    return json.loads(match.group(0))


def extract_shift_dates(image_bytes: bytes, media_type: str, user_name: str) -> dict:
    today = datetime.now(_JST).strftime("%Y-%m-%d")
    prompt = _PROMPT_TEMPLATE.format(name=user_name, today=today)

    response = _client.messages.create(
        model="claude-sonnet-5",
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(image_bytes).decode("utf-8"),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )

    text = "".join(block.text for block in response.content if block.type == "text")
    return _extract_json(text)

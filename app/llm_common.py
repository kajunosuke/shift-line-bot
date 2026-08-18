import json
import os
import re

import anthropic

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def extract_json(text: str) -> dict:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"Claudeの応答からJSONを抽出できませんでした: {text!r}")
    return json.loads(match.group(0))

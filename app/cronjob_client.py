import logging
import os
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger("shiftbot")

_API_BASE = "https://api.cron-job.org"
_API_KEY = os.environ.get("CRONJOB_API_KEY")
_JOB_ID = os.environ.get("CRONJOB_ALERT_JOB_ID")


def schedule_next_shift_alert(target_dt: datetime) -> None:
    """cron-job.org上の出勤アラート用ジョブのスケジュールを書き換えて、
    target_dt(JSTのdatetime)にちょうど1回だけ実行されるようにする。
    CRONJOB_API_KEY / CRONJOB_ALERT_JOB_ID が未設定なら何もしない。"""
    if not _API_KEY or not _JOB_ID:
        return

    expires_at = int((target_dt + timedelta(minutes=5)).strftime("%Y%m%d%H%M%S"))
    body = {
        "job": {
            "enabled": True,
            "expiresAt": expires_at,
            "schedule": {
                "timezone": "Asia/Tokyo",
                "hours": [target_dt.hour],
                "minutes": [target_dt.minute],
                "mdays": [target_dt.day],
                "months": [target_dt.month],
                "wdays": [-1],
            },
        }
    }

    try:
        response = httpx.patch(
            f"{_API_BASE}/jobs/{_JOB_ID}",
            headers={
                "Authorization": f"Bearer {_API_KEY}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=10,
        )
        response.raise_for_status()
        logger.info("scheduled next shift alert for %s", target_dt.isoformat())
    except Exception:
        logger.exception("failed to update cron-job.org schedule")

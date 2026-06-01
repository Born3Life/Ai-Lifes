from __future__ import annotations

import logging
from os import getenv

from requests import Session

logger = logging.getLogger(__name__)

TG_API = "https://api.telegram.org/bot"


def publish(text: str) -> bool:
    token = getenv("NG_TG_TOKEN")
    if not token:
        logger.error("NG_TG_TOKEN not set")
        return False

    channel = getenv("NG_TG_CHANNEL", "@Ai_Lifes")

    session = Session()
    url = f"{TG_API}{token}/sendMessage"
    payload = {
        "chat_id": channel,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        resp = session.post(url, json=payload, timeout=30)
        data = resp.json()
        if not data.get("ok"):
            logger.error("TG API error: %s", data.get("description", ""))
            return False
        logger.info("published to TG: %s…", text[:60])
        return True
    except Exception:
        logger.exception("TG publish failed")
        return False
    finally:
        session.close()

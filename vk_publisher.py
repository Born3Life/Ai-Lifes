from __future__ import annotations

import logging
from os import getenv

from requests import Session

logger = logging.getLogger(__name__)

VK_API = "https://api.vk.com/method/wall.post"
VK_VERSION = "5.199"


def publish(text: str) -> bool:
    token = getenv("NG_VK_TOKEN")
    if not token:
        logger.error("NG_VK_TOKEN not set")
        return False

    raw = getenv("NG_VK_GROUP")
    if not raw:
        logger.error("NG_VK_GROUP not set")
        return False
    owner_id = -abs(int(raw))

    session = Session()
    payload = {
        "owner_id": owner_id,
        "message": text,
        "access_token": token,
        "v": VK_VERSION,
    }

    try:
        resp = session.post(VK_API, data=payload, timeout=30)
        data = resp.json()
        if "error" in data:
            logger.error("VK API error: %s", data["error"].get("error_msg", ""))
            return False
        logger.info("published to VK: %s…", text[:60])
        return True
    except Exception:
        logger.exception("VK publish failed")
        return False
    finally:
        session.close()

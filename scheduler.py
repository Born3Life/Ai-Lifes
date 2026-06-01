from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from content_generator import generate
from tg_publisher import publish as tg_publish
from vk_publisher import publish as vk_publish

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("scheduler")

STATE_PATH = Path(__file__).parent / "state.json"

# Время постов (МСК, UTC+3)
SCHEDULE: dict[str, int] = {
    "morning": 9,
    "afternoon": 14,
    "evening": 20,
}


def _now_hour() -> int:
    return datetime.utcnow().hour + 3  # MSK


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _should_post(slot: str, state: dict) -> bool:
    today = _today()
    posted = state.get(today, {})
    return not posted.get(slot, False)


def _mark_posted(slot: str, state: dict) -> None:
    today = _today()
    if today not in state:
        state[today] = {}
    state[today][slot] = True
    _save_state(state)


def _run_slot(slot: str) -> None:
    logger.info("generating %s post…", slot)
    text = generate(slot)
    if not text:
        logger.error("empty content for %s, skipping", slot)
        return

    logger.info("publishing %s…", text[:80])

    tg_ok = tg_publish(text)
    vk_ok = vk_publish(text)

    if tg_ok or vk_ok:
        _mark_posted(slot, _load_state())
        logger.info("%s done — TG:%s VK:%s", slot, tg_ok, vk_ok)
    else:
        logger.error("%s failed everywhere", slot)


def main() -> None:
    logger.info("neuro-guide scheduler started")

    while True:
        hour = _now_hour()
        state = _load_state()

        for slot, scheduled_hour in SCHEDULE.items():
            if hour == scheduled_hour and _should_post(slot, state):
                _run_slot(slot)

        time.sleep(60)


if __name__ == "__main__":
    main()

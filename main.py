#!/usr/bin/env python3
"""Entry point for Render: health server + scheduler loop."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from neuro import run_once as neuro_run
from content_generator import generate as cg_generate
from tg_publisher import publish as tg_publish
from vk_publisher import publish as vk_publish

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("main")

STATE_PATH = Path(__file__).parent / "state.json"

SCHEDULE: dict[str, int] = {
    "morning": 9,
    "afternoon": 14,
    "evening": 20,
}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:
        pass


def _health_server(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("health server on port %d", port)
    server.serve_forever()


def _now_hour() -> int:
    return datetime.utcnow().hour + 3


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
    logger.info("generating %s post via neuro.py", slot)
    neuro_run(channel="ai", slot=slot)
    _mark_posted(slot, _load_state())


def _scheduler_loop() -> None:
    logger.info("scheduler started")
    while True:
        hour = _now_hour()
        state = _load_state()
        for slot, scheduled_hour in SCHEDULE.items():
            if hour == scheduled_hour and _should_post(slot, state):
                _run_slot(slot)
        time.sleep(60)


def main() -> None:
    port_str = os.environ.get("PORT")
    if port_str:
        port = int(port_str)
        t = threading.Thread(target=_health_server, args=(port,), daemon=True)
        t.start()
    else:
        logger.warning("PORT not set — no health server")

    _scheduler_loop()


if __name__ == "__main__":
    main()

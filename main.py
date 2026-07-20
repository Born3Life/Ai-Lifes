#!/usr/bin/env python3
"""Entry point for Render: health server + scheduler loop for AI & Science channels."""
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("main")

STATE_PATH = Path(__file__).parent / "state.json"

SCHEDULE: dict[str, dict[str, int]] = {
    "ai": {
        "afternoon": 14,
        "evening": 20,
    },
    "science": {
        "afternoon": 14,
        "evening": 20,
    },
}


class TriggerHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.rstrip("/")
        if path in ("/health", ""):
            self._respond(200, "ok\n")
        elif path == "/trigger/ai":
            self._run_and_respond("ai", "afternoon")
        elif path == "/trigger/science":
            self._run_and_respond("science", "afternoon")
        else:
            self._respond(404, "not found\n")

    def _respond(self, code: int, body: str) -> None:
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body.encode())

    def _run_and_respond(self, channel: str, slot: str) -> None:
        logger.info("manual trigger %s/%s", channel, slot)
        try:
            neuro_run(channel=channel, slot=slot)
            self._respond(200, f"{channel}/{slot} done\n")
        except Exception as exc:
            logger.exception("trigger failed")
            self._respond(500, f"error: {exc}\n")

    def log_message(self, format: str, *args: object) -> None:
        pass


def _health_server(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), TriggerHandler)
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


def _should_post(channel: str, slot: str, state: dict) -> bool:
    today = _today()
    posted = state.get(today, {}).get(channel, {})
    return not posted.get(slot, False)


def _mark_posted(channel: str, slot: str, state: dict) -> None:
    today = _today()
    if today not in state:
        state[today] = {}
    if channel not in state[today]:
        state[today][channel] = {}
    state[today][channel][slot] = True
    _save_state(state)


def _scheduler_loop() -> None:
    logger.info("scheduler started")
    while True:
        hour = _now_hour()
        state = _load_state()
        for channel, slots in SCHEDULE.items():
            for slot, scheduled_hour in slots.items():
                if hour == scheduled_hour and _should_post(channel, slot, state):
                    logger.info("generating %s/%s post", channel, slot)
                    neuro_run(channel=channel, slot=slot)
                    _mark_posted(channel, slot, _load_state())
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

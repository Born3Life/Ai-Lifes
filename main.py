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
        "afternoon": 12,
        "evening": 20,
    },
    "science": {
        "morning": 10,
        "evening": 18,
    },
}


class TriggerHandler(BaseHTTPRequestHandler):
    VK_AUTH_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VK Auth</title></head><body><div id="app"></div>
<script>
(function(){
  var app=document.getElementById('app');
  var h=window.location.hash.substring(1);
  if(h){
    var p=new URLSearchParams(h),t=p.get('access_token');
    if(t){app.innerHTML='<h2>OK!</h2><textarea style="width:100%;height:60px">'+t+'</textarea><p>Скопируй токен</p>';return}
  }
  var q=new URLSearchParams(window.location.search),c=q.get('code');
  if(c){app.innerHTML='<h2>Code: '+c+'</h2><p>Скопируй код</p>';return}
  var s=document.createElement('script');
  s.src='https://unpkg.com/@vkid/sdk@latest/dist/sdk.js';
  s.onload=function(){
    var VKID=window.VKID;
    VKID.Config.init({appId:54686016,redirectUrl:window.location.href.split('?')[0].split('#')[0],
      state:'vk',scope:'photos,wall,groups,offline',responseType:'token',mode:VKID.ConfigAuth.FLOAT});
    new VKID.Auth().render({container:app,width:300})
  };
  document.head.appendChild(s)
})();
</script></body></html>"""

    def do_GET(self) -> None:
        path = self.path.rstrip("/")
        if path in ("/health", ""):
            self._respond(200, "ok\n")
        elif path == "/trigger/ai":
            self._respond(200, "started\n")
            t = threading.Thread(target=neuro_run, args=("ai", "afternoon"), daemon=True)
            t.start()
        elif path == "/trigger/science":
            self._respond(200, "started\n")
            t = threading.Thread(target=neuro_run, args=("science", "afternoon"), daemon=True)
            t.start()
        elif path == "/vk-auth":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(self.VK_AUTH_HTML.encode("utf-8"))
        else:
            self._respond(404, "not found\n")

    def _respond(self, code: int, body: str) -> None:
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body.encode())

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

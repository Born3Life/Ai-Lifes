#!/usr/bin/env python3
"""Entry point for Render: health server + scheduler loop for AI & Science channels."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
import urllib.parse
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
        "morning": 10,
        "evening": 20,
    },
    "science": {
        "day": 12,
        "evening": 22,
    },
}


class TriggerHandler(BaseHTTPRequestHandler):
    VK_AUTH_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VK Auth</title></head><body><div id="app"><p>Загрузка...</p></div>
<script>
(function(){
  var app=document.getElementById('app');
  var q=new URLSearchParams(window.location.search),code=q.get('code');
  if(code){
    app.innerHTML='<p>Обмениваю код на токен...</p>';
    fetch('/vk-token-exchange?code='+encodeURIComponent(code))
      .then(function(r){return r.text()})
      .then(function(t){
        if(t.startsWith('{')){var d=JSON.parse(t);app.innerHTML='<h2>Ошибка</h2><pre>'+d.error+'</pre>';return}
        app.innerHTML='<h2>OK!</h2><textarea style="width:100%;height:60px;font-size:14px">'+t+'</textarea><p>Скопируй токен и отправь мне</p>'
      });
    return
  }
  var base=window.location.href.split('?')[0].split('#')[0];
  var url='https://oauth.vk.com/authorize?client_id=54686016&redirect_uri='+encodeURIComponent(base)
    +'&scope=photos,wall,groups,offline&response_type=code&v=5.199&state=vk';
  app.innerHTML='<p style="margin-bottom:20px">Нажми кнопку, чтобы получить токен VK:</p>'
    +'<a href="'+url+'" style="display:inline-block;padding:14px 28px;background:#0077ff;color:#fff;'
    +'text-decoration:none;border-radius:8px;font-size:18px;font-weight:600">Разрешить VK</a>'
    +'<p style="margin-top:20px;color:#888;font-size:13px">Откроется страница VK &rarr; нажми Разрешить &rarr; вернёшься сюда с токеном</p>';
})();
</script></body></html>"""

    def do_GET(self) -> None:
        path = self.path.rstrip("/")
        if path in ("/health", ""):
            self._respond(200, "ok\n")
        elif path == "/trigger/ai":
            self._respond(200, "started\n")
            hour = _now_hour()
            slot = "morning" if hour < 15 else "evening"
            t = threading.Thread(target=neuro_run, args=("ai", slot), daemon=True)
            t.start()
        elif path == "/trigger/science":
            self._respond(200, "started\n")
            hour = _now_hour()
            slot = "day" if hour < 17 else "evening"
            t = threading.Thread(target=neuro_run, args=("science", slot), daemon=True)
            t.start()
        elif path == "/vk-auth":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(self.VK_AUTH_HTML.encode("utf-8"))
        elif path.startswith("/vk-token-exchange"):
            code = urllib.parse.parse_qs(path.split("?", 1)[1] if "?" in path else "").get("code", [""])[0]
            if not code:
                self._respond(400, "no code\n"); return
            secret = os.environ.get("VK_CLIENT_SECRET")
            if not secret:
                self._respond(500, "VK_CLIENT_SECRET not set\n"); return
            data = urllib.parse.urlencode({
                "client_id": 54686016, "client_secret": secret,
                "redirect_uri": "https://ai-lifes-bot.onrender.com/vk-auth",
                "code": code,
            }).encode()
            try:
                req = urllib.request.Request("https://oauth.vk.com/access_token", data=data, method="POST")
                with urllib.request.urlopen(req, timeout=15) as r:
                    resp = json.loads(r.read().decode())
                token = resp.get("access_token")
                if token:
                    self._respond(200, token + "\n")
                else:
                    self._respond(400, json.dumps(resp) + "\n")
            except Exception as ex:
                self._respond(500, str(ex) + "\n")
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
    return _now_msk().hour


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _now_msk() -> datetime:
    from datetime import timedelta
    return datetime.utcnow() + timedelta(hours=3)


def _today() -> str:
    return _now_msk().strftime("%Y-%m-%d")


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
    logger.info("scheduler started — times: AI(10:00, 20:00) Science(12:00, 22:00) MSK")
    while True:
        hour = _now_hour()
        state = _load_state()
        for channel, slots in SCHEDULE.items():
            for slot, scheduled_hour in slots.items():
                if hour == scheduled_hour and _should_post(channel, slot, state):
                    logger.info("SCHEDULED: %s/%s at %d:00 MSK", channel, slot, hour)
                    try:
                        neuro_run(channel=channel, slot=slot)
                        _mark_posted(channel, slot, _load_state())
                        logger.info("DONE: %s/%s posted", channel, slot)
                    except Exception as exc:
                        logger.error("FAILED: %s/%s — %s", channel, slot, exc)
        if hour % 6 == 0:
            logger.info("scheduler heartbeat — MSK hour %d", hour)
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

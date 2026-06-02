#!/usr/bin/env python3
"""neuro-guide: автопостинг в TG и VK — утро, день, вечер."""
from __future__ import annotations

import json
import logging
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("neuro")

CTX = ssl._create_unverified_context()
STATE = Path(__file__).parent / "state.json"
SCHEDULE = {"morning": 9, "afternoon": 14, "evening": 20}

def _env(key, default=None):
    val = os.environ.get(key)
    if val:
        return val
    p = Path(__file__).parent / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
    return default


def _post(url, data, headers=None, timeout=60):
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode()[:200]
        if e.code == 429:
            log.warning("429 (quota)")
        elif e.code == 503:
            log.info("503 (loading)")
        else:
            log.warning("HTTP %d: %s", e.code, body_txt)
    except Exception as e:
        log.debug("req error: %s", e)
    return None


FORMAT = (
    "Оформление поста (строго соблюдай):\n"
    "1. Первая строка — яркий заголовок-кликбейт (одна строка, с эмодзи).\n"
    "2. После заголовка — пустая строка (отступ).\n"
    "3. Основной текст разбивай на абзацы по 2-3 предложения. "
    "Между абзацами пустая строка. Используй эмодзи в начале абзацев.\n"
    "4. Если перечисляешь — используй маркированные списки с • или —, "
    "каждый пункт с новой строки.\n"
    "5. Тон — живой, энергичный, но без воды. "
    "Минимум прилагательных, максимум фактов и пользы.\n"
    "6. В конце — пустая строка, затем призыв подписаться (строка с эмодзи).\n"
    "7. НЕ используй хештеги. НЕ обращайся к читателю напрямую "
    "('вы', 'друзья', 'ребята'). Используй безличные конструкции.\n"
    "8. Длина: 500-1000 символов. Закончи мысль, не обрывай."
)

PROMPTS = {
    "morning": (
        "Ты пишешь пост для Telegram-канала «Нейросети для жизни». "
        "Это НЕ чат, а информационный канал."
        + FORMAT + "\n\n" +
        "Тема поста: выбери ОДНУ свежую и конкретную новость из мира ИИ "
        "(выход модели, обновление ChatGPT/Claude/Gemini, новый инструмент). "
        "Объясни суть новости и почему это важно обычному пользователю. "
        "Пример заголовка: "
        "«🔥 Google Gemini 2.5 Flash теперь бесплатен для всех»"
    ),
    "afternoon": (
        "Ты пишешь пост для Telegram-канала «Нейросети для жизни». "
        "Это НЕ чат, а информационный канал."
        + FORMAT + "\n\n" +
        "Тема поста: пошаговая инструкция «как сделать конкретную вещь "
        "с помощью нейросети». Выбери одну задачу (перевести видео, "
        "написать пост, создать картинку, сделать конспект). "
        "Распиши 3-5 шагов списком. "
        "Пример заголовка: "
        "«📱 Как перевести любое видео на русский за 2 минуты»"
    ),
    "evening": (
        "Ты пишешь пост для Telegram-канала «Нейросети для жизни». "
        "Это НЕ чат, а информационный канал."
        + FORMAT + "\n\n" +
        "Тема поста: подборка 3-4 конкретных и полезных AI-инструментов. "
        "По каждому: название, что делает, почему стоит попробовать. "
        "ИЛИ разбор одного интересного кейса использования нейросетей. "
        "Пример заголовка: "
        "«🧰 3 бесплатных нейросети, которые заменят дизайнера»"
    ),
}


def _fetch_news():
    queries = [
        "искусственный интеллект нейросети новости",
        "AI artificial intelligence news",
        "ChatGPT Gemini Claude новости",
    ]
    seen: set[str] = set()
    items: list[str] = []
    for q in queries:
        url = ("https://news.google.com/rss/search?"
               + urllib.parse.urlencode({"q": q, "hl": "ru", "gl": "RU"}))
        try:
            with urllib.request.urlopen(url, timeout=15, context=CTX) as r:
                root = ET.fromstring(r.read())
                for item in root.iter("item"):
                    title = item.findtext("title", "")
                    if not title or title in seen:
                        continue
                    seen.add(title)
                    items.append(title)
                    if len(items) >= 8:
                        return items
        except Exception as e:
            log.debug("news fetch error: %s", e)
    return items


def generate(slot):
    prompt = PROMPTS[slot]
    if slot in ("morning", "evening"):
        news = _fetch_news()
        if news:
            prompt += "\n\nВот свежие новости из мира ИИ (используй их как основу для поста):\n"
            prompt += "\n".join(f"— {n}" for n in news)
    key = os.environ.get("NG_GEMINI_KEY") or _env("NG_GEMINI_KEY")
    if key:
        for model in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-001"]:
            for _ in range(3):
                url = ("https://generativelanguage.googleapis.com/v1beta/"
                       "models/{}:generateContent?key={}").format(model, key)
                payload = {"contents": [{"parts": [{"text": prompt}]}],
                           "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.85}}
                data = _post(url, payload, {"Content-Type": "application/json"}, timeout=60)
                if data is None:
                    time.sleep(10)
                    continue
                try:
                    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    log.info("Generated %d chars: %s", len(text), repr(text[:80]))
                    return text
                except (KeyError, IndexError):
                    break
    log.error("no provider")
    return None


def tg_publish(text):
    token = os.environ.get("NG_TG_TOKEN") or _env("NG_TG_TOKEN")
    if not token:
        log.error("no TG token"); return False
    channel = os.environ.get("NG_TG_CHANNEL") or _env("NG_TG_CHANNEL", "@Ai_Lifes")
    data = _post(
        "https://api.telegram.org/bot{}/sendMessage".format(token),
        {"chat_id": channel, "text": text},
        {"Content-Type": "application/json"}, timeout=30)
    if data and data.get("ok"):
        log.info("TG OK"); return True
    log.error("TG fail"); return False


def vk_publish(text):
    token = os.environ.get("NG_VK_TOKEN") or _env("NG_VK_TOKEN")
    group = os.environ.get("NG_VK_GROUP") or _env("NG_VK_GROUP")
    if not token or not group:
        log.error("no VK"); return False
    owner = -abs(int(group))
    body = urllib.parse.urlencode({"owner_id": owner, "message": text,
                                    "access_token": token, "v": "5.199"}).encode()
    req = urllib.request.Request("https://api.vk.com/method/wall.post", data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            if "error" not in json.loads(r.read().decode()):
                log.info("VK OK"); return True
    except Exception as e:
        log.error("VK fail: %s", e)
    return False


def _now_hour():
    return datetime.utcnow().hour + 4


def _slot_by_hour(hour):
    for slot, h in SCHEDULE.items():
        if hour == h:
            return slot
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    return "evening"


def run_once(slot=None):
    if not slot:
        hour = _now_hour()
        slot = _slot_by_hour(hour)
    log.info("slot: %s (hour %d)", slot, _now_hour())
    text = generate(slot)
    if text:
        tg_publish(text)
        vk_publish(text)
        log.info("%s done", slot)
    else:
        log.error("%s failed", slot)


def main():
    if "--once" in sys.argv:
        i = sys.argv.index("--once")
        slot = sys.argv[i + 1] if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("-") else None
        run_once(slot)
        return
    log.info("neuro-guide started (scheduler mode)")
    while True:
        hour = _now_hour()
        state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
        today = datetime.utcnow().strftime("%Y-%m-%d")
        for slot, h in SCHEDULE.items():
            if hour == h and not state.get(today, {}).get(slot):
                text = generate(slot)
                if text:
                    tg_publish(text)
                    vk_publish(text)
                    state.setdefault(today, {})[slot] = True
                    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        time.sleep(60)


if __name__ == "__main__":
    main()

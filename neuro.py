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
    "8. Длина: 500-1000 символов, но если темы требует больше — пиши больше. "
    "Главное — закончи мысль, не обрывай."
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
    queries = ["искусственный интеллект нейросети новости"]
    seen: set[str] = set()
    items: list[str] = []
    for q in queries:
        url = ("https://news.google.com/rss/search?"
               + urllib.parse.urlencode({"q": q, "hl": "ru", "gl": "RU", "tbs": "qdr:d"}))
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
    ctx = _history_context()
    if ctx:
        prompt += ctx
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
                           "generationConfig": {"maxOutputTokens": 4096, "temperature": 0.9}}
                data = _post(url, payload, {"Content-Type": "application/json"}, timeout=90)
                if data is None:
                    time.sleep(10)
                    continue
                try:
                    cand = data["candidates"][0]
                    raw = cand["content"]["parts"][0]["text"].strip()
                    reason = cand.get("finishReason", "?")
                    first_line = raw.split("\n")[0].strip()
                    img_prompt = "AI technology " + first_line[:50]
                    log.info("Generated %d chars, finish=%s, img=%s", len(raw), reason, img_prompt)
                    return raw, img_prompt
                except (KeyError, IndexError):
                    break
    log.error("no provider")
    return None, None


def _generate_image(prompt):
    if not prompt:
        return None
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt) + "?width=1024&height=768"
    log.info("img url: %s", url)
    return url


def _download(url):
    try:
        with urllib.request.urlopen(url, timeout=20, context=CTX) as r:
            return r.read()
    except Exception as e:
        log.warning("download fail: %s", e)
    return None


def tg_publish(text, image_url=None):
    token = os.environ.get("NG_TG_TOKEN") or _env("NG_TG_TOKEN")
    if not token:
        log.error("no TG token"); return False
    channel = os.environ.get("NG_TG_CHANNEL") or _env("NG_TG_CHANNEL", "@Ai_Lifes")
    if image_url:
        data = _post(
            "https://api.telegram.org/bot{}/sendPhoto".format(token),
            {"chat_id": channel, "photo": image_url, "caption": text},
            {"Content-Type": "application/json"}, timeout=60)
        if data and data.get("ok"):
            log.info("TG photo OK"); return True
        log.warning("TG photo fail: %s", data)
    data = _post(
        "https://api.telegram.org/bot{}/sendMessage".format(token),
        {"chat_id": channel, "text": text},
        {"Content-Type": "application/json"}, timeout=30)
    if data and data.get("ok"):
        log.info("TG OK"); return True
    log.error("TG fail"); return False


def _vk_upload_image(group, image_data):
    try:
        upload_url = ("https://api.vk.com/method/photos.getWallUploadServer?"
                      + urllib.parse.urlencode({"group_id": group,
                                                 "access_token": os.environ.get("NG_VK_TOKEN") or _env("NG_VK_TOKEN"),
                                                 "v": "5.199"}))
        with urllib.request.urlopen(upload_url, timeout=15, context=CTX) as r:
            resp = json.loads(r.read().decode())
        url = resp.get("response", {}).get("upload_url")
        if not url:
            return None
        boundary = "----boundary456"
        h = ('Content-Disposition: form-data; name="photo"; filename="img.jpg"\r\n'
             'Content-Type: image/jpeg\r\n\r\n').encode("utf-8")
        body = b"--" + boundary.encode() + b"\r\n" + h + image_data + b"\r\n--" + boundary.encode() + b"--\r\n"
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            upload = json.loads(r.read().decode())
        save_url = ("https://api.vk.com/method/photos.saveWallPhoto?"
                    + urllib.parse.urlencode({"group_id": group,
                                              "server": upload.get("server"),
                                              "photo": upload.get("photo"),
                                              "hash": upload.get("hash"),
                                              "access_token": os.environ.get("NG_VK_TOKEN") or _env("NG_VK_TOKEN"),
                                              "v": "5.199"}))
        with urllib.request.urlopen(save_url, timeout=15, context=CTX) as r:
            save = json.loads(r.read().decode())
        items = save.get("response", [])
        if items:
            return "photo{}_{}".format(items[0]["owner_id"], items[0]["id"])
    except Exception as e:
        log.warning("VK upload fail: %s", e)
    return None


def vk_publish(text, image_url=None):
    token = os.environ.get("NG_VK_TOKEN") or _env("NG_VK_TOKEN")
    group = os.environ.get("NG_VK_GROUP") or _env("NG_VK_GROUP")
    if not token or not group:
        log.error("no VK"); return False
    owner = -abs(int(group))
    attach = []
    if image_url:
        img_data = _download(image_url)
        if img_data:
            att = _vk_upload_image(group, img_data)
            if att:
                attach.append(att)
    params = {"owner_id": owner, "message": text,
              "access_token": token, "v": "5.199"}
    if attach:
        params["attachments"] = ",".join(attach)
    body = urllib.parse.urlencode(params).encode()
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


HISTORY_FILE = Path(__file__).parent / "history.json"


def _load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return []


def _save_history(text):
    h = _load_history()
    h.append(text)
    HISTORY_FILE.write_text(json.dumps(h[-10:], ensure_ascii=False), encoding="utf-8")


def _history_context():
    h = _load_history()
    if not h:
        return ""
    return "\n\nВАЖНО: не повторяй темы этих предыдущих постов:\n" + "\n".join(
        f"— {p.split(chr(10))[0][:60]}" for p in h[-10:]
    )


def run_once(slot=None):
    if not slot:
        hour = _now_hour()
        slot = _slot_by_hour(hour)
    log.info("slot: %s (hour %d)", slot, _now_hour())
    text, img_prompt = generate(slot)
    img_url = _generate_image(img_prompt) if img_prompt else None
    if text:
        _save_history(text)
        tg_publish(text, img_url)
        vk_publish(text, img_url)
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
                text, img_prompt = generate(slot)
                img_url = _generate_image(img_prompt) if img_prompt else None
                if text:
                    tg_publish(text, img_url)
                    vk_publish(text, img_url)
                    state.setdefault(today, {})[slot] = True
                    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        time.sleep(60)


if __name__ == "__main__":
    main()

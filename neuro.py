#!/usr/bin/env python3
from __future__ import annotations

import io
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

from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("neuro")

CTX = ssl._create_unverified_context()

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _env(key, default=None):
    v = os.environ.get(key)
    if v:
        return v
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
        t = e.read().decode()[:200]
        if e.code == 429:
            log.warning("429 (quota)")
        elif e.code == 503:
            log.info("503 (loading)")
        else:
            log.warning("HTTP %d: %s", e.code, t)
    except Exception as e:
        log.debug("req error: %s", e)
    return None


def _multipart_post(url, fields, file_field, file_data, filename="img.jpg", timeout=60):
    boundary = "----boundary789"
    body = b""
    for k, v in fields.items():
        body += ("--" + boundary + "\r\n"
                 'Content-Disposition: form-data; name="{}"\r\n'
                 'Content-Type: text/plain; charset=utf-8\r\n\r\n'
                 "{}\r\n").format(k, v).encode("utf-8")
    body += ("--" + boundary + "\r\n"
             'Content-Disposition: form-data; name="{}"; filename="{}"\r\n'
             'Content-Type: image/jpeg\r\n\r\n').format(file_field, filename).encode("utf-8")
    body += file_data
    body += ("\r\n--" + boundary + "--\r\n").encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            resp = json.loads(r.read().decode())
            log.info("multipart OK: %s", str(resp.get("ok")))
            return resp
    except urllib.error.HTTPError as e:
        log.warning("multipart HTTP %d: %s", e.code, e.read().decode()[:200])
    except Exception as e:
        log.warning("multipart fail: %s", e)
    return None


FORMAT = (
    "Оформление поста (строго соблюдай):\n"
    "1. Первая строка — яркий заголовок (одна строка, с эмодзи, как новость).\n"
    "2. После заголовка — пустая строка.\n"
    "3. Текст: 3-4 коротких абзаца, только факты, без воды.\n"
    "4. Никаких приветствий, обращений к читателю, вопросов.\n"
    "5. Каждый абзац — 1-2 предложения, между абзацами пустая строка.\n"
    "6. Тон — журналистский, нейтральный, информационный.\n"
    "7. Длина: 400-800 символов. Закончи мысль.\n"
    "8. Без хештегов."
)

PROMPTS_AI = {
    "default": (
        "Ты пишешь пост для Telegram-канала «Нейросети для жизни» (AI-новости)."
        + FORMAT + "\n\n"
        + "Тема: свежая новость из мира искусственного интеллекта "
        "(выход модели, обновление, новый инструмент, событие в индустрии). "
        "Объясни суть и почему это важно. "
        "Пример заголовка: «Google Gemini 2.5 Flash теперь доступен всем бесплатно»"
    ),
}

PROMPTS_SCIENCE = {
    "default": (
        "Ты пишешь пост для Telegram-канала о науке. "
        "Это НЕ чат, а новостной канал."
        + FORMAT + "\n\n"
        + "Тема: новость из мира науки (исследование, открытие, "
        "медицина, физика, биология, психология, технологии). "
        "Выбери конкретную новость. Опиши суть исследования и его значение. "
        "Пример заголовка: «Учёные выяснили, что сауна снижает риск инфаркта на 50%»"
    ),
}


def _fetch_news(query):
    url = ("https://news.google.com/rss/search?"
           + urllib.parse.urlencode({"q": query, "hl": "ru", "gl": "RU", "tbs": "qdr:d"}))
    items = []
    seen = set()
    try:
        with urllib.request.urlopen(url, timeout=15, context=CTX) as r:
            root = ET.fromstring(r.read())
            for item in root.iter("item"):
                title = item.findtext("title", "")
                if title and title not in seen:
                    seen.add(title)
                    items.append(title)
                    if len(items) >= 5:
                        break
    except Exception as e:
        log.debug("news fetch error: %s", e)
    return items


def _gemini(prompt):
    key = _env("NG_GEMINI_KEY")
    if not key:
        return None
    for model in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-001"]:
        for _ in range(3):
            url = ("https://generativelanguage.googleapis.com/v1beta/"
                   "models/{}:generateContent?key={}").format(model, key)
            payload = {"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.85}}
            data = _post(url, payload, {"Content-Type": "application/json"}, timeout=60)
            if data is None:
                time.sleep(5)
                continue
            try:
                cand = data["candidates"][0]
                text = cand["content"]["parts"][0]["text"].strip()
                reason = cand.get("finishReason", "?")
                log.info("Gemini: %d chars, finish=%s", len(text), reason)
                return text
            except (KeyError, IndexError):
                break
    return None


def generate(channel, slot="default"):
    if channel == "ai":
        prompts = PROMPTS_AI
        news_query = "искусственный интеллект нейросети ChatGPT новости"
    else:
        prompts = PROMPTS_SCIENCE
        news_query = "наука открытия исследования природа технология"
    prompt = prompts.get(slot, prompts["default"])
    ctx = _history_context(channel)
    if ctx:
        prompt += ctx
    news = _fetch_news(news_query)
    if news:
        prompt += "\n\nСвежие новости (используй как основу для поста):\n" + "\n".join(f"— {n}" for n in news)
    text = _gemini(prompt)
    if not text:
        return None, None
    headline = text.split("\n")[0].strip()
    img_prompt = "news " + headline[:60]
    return text, img_prompt


def _make_image(prompt, channel="ai"):
    log.info("generating image from prompt: %s", prompt[:60])
    W, H = 1024, 768
    if channel == "ai":
        c1, c2 = (20, 30, 60), (40, 60, 120)
    else:
        c1, c2 = (20, 60, 40), (40, 120, 80)
    img = Image.new("RGB", (W, H), c1)
    draw = ImageDraw.Draw(img)
    for y in range(H):
        r = int(c1[0] + (c2[0] - c1[0]) * y / H)
        g = int(c1[1] + (c2[1] - c1[1]) * y / H)
        b = int(c1[2] + (c2[2] - c1[2]) * y / H)
        draw.line([(0, y), (W, y)], fill=(r, g, b))
    bar_h = int(H * 0.30)
    overlay = Image.new("RGBA", (W, bar_h), (255, 255, 255, 220))
    img.paste(overlay, (0, 0), overlay)
    font = None
    for size in range(40, 18, -2):
        try:
            font = ImageFont.truetype(FONT_PATH, size)
            break
        except OSError:
            continue
    if not font:
        try:
            font = ImageFont.load_default()
        except Exception:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()
    text = prompt[:120]
    lines = []
    for line in text.split("\n"):
        if draw.textlength(line, font=font) > W - 40:
            words = line.split()
            cur = ""
            for w in words:
                test = (cur + " " + w).strip()
                if draw.textlength(test, font=font) > W - 40:
                    lines.append(cur)
                    cur = w
                else:
                    cur = test
            if cur:
                lines.append(cur)
        else:
            lines.append(line)
    y = (bar_h - len(lines) * (size + 4)) // 2 + 4
    for line in lines:
        tw = draw.textlength(line, font=font)
        x = (W - tw) // 2
        draw.text((x, y), line, font=font, fill=(20, 20, 20))
        y += size + 4
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    result = buf.getvalue()
    log.info("image done: %d bytes, %dx%d", len(result), W, H)
    return result


def tg_publish(channel, text, image_data=None):
    token = (_env("NG_TG_TOKEN") if channel == "ai" else _env("NG_TG_TOKEN_SCIENCE")).strip()
    chat = (_env("NG_TG_CHANNEL") if channel == "ai" else _env("NG_TG_CHANNEL_SCIENCE")).strip()
    if not token or not chat:
        log.error("no TG config for %s", channel)
        return False
    if image_data:
        resp = _multipart_post(
            "https://api.telegram.org/bot{}/sendPhoto".format(token),
            {"chat_id": chat, "caption": text},
            "photo", image_data, timeout=60)
        if resp and resp.get("ok"):
            log.info("TG photo OK"); return True
        log.warning("TG photo fail, fallback text")
    resp = _post(
        "https://api.telegram.org/bot{}/sendMessage".format(token),
        {"chat_id": chat, "text": text},
        {"Content-Type": "application/json"}, timeout=30)
    if resp and resp.get("ok"):
        log.info("TG OK"); return True
    log.error("TG fail"); return False


def _vk_upload(group, image_data):
    try:
        token = _env("NG_VK_TOKEN")
        if not token:
            return None
        url = ("https://api.vk.com/method/photos.getWallUploadServer?"
               + urllib.parse.urlencode({"group_id": group, "access_token": token, "v": "5.199"}))
        with urllib.request.urlopen(url, timeout=15, context=CTX) as r:
            resp = json.loads(r.read().decode())
        upload_url = resp.get("response", {}).get("upload_url")
        if not upload_url:
            return None
        boundary = "----vk789"
        h = ('Content-Disposition: form-data; name="photo"; filename="img.jpg"\r\n'
             'Content-Type: image/jpeg\r\n\r\n').encode("utf-8")
        body = (b"--" + boundary.encode() + b"\r\n" + h + image_data +
                b"\r\n--" + boundary.encode() + b"--\r\n")
        req = urllib.request.Request(upload_url, data=body,
                                     headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            upload = json.loads(r.read().decode())
        save_url = ("https://api.vk.com/method/photos.saveWallPhoto?"
                    + urllib.parse.urlencode({"group_id": group,
                                              "server": upload.get("server"),
                                              "photo": upload.get("photo"),
                                              "hash": upload.get("hash"),
                                              "access_token": token,
                                              "v": "5.199"}))
        with urllib.request.urlopen(save_url, timeout=15, context=CTX) as r:
            save = json.loads(r.read().decode())
        items = save.get("response", [])
        if items:
            return "photo{}_{}".format(items[0]["owner_id"], items[0]["id"])
    except Exception as e:
        log.warning("VK upload fail: %s", e)
    return None


def vk_publish(channel, text, image_data=None):
    token = (_env("NG_VK_TOKEN") if channel == "ai" else _env("NG_VK_TOKEN_SCIENCE")).strip()
    group = (_env("NG_VK_GROUP") if channel == "ai" else _env("NG_VK_GROUP_SCIENCE")).strip()
    if not token or not group:
        log.error("no VK config for %s", channel); return False
    owner = -abs(int(group))
    attach = []
    if image_data:
        att = _vk_upload(group, image_data)
        if att:
            attach.append(att)
    params = {"owner_id": owner, "message": text, "access_token": token, "v": "5.199"}
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


HISTORY_DIR = Path(__file__).parent


def _history_path(channel):
    return HISTORY_DIR / f"history_{channel}.json"


def _load_history(channel):
    p = _history_path(channel)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def _save_history(channel, text):
    h = _load_history(channel)
    h.append(text)
    _history_path(channel).write_text(json.dumps(h[-10:], ensure_ascii=False), encoding="utf-8")


def _history_context(channel):
    h = _load_history(channel)
    if not h:
        return ""
    return "\n\nНЕ повторяй темы этих предыдущих постов:\n" + "\n".join(
        f"— {p.split(chr(10))[0][:60]}" for p in h[-10:]
    )


def run_once(channel, slot="default"):
    log.info("channel=%s slot=%s", channel, slot)
    text, img_prompt = generate(channel, slot)
    if not text:
        log.error("generate failed"); return
    log.info("post text: %d chars", len(text))
    img_raw = _make_image(img_prompt, channel)
    log.info("img_raw: %s", "OK" if img_raw else "NONE")
    _save_history(channel, text)
    tg_r = tg_publish(channel, text, img_raw)
    vk_r = vk_publish(channel, text, img_raw)
    log.info("%s done: TG=%s VK=%s", channel, tg_r, vk_r)


def main():
    channel = "ai"
    slot = "default"
    if "--channel" in sys.argv:
        i = sys.argv.index("--channel")
        if i + 1 < len(sys.argv):
            channel = sys.argv[i + 1]
    if "--slot" in sys.argv:
        i = sys.argv.index("--slot")
        if i + 1 < len(sys.argv):
            slot = sys.argv[i + 1]
    run_once(channel, slot)


if __name__ == "__main__":
    main()

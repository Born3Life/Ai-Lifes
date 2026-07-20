#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import logging
import os
import re
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

_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "\U00002B50"
    "\U00002764"
    "]+", flags=re.UNICODE
)

VK_API_VERSION = "5.199"


def _env(key: str, default: str | None = None) -> str | None:
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


def _env_strip(key: str) -> str | None:
    v = _env(key)
    return v.strip() if v else None


def _post(url: str, data: dict, headers: dict | None = None, timeout: int = 60) -> dict | None:
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


def _multipart_post(url: str, fields: dict, file_field: str, file_data: bytes, filename: str = "img.jpg", timeout: int = 60) -> dict | None:
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
    "5. Каждый абзац — 1-2 предложения, между абзацами обязательно пустая строка.\n"
    "6. Тон — журналистский, нейтральный, информационный.\n"
    "7. Длина: 400-800 символов. Закончи мысль.\n"
    "8. Без хештегов.\n"
    "9. ЗАПРЕЩЕНО использовать символы ** для выделения текста. "
    "Пиши обычным текстом, без Markdown.\n"
    "10. В конце напиши строку --END-- после текста."
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


def _fetch_rss(url: str, max_items: int = 5) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    try:
        with urllib.request.urlopen(url, timeout=15, context=CTX) as r:
            root = ET.fromstring(r.read())
            for item in root.iter("item"):
                title = item.findtext("title", "")
                if title and title not in seen:
                    seen.add(title)
                    items.append(title)
                    if len(items) >= max_items:
                        break
    except Exception as e:
        log.debug("RSS fetch error %s: %s", url[:50], e)
    return items


def _fetch_news(query: str) -> list[str]:
    url = ("https://news.google.com/rss/search?"
           + urllib.parse.urlencode({"q": query, "hl": "ru", "gl": "RU", "tbs": "qdr:d"}))
    return _fetch_rss(url)


def _fetch_habr(tag: str = "AI") -> list[str]:
    return _fetch_rss(f"https://habr.com/ru/rss/hub/{tag}/?fl=ru")


def _fetch_tass() -> list[str]:
    return _fetch_rss("https://tass.ru/rss/v2.xml")


def _fetch_nplus1() -> list[str]:
    return _fetch_rss("https://nplus1.ru/rss")


def _fetch_marktechpost() -> list[str]:
    return _fetch_rss("https://www.marktechpost.com/feed/")


def _collect_news(news_query: str, channel: str) -> str:
    sources: list[tuple[str, str]] = [
        ("Google News", _fetch_news(news_query)),
    ]

    if channel == "ai":
        sources.append(("Habr AI", _fetch_habr("artificial_intelligence")))
        sources.append(("Habr ML", _fetch_habr("machine_learning")))
        sources.append(("Habr DL", _fetch_habr("deep_learning")))
        sources.append(("MarkTechPost", _fetch_marktechpost()))
    else:
        sources.append(("Habr Science", _fetch_habr("science")))
        sources.append(("TASS", _fetch_tass()))
        sources.append(("N+1", _fetch_nplus1()))

    result = ""
    for name, items in sources:
        if items:
            result += f"\n— {name}:\n" + "\n".join(f"— {n}" for n in items)

    return result.strip() or ""


def _strip_emojis(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


def _hf_image(prompt: str) -> bytes | None:
    token = _env_strip("NG_HF_TOKEN")
    if not token:
        log.info("NG_HF_TOKEN not set, skipping HF image")
        return None
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"inputs": prompt, "options": {"wait_for_model": True}}
    models = [
        "black-forest-labs/FLUX.1-schnell",
        "stabilityai/stable-diffusion-3.5-large",
    ]
    urls = []
    for m in models:
        urls.append(f"https://router.huggingface.co/hf-inference/models/{m}")
        urls.append(f"https://api-inference.huggingface.co/models/{m}")
    for url in urls:
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
                    raw = r.read()
                    img = Image.open(io.BytesIO(raw))
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=90)
                    result = buf.getvalue()
                    log.info("HF image: %d bytes", len(result))
                    return result
            except urllib.error.HTTPError as e:
                body = e.read().decode()[:200]
                if e.code == 500 and attempt == 0:
                    log.info("HF 500, retrying...")
                    time.sleep(5)
                    continue
                log.warning("HF HTTP %d (%s): %s", e.code, url.split("/")[2], body)
                break
            except Exception as e:
                log.warning("HF error %s: %s", url.split("/")[2], e)
                break
    return None


def _random_photo(prompt: str) -> bytes | None:
    for url in ["https://picsum.photos/1024/768", "https://picsum.photos/1024/768?grayscale"]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
                raw = r.read()
                img = Image.open(io.BytesIO(raw))
                if img.mode != "RGB":
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                result = buf.getvalue()
                log.info("Random photo: %d bytes", len(result))
                return result
        except Exception as e:
            log.warning("Random photo fail: %s", e)
    return None


def _pollinations_image(prompt: str) -> bytes | None:
    q = urllib.parse.quote(prompt[:100])
    url = f"https://image.pollinations.ai/prompt/{q}?width=1024&height=768&nologo=true&seed=42&safe=false"
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/webp,*/*"})
            with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
                raw = r.read()
                if not raw or len(raw) < 1000:
                    log.warning("Pollinations: too small (%d)", len(raw) if raw else 0)
                    return None
                img = Image.open(io.BytesIO(raw))
                if img.mode != "RGB":
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=90)
                result = buf.getvalue()
                log.info("Pollinations: %d bytes", len(result))
                return result
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:100]
            if e.code == 402 and attempt == 0:
                log.info("Pollinations queue full, retrying in 60s...")
                time.sleep(60)
                continue
            log.warning("Pollinations HTTP %d: %s", e.code, body)
            break
        except Exception as e:
            log.warning("Pollinations fail: %s", e)
            break
    return None


def _gemini(prompt: str, max_tokens: int = 2048) -> str | None:
    key = _env_strip("NG_GEMINI_KEY")
    if not key:
        return None
    for model in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-001"]:
        for _ in range(3):
            url = ("https://generativelanguage.googleapis.com/v1beta/"
                   "models/{}:generateContent?key={}").format(model, key)
            payload = {"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.85}}
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


def generate(channel: str, slot: str = "default") -> str | None:
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

    news = _collect_news(news_query, channel)
    if news:
        prompt += "\n\nСвежие новости (используй как основу для поста):\n" + news

    raw = _gemini(prompt, max_tokens=4096)
    if not raw:
        return None
    text = raw.strip()
    if text.endswith("--END--"):
        text = text[:-7].strip()
    return text


def _generate_image_prompt(post_text: str) -> str:
    prompt = (
        "Based on this news post, write a SHORT visual description in English "
        "(4-10 words, no quotes, no labels, just visual elements) "
        "for an AI image generator:\n\n" + post_text[:500]
    )
    result = _gemini(prompt, max_tokens=100)
    if result:
        clean = result.strip().strip("\"'")
        log.info("image prompt: %s", clean)
        return clean
    headline = post_text.split("\n")[0][:60]
    fallback = f"news illustration of {headline}"
    log.info("image prompt fallback: %s", fallback)
    return fallback


def _verify_image(data: bytes | None) -> bytes | None:
    if data is None or len(data) < 5000:
        log.warning("image too small or empty (%s), skipping", len(data) if data else 0)
        return None
    try:
        img = Image.open(io.BytesIO(data))
        if img.mode != "RGB":
            img = img.convert("RGB")
        extrema = img.getextrema()
        if extrema and all(mn == mx for mn, mx in extrema):
            log.warning("image is a solid color, skipping")
            return None
        return data
    except Exception as e:
        log.warning("image verify failed: %s", e)
        return None


def _make_image(prompt: str, channel: str = "ai") -> bytes | None:
    log.info("generating image from prompt: %s", prompt[:80])
    providers = [
        ("HF", _hf_image(prompt)),
        ("Pollinations", _pollinations_image(prompt)),
    ]
    for name, data in providers:
        if data:
            verified = _verify_image(data)
            if verified:
                log.info("%s image OK (%d bytes)", name, len(verified))
                return verified
            log.warning("%s image failed verification", name)
    log.info("all AI image providers failed, trying random photo")
    rand = _random_photo(prompt)
    return _verify_image(rand)


def tg_publish(channel: str, text: str, image_data: bytes | None = None) -> bool:
    token = _env_strip("NG_TG_TOKEN") if channel == "ai" else _env_strip("NG_TG_TOKEN_SCIENCE")
    chat = _env_strip("NG_TG_CHANNEL") if channel == "ai" else _env_strip("NG_TG_CHANNEL_SCIENCE")
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


def _vk_upload(channel: str, group: str, image_data: bytes) -> str | None:
    try:
        token = _env_strip("NG_VK_TOKEN") if channel == "ai" else _env_strip("NG_VK_TOKEN_SCIENCE")
        if not token:
            log.warning("VK upload: no token for %s", channel)
            return None

        url = ("https://api.vk.com/method/photos.getWallUploadServer?"
               + urllib.parse.urlencode({"group_id": group, "access_token": token, "v": VK_API_VERSION}))
        with urllib.request.urlopen(url, timeout=15, context=CTX) as r:
            resp = json.loads(r.read().decode())
        if "error" in resp:
            log.warning("VK getWallUploadServer error: %s",
                        resp["error"].get("error_msg", str(resp["error"])[:100]))
            return None
        upload_url = resp.get("response", {}).get("upload_url")
        if not upload_url:
            log.warning("VK upload: no upload_url in response")
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
                                              "v": VK_API_VERSION}))
        with urllib.request.urlopen(save_url, timeout=15, context=CTX) as r:
            save = json.loads(r.read().decode())

        if "error" in save:
            log.warning("VK saveWallPhoto error: %s",
                        save["error"].get("error_msg", str(save["error"])[:100]))
            return None

        items = save.get("response", [])
        if items:
            return "photo{}_{}".format(items[0]["owner_id"], items[0]["id"])
        log.warning("VK upload: saveWallPhoto empty, save=%s", str(save)[:200])
    except Exception as e:
        log.warning("VK upload fail: %s", e)
    return None


def vk_publish(channel: str, text: str, image_data: bytes | None = None) -> bool:
    token = _env_strip("NG_VK_TOKEN") if channel == "ai" else _env_strip("NG_VK_TOKEN_SCIENCE")
    group = _env_strip("NG_VK_GROUP") if channel == "ai" else _env_strip("NG_VK_GROUP_SCIENCE")
    if not token or not group:
        log.error("no VK config for %s", channel)
        return False
    owner = -abs(int(group))
    attach: list[str] = []
    if image_data:
        att = _vk_upload(channel, group, image_data)
        if att:
            attach.append(att)
    params = {
        "owner_id": owner,
        "from_group": 1,
        "message": text,
        "access_token": token,
        "v": VK_API_VERSION,
    }
    if attach:
        params["attachments"] = ",".join(attach)
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request("https://api.vk.com/method/wall.post", data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            resp = json.loads(r.read().decode())
            if "error" in resp:
                log.error("VK wall.post error: %s",
                          resp["error"].get("error_msg", str(resp["error"])[:100]))
                return False
            log.info("VK OK")
            return True
    except Exception as e:
        log.error("VK fail: %s", e)
    return False


HISTORY_DIR = Path(__file__).parent


def _history_path(channel: str) -> Path:
    return HISTORY_DIR / f"history_{channel}.json"


def _load_history(channel: str) -> list[str]:
    p = _history_path(channel)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def _save_history(channel: str, text: str) -> None:
    h = _load_history(channel)
    h.append(text)
    _history_path(channel).write_text(json.dumps(h[-10:], ensure_ascii=False), encoding="utf-8")


def _history_context(channel: str) -> str:
    h = _load_history(channel)
    if not h:
        return ""
    return "\n\nНЕ повторяй темы этих предыдущих постов:\n" + "\n".join(
        f"— {p.split(chr(10))[0][:60]}" for p in h[-10:]
    )


def run_once(channel: str, slot: str = "default") -> None:
    log.info("channel=%s slot=%s", channel, slot)

    text = generate(channel, slot)
    if not text:
        log.error("generate failed")
        return
    log.info("post text: %d chars", len(text))

    img_prompt = _generate_image_prompt(text)
    log.info("image prompt: %s", img_prompt[:80])

    img_raw = _make_image(img_prompt, channel)
    log.info("img_raw: %s", "OK" if img_raw else "NONE")

    _save_history(channel, text)
    tg_r = tg_publish(channel, text, img_raw)
    vk_r = vk_publish(channel, text, img_raw)
    log.info("%s done: TG=%s VK=%s", channel, tg_r, vk_r)


def main() -> None:
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

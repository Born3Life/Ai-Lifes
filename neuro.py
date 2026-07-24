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
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("neuro")

CTX = ssl._create_unverified_context()
_3H = timedelta(hours=3)

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
        log.warning("req error: %s", e)
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
    "10. ЗАПРЕЩЕНО писать свои рассуждения, выбор темы или объяснения в "
    "квадратных скобках. Пиши ТОЛЬКО готовый пост.\n"
    "11. В конце напиши строку --END-- после текста."
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

_EVERGREEN_AI = (
    "Свежих новостей нет. Выбери ОДНУ из вечных тем ниже и напиши полноценный пост "
    "(заголовок + 3-4 абзаца, 400-800 символов):\n"
    "• Туториал: как использовать нейросеть для конкретной задачи (пошаговая инструкция)\n"
    "• Сравнение двух AI-инструментов для одной задачи (что лучше и почему)\n"
    "• Топ-5 AI-инструментов для определённой категории (дизайн, код, текст, видео)\n"
    "• Разбор мифа об ИИ: что правда, а что нет\n"
    "• Кейс: реальный пример использования нейросетей в работе или жизни\n"
    "• История создания известной AI-модели или компании\n"
    "• Что такое [термин из ИИ] простыми словами\n"
    "• Как нейросети меняют конкретную индустрию\n"
    "• Подборка полезных промптов для повседневных задач\n"
    "• Будущее технологии: прогнозы и тренды"
)

_EVERGREEN_SCIENCE = (
    "Свежих новостей нет. Выбери ОДНУ из вечных тем ниже и напиши полноценный пост "
    "(заголовок + 3-4 абзаца, 400-800 символов):\n"
    "• Как работает конкретное научное явление (объяснение для обычного человека)\n"
    "• История великого открытия: кто, когда и как сделал\n"
    "• Краткая биография учёного и его главное открытие\n"
    "• Почему происходит природное явление (научное объяснение)\n"
    "• Как технология изменит нашу жизнь через 10 лет\n"
    "• Что мы знаем о [тема: космос, океан, мозг, генетика]\n"
    "• Научный метод: как учёные делают открытия\n"
    "• 5 удивительных научных фактов на одну тему\n"
    "• Как работает современная технология с точки зрения физики/химии\n"
    "• Медицинское открытие, которое спасло миллионы жизней"
)


def _parse_rss_date(dt_str: str) -> str:
    try:
        dt = datetime.strptime(dt_str.rsplit(" ", 1)[0], "%a, %d %b %Y %H:%M:%S")
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return dt_str[:10]


def _fetch_rss(url: str, max_items: int = 5) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    try:
        with urllib.request.urlopen(url, timeout=15, context=CTX) as r:
            root = ET.fromstring(r.read())
            for item in root.iter("item"):
                title = item.findtext("title", "")
                if title and title not in seen:
                    seen.add(title)
                    pub = item.findtext("pubDate", "") or item.findtext("dc:date", "")
                    items.append({
                        "title": title,
                        "date": _parse_rss_date(pub) if pub else "??",
                        "source": url.split("/")[2] if "//" in url else url[:30],
                    })
                    if len(items) >= max_items:
                        break
    except Exception as e:
        log.debug("RSS fetch error %s: %s", url[:50], e)
    return items


def _fetch_news(query: str) -> list[dict]:
    url = ("https://news.google.com/rss/search?"
           + urllib.parse.urlencode({"q": query, "hl": "ru", "gl": "RU", "tbs": "qdr:d"}))
    return _fetch_rss(url)


def _fetch_habr(tag: str = "AI") -> list[dict]:
    return _fetch_rss(f"https://habr.com/ru/rss/hub/{tag}/?fl=ru")


def _fetch_tass() -> list[dict]:
    return _fetch_rss("https://tass.ru/rss/v2.xml")


def _fetch_nplus1() -> list[dict]:
    return _fetch_rss("https://nplus1.ru/rss")


def _fetch_marktechpost() -> list[dict]:
    return _fetch_rss("https://www.marktechpost.com/feed/")


def _fetch_techcrunch() -> list[dict]:
    return _fetch_rss("https://techcrunch.com/category/artificial-intelligence/feed/")


def _fetch_venturebeat() -> list[dict]:
    return _fetch_rss("https://venturebeat.com/category/ai/feed/")


def _fetch_sciencedaily() -> list[dict]:
    return _fetch_rss("https://www.sciencedaily.com/rss/all.xml")


def _fetch_physorg() -> list[dict]:
    return _fetch_rss("https://phys.org/rss-feed/")


def _fetch_nature() -> list[dict]:
    return _fetch_rss("https://www.nature.com/nature.rss")


def _fetch_newscientist() -> list[dict]:
    return _fetch_rss("https://www.newscientist.com/feed/home")


def _fetch_eurekalert() -> list[dict]:
    return _fetch_rss("https://www.eurekalert.org/rss/news.xml")


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
    for m in models:
        for prefix in ("https://router.huggingface.co/hf-inference/models/",
                       "https://api-inference.huggingface.co/models/"):
            url = prefix + m
            try:
                req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
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
                log.warning("HF HTTP %d (%s): %s", e.code, m, e.read().decode()[:100])
            except Exception as e:
                log.warning("HF error %s: %s", m, e)
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
    urls = [
        f"https://image.pollinations.ai/prompt/{q}?width=1024&height=768&nologo=true&seed={int(time.time())}&safe=false",
        f"https://image.pollinations.ai/prompt/{q}?width=800&height=600&nologo=true&seed={int(time.time())}&safe=false",
    ]
    for url in urls:
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/webp,*/*"})
                with urllib.request.urlopen(req, timeout=90, context=CTX) as r:
                    raw = r.read()
                    if not raw or len(raw) < 1000:
                        log.warning("Pollinations: too small (%d)", len(raw) if raw else 0)
                        continue
                    img = Image.open(io.BytesIO(raw))
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=90)
                    result = buf.getvalue()
                    log.info("Pollinations: %d bytes from %s", len(result), url[:60])
                    return result
            except urllib.error.HTTPError as e:
                body = e.read().decode()[:100]
                if e.code == 402 and attempt == 0:
                    log.info("Pollinations queue full, retrying in 30s...")
                    time.sleep(30)
                    continue
                log.warning("Pollinations HTTP %d: %s", e.code, body)
            except Exception as e:
                log.warning("Pollinations fail: %s", e)
    return None


def _gemini(prompt: str, max_tokens: int = 2048) -> str | None:
    key = _env_strip("NG_GEMINI_KEY")
    if not key:
        return None
    for model in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-001"]:
        url = ("https://generativelanguage.googleapis.com/v1beta/"
               "models/{}:generateContent?key={}").format(model, key)
        payload = {"contents": [{"parts": [{"text": prompt}]}],
                   "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.85}}
        data = _post(url, payload, {"Content-Type": "application/json"}, timeout=20)
        if data is None:
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


def _openrouter(prompt: str, max_tokens: int = 2048) -> str | None:
    key = _env_strip("OPENROUTER_API_KEY")
    if not key:
        return None
    models = [
        "openai/gpt-4o-mini",
        "deepseek/deepseek-v4-flash:free",
        "openrouter/free",
    ]
    for model in models:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        data = _post(
            "https://openrouter.ai/api/v1/chat/completions",
            payload,
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=30,
        )
        if data and isinstance(data, dict):
            try:
                content = data["choices"][0]["message"]["content"]
                if content and content.strip():
                    text = content.strip()
                    log.info("OpenRouter %s OK (%d chars)", model, len(text))
                    return text
            except (KeyError, IndexError):
                continue
    return None


def _deepseek(prompt: str, max_tokens: int = 2048) -> str | None:
    key = _env_strip("DEEPSEEK_API_KEY")
    if not key:
        return None
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    data = _post(
        "https://api.deepseek.com/chat/completions",
        payload,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=30,
    )
    if data and isinstance(data, dict):
        try:
            content = data["choices"][0]["message"]["content"]
            if content and content.strip():
                text = content.strip()
                log.info("DeepSeek OK (%d chars)", len(text))
                return text
        except (KeyError, IndexError):
            pass
    return None


def _ask(prompt: str, max_tokens: int = 2048) -> str | None:
    r = _gemini(prompt, max_tokens)
    if r:
        return r
    log.info("Gemini failed, trying DeepSeek")
    r = _deepseek(prompt, max_tokens)
    if r:
        return r
    log.info("DeepSeek failed, trying OpenRouter")
    return _openrouter(prompt, max_tokens)


HISTORY_DIR = Path(__file__).parent


def _load_agents() -> str:
    p = HISTORY_DIR / "AGENTS.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return ""


def _topics_log_path(channel: str) -> Path:
    return HISTORY_DIR / f"topics_log_{channel}.json"


def _load_topics_log(channel: str) -> list[dict]:
    p = _topics_log_path(channel)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def _now_msk_str() -> str:
    return (datetime.utcnow() + _3H).strftime("%d.%m.%Y %H:%M")


def _save_topics_log(channel: str, text: str) -> None:
    title = text.split("\n")[0][:80]
    words = [w.lower().strip(",.!?«»\"'()[]-") for w in title.split() if len(w) > 3]
    entry = {"title": title[:60], "keywords": words[:10], "date": _now_msk_str()}
    log = _load_topics_log(channel)
    log.append(entry)
    _topics_log_path(channel).write_text(json.dumps(log[-50:], ensure_ascii=False), encoding="utf-8")


def _similar_to_log(title: str, log: list[dict]) -> bool:
    tw = _words(title)
    if not tw:
        return False
    for entry in log:
        overlap = len(tw & set(entry["keywords"])) / max(len(tw), 1)
        if overlap > 0.35:
            return True
    return False


_THOUGHT_RE = re.compile(
    r"(?:^|\n)\s*\[(?:выбор|рассуждени|анализ|размышлени|thought|reasoning|объяснени|пояснени|заметк)"
    r":[^\]]*\]\s*(?:\n|$)",
    re.DOTALL | re.IGNORECASE,
)


def _clean_thoughts(text: str) -> str:
    text = _THOUGHT_RE.sub("", text)
    text = re.sub(r'(?:^|\n)\s*\[[^\]]{20,}\]\s*(?:\n|$)', "", text)
    lines = text.split("\n")
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and (stripped.startswith("[") and stripped.endswith("]") and len(stripped) > 30):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def generate(channel: str, slot: str = "default") -> str | None:
    if channel == "ai":
        prompts = PROMPTS_AI
        news_query = "искусственный интеллект нейросети ChatGPT новости"
        evergreen = _EVERGREEN_AI
    else:
        prompts = PROMPTS_SCIENCE
        news_query = "наука открытия исследования природа технология"
        evergreen = _EVERGREEN_SCIENCE

    prompt = prompts.get(slot, prompts["default"])

    agents = _load_agents()
    if agents:
        prompt = "Инструкции для агента:\n" + agents + "\n\n" + prompt

    topics_log = _load_topics_log(channel)
    if topics_log:
        prompt += (
            "\n\nБлокнот тем (уже постили, не повторяй):\n"
            + "\n".join(f"— {e['title']} ({e['date']})" for e in topics_log[-15:])
        )

    ctx = _history_context(channel)
    if ctx:
        prompt += ctx

    news = _collect_news(news_query, channel)
    if news:
        prompt += "\n\nДоступные темы (выбери ОДНУ самую интересную):\n" + news
        if "ранее опубликованные" in news:
            prompt += (
                "\nТак как это из архива — обязательно укажи дату в посте."
            )
    else:
        prompt += "\n\n" + evergreen

    for attempt in range(3):
        raw = _ask(prompt, max_tokens=4096)
        if not raw:
            return None
        text = _clean_thoughts(raw.strip())
        if text.endswith("--END--"):
            text = text[:-7].strip()
        text = text.strip()
        if not text:
            continue

        title = text.split("\n")[0]
        if _similar_to_log(title, topics_log):
            log.warning("duplicate topic (attempt %d): %s…", attempt + 1, title[:50])
            prompt += (
                "\n\nПРЕДУПРЕЖДЕНИЕ: предыдущий ответ повторяет "
                "уже использованную тему. Выбери ДРУГУЮ тему. Не пиши про это."
            )
            continue

        return text

    log.error("all 3 attempts failed — duplicate topics")
    return None


def _generate_image_prompt(post_text: str) -> str:
    prompt = (
        "Based on this news post, write a SHORT visual description in English "
        "(4-10 words, no quotes, no labels, just visual elements) "
        "for an AI image generator:\n\n" + post_text[:500]
    )
    result = _ask(prompt, max_tokens=100)
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


def _fallback_image() -> bytes | None:
    urls = [
        "https://picsum.photos/1024/768",
        "https://picsum.photos/800/600",
        "https://source.unsplash.com/1024x768/?technology,ai",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
                raw = r.read()
                if raw and len(raw) > 5000:
                    img = Image.open(io.BytesIO(raw))
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=85)
                    log.info("Fallback image: %d bytes from %s", len(raw), url[:40])
                    return buf.getvalue()
        except Exception as e:
            log.warning("Fallback fail %s: %s", url[:40], e)
    return None


def _make_image(prompt: str, channel: str = "ai") -> bytes | None:
    log.info("generating image from prompt: %s", prompt[:80])
    providers: list[tuple[str, Callable[[], bytes | None]]] = [
        ("Pollinations", lambda: _pollinations_image(prompt)),
        ("HF", lambda: _hf_image(prompt)),
    ]
    for name, fn in providers:
        log.info("trying %s...", name)
        try:
            data = fn()
            if data:
                verified = _verify_image(data)
                if verified:
                    log.info("%s image OK (%d bytes)", name, len(verified))
                    return verified
                log.warning("%s image failed verification", name)
        except Exception as exc:
            log.warning("%s image error: %s", name, exc)
    log.info("all AI image providers failed, trying fallback")
    try:
        fb = _fallback_image()
        return _verify_image(fb)
    except Exception as exc:
        log.warning("fallback image error: %s", exc)
    return None


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
            msg = resp["error"].get("error_msg", str(resp["error"])[:100])
            log.warning("VK upload photo need USER token (not group). %s", msg)
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


def _upload_to_hosting(image_data: bytes) -> str | None:
    """Upload image to 0x0.st (free no-registration file host)."""
    boundary = "----boundary123"
    body = (
        b"--" + boundary.encode() + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="img.jpg"\r\n'
        b"Content-Type: image/jpeg\r\n\r\n" + image_data +
        b"\r\n--" + boundary.encode() + b"--\r\n"
    )
    for url in ("https://0x0.st", "https://envs.sh"):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                result = r.read().decode().strip()
                if result:
                    log.info("uploaded to %s: %s", url, result[:60])
                    return result
        except Exception as e:
            log.warning("upload to %s fail: %s", url, e)
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
        else:
            img_url = _upload_to_hosting(image_data)
            if img_url:
                attach.append(img_url)
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


def _history_path(channel: str) -> Path:
    return HISTORY_DIR / f"history_{channel}.json"


def _used_rss_path(channel: str) -> Path:
    return HISTORY_DIR / f"used_rss_{channel}.json"


def _load_history(channel: str) -> list[str]:
    p = _history_path(channel)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def _save_history(channel: str, text: str) -> None:
    h = _load_history(channel)
    h.append(text)
    _history_path(channel).write_text(json.dumps(h[-20:], ensure_ascii=False), encoding="utf-8")


def _load_used_rss(channel: str) -> list[str]:
    p = _used_rss_path(channel)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def _save_used_rss(channel: str, titles: list[str]) -> None:
    old = _load_used_rss(channel)
    old.extend(titles)
    _used_rss_path(channel).write_text(json.dumps(old[-50:], ensure_ascii=False), encoding="utf-8")


def _words(text: str) -> set[str]:
    return set(w.lower().strip(",.!?«»\"'()[]-") for w in text.split() if len(w) > 3)


def _similar_to_used(title: str, used: list[str]) -> bool:
    tw = _words(title)
    if not tw:
        return False
    for u in used:
        uw = _words(u)
        if not uw:
            continue
        overlap = len(tw & uw) / max(len(tw), len(uw))
        if overlap > 0.4:
            return True
    return False


def _rss_archive_path(channel: str) -> Path:
    return HISTORY_DIR / f"rss_archive_{channel}.json"


def _load_rss_archive(channel: str) -> list[dict]:
    p = _rss_archive_path(channel)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def _save_rss_archive(channel: str, items: list[dict]) -> None:
    old = _load_rss_archive(channel)
    existing = {i["title"] for i in old}
    new = [i for i in items if i["title"] not in existing]
    if new:
        old.extend(new)
        _rss_archive_path(channel).write_text(
            json.dumps(old[-200:], ensure_ascii=False), encoding="utf-8")


def _format_news(items: list[dict]) -> str:
    return "\n".join(
        f"• [{i['date']}] [{i['source']}] {i['title']}" for i in items)


def _collect_news(news_query: str, channel: str) -> str:
    sources: list[tuple[str, list[dict]]] = [
        ("Google News", _fetch_news(news_query)),
    ]

    if channel == "ai":
        sources.extend([
            ("Habr AI", _fetch_habr("artificial_intelligence")),
            ("Habr ML", _fetch_habr("machine_learning")),
            ("Habr DL", _fetch_habr("deep_learning")),
            ("MarkTechPost", _fetch_marktechpost()),
            ("TechCrunch AI", _fetch_techcrunch()),
            ("VentureBeat AI", _fetch_venturebeat()),
        ])
    else:
        sources.extend([
            ("Habr Science", _fetch_habr("science")),
            ("TASS", _fetch_tass()),
            ("N+1", _fetch_nplus1()),
            ("ScienceDaily", _fetch_sciencedaily()),
            ("Phys.org", _fetch_physorg()),
            ("Nature", _fetch_nature()),
            ("New Scientist", _fetch_newscientist()),
            ("EurekAlert", _fetch_eurekalert()),
        ])

    used = _load_used_rss(channel)
    fresh: list[dict] = []
    all_items: list[dict] = []
    for _name, items in sources:
        all_items.extend(items)
        for i in items:
            if not _similar_to_used(i["title"], used):
                fresh.append(i)
    _save_rss_archive(channel, all_items)

    if fresh:
        _save_used_rss(channel, [i["title"] for i in fresh])
        return "Свежие новости:\n" + _format_news(fresh)

    archive = _load_rss_archive(channel)
    unposted = [i for i in archive if not _similar_to_used(i["title"], used)]
    if unposted:
        picked = unposted[:3]
        _save_used_rss(channel, [i["title"] for i in picked])
        return "Нет свежих новостей. Используй ранее опубликованные (укажи дату):\n" + _format_news(picked)

    return ""


def _history_context(channel: str) -> str:
    h = _load_history(channel)
    if not h:
        return ""
    return "\n\nКатегорически НЕ повторяй темы этих предыдущих постов. Выбери другую:\n" + "\n".join(
        f"— {p.split(chr(10))[0][:80]}" for p in h[-15:]
    )


def _today_msk() -> str:
    return (datetime.utcnow() + _3H).strftime("%Y-%m-%d")


def _posted_today(channel: str) -> int:
    p = HISTORY_DIR / f"posted_{channel}.json"
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        today = _today_msk()
        return data.get(today, 0)
    return 0


def _inc_posted(channel: str) -> None:
    p = HISTORY_DIR / f"posted_{channel}.json"
    data: dict = {}
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
    today = _today_msk()
    data[today] = data.get(today, 0) + 1
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


MAX_POSTS_PER_DAY = 2


def run_once(channel: str, slot: str = "default") -> None:
    log.info("channel=%s slot=%s", channel, slot)
    posted = _posted_today(channel)
    if posted >= MAX_POSTS_PER_DAY:
        log.info("%s already posted %d times today, skip", channel, posted)
        return
    try:
        text = generate(channel, slot)
        if not text:
            log.error("generate failed")
            return
        log.info("post text: %d chars", len(text))

        img_prompt = _generate_image_prompt(text)
        log.info("image prompt: %s", img_prompt[:80])

        img_raw = _make_image(img_prompt, channel)
        log.info("img_raw: %s", "OK" if img_raw else "NONE")

        _save_topics_log(channel, text)
        _save_history(channel, text)
        tg_r = tg_publish(channel, text, img_raw)
        vk_r = vk_publish(channel, text, img_raw)
        _inc_posted(channel)
        log.info("%s done: TG=%s VK=%s", channel, tg_r, vk_r)
    except Exception:
        log.exception("run_once failed")


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

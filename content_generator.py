from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.request
from os import getenv

logger = logging.getLogger(__name__)

CHANNEL = "Ai Lifes"
CTX = ssl._create_unverified_context()


def _prompts() -> dict[str, str]:
    return {
        "morning": (
            f"Ты — автор Telegram-канала «Нейросети для жизни» ({CHANNEL}). "
            "Напиши пост на утро (до 300 символов) на русском: "
            "короткая новость или полезная фишка из мира ИИ, "
            "которая появилась за последнюю неделю. "
            "Добавь эмодзи. Тон — дружелюбный, понятный новичкам. "
            "Закончи призывом подписаться. Без хештегов."
        ),
        "afternoon": (
            f"Ты — автор Telegram-канала «Нейросети для жизни» ({CHANNEL}). "
            "Напиши пост на день (до 400 символов) на русском: "
            "короткая инструкция «как сделать Х с помощью нейросети». "
            "Пример: как перевести видео, как сделать конспект, "
            "как сгенерировать картинку. Шаги 1-2-3. "
            "Добавь эмодзи. Тон — practical, понятный. "
            "Закончи вопросом к аудитории. Без хештегов."
        ),
        "evening": (
            f"Ты — автор Telegram-канала «Нейросети для жизни» ({CHANNEL}). "
            "Напиши пост на вечер (до 300 символов) на русском: "
            "подборка из 3-4 полезных AI-инструментов или ссылок, "
            "или короткая история/мем про нейросети. "
            "Добавь эмодзи. Тон — лёгкий, развлекательный. "
            "Без хештегов."
        ),
    }


def _post(url: str, data: dict, headers: dict | None = None, timeout: int = 60) -> dict | list | None:
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        if e.code == 429:
            logger.warning("429 quota exceeded")
        elif e.code == 503:
            logger.info("503 model loading, will retry")
        else:
            logger.warning("HTTP %d: %s", e.code, body)
        return None
    except Exception as e:
        logger.debug("request error: %s", e)
        return None


GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-3-flash",
]


def _gemini(prompt: str) -> str | None:
    key = getenv("NG_GEMINI_KEY")
    if not key:
        return None
    models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-3-flash"]
    for model in models:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/"
            f"models/{model}:generateContent?key={key}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 500, "temperature": 0.8},
        }
        for attempt in range(3):
            data = _post(url, payload, {"Content-Type": "application/json"}, timeout=60)
            if data is None:
                time.sleep(10)
                continue
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except (KeyError, IndexError):
                break
    return None


def _hf(prompt: str) -> str | None:
    key = getenv("NG_HF_TOKEN")
    if not key:
        return None
    url = "https://router.huggingface.co/hf-inference/v1/chat/completions"
    models = [
        "Qwen/Qwen2.5-7B-Instruct",
        "meta-llama/Llama-3.1-8B-Instruct",
        "microsoft/Phi-3-mini-4k-instruct",
    ]
    for model in models:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 500,
            "temperature": 0.8,
        }
        data = _post(url, payload, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout=90)
        if data and isinstance(data, dict):
            try:
                return data["choices"][0]["message"]["content"].strip()
            except (KeyError, IndexError):
                pass
    return None


def generate(slot: str) -> str | None:
    prompt = _prompts().get(slot, _prompts()["morning"])
    r = _gemini(prompt)
    if r:
        logger.info("Gemini OK")
        return r
    r = _hf(prompt)
    if r:
        logger.info("HF OK")
        return r
    logger.error("no provider available")
    return None

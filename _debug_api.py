"""Debug: test Gemini API connectivity."""
from __future__ import annotations

import json
import os
import ssl
import urllib.request

ctx = ssl._create_unverified_context()
key = os.environ.get("NG_GEMINI_KEY") or ""
print("KEY length:", len(key), "prefix:", key[:4] if key else "EMPTY")

url = "https://generativelanguage.googleapis.com/v1beta/models?key=" + key
try:
    with urllib.request.urlopen(url, context=ctx, timeout=15) as r:
        print("Models list:", r.read().decode()[:2000])
except Exception as e:
    print("Models error:", type(e).__name__, e)
    if hasattr(e, "read"):
        print("Body:", e.read().decode()[:500])

for model in ["gemini-1.5-flash", "gemini-2.0-flash", "gemini-2.5-flash"]:
    url = (f"https://generativelanguage.googleapis.com/v1beta/"
           f"models/{model}:generateContent?key={key}")
    body = json.dumps({"contents": [{"parts": [{"text": "hi"}]}]}).encode()
    try:
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            print(f"Model {model}: OK", r.read().decode()[:300])
    except Exception as e:
        print(f"Model {model}: error", type(e).__name__, e)
        if hasattr(e, "read"):
            print("Body:", e.read().decode()[:300])

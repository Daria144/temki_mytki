"""Які моделі Gemini реально відповідають на ЦЬОМУ ключі — і скільки коштує відмова.

Причина: у журналі видно 503 від gemini-flash-latest і одразу 429 від запасної
gemini-2.5-flash. Якщо в запасної нульова квота, кожна невдача коштує ДВА
запити замість одного, і безплатна квота витрачається на гарантований 429.

Робить по одному короткому запиту на модель. Ключ не друкує.
"""
import asyncio
import json

import httpx

from app.config import config

API = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"

CANDIDATES = (
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
)


async def main():
    headers = {"x-goog-api-key": config.gemini_api_key}
    print("модель у конфігу:", config.gemini_model)
    print("запасна модель:  ", config.gemini_fallback_model)
    print()

    async with httpx.AsyncClient(timeout=60) as cl:
        r = await cl.get("https://generativelanguage.googleapis.com/v1beta/models",
                         headers=headers, params={"pageSize": "200"})
        names = sorted(
            m["name"].removeprefix("models/")
            for m in (r.json().get("models") or [])
            if "generateContent" in (m.get("supportedGenerationMethods") or [])
        )
        print(f"доступних моделей у ключа: {len(names)}")
        print("  flash-подібні:", ", ".join(n for n in names if "flash" in n))
        print()

        for m in CANDIDATES:
            payload = {
                "contents": [{"parts": [{"text": "Відповідай одним словом: тест"}]}],
                "generationConfig": {"maxOutputTokens": 512, "temperature": 0.0},
            }
            try:
                resp = await cl.post(API.format(m=m), headers=headers, json=payload)
                body = resp.json()
                if resp.status_code == 200:
                    txt = (body["candidates"][0]["content"]["parts"][0]["text"]
                           if body.get("candidates") else "(порожньо)")
                    print(f"  {m:26} 200  {txt.strip()[:40]!r}")
                else:
                    err = (body.get("error") or {})
                    print(f"  {m:26} {resp.status_code}  {err.get('status')} — "
                          f"{str(err.get('message'))[:110]}")
                    for d in err.get("details") or []:
                        if d.get("@type", "").endswith("QuotaFailure"):
                            print("       квоти:", json.dumps(d.get("violations"),
                                                              ensure_ascii=False)[:400])
            except Exception as e:  # noqa: BLE001
                print(f"  {m:26} збій: {e}")
            await asyncio.sleep(1.0)


asyncio.run(main())

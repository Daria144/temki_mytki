"""Скільки запитів до Gemini коштує одна порада — і чи не палимо ми квоту дарма.

Привід: у журналі сервера поруч стояли 503 від основної моделі й 429 від запасної.
Виявилось, що запасною за замовчуванням стояла gemini-2.5-flash, а в неї на цьому
ключі денна межа 20 запитів і вона вичерпана. Тобто КОЖНА відмова основної моделі
коштувала другий, наперед приречений запит — і порада все одно не приходила.

Тут стережемо три речі:
  * запасна модель відрізняється від основної й не є тією, що вже не працює;
  * одна невдача = рівно один запит на модель (429/503 повтором не лікуються);
  * робочу модель памʼятають УСІ гілки, а не лише частина.

Запуск: ./.venv/bin/python -m pytest tests/test_gemini_quota.py -q
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app import advisor
from app.config import Config, config

# Модель, у якої на цьому ключі безплатна квота — 20 запитів на добу і вона
# вичерпана. Ставити її запасною означає гарантований 429 на кожній відмові.
EXHAUSTED = "gemini-2.5-flash"


def _defaults(monkeypatch) -> Config:
    """Config без змінних оточення — саме те, що діє на сервері без GEMINI_FALLBACK_MODEL."""
    for name in ("GEMINI_MODEL", "GEMINI_FALLBACK_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return Config()


def test_fallback_model_is_not_the_exhausted_one(monkeypatch):
    cfg = _defaults(monkeypatch)
    assert cfg.gemini_fallback_model != EXHAUSTED
    assert cfg.gemini_model != EXHAUSTED


def test_fallback_model_differs_from_the_main_one(monkeypatch):
    """Однакові моделі означають, що запасної фактично немає."""
    cfg = _defaults(monkeypatch)
    assert cfg.gemini_model != cfg.gemini_fallback_model


class _Responses:
    """Підміна httpx.AsyncClient: рахує запити й віддає задане по кожній моделі."""

    def __init__(self, by_model: dict):
        self.by_model = by_model
        self.calls: list = []

    def client(self, *a, **kw):
        outer = self

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, headers=None, json=None):
                model = url.split("/models/")[1].split(":")[0]
                outer.calls.append(model)
                status, payload = outer.by_model.get(model, (500, {}))
                request = httpx.Request("POST", url)
                return httpx.Response(status, json=payload, request=request)

        return Client()


def _answer(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


DECISION = json.dumps({
    "moment": "wait", "headline": "Чекаємо", "deploy_now_pct": 0,
    "coins": [], "reasoning": "Зараз недешево.",
})


def _run(monkeypatch, by_model, coro_factory, model="a", fallback="b"):
    fake = _Responses(by_model)
    monkeypatch.setattr(httpx, "AsyncClient", fake.client)
    monkeypatch.setattr(config, "gemini_api_key", "test-key")
    monkeypatch.setattr(config, "gemini_model", model)
    monkeypatch.setattr(config, "gemini_fallback_model", fallback)
    advisor.reset_health()
    try:
        return asyncio.run(coro_factory()), fake.calls
    finally:
        advisor.reset_health()


def test_overloaded_main_costs_exactly_one_request_before_the_fallback(monkeypatch):
    """503 повтором не лікується: одна відмова — один запит, а не два."""
    result, calls = _run(monkeypatch, {
        "a": (503, {"error": {"message": "overloaded"}}),
        "b": (200, _answer(DECISION)),
    }, lambda: advisor.decide([], 30, 100.0))
    assert result is not None and result["moment"] == "wait"
    assert calls == ["a", "b"], calls


def test_dead_fallback_does_not_double_every_failure(monkeypatch):
    """Обидві мовчать — усього два запити, і бот чесно віддає None (буде формула)."""
    fake = _Responses({"a": (503, {"error": {"message": "overloaded"}}),
                       "b": (429, {"error": {"message": "quota"}})})
    monkeypatch.setattr(httpx, "AsyncClient", fake.client)
    monkeypatch.setattr(config, "gemini_api_key", "test-key")
    monkeypatch.setattr(config, "gemini_model", "a")
    monkeypatch.setattr(config, "gemini_fallback_model", "b")
    advisor.reset_health()
    try:
        assert asyncio.run(advisor.decide([], 30, 100.0)) is None
        assert fake.calls == ["a", "b"], fake.calls
        # Мовчання має бути ВИДИМИМ: саме з цього рахується алерт про порадника.
        assert advisor.health()["ok"] is False
    finally:
        advisor.reset_health()


def test_the_working_model_is_remembered_and_tried_first(monkeypatch):
    """Інакше наступний виклик знову стукає в перевантажений аліас — зайвий запит."""
    fake = _Responses({"a": (503, {}), "b": (200, _answer(DECISION))})
    monkeypatch.setattr(httpx, "AsyncClient", fake.client)
    monkeypatch.setattr(config, "gemini_api_key", "test-key")
    monkeypatch.setattr(config, "gemini_model", "a")
    monkeypatch.setattr(config, "gemini_fallback_model", "b")
    advisor.reset_health()
    try:
        asyncio.run(advisor.decide([], 30, 100.0))
        asyncio.run(advisor.decide([], 30, 100.0))
        # Другий виклик починає з робочої моделі: 2 запити замість чотирьох.
        assert fake.calls == ["a", "b", "b"], fake.calls
    finally:
        advisor.reset_health()


PROMOS = json.dumps({"items": [{
    "i": 0, "is_offer": True, "title_uk": "Бонус", "what": "Поклади гроші",
    "kind": "deposit", "verdict": "maybe", "why": "умов не видно",
}]})


def test_promo_branch_remembers_the_working_model_too(monkeypatch):
    """Розбір акцій — така сама гілка: без памʼяті вона палила зайвий запит щоразу."""
    fake = _Responses({"a": (503, {}), "b": (200, _answer(PROMOS))})
    monkeypatch.setattr(httpx, "AsyncClient", fake.client)
    monkeypatch.setattr(config, "gemini_api_key", "test-key")
    monkeypatch.setattr(config, "gemini_model", "a")
    monkeypatch.setattr(config, "gemini_fallback_model", "b")
    advisor.reset_health()
    try:
        out = asyncio.run(advisor.explain_promos(["Earn 10% APR"]))
        assert out and advisor._models()[0] == "b"
        asyncio.run(advisor.explain_promos(["Earn 10% APR"]))
        assert fake.calls == ["a", "b", "b"], fake.calls
    finally:
        advisor.reset_health()


def test_no_key_means_no_requests_at_all(monkeypatch):
    """Без ключа бот не має ходити в мережу взагалі — працює формула."""
    fake = _Responses({})
    monkeypatch.setattr(httpx, "AsyncClient", fake.client)
    monkeypatch.setattr(config, "gemini_api_key", "")
    assert asyncio.run(advisor.decide([], 30, 100.0)) is None
    assert asyncio.run(advisor.explain_promos(["Earn 10% APR"])) == {}
    assert asyncio.run(advisor.explain_coins({"SOL": {"rank": 5}})) == {}
    assert asyncio.run(advisor.parse_deposits("200 usdt bybit")) is None
    assert fake.calls == []


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))

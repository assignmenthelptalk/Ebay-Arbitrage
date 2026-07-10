import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import CategoryAspects
from services import cassini
from services import ebay_client as ec


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test_cassini.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _clear_tree_id_cache():
    cassini._tree_id_cache.clear()
    yield
    cassini._tree_id_cache.clear()


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = json.dumps(self._json_data)
        self.content = self.text.encode()
        self.request = object()

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(str(self.status_code), request=self.request, response=self)


def _fake_async_client(responder, calls):
    """Stands in for ebay_client.httpx.AsyncClient — records every call and
    routes it to `responder(method, url, params=params)`, no real network."""

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, data=None):
            calls.append(("POST", url))
            return responder("POST", url)

        async def request(self, method, url, headers=None, params=None, json=None):
            calls.append((method, url, params))
            return responder(method, url, params=params)

    return _FakeAsyncClient


def _make_responder(tree_resp=None, suggestions_resp=None, aspects_resp=None, token_resp=None, taxonomy_status=200):
    token_resp = token_resp if token_resp is not None else {"access_token": "app-tok-123", "expires_in": 7200}

    def responder(method, url, params=None):
        if "oauth2/token" in url:
            return _FakeResponse(json_data=token_resp)
        if "get_default_category_tree_id" in url:
            return _FakeResponse(status_code=taxonomy_status, json_data=tree_resp or {"categoryTreeId": "0"})
        if "get_category_suggestions" in url:
            return _FakeResponse(status_code=taxonomy_status, json_data=suggestions_resp or {"categorySuggestions": []})
        if "get_item_aspects_for_category" in url:
            return _FakeResponse(status_code=taxonomy_status, json_data=aspects_resp or {"aspects": []})
        raise AssertionError(f"unexpected URL in test: {url}")

    return responder


def _set_creds(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "test-client-secret")


# --- resolve_category ---


def test_resolve_category_parses_top_suggestion(db, monkeypatch):
    _set_creds(monkeypatch)
    calls = []
    responder = _make_responder(
        suggestions_resp={
            "categorySuggestions": [
                {"category": {"categoryId": "112529", "categoryName": "Headphones"}},
                {"category": {"categoryId": "80077", "categoryName": "Headsets"}},
            ]
        }
    )
    monkeypatch.setattr(ec.httpx, "AsyncClient", _fake_async_client(responder, calls))

    result = _run(cassini.resolve_category(db, "wireless earbuds"))
    assert result == {"category_id": "112529", "category_name": "Headphones", "tree_id": "0"}


def test_resolve_category_no_suggestions_returns_none(db, monkeypatch):
    _set_creds(monkeypatch)
    calls = []
    responder = _make_responder(suggestions_resp={"categorySuggestions": []})
    monkeypatch.setattr(ec.httpx, "AsyncClient", _fake_async_client(responder, calls))

    assert _run(cassini.resolve_category(db, "some obscure title")) is None


def test_resolve_category_without_credentials_makes_no_call(db, monkeypatch):
    monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
    calls = []

    def _boom(method, url, params=None):
        raise AssertionError("should never call eBay without credentials")

    monkeypatch.setattr(ec.httpx, "AsyncClient", _fake_async_client(_boom, calls))

    assert _run(cassini.resolve_category(db, "wireless earbuds")) is None
    assert calls == []


def test_resolve_category_403_degrades_to_none(db, monkeypatch):
    _set_creds(monkeypatch)
    calls = []
    responder = _make_responder(taxonomy_status=403)
    monkeypatch.setattr(ec.httpx, "AsyncClient", _fake_async_client(responder, calls))

    assert _run(cassini.resolve_category(db, "wireless earbuds")) is None


# --- get_aspects ---


def test_get_aspects_parses_required_and_optional_with_allowed_values(db, monkeypatch):
    _set_creds(monkeypatch)
    calls = []
    responder = _make_responder(aspects_resp={
        "aspects": [
            {
                "localizedAspectName": "Brand",
                "aspectConstraint": {"aspectRequired": True},
                "aspectValues": [{"localizedValue": "Sony"}, {"localizedValue": "Bose"}],
            },
            {
                "localizedAspectName": "Color",
                "aspectConstraint": {"aspectRequired": False},
                "aspectValues": [],
            },
        ]
    })
    monkeypatch.setattr(ec.httpx, "AsyncClient", _fake_async_client(responder, calls))

    result = _run(cassini.get_aspects(db, "112529", "0", "Headphones"))
    assert result == [
        {"name": "Brand", "required": True, "allowed_values": ["Sony", "Bose"]},
        {"name": "Color", "required": False, "allowed_values": []},
    ]

    row = db.query(CategoryAspects).filter(CategoryAspects.category_id == "112529").first()
    assert row is not None
    assert row.aspects == result
    assert row.category_name == "Headphones"
    assert row.tree_id == "0"


def test_get_aspects_cache_hit_makes_no_further_ebay_call(db, monkeypatch):
    _set_creds(monkeypatch)
    calls = []
    responder = _make_responder(aspects_resp={
        "aspects": [{"localizedAspectName": "Brand", "aspectConstraint": {"aspectRequired": True}, "aspectValues": []}]
    })
    monkeypatch.setattr(ec.httpx, "AsyncClient", _fake_async_client(responder, calls))

    first = _run(cassini.get_aspects(db, "112529", "0"))
    calls_after_first = len(calls)
    assert calls_after_first > 0

    second = _run(cassini.get_aspects(db, "112529", "0"))
    assert second == first
    assert len(calls) == calls_after_first, "cache hit must not make any further eBay call"


def test_get_aspects_stale_cache_refetches(db, monkeypatch):
    _set_creds(monkeypatch)
    monkeypatch.setenv("CASSINI_ASPECTS_TTL_DAYS", "30")
    calls = []
    responder = _make_responder(aspects_resp={
        "aspects": [{"localizedAspectName": "Brand", "aspectConstraint": {"aspectRequired": True}, "aspectValues": []}]
    })
    monkeypatch.setattr(ec.httpx, "AsyncClient", _fake_async_client(responder, calls))

    _run(cassini.get_aspects(db, "112529", "0"))
    row = db.query(CategoryAspects).filter(CategoryAspects.category_id == "112529").first()
    row.fetched_at = datetime.utcnow() - timedelta(days=31)
    db.commit()

    calls_before_refetch = len(calls)
    _run(cassini.get_aspects(db, "112529", "0"))
    assert len(calls) > calls_before_refetch, "stale cache entry must trigger a refetch"


def test_get_aspects_403_degrades_to_none_when_no_cache(db, monkeypatch):
    _set_creds(monkeypatch)
    calls = []
    responder = _make_responder(taxonomy_status=403)
    monkeypatch.setattr(ec.httpx, "AsyncClient", _fake_async_client(responder, calls))

    assert _run(cassini.get_aspects(db, "112529", "0")) is None


def test_get_aspects_without_credentials_makes_no_call(db, monkeypatch):
    monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
    calls = []

    def _boom(method, url, params=None):
        raise AssertionError("should never call eBay without credentials")

    monkeypatch.setattr(ec.httpx, "AsyncClient", _fake_async_client(_boom, calls))

    assert _run(cassini.get_aspects(db, "112529", "0")) is None
    assert calls == []


# --- CASSINI_ENABLED off switch ---


def test_cassini_disabled_short_circuits_with_no_ebay_call(db, monkeypatch):
    _set_creds(monkeypatch)
    monkeypatch.setenv("CASSINI_ENABLED", "false")
    calls = []

    def _boom(method, url, params=None):
        raise AssertionError("should never call eBay when CASSINI_ENABLED=false")

    monkeypatch.setattr(ec.httpx, "AsyncClient", _fake_async_client(_boom, calls))

    assert _run(cassini.resolve_category(db, "wireless earbuds")) is None
    assert _run(cassini.get_aspects(db, "112529", "0")) is None
    assert calls == []


# --- app token reuse across calls ---


def test_app_token_cached_across_resolve_and_get_aspects(db, monkeypatch):
    _set_creds(monkeypatch)
    calls = []
    responder = _make_responder(
        suggestions_resp={"categorySuggestions": [{"category": {"categoryId": "112529", "categoryName": "Headphones"}}]},
        aspects_resp={"aspects": []},
    )
    monkeypatch.setattr(ec.httpx, "AsyncClient", _fake_async_client(responder, calls))

    _run(cassini.resolve_category(db, "wireless earbuds"))
    token_calls = sum(1 for c in calls if c[0] == "POST" and "oauth2/token" in c[1])
    assert token_calls == 1

    _run(cassini.get_aspects(db, "112529", "0"))
    token_calls_after_both = sum(1 for c in calls if c[0] == "POST" and "oauth2/token" in c[1])
    assert token_calls_after_both == 1, "app token must be reused from the tokens table cache, not refetched"

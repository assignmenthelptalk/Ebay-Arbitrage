import socket
from urllib.parse import unquote_plus

import services.ebay_search as ebay_search_module
from services.ebay_search import build_ebay_search_url


def test_module_imports_no_http_client():
    # Structural guarantee, not just behavioral — the module can't make a
    # network call if it never imported anything capable of one.
    assert not hasattr(ebay_search_module, "httpx")
    assert not hasattr(ebay_search_module, "requests")


def test_build_url_makes_no_network_call(monkeypatch):
    def _explode(*_args, **_kwargs):
        raise AssertionError("socket connect attempted — build_ebay_search_url must never touch the network")

    monkeypatch.setattr(socket.socket, "connect", _explode)

    url = build_ebay_search_url("Brand New Sealed Apple iPhone 7 32GB Free Shipping")
    assert url.startswith("https://www.ebay.com/sch/i.html?_nkw=")

    url_upc = build_ebay_search_url("anything", upc="012345678905")
    assert url_upc == "https://www.ebay.com/sch/i.html?_nkw=012345678905&LH_Sold=1&LH_Complete=1"


def test_filters_to_sold_and_completed_listings():
    url = build_ebay_search_url("Apple iPhone 7")
    assert "LH_Sold=1" in url
    assert "LH_Complete=1" in url


def test_upc_present_searches_by_upc_not_title():
    url = build_ebay_search_url("Some completely different noisy title", upc="012345678905")
    assert unquote_plus(url.split("_nkw=")[1].split("&")[0]) == "012345678905"


def test_blank_upc_falls_back_to_cleaned_title():
    url = build_ebay_search_url("Brand New Apple iPhone 7 32GB Free Shipping", upc="   ")
    query = unquote_plus(url.split("_nkw=")[1].split("&")[0])
    assert "Apple" in query
    assert "iPhone" in query
    assert "Brand New" not in query


def test_empty_title_returns_valid_but_empty_query_url():
    url = build_ebay_search_url("")
    assert url == "https://www.ebay.com/sch/i.html?_nkw=&LH_Sold=1&LH_Complete=1"


def test_all_noise_title_falls_back_to_raw_title_never_blank():
    title = "Brand New Sealed Free Shipping!!!"
    url = build_ebay_search_url(title)
    query = url.split("_nkw=", 1)[1]
    assert query != "&LH_Sold=1&LH_Complete=1"

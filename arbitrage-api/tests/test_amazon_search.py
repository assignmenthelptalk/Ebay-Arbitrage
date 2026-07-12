import socket
from urllib.parse import unquote_plus

import services.amazon_search as amazon_search_module
from services.amazon_search import build_amazon_search_url, clean_title_for_search


def test_module_imports_no_http_client():
    # Structural guarantee, not just behavioral — the module can't make a
    # network call if it never imported anything capable of one.
    assert not hasattr(amazon_search_module, "httpx")
    assert not hasattr(amazon_search_module, "requests")


def test_build_url_makes_no_network_call(monkeypatch):
    def _explode(*_args, **_kwargs):
        raise AssertionError("socket connect attempted — build_amazon_search_url must never touch the network")

    monkeypatch.setattr(socket.socket, "connect", _explode)

    url = build_amazon_search_url("Brand New Sealed Apple iPhone 7 32GB Free Shipping")
    assert url.startswith("https://www.amazon.com/s?k=")

    url_upc = build_amazon_search_url("anything", upc="012345678905")
    assert url_upc == "https://www.amazon.com/s?k=012345678905"


def test_upc_present_searches_by_upc_not_title():
    url = build_amazon_search_url("Some completely different noisy title", upc="012345678905")
    assert url == "https://www.amazon.com/s?k=012345678905"


def test_blank_upc_falls_back_to_cleaned_title():
    url = build_amazon_search_url("Apple iPhone 7 32GB", upc="   ")
    assert unquote_plus(url.split("k=")[1]) == "Apple iPhone 7 32GB"


def test_clean_title_strips_noise_keeps_brand_model():
    cleaned = clean_title_for_search(
        "Brand New Sealed Apple iPhone 7 32GB Unlocked Free Fast Shipping!!! 🔥📱"
    )
    assert "Apple" in cleaned
    assert "iPhone" in cleaned
    assert "32GB" in cleaned
    assert "Unlocked" in cleaned
    assert "Brand New" not in cleaned
    assert "Sealed" not in cleaned
    assert "Shipping" not in cleaned


def test_clean_title_strips_condition_and_seller_boilerplate():
    cleaned = clean_title_for_search("NIB Nike Air Max 90 Size 10 Authentic USA Seller L@@K")
    assert "Nike" in cleaned
    assert "Air" in cleaned
    assert "Max" in cleaned
    assert "90" in cleaned
    assert "NIB" not in cleaned
    assert "Authentic" not in cleaned
    assert "Seller" not in cleaned
    assert "L@@K" not in cleaned


def test_all_noise_title_falls_back_to_raw_title_never_blank():
    title = "Brand New Sealed Free Shipping!!!"
    cleaned = clean_title_for_search(title)
    assert cleaned  # never empty, even when every word is a noise phrase
    assert cleaned == title.strip()

    url = build_amazon_search_url(title)
    query = url.split("k=", 1)[1]
    assert query != ""


def test_empty_title_returns_valid_but_empty_query_url():
    url = build_amazon_search_url("")
    assert url == "https://www.amazon.com/s?k="


def test_url_encoding_uses_plus_for_spaces():
    url = build_amazon_search_url("Apple iPhone 7")
    assert url == "https://www.amazon.com/s?k=Apple+iPhone+7"


def test_long_title_capped_to_max_query_words():
    title = "Apple iPhone 7 32GB Unlocked Gold Excellent Condition Model A1660 Extra Words Here Too Many"
    cleaned = clean_title_for_search(title)
    assert len(cleaned.split(" ")) <= amazon_search_module.MAX_QUERY_WORDS

"""eBay search-URL builder (§4C.1) — the mirror image of
services/amazon_search.py. An Amazon product page gives amazon_cost but no
observed eBay sale_price; this builds a link to eBay's sold/completed
listings for a human to eyeball and paste a real price back from, same
manual-honesty reasoning as §4C.2 (no auto-matching, no scraping, no fake
precision). Never fetches or parses eBay — pure string builder.
"""

from urllib.parse import quote_plus

from services.amazon_search import clean_title_for_search


def build_ebay_search_url(title: str, upc: str | None = None) -> str:
    """Pure string builder. Reuses amazon_search's noise-stripping (the same
    marketplace boilerplate — "brand new", "free shipping", etc. — pollutes
    titles regardless of which marketplace they came from). Filters to
    sold + completed listings so the human sees real recent sale prices,
    not asking prices.
    """
    upc_clean = (upc or "").strip()
    query = upc_clean if upc_clean else clean_title_for_search(title or "")
    if not query:
        query = (title or "").strip()

    return f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(query)}&LH_Sold=1&LH_Complete=1"

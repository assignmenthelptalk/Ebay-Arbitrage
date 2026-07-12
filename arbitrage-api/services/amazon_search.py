"""Amazon search-URL builder (§4C.2 replacement) — fast MANUAL sourcing, not
auto-matching. Deliberate decision: no legitimate Amazon data source exists
(PA API gated behind a sales history this dormant account doesn't have;
scraping is off the table; Keepa costs money and isn't wanted), and an
auto-matcher's worst failure — a confidently-WRONG match producing a precise,
false margin that flows through the scorer/dashboard/approval — is the most
dangerous failure mode in the system. This module only builds a search URL for
a human to open, look at, and match themselves; it never fetches or parses
Amazon. No httpx/requests import here, on purpose.
"""

import re
from urllib.parse import quote_plus

# Conservative noise-phrase list: eBay condition/marketing/shipping boilerplate
# that adds nothing to an Amazon product search and can crowd out the actual
# brand/model in a long, keyword-stuffed eBay title. Deliberately does NOT
# strip generic words like "new" alone (too likely to be part of a real
# product name) — only multi-word phrases or clearly non-product tokens.
_NOISE_PHRASES = [
    r"brand\s*new(?:\s*(?:sealed|in\s*box|with\s*tags))?",
    r"new\s*in\s*box",
    r"new\s*with\s*tags",
    r"nwt",
    r"nib",
    r"sealed",
    r"open\s*box",
    r"like\s*new",
    r"pre[\s-]?owned",
    r"refurbished",
    r"mint(?:\s*condition)?",
    r"excellent\s*condition",
    r"great\s*condition",
    r"good\s*condition",
    r"100%\s*authentic",
    r"authentic",
    r"genuine",
    r"free\s*(?:fast\s*)?shipping",
    r"fast\s*(?:free\s*)?shipping",
    r"fast\s*dispatch",
    r"same\s*day\s*dispatch",
    r"ships?\s*fast",
    r"ships?\s*free",
    r"free\s*ship",
    r"us a?\s*seller",
    r"usa?\s*seller",
    r"l@@k",
    r"must\s*see",
    r"read\s*description",
    r"hot\s*sale",
    r"rare",
]
_NOISE_RE = re.compile(r"\b(" + "|".join(_NOISE_PHRASES) + r")\b", re.IGNORECASE)

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]+",
    flags=re.UNICODE,
)
_PUNCT_RE = re.compile(r"[^\w\s]+", flags=re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")

# Cap query length — eBay titles can run to 80 keyword-stuffed characters;
# Amazon's search doesn't need all of them and a shorter query is easier for
# the human to eyeball/refine in the new tab.
MAX_QUERY_WORDS = 10


def clean_title_for_search(title: str) -> str:
    """Conservative cleanup of an eBay title into an Amazon search query.
    Strips emoji, condition/shipping/marketing noise phrases, and excess
    punctuation, then caps length. If cleaning would empty the title out
    entirely (e.g. a title that's ALL noise phrases), falls back to the
    original title rather than returning nothing — a decent starting query
    for the human to refine, never a blank one.
    """
    if not title:
        return ""

    text = _EMOJI_RE.sub(" ", title)
    text = _NOISE_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()

    if not text:
        return title.strip()

    words = text.split(" ")
    if len(words) > MAX_QUERY_WORDS:
        text = " ".join(words[:MAX_QUERY_WORDS])
    return text


def build_amazon_search_url(title: str, upc: str | None = None) -> str:
    """Pure string builder. Prefers an exact UPC/EAN/GTIN search (most
    precise) when one is available; otherwise builds a cleaned keyword query
    from the eBay title. Never fetches or parses Amazon — this function makes
    no network call of any kind.
    """
    upc_clean = (upc or "").strip()
    query = upc_clean if upc_clean else clean_title_for_search(title or "")
    if not query:
        query = (title or "").strip()

    return f"https://www.amazon.com/s?k={quote_plus(query)}"

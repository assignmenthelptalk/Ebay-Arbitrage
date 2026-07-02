"""
Phase 1 - eBay Arbitrage Scraper (Playwright edition)
Uses a real headless Chromium browser to bypass eBay bot detection.
Scrapes sold listings (cost basis) vs active listings (sell price),
then calculates net margin after eBay/PayPal fees.
"""

import os
import random
import statistics
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from playwright.sync_api import Page, sync_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROXY_SERVER = "http://p.webshare.io:443"
PROXY_USERNAME = os.environ.get("PROXY_USERNAME", "")
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD", "")

EBAY_FEE_RATE = 0.1325          # 13.25% final value fee (standard category)
PAYMENT_FEE_RATE = 0.0299       # 2.99% managed payments fee
PAYMENT_FIXED_FEE = 0.49        # $0.49 fixed managed payments fee
SHIPPING_COST = 4.50            # assumed outbound shipping cost
MIN_MARGIN = 0.15               # only surface deals with ≥15% margin

BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-extensions",
]

# Masks common Playwright/CDP fingerprints that eBay checks via JS
STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    window.chrome = {runtime: {}};
"""

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Opportunity:
    query: str
    avg_sold_price: float
    avg_active_price: float
    sample_sold: int
    sample_active: int
    margin: float
    net_profit: float


# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------

def _ebay_url(params: dict) -> str:
    return "https://www.ebay.com/sch/i.html?" + urlencode(params)


def _load_listings(page: Page, url: str) -> bool:
    """Navigate and wait for the listing grid to render. Returns True on success."""
    time.sleep(random.uniform(1.5, 3.0))
    try:
        page.goto(url, wait_until="networkidle", timeout=60_000)
        page.wait_for_selector(".s-item", timeout=30_000)
        return True
    except Exception as e:
        print(f"  [warn] page load failed: {e}")
        screenshot_path = f"debug_{int(time.time())}.png"
        try:
            page.screenshot(path=screenshot_path, full_page=True)
            print(f"  [debug] screenshot saved → {screenshot_path}")
        except Exception as ss_err:
            print(f"  [debug] screenshot failed: {ss_err}")
        return False


def _parse_prices(page: Page, require_sold_badge: bool = False) -> list[float]:
    """Extract USD prices from the currently loaded search results page."""
    prices = []
    for item in page.query_selector_all(".s-item"):
        if require_sold_badge and not item.query_selector(".POSITIVE"):
            continue
        price_el = item.query_selector(".s-item__price")
        if not price_el:
            continue
        raw = price_el.inner_text().replace("$", "").replace(",", "").strip()
        if " to " in raw:           # price ranges are ambiguous — skip
            continue
        try:
            prices.append(float(raw))
        except ValueError:
            continue
    return prices


def scrape_sold_prices(page: Page, query: str) -> list[float]:
    url = _ebay_url({
        "_nkw": query,
        "LH_Sold": "1",
        "LH_Complete": "1",
        "_sop": "13",   # sort: price + shipping, lowest first
        "_ipg": "60",
    })
    return _parse_prices(page, require_sold_badge=True) if _load_listings(page, url) else []


def scrape_active_prices(page: Page, query: str) -> list[float]:
    url = _ebay_url({
        "_nkw": query,
        "LH_BIN": "1",  # Buy It Now only
        "_sop": "15",   # sort: price + shipping, highest first
        "_ipg": "60",
    })
    return _parse_prices(page, require_sold_badge=False) if _load_listings(page, url) else []


# ---------------------------------------------------------------------------
# Margin calculation
# ---------------------------------------------------------------------------

def calc_margin(buy_price: float, sell_price: float) -> tuple[float, float]:
    """Returns (margin_rate, net_profit) after eBay FVF + managed payments + shipping."""
    ebay_fee = sell_price * EBAY_FEE_RATE
    payment_fee = sell_price * PAYMENT_FEE_RATE + PAYMENT_FIXED_FEE
    total_cost = buy_price + ebay_fee + payment_fee + SHIPPING_COST
    net_profit = sell_price - total_cost
    margin = net_profit / sell_price if sell_price > 0 else 0.0
    return margin, net_profit


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def analyze(queries: list[str]) -> list[Opportunity]:
    results = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=BROWSER_ARGS)
        proxy_config = {
            "server": PROXY_SERVER,
            "username": PROXY_USERNAME,
            "password": PROXY_PASSWORD,
        } if PROXY_USERNAME and PROXY_PASSWORD else None

        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="America/New_York",
            proxy=proxy_config,
        )
        context.add_init_script(STEALTH_SCRIPT)
        page = context.new_page()

        for query in queries:
            print(f"\nAnalyzing: {query!r}")

            sold = scrape_sold_prices(page, query)
            if not sold:
                print("  No sold listings found — skipping.")
                continue
            print(f"  Sold samples  : {len(sold)}  | e.g. {sorted(sold)[:5]}")

            active = scrape_active_prices(page, query)
            if not active:
                print("  No active listings found — skipping.")
                continue
            print(f"  Active samples: {len(active)} | e.g. {sorted(active)[:5]}")

            avg_sold = statistics.median(sold)
            avg_active = statistics.median(active)
            margin, net_profit = calc_margin(avg_sold, avg_active)

            results.append(Opportunity(
                query=query,
                avg_sold_price=avg_sold,
                avg_active_price=avg_active,
                sample_sold=len(sold),
                sample_active=len(active),
                margin=margin,
                net_profit=net_profit,
            ))
            print(
                f"  Median buy: ${avg_sold:.2f} | Median sell: ${avg_active:.2f} | "
                f"Margin: {margin:.1%} | Net: ${net_profit:.2f}"
            )

        browser.close()

    return results


def print_report(results: list[Opportunity]) -> None:
    profitable = sorted(
        [r for r in results if r.margin >= MIN_MARGIN],
        key=lambda r: r.margin,
        reverse=True,
    )

    print("\n" + "=" * 70)
    print(f"{'ARBITRAGE OPPORTUNITIES':^70}")
    print(f"  min margin filter: {MIN_MARGIN:.0%}")
    print("=" * 70)

    if not profitable:
        print("  No opportunities meet the margin threshold.")
        return

    fmt = "{:<30} {:>8} {:>8} {:>8} {:>8}"
    print(fmt.format("Query", "Buy", "Sell", "Margin", "Net $"))
    print("-" * 70)
    for r in profitable:
        print(fmt.format(
            r.query[:30],
            f"${r.avg_sold_price:.2f}",
            f"${r.avg_active_price:.2f}",
            f"{r.margin:.1%}",
            f"${r.net_profit:.2f}",
        ))


# ---------------------------------------------------------------------------
# Entry point — edit SEARCH_QUERIES to test your own product ideas
# ---------------------------------------------------------------------------

SEARCH_QUERIES = [
    "vintage casio watch",
    "lego star wars set",
    "pokemon card lot",
    "nintendo ds game lot",
    "mechanical keyboard switches",
]

if __name__ == "__main__":
    results = analyze(SEARCH_QUERIES)
    print_report(results)

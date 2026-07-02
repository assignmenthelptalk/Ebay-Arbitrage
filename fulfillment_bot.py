"""
eBay Arbitrage — Amazon Fulfillment Bot
Polls GET /orders/queue every 60 seconds.
For each job: navigates Amazon, places order, reports back.
"""
import json
import os
import time
import traceback

import requests
from dotenv import load_dotenv
from playwright.sync_api import Page, sync_playwright

load_dotenv()

API_BASE = "http://localhost:8000"
AMAZON_BASE = os.getenv("AMAZON_BASE_URL", "https://www.amazon.co.uk")
POLL_INTERVAL = int(os.getenv("QUEUE_POLL_INTERVAL", 60))
MAX_PRICE_DRIFT = float(os.getenv("MAX_PRICE_DRIFT_PCT", 0.10))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
SESSION_FILE = "/root/arbitrage-api/amazon_session.json"
DEBUG_DIR = "/root/arbitrage-api"

STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
    Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
    window.chrome = {runtime: {}};
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return 'Intel Inc.';
        if (parameter === 37446) return 'Intel Iris OpenGL Engine';
        return getParameter.call(this, parameter);
    };
"""


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def get_queue() -> list:
    try:
        r = requests.get(f"{API_BASE}/orders/queue", timeout=10)
        return r.json().get("jobs", [])
    except Exception as e:
        print(f"[queue] Failed to fetch queue: {e}")
        return []


def update_order_status(order_id: str, status: str,
                        tracking: str = None, note: str = None):
    payload = {"status": status}
    if tracking:
        payload["tracking_number"] = tracking
    if note:
        payload["note"] = note
    try:
        requests.patch(
            f"{API_BASE}/orders/{order_id}/status",
            json=payload,
            timeout=10,
        )
        print(f"[api] Order {order_id} → {status}")
    except Exception as e:
        print(f"[api] Failed to update order {order_id}: {e}")


def save_debug_screenshot(page: Page, order_id: str, reason: str):
    ts = int(time.time())
    path = f"{DEBUG_DIR}/debug_{order_id}_{reason}_{ts}.png"
    try:
        page.screenshot(path=path, full_page=True)
        print(f"[debug] Screenshot → {path}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Browser setup
# ---------------------------------------------------------------------------

def build_context(pw):
    browser = pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="America/New_York",
    )
    context.add_init_script(STEALTH_SCRIPT)

    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE) as f:
                cookies = json.load(f)
            context.add_cookies(cookies)
            print("[session] Loaded Amazon cookies from file")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[session] WARNING: Session file is unreadable/corrupt ({exc}) — "
                  f"run setup_session.py to regenerate it")
    else:
        print("[session] WARNING: No session file found — run setup_session.py first")

    return browser, context


def save_session(context):
    cookies = context.cookies()
    with open(SESSION_FILE, "w") as f:
        json.dump(cookies, f, indent=2)
    print(f"[session] Saved {len(cookies)} cookies")


# ---------------------------------------------------------------------------
# Fulfillment steps
# ---------------------------------------------------------------------------

def set_delivery_zip(page: Page, zipcode: str, order_id: str) -> None:
    """Set Amazon delivery ZIP before browsing so geo-IP doesn't default to Europe."""
    try:
        print(f"[bot] Setting delivery ZIP to {zipcode}")
        page.goto(AMAZON_BASE, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(2)
        save_debug_screenshot(page, order_id, "homepage")

        loc_link = (
            page.query_selector("#nav-global-location-popover-link")
            or page.query_selector("[data-nav-role='GLUXDeliveryBlockMessage']")
        )
        if not loc_link:
            print("[bot] Location link not found on homepage")
            return
        loc_link.click()
        time.sleep(1.5)

        zip_input = (
            page.query_selector("#GLUXZipUpdateInput")
            or page.query_selector("input[placeholder*='zip' i]")
        )
        if not zip_input:
            print("[bot] ZIP input field not found")
            return
        zip_input.fill("")
        zip_input.type(zipcode, delay=60)
        time.sleep(0.5)

        apply_btn = (
            page.query_selector("#GLUXZipUpdate input")
            or page.query_selector("span.a-button-inner input[aria-labelledby='GLUXZipUpdate-announce']")
            or page.query_selector("input[aria-labelledby*='GLUXZipUpdate']")
        )
        if apply_btn:
            apply_btn.click()
            time.sleep(2)
            print(f"[bot] Delivery ZIP applied: {zipcode}")
        else:
            print("[bot] Apply button not found")
    except Exception as e:
        print(f"[bot] Could not set delivery ZIP ({e}) — proceeding anyway")


def check_session_valid(page: Page) -> bool:
    if "ap/signin" in page.url or "signin" in page.url:
        print("[session] Session expired — login required")
        return False
    return True


def go_to_product(page: Page, asin: str, job: dict) -> bool:
    zipcode = job.get("shipping_address", {}).get("postcode", "")
    url = f"{AMAZON_BASE}/dp/{asin}"
    if zipcode:
        url += f"?th=1&psc=1&deliveryPostalCode={zipcode}"
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    time.sleep(2)

    if not check_session_valid(page):
        update_order_status(job["order_id"], "failed", note="session expired")
        return False

    if page.query_selector("form[action='/errors/validateCaptcha']"):
        update_order_status(job["order_id"], "failed", note="captcha blocked")
        return False

    # Detect Amazon 404 ("Sorry, we couldn't find that page")
    if page.query_selector("img[alt='RoRo']") or "couldn't find that page" in (page.content()[:2000]):
        update_order_status(job["order_id"], "failed", note=f"ASIN {asin} not found on this marketplace")
        return False

    try:
        page.wait_for_selector("#productTitle", timeout=30_000)
    except Exception:
        print(f"[bot] Product title not found. Current URL: {page.url}")
        save_debug_screenshot(page, job["order_id"], "no_product_title")
        update_order_status(job["order_id"], "failed", note="product page did not load")
        return False

    avail = page.query_selector("#availability")
    if avail:
        avail_text = avail.inner_text().strip()
        print(f"[bot] Availability text: '{avail_text}'")
        if any(x in avail_text.lower() for x in ["currently unavailable", "out of stock", "no longer available"]):
            save_debug_screenshot(page, job["order_id"], "out_of_stock")
            update_order_status(job["order_id"], "failed", note=f"Amazon out of stock: {avail_text[:80]}")
            return False

    price_el = page.query_selector(".a-price-whole")
    if price_el:
        try:
            current_price = float(price_el.inner_text().replace(",", "").strip())
            original_price = float(job.get("amazon_price", 0))
            if original_price > 0:
                drift = (current_price - original_price) / original_price
                if drift > MAX_PRICE_DRIFT:
                    update_order_status(
                        job["order_id"], "failed",
                        note=f"price changed {drift:.1%} above threshold",
                    )
                    return False
        except Exception:
            pass  # price check failed silently — proceed

    return True


def add_to_cart(page: Page, job: dict) -> bool:
    try:
        # Try standard button, then variation/device-specific fallbacks
        btn = (
            page.query_selector("#add-to-cart-button")
            or page.query_selector("input[name='submit.add-to-cart']")
            or page.query_selector("[data-action='add-to-cart'] input")
        )
        if not btn:
            try:
                page.wait_for_selector(
                    "#add-to-cart-button, input[name='submit.add-to-cart']",
                    timeout=10_000,
                )
                btn = page.query_selector("#add-to-cart-button") or \
                      page.query_selector("input[name='submit.add-to-cart']")
            except Exception:
                pass

        if not btn:
            update_order_status(job["order_id"], "failed", note="add to cart button not found")
            return False

        btn.click()
        time.sleep(2)

        # Detect sign-in redirect (session not authenticated)
        if "ap/signin" in page.url or "signin" in page.url:
            save_debug_screenshot(page, job["order_id"], "signin_redirect")
            update_order_status(job["order_id"], "failed", note="add to cart redirected to sign-in — session expired or wrong domain")
            return False

        # Dismiss Asurion / protection-plan upsell modal if it appeared
        no_thanks = (
            page.query_selector("#attachSiNoCoverage")
            or page.query_selector("#no-coverage-link")
            or page.query_selector("button:has-text('No thanks')")
            or page.query_selector("a:has-text('No thanks')")
        )
        if no_thanks:
            print("[bot] Dismissing protection plan modal")
            no_thanks.click()
            time.sleep(2)

        try:
            page.wait_for_selector("#NATC_SMART_WAGON_CONF_MSG_SUCCESS", timeout=8_000)
            return True
        except Exception:
            pass

        # Fallback: check nav cart count on current page before navigating
        nav_count = page.query_selector("#nav-cart-count")
        count_on_page = nav_count.inner_text().strip() if nav_count else "0"
        print(f"[bot] Nav cart count on product page: '{count_on_page}'")
        if count_on_page != "0":
            return True

        # Last resort: navigate to cart and check for items
        page.goto(f"{AMAZON_BASE}/cart", wait_until="domcontentloaded")

        if "ap/signin" in page.url or "signin" in page.url:
            save_debug_screenshot(page, job["order_id"], "cart_signin_redirect")
            update_order_status(job["order_id"], "failed", note="cart page redirected to sign-in — session not authenticated for amazon.com")
            return False

        cart_items = page.query_selector_all(".sc-list-item-content, [data-asin].sc-list-item")
        print(f"[bot] Cart items found on /cart page: {len(cart_items)}")
        if not cart_items:
            update_order_status(job["order_id"], "failed", note="add to cart failed — cart empty after click")
            return False

        return True
    except Exception as e:
        update_order_status(job["order_id"], "failed", note=f"add to cart error: {e}")
        return False


def _is_error_page(page: Page) -> bool:
    try:
        content_start = page.content()[:1000].lower()
        return "went wrong" in content_start or "something went wrong" in content_start
    except Exception:
        return False


def _click_first(page: Page, selectors: list) -> bool:
    """Click the first matching selector. Returns True if one was found."""
    for sel in selectors:
        el = page.query_selector(sel)
        if el:
            el.click()
            return True
    return False


def enter_shipping_address(page: Page, addr: dict, job: dict) -> bool:
    try:
        # Navigate via cart → Proceed to checkout (direct URL rejected by Amazon)
        page.goto(f"{AMAZON_BASE}/cart", wait_until="domcontentloaded", timeout=30_000)
        time.sleep(2)

        checkout_btn = (
            page.query_selector("input[name='proceedToRetailCheckout']")
            or page.query_selector("#checkout-button-top")
            or page.query_selector("input[data-feature-id='proceed-to-checkout-action']")
        )
        if not checkout_btn:
            update_order_status(job["order_id"], "failed", note="proceed to checkout button not found on cart page")
            return False

        checkout_btn.click()
        time.sleep(3)
        print(f"[bot] Checkout URL: {page.url}")

        if _is_error_page(page):
            save_debug_screenshot(page, job["order_id"], "checkout_error_page")
            update_order_status(job["order_id"], "failed", note="Amazon checkout error page after Proceed to Checkout")
            return False

        if "ap/signin" in page.url:
            save_debug_screenshot(page, job["order_id"], "checkout_signin")
            update_order_status(job["order_id"], "failed", note="checkout redirected to sign-in")
            return False

        # Address step: click "Deliver to this address" to confirm the selected address.
        # Opening the "Add new address" popover blocks subsequent clicks, so we use
        # the pre-selected account address for now.
        if "address" in page.url:
            save_debug_screenshot(page, job["order_id"], "address_step")
            try:
                page.get_by_role("button", name="Deliver to this address").first.click()
                print("[bot] Clicked 'Deliver to this address'")
                time.sleep(2)
            except Exception:
                print("[bot] 'Deliver to this address' not clicked — may already be past address step")

        print(f"[bot] Post-address URL: {page.url}")
        return True
    except Exception as e:
        update_order_status(job["order_id"], "failed", note=f"address entry error: {e}")
        return False


def complete_purchase(page: Page, job: dict) -> str | None:
    try:
        print(f"[bot] Starting complete_purchase at: {page.url}")
        save_debug_screenshot(page, job["order_id"], "checkout_start")

        # Dismiss any open popover/modal that might block clicks
        modal = page.query_selector("[data-action='a-popover-floating-close']")
        if modal:
            try:
                modal.click(timeout=3_000)
                print("[bot] Dismissed open modal")
                time.sleep(1)
            except Exception:
                pass

        # Step 1: shipping option — pick last (slowest = cheapest)
        shipping_options = page.query_selector_all("input[name='deliveryOptions']")
        if shipping_options:
            print(f"[bot] Selecting shipping option ({len(shipping_options)} found)")
            shipping_options[-1].click()
            time.sleep(1)

        # Step 2: click Continue on shipping step (if multi-step)
        for _ in range(2):
            cont = (
                page.query_selector("input[value='Continue']")
                or page.query_selector("span[id*='continue-button']")
            )
            if cont:
                print("[bot] Clicking Continue (shipping step)")
                cont.click()
                time.sleep(2)

        # Step 3: confirm payment method
        # Detect if no payment method is on the account
        no_card = page.query_selector("a:has-text('Add a credit or debit card')")
        if no_card and not page.query_selector("input[id*='payment'][type='radio']"):
            save_debug_screenshot(page, job["order_id"], "no_payment_method")
            update_order_status(job["order_id"], "failed", note="no payment method on Amazon account — add a credit card to complete orders")
            return None

        use_payment = (
            page.query_selector("input[value*='Use this payment']")
            or page.query_selector("span[data-feature-id='checkout-page-use-this-payment-button'] input")
            or page.query_selector("input[aria-label*='Use this payment']")
        )
        if use_payment:
            print("[bot] Clicking 'Use this payment method'")
            use_payment.click()
            time.sleep(2)
            save_debug_screenshot(page, job["order_id"], "after_use_payment")

        # Step 4: Place Your Order
        place_order_btn = (
            page.query_selector("input[name='placeYourOrder1']")
            or page.query_selector("span[id*='place-order-button']")
            or page.query_selector("input[value*='Place your order']")
            or page.query_selector("input[aria-label*='Place your order']")
        )
        if not place_order_btn:
            save_debug_screenshot(page, job["order_id"], "no_place_order_btn")
            update_order_status(job["order_id"], "failed", note="place order button not found — check no_place_order_btn screenshot")
            return None

        # DRY RUN: stop here — do not click the buy button
        if DRY_RUN:
            save_debug_screenshot(page, job["order_id"], "dry_run_order_review")
            print(f"[dry-run] Order review page reached for {job['order_id']} — stopping before purchase. Screenshot saved.")
            return "DRY-RUN-SIMULATED"

        print("[bot] Clicking Place Your Order")
        place_order_btn.click()
        time.sleep(3)

        page.wait_for_selector(
            ".thank-you-message, #widget-purchaseConfirmationStatus, h4.a-alert-heading",
            timeout=30_000,
        )

        order_el = page.query_selector("bdi")
        amazon_order_num = order_el.inner_text().strip() if order_el else "UNKNOWN"
        print(f"[order] Amazon order placed: {amazon_order_num}")
        return amazon_order_num

    except Exception as e:
        update_order_status(job["order_id"], "failed", note=f"checkout error: {e}")
        return None


# ---------------------------------------------------------------------------
# Main fulfillment function
# ---------------------------------------------------------------------------

def fulfill(job: dict, pw) -> None:
    order_id = job["order_id"]
    asin = job["amazon_asin"]
    print(f"\n[bot] Processing order {order_id} | ASIN: {asin}")

    raw_addr = job.get("shipping_address", {})
    if isinstance(raw_addr, str):
        raw_addr = json.loads(raw_addr)

    addr = {
        "full_name": raw_addr.get("full_name", raw_addr.get("fullName", "")),
        "line1": raw_addr.get("line1", raw_addr.get("addressLine1", "")),
        "line2": raw_addr.get("line2", raw_addr.get("addressLine2", "")),
        "city": raw_addr.get("city", ""),
        "county": raw_addr.get("county", ""),
        "postcode": raw_addr.get("postcode", raw_addr.get("postalCode", "")),
        "phone": raw_addr.get("phone", raw_addr.get("phoneNumber", "")),
    }

    browser, context = build_context(pw)
    page = context.new_page()

    try:
        set_delivery_zip(page, addr.get("postcode", "10001"), order_id)
        if not go_to_product(page, asin, job):
            return
        if not add_to_cart(page, job):
            save_debug_screenshot(page, order_id, "cart_fail")
            return
        if not enter_shipping_address(page, addr, job):
            save_debug_screenshot(page, order_id, "address_fail")
            return

        amazon_order_num = complete_purchase(page, job)
        if amazon_order_num:
            save_session(context)
            update_order_status(
                order_id,
                status="fulfilled",
                tracking=f"AMAZON-{amazon_order_num}",
                note="Dry-run completed — no real order placed" if DRY_RUN else "Auto-fulfilled by bot",
            )
        else:
            save_debug_screenshot(page, order_id, "purchase_fail")

    except Exception as e:
        print(f"[bot] Unhandled error on {order_id}: {e}")
        traceback.print_exc()
        save_debug_screenshot(page, order_id, "unhandled_error")
        update_order_status(order_id, "failed", note=f"unhandled: {str(e)[:200]}")
    finally:
        browser.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print(f"[bot] Fulfillment bot started {'(DRY RUN — no real purchases)' if DRY_RUN else ''}")
    print(f"[bot] Polling every {POLL_INTERVAL}s | Amazon: {AMAZON_BASE}")

    with sync_playwright() as pw:
        while True:
            jobs = get_queue()
            if jobs:
                print(f"[queue] {len(jobs)} job(s) found")
                for job in jobs:
                    fulfill(job, pw)
            else:
                print(f"[queue] Empty — sleeping {POLL_INTERVAL}s")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
